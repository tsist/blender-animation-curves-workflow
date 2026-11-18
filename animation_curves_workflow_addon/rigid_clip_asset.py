"""Native apply core for frozen no-bone rigid-part clip assets.

This is a sibling of ``clip_asset.apply_clip`` for ordinary MESH/Object parts.
It deliberately does not reinterpret armatures, skinning, materials or lights.
The public ``animation.clip-apply`` command reaches this module only through
the explicit ``animation-native-rigid-clip-v1`` clip schema.

Strict support boundary:

* one or two explicit asset-marked Collections from one local .blend library;
* exactly one identity parentless Empty root and an explicit list of MESH parts;
* every part has one exclusive native Action with only location/rotation_euler
  curves, inside one whole-clip source interval;
* uniform positive scale, 3D translation and integer quarter-turn yaw;
* no materials, lights, cameras, armatures, images, textures, worlds, linked or
  override IDs, shared Actions, NLA/drivers/tweak mode or curve modifiers;
* the whole frozen source clip or an explicit source_window, extracted exactly
  from native keys with no bake or linearization; no existing-character
  retargeting.

The module reuses ``clip_asset``'s before-ID map, new-ID assertions, exact key
rewrite and cleanup helpers so it shares the existing transaction boundary
instead of introducing a second scheduler.
"""
from __future__ import annotations

import copy
import json
import math
import os
import uuid
from pathlib import Path

try:
    from . import clip_asset as _base
    from .contract import WorkflowError
except (ImportError, ValueError):  # local host-side import
    import clip_asset as _base
    from contract import WorkflowError


REQUEST_SCHEMA = 'animation-native-clip-apply-v1'
CLIP_SCHEMA = 'animation-native-rigid-clip-v1'
RESULT_SCHEMA = 'animation-native-rigid-clip-apply-result-v1'
LIBRARY_COLLECTION_SCHEMA = 'animation-native-rigid-clip-collection-v1'
PART_LIMIT = 64
INVENTORY_CATEGORIES = ('materials', 'images', 'textures', 'lights',
                        'cameras', 'armatures', 'worlds', 'node_groups')


def _fail(code, message, details=None):
    raise WorkflowError(code, message, details or {})


def _canonical_uuid(value, label):
    return _base._canonical_uuid(value, label)


def _text(value, label, maximum=512):
    return _base._text(value, label, maximum)


def _finite(value, label):
    return _base._finite(value, label)


def _fields(value, expected, label):
    if not isinstance(value, dict):
        _fail('INVALID_REQUEST', label + ' must be an object')
    unknown = set(value) - set(expected)
    if unknown:
        _fail('INVALID_REQUEST', 'Unknown ' + label + ' fields: '
              + ', '.join(sorted(unknown)))
    missing = set(expected) - set(value)
    if missing:
        _fail('INVALID_REQUEST', 'Missing ' + label + ' fields: '
              + ', '.join(sorted(missing)))
    return value


def _inventory(bpy):
    result = {}
    for category in INVENTORY_CATEGORIES:
        collection = getattr(bpy.data, category, None)
        result[category] = sorted(item.name for item in collection) \
            if collection is not None else []
    return result


def _top_level_ids(bpy):
    return {item for item in bpy.data.user_map() if not item.is_embedded_data}


def _normalize_request(request):
    if not isinstance(request, dict):
        _fail('INVALID_REQUEST', 'request must be an object')
    expected = ('schema', 'clip', 'instance_name', 'start', 'end',
                'scale', 'translation', 'yaw_quarter_turns')
    if 'source_window' in request:
        expected += ('source_window',)
    _fields(request, expected, 'request')
    if request['schema'] != REQUEST_SCHEMA:
        _fail('INVALID_REQUEST', 'request.schema must be ' + REQUEST_SCHEMA)
    out = copy.deepcopy(request)
    _text(out['instance_name'], 'request.instance_name', 200)
    out['start'] = _finite(out['start'], 'request.start')
    out['end'] = _finite(out['end'], 'request.end')
    if not 0.0 < out['start'] < out['end']:
        _fail('INVALID_REQUEST', 'request requires positive start < end')
    out['scale'] = _finite(out['scale'], 'request.scale')
    if out['scale'] <= 0.0:
        _fail('INVALID_REQUEST', 'request.scale must be positive')
    if (not isinstance(out['translation'], (list, tuple))
            or len(out['translation']) != 3):
        _fail('INVALID_REQUEST', 'request.translation must contain three numbers')
    out['translation'] = [_finite(value, 'request.translation[%d]' % index)
                          for index, value in enumerate(out['translation'])]
    try:
        scale_in_blender = _base._f32(out['scale'])
        for value in out['translation']:
            _base._f32(value)
    except (OverflowError, ValueError):
        _fail('INVALID_REQUEST', 'Rigid placement exceeds Blender native numeric range')
    if scale_in_blender <= 0:
        _fail('INVALID_REQUEST', 'Rigid scale is too small for Blender native placement')
    yaw = out['yaw_quarter_turns']
    if isinstance(yaw, bool) or not isinstance(yaw, int) or not 0 <= yaw <= 3:
        _fail('INVALID_REQUEST', 'request.yaw_quarter_turns must be an integer 0..3')

    clip = out['clip']
    if not isinstance(clip, dict):
        _fail('INVALID_REQUEST', 'request.clip must be an object')
    _fields(clip, ('schema', 'clip_id', 'title', 'library', 'source', 'timing',
                   'meters_per_blender_unit', 'actors', 'lights'), 'clip')
    if clip['schema'] != CLIP_SCHEMA:
        _fail('INVALID_REQUEST', 'clip.schema must be ' + CLIP_SCHEMA)
    _canonical_uuid(clip['clip_id'], 'clip.clip_id')
    _text(clip['title'], 'clip.title', 512)
    _finite(clip['meters_per_blender_unit'], 'clip.meters_per_blender_unit')
    if clip['meters_per_blender_unit'] <= 0:
        _fail('INVALID_REQUEST', 'clip.meters_per_blender_unit must be positive')
    for block, label in ((clip['library'], 'clip.library'),
                         (clip['source'], 'clip.source')):
        _fields(block, ('file', 'expected_sha256') if label == 'clip.library'
                else ('file', 'sha256'), label)
        _text(block['file'], label + '.file', 4096)
        if label == 'clip.library' and not os.path.isabs(block['file']):
            _fail('INVALID_REQUEST', label + '.file must be absolute')
        if label == 'clip.library' and not block['file'].lower().endswith('.blend'):
            _fail('INVALID_REQUEST', 'clip.library.file must be a .blend file')
        sha = block.get('expected_sha256', block.get('sha256'))
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
            _fail('INVALID_REQUEST', label + ' hash must be a lowercase SHA-256')
    _fields(clip['timing'], ('start', 'end', 'fps', 'fps_base'), 'clip.timing')
    timing = clip['timing']
    for key in ('start', 'end'):
        timing[key] = _finite(timing[key], 'clip.timing.' + key)
    if not timing['start'] > 0 or not timing['end'] > timing['start']:
        _fail('INVALID_REQUEST', 'clip.timing requires positive start < end')
    for key in ('fps', 'fps_base'):
        timing[key] = _finite(timing[key], 'clip.timing.' + key)
        if timing[key] <= 0:
            _fail('INVALID_REQUEST', 'clip.timing fps/fps_base must be positive')
    if not timing['fps'].is_integer():
        _fail('INVALID_REQUEST', 'clip.timing.fps must be integral; use fps_base for fractional rates')
    if 'source_window' in out:
        window = out['source_window']
        if not isinstance(window, dict) or set(window) != {'start', 'end'}:
            _fail('INVALID_REQUEST', 'source_window requires exactly start and end')
        first = _finite(window['start'], 'source_window.start')
        last = _finite(window['end'], 'source_window.end')
        if not timing['start'] <= first < last <= timing['end']:
            _fail('INVALID_REQUEST', 'source_window must lie within the frozen clip timing')
        try:
            native_first, native_last = _base._f32(first), _base._f32(last)
        except (OverflowError, ValueError):
            _fail('INVALID_REQUEST', 'source_window exceeds the native frame range')
        if not timing['start'] <= native_first < native_last <= timing['end']:
            _fail('INVALID_REQUEST',
                  'source_window collapses or exceeds the native frame range')
        out['source_window'] = {'start': native_first, 'end': native_last,
                                'requested': {'start': first, 'end': last}}
    if not isinstance(clip['lights'], list) or clip['lights']:
        _fail('INVALID_REQUEST', 'rigid clip lights must be an empty list')
    actors = clip['actors']
    if not isinstance(actors, list) or not 1 <= len(actors) <= 2:
        _fail('INVALID_REQUEST', 'rigid clip actors must contain one or two rows')
    roles = set()
    collection_names = set()
    asset_ids = set()
    template_uuids = set()
    part_objects = set()
    part_identities = set()
    normalized_actors = []
    for index, actor in enumerate(actors):
        label = 'clip.actors[' + str(index) + ']'
        _fields(actor, ('role', 'collection', 'asset_id', 'template_uuid',
                        'root', 'parts'), label)
        role = actor['role']
        if role not in ('A', 'B') or role in roles:
            _fail('INVALID_REQUEST', 'rigid actor roles must be unique A/B')
        collection = _text(actor['collection'], label + '.collection')
        if collection in collection_names:
            _fail('INVALID_REQUEST', 'rigid actor collection names must be unique')
        asset_id = _canonical_uuid(actor['asset_id'], label + '.asset_id')
        if asset_id in asset_ids:
            _fail('INVALID_REQUEST', 'rigid actor asset_ids must be unique')
        template_uuid = _text(actor['template_uuid'], label + '.template_uuid')
        if len(template_uuid) != 32 or not all(
                character in '0123456789abcdef' for character in template_uuid):
            _fail('INVALID_REQUEST', label + '.template_uuid must be 32 lowercase hex')
        if template_uuid in template_uuids:
            _fail('INVALID_REQUEST', 'rigid actor template_uuids must be unique')
        root = _text(actor['root'], label + '.root')
        parts = actor['parts']
        if not isinstance(parts, list) or not 1 <= len(parts) <= PART_LIMIT:
            _fail('INVALID_REQUEST', label + '.parts must contain 1..64 rows')
        normalized_parts = []
        seen_objects = set()
        seen_parts = set()
        for part_index, part in enumerate(parts):
            part_label = label + '.parts[' + str(part_index) + ']'
            _fields(part, ('object', 'character_id', 'part_id'), part_label)
            object_name = _text(part['object'], part_label + '.object', 256)
            character_id = _text(part['character_id'],
                                 part_label + '.character_id', 256)
            part_id = _text(part['part_id'], part_label + '.part_id', 256)
            identity = character_id + '\x00' + part_id
            if object_name in seen_objects or object_name in part_objects:
                _fail('INVALID_REQUEST', 'rigid part object names must be unique')
            if identity in seen_parts or identity in part_identities:
                _fail('INVALID_REQUEST', 'rigid part identities must be unique')
            seen_objects.add(object_name)
            seen_parts.add(identity)
            part_objects.add(object_name)
            part_identities.add(identity)
            normalized_parts.append({'object': object_name,
                                     'character_id': character_id,
                                     'part_id': part_id})
        roles.add(role)
        collection_names.add(collection)
        asset_ids.add(asset_id)
        template_uuids.add(template_uuid)
        normalized_actors.append({
            'role': role, 'collection': collection, 'asset_id': asset_id,
            'template_uuid': template_uuid, 'root': root,
            'parts': normalized_parts,
        })
    if len(normalized_actors) == 1 and roles != {'A'}:
        _fail('INVALID_REQUEST', 'single rigid actor must use role A')
    if len(normalized_actors) == 2 and roles != {'A', 'B'}:
        _fail('INVALID_REQUEST', 'double rigid actors must contain roles A and B')
    out['clip']['actors'] = normalized_actors
    out['clip']['lights'] = []
    return out


def _identity_root(root):
    from mathutils import Matrix

    if root.type != 'EMPTY' or root.parent is not None:
        _fail('UNSUPPORTED', 'Rigid root must be a parentless Empty')
    if (root.constraints or root.animation_data is not None
            or root.instance_type != 'NONE' or root.rigid_body_constraint
            or (root.field is not None and root.field.type != 'NONE')):
        _fail('UNSUPPORTED', 'Rigid root must be a static, unconstrained, non-instancing Empty')
    identity = Matrix.Identity(4)
    basis = root.matrix_basis
    if any(not math.isfinite(basis[i][j]) or abs(basis[i][j] - identity[i][j]) > 1e-7
           for i in range(4) for j in range(4)):
        _fail('UNSUPPORTED', 'Rigid root transform must be identity')


def _part_channels(action, owner, source_start, source_end):
    if action is None:
        _fail('UNSUPPORTED', 'Rigid part requires an exclusive native Action',
              {'object': owner.name})
    if owner.animation_data is None or owner.animation_data.action is not action:
        _fail('CONFLICT', 'Rigid part Action binding is not the native binding',
              {'object': owner.name})
    ad = owner.animation_data
    if len(ad.nla_tracks) or len(ad.drivers) or getattr(ad, 'use_tweak_mode', False):
        _fail('UNSUPPORTED', 'Rigid part NLA/drivers/tweak mode are unsupported',
              {'object': owner.name})
    if ad.action_blend_type != 'REPLACE' or abs(float(ad.action_influence) - 1.0) > 1e-6:
        _fail('UNSUPPORTED', 'Rigid part Action must be REPLACE at full influence')
    if len(action.slots) != 1:
        _fail('UNSUPPORTED', 'Rigid part Action must contain exactly one slot')
    slot = action.slots[0]
    if ad.action_slot != slot or slot.target_id_type != 'OBJECT':
        _fail('UNSUPPORTED', 'Rigid part Action slot type/binding is unsupported')
    if len(action.layers) != 1 or len(action.layers[0].strips) != 1:
        _fail('UNSUPPORTED', 'Rigid part Action must contain one layer and one strip')
    strip = action.layers[0].strips[0]
    if strip.type != 'KEYFRAME':
        _fail('UNSUPPORTED', 'Rigid part Action must be a KEYFRAME strip')
    if hasattr(action.layers[0], 'influence') \
            and abs(action.layers[0].influence - 1.0) > 1e-6:
        _fail('UNSUPPORTED', 'Rigid part Action layer influence must be one')
    if getattr(action.layers[0], 'mute', False):
        _fail('UNSUPPORTED', 'Rigid part Action layer must not be muted')
    bag = strip.channelbag(slot)
    if bag is None or (hasattr(strip, 'channelbags')
                       and len(strip.channelbags) != 1):
        _fail('UNSUPPORTED', 'Rigid part Action requires exactly one channelbag')
    curves = list(bag.fcurves)
    if not curves:
        _fail('UNSUPPORTED', 'Rigid part Action has no native FCurves')
    seen = set()
    for curve in curves:
        address = (curve.data_path, int(curve.array_index))
        if address in seen:
            _fail('UNSUPPORTED', 'Rigid part Action has duplicate FCurve addresses')
        seen.add(address)
        if curve.data_path not in {'location', 'rotation_euler'} or curve.array_index not in range(3):
            _fail('UNSUPPORTED', 'Rigid part channels must be location/rotation_euler',
                  {'object': owner.name, 'path': curve.data_path})
        if len(curve.modifiers) or len(curve.sampled_points) or curve.mute or curve.lock:
            _fail('UNSUPPORTED', 'Rigid part FCurve modifiers/mute/lock are unsupported')
        keys = list(curve.keyframe_points)
        if not keys:
            _fail('UNSUPPORTED', 'Rigid part FCurve has no keys')
        times = [float(key.co.x) for key in keys]
        if any(not math.isfinite(value) or not math.isfinite(float(key.co.y))
               for value, key in zip(times, keys)):
            _fail('UNSUPPORTED', 'Rigid part keys must be finite')
        if any(b <= a for a, b in zip(times, times[1:])):
            _fail('UNSUPPORTED', 'Rigid part key times must be strictly increasing')
        if abs(times[0] - source_start) > 1e-6 or abs(times[-1] - source_end) > 1e-6:
            _fail('UNSUPPORTED', 'Rigid part Action must span the complete source clip',
                  {'object': owner.name, 'path': curve.data_path,
                   'first': times[0], 'last': times[-1]})
    return slot, bag


def _actor_structure(collection, spec, before_map, before_node_trees,
                     source_start, source_end, action_rows, action_pointers):
    if collection.asset_data is None:
        _fail('CONFLICT', 'Rigid actor Collection is not asset-marked',
              {'collection': collection.name})
    if collection.get('asset_id') != spec['asset_id']:
        _fail('CONFLICT', 'Rigid collection asset_id differs from manifest')
    if collection.get('rp_template_uuid') != spec['template_uuid']:
        _fail('CONFLICT', 'Rigid collection template UUID differs from manifest')
    if collection.get('rp_asset_schema') != LIBRARY_COLLECTION_SCHEMA:
        _fail('CONFLICT', 'Rigid collection asset schema is unsupported')
    _base._assert_new(collection, before_map, 'collections', before_node_trees)
    if collection.children:
        _fail('UNSUPPORTED', 'Rigid actor Collection cannot contain child Collections')
    if collection.users != int(collection.use_fake_user):
        _fail('UNSUPPORTED', 'Rigid actor Collection must enter the scene through the new wrapper')

    objects = list(collection.objects)
    if any(tuple(obj.users_collection) != (collection,) for obj in objects):
        _fail('UNSUPPORTED', 'Rigid actor objects must belong exclusively to their Collection')
    roots = [obj for obj in objects if obj.type == 'EMPTY']
    if len(roots) != 1:
        _fail('UNSUPPORTED', 'Rigid actor requires exactly one root Empty')
    root = roots[0]
    _base._assert_new(root, before_map, 'objects', before_node_trees)
    _identity_root(root)
    if root.get('rp_template_uuid') != spec['template_uuid']:
        _fail('CONFLICT', 'Rigid root template UUID differs from manifest')

    meshes = [obj for obj in objects if obj.type == 'MESH']
    expected = {(row['character_id'], row['part_id']): row for row in spec['parts']}
    if len(meshes) != len(expected):
        _fail('CONFLICT', 'Rigid Collection MESH count differs from manifest')
    if any(obj.type not in {'MESH', 'EMPTY'} for obj in objects):
        _fail('UNSUPPORTED', 'Rigid Collection contains an unsupported object type')

    parts = []
    for obj in sorted(meshes, key=lambda item: item.name):
        identity = (obj.get('rp_character_id'), obj.get('rp_part_id'))
        if not all(isinstance(value, str) for value in identity) or identity not in expected:
            _fail('CONFLICT', 'Rigid part identity differs from manifest or is duplicated',
                  {'object': obj.name})
        part_spec = expected.pop(identity)
        if obj.parent is not root or obj.parent_type != 'OBJECT':
            _fail('UNSUPPORTED', 'Rigid part must be a direct child of the actor root',
                  {'object': obj.name})
        if any(abs(float(obj.matrix_parent_inverse[i][j])
                   - (1.0 if i == j else 0.0)) > 1e-9
               for i in range(4) for j in range(4)):
            _fail('UNSUPPORTED', 'Rigid part parent inverse must be identity',
                  {'object': obj.name})
        if (obj.constraints or obj.modifiers or obj.rigid_body or obj.rigid_body_constraint
                or obj.instance_type != 'NONE' or (obj.field is not None and obj.field.type != 'NONE')):
            _fail('UNSUPPORTED', 'Rigid part has unsupported constraints/dependencies',
                  {'object': obj.name})
        if obj.rotation_mode != 'XYZ':
            _fail('UNSUPPORTED', 'Rigid part requires XYZ Euler rotation')
        if obj.data is None:
            _fail('UNSUPPORTED', 'Rigid part has no mesh data')
        if obj.data.shape_keys is not None or obj.data.animation_data is not None:
            _fail('UNSUPPORTED', 'Rigid part mesh data must be static')
        if len(obj.data.materials) or len(obj.material_slots):
            _fail('UNSUPPORTED', 'Rigid part materials are outside this clip policy',
                  {'object': obj.name})
        if any(abs(float(value)) > 1e-9 for value in obj.delta_location) \
                or any(abs(float(value)) > 1e-9 for value in obj.delta_rotation_euler) \
                or any(abs(float(value) - 1.0) > 1e-9 for value in obj.delta_scale):
            _fail('UNSUPPORTED', 'Rigid part delta transforms must be identity')
        _base._assert_new(obj, before_map, 'objects', before_node_trees)
        _base._assert_new(obj.data, before_map, 'meshes', before_node_trees)
        if obj.get('rp_template_uuid') != spec['template_uuid']:
            _fail('CONFLICT', 'Rigid part template UUID differs from manifest',
                  {'object': obj.name})
        action = obj.animation_data.action if obj.animation_data else None
        slot, bag = _part_channels(action, obj, source_start, source_end)
        if action.as_pointer() in action_pointers:
            _fail('CONFLICT', 'Rigid Action is shared between parts',
                  {'action': action.name})
        _base._assert_new(action, before_map, 'actions', before_node_trees)
        if action.users - int(bool(action.use_fake_user)) != 1:
            _fail('CONFLICT', 'Rigid part Action must be exclusively owned',
                  {'action': action.name, 'users': action.users})
        action_pointers.add(action.as_pointer())
        action_rows.append({
            'action': action,
            'owner': obj,
            'owner_role': spec['role'],
            'part': part_spec,
            'slot': slot,
            'bag': bag,
            'source_curves': len(bag.fcurves),
            'source_keys': sum(len(curve.keyframe_points) for curve in bag.fcurves),
            'source_first_frame': source_start,
            'source_last_frame': source_end,
        })
        parts.append({
            'object': obj,
            'spec': part_spec,
            'action': action,
            'slot': slot,
            'bag': bag,
        })
    return {'collection': collection, 'root': root, 'parts': parts,
            'objects': objects}


def _mapped_curve_count(row, target_start, target_end):
    for curve in row['bag'].fcurves:
        keys = list(curve.keyframe_points)
        if not keys:
            _fail('VALIDATION_FAILED', 'Rigid curve lost its keys during retime')
        first = float(keys[0].co.x)
        last = float(keys[-1].co.x)
        if abs(first - target_start) > 1e-5 or abs(last - target_end) > 1e-5:
            _fail('VALIDATION_FAILED', 'Rigid curve does not span target timing',
                  {'action': row['action'].name, 'first': first, 'last': last})
        if any(float(key.co.x) < target_start - 1e-5
               or float(key.co.x) > target_end + 1e-5 for key in keys):
            _fail('VALIDATION_FAILED', 'Rigid curve retains keys outside target timing',
                  {'action': row['action'].name})
        if any(not math.isfinite(float(key.co.x))
               or not math.isfinite(float(key.co.y)) for key in keys):
            _fail('VALIDATION_FAILED', 'Rigid curve contains non-finite keys')


def apply_rigid_clip(context, request):
    """Apply one frozen rigid clip; returns a JSON-safe result."""
    import bpy
    from mathutils import Matrix

    req = _normalize_request(request)
    clip = req['clip']
    scene, layer = _base._resolve_context(bpy, context)
    _base._validate_scene(scene, req)
    scene_before = _base._scene_state(scene)
    library_path = clip['library']['file']
    library_sha = _base._sha256_file(library_path, 'clip.library.file')
    if library_sha != clip['library']['expected_sha256']:
        _fail('CONFLICT', 'Rigid frozen library SHA256 does not match request',
              {'expected': clip['library']['expected_sha256'],
               'actual': library_sha})

    before_map = _base._id_collections(bpy)
    before_node_trees = _base._before_node_trees(bpy)
    before_ids = _top_level_ids(bpy)
    inventory_before = _inventory(bpy)
    existing_uuids = set()
    for category in ('collections', 'objects', 'meshes', 'actions',
                     'materials', 'armatures'):
        for item in getattr(bpy.data, category):
            if 'rp_instance_uuid' in item.keys():
                existing_uuids.add(item.get('rp_instance_uuid'))
    for actor in clip['actors']:
        existing_uuids.add(actor['template_uuid'])
        existing_uuids.add(actor['asset_id'])

    action_rows = []
    action_pointers = set()
    asset_records = []
    actor_rows = []
    try:
        with bpy.data.libraries.load(library_path, link=False,
                                     relative=False) as (data_from, data_to):
            source_names = set(data_from.objects)
            for actor in clip['actors']:
                names = {actor['root'], *(part['object'] for part in actor['parts'])}
                missing = names - source_names
                if missing:
                    _fail('NOT_FOUND', 'Rigid manifest objects are missing from the library',
                          {'objects': sorted(missing)})
            data_to.collections = [actor['collection'] for actor in clip['actors']]
        if _base._sha256_file(library_path, 'clip.library.file') != library_sha:
            _fail('CONFLICT', 'Rigid frozen library changed during import')
        loaded = list(data_to.collections)
        if len(loaded) != len(clip['actors']) or any(item is None for item in loaded):
            _fail('NOT_FOUND', 'Rigid library did not load the explicit Collections',
                  {'loaded': len(loaded)})
        by_asset_id = {}
        for collection in loaded:
            asset_id = _canonical_uuid(collection.get('asset_id'), 'rigid collection asset_id')
            if asset_id in by_asset_id:
                _fail('CONFLICT', 'Duplicate loaded rigid Collection asset_id')
            by_asset_id[asset_id] = collection
        if set(by_asset_id) != {actor['asset_id'] for actor in clip['actors']}:
            _fail('CONFLICT', 'Loaded rigid Collection asset IDs differ from manifest')

        for actor in clip['actors']:
            collection = by_asset_id[actor['asset_id']]
            structure = _actor_structure(
                collection, actor, before_map, before_node_trees,
                clip['timing']['start'], clip['timing']['end'],
                action_rows, action_pointers)
            actor_rows.append({
                'spec': actor,
                'collection': collection,
                'structure': structure,
                'source_asset_id': actor['asset_id'],
                'template_uuid': actor['template_uuid'],
                'new_uuid': None,
            })
            asset_records.append({
                'kind': 'collection', 'role': actor['role'],
                'asset_id': actor['asset_id'],
                'source_name': actor['collection'],
                'loaded_name': collection.name,
                'template_uuid': actor['template_uuid'],
            })

        if not action_rows:
            _fail('UNSUPPORTED', 'Rigid clip contains no owned native Actions')
        if len(action_rows) != len({row['action'].as_pointer() for row in action_rows}):
            _fail('CONFLICT', 'Rigid clip Action inventory contains duplicates')
        expected_ids = set(loaded)
        for actor_row in actor_rows:
            structure = actor_row['structure']
            expected_ids.update(structure['objects'])
            for part in structure['parts']:
                expected_ids.update((part['object'].data, part['action']))
        imported_ids = _top_level_ids(bpy) - before_ids
        # Blender keeps an append-source Library handle in memory until the
        # file is saved. The expected IDs themselves must all be local.
        source_handles = {item for item in imported_ids if isinstance(item, bpy.types.Library)
                          and Path(bpy.path.abspath(item.filepath)).resolve() == Path(library_path).resolve()}
        if imported_ids - source_handles != expected_ids:
            _fail('UNSUPPORTED', 'Rigid library brought undeclared native data',
                  {'extra': sorted(f'{item.bl_rna.identifier}:{item.name}'
                                   for item in imported_ids - expected_ids - source_handles)})
        for row in action_rows:
            _mapped_curve_count(row, clip['timing']['start'], clip['timing']['end'])

        source_start = clip['timing']['start']
        source_end = clip['timing']['end']
        trim_report = None
        if 'source_window' in req:
            from . import curve_segments, native
            window = req['source_window']
            source_start, source_end = window['start'], window['end']
            library_keys = sum(row['source_keys'] for row in action_rows)
            curve_count = sum(row['source_curves'] for row in action_rows)
            for row in action_rows:
                for curve in row['bag'].fcurves:
                    try:
                        selected = curve_segments.trim_states(
                            [native.key_state(key) for key in curve.keyframe_points],
                            source_start, source_end)
                        curve_segments.write_states(curve, selected)
                    except ValueError as exc:
                        _fail('UNSUPPORTED',
                              'Rigid native source-window extraction is unsupported',
                              {'action': row['action'].name,
                               'path': curve.data_path,
                               'index': curve.array_index,
                               'reason': str(exc)})
                row['source_keys'] = sum(len(curve.keyframe_points)
                                         for curve in row['bag'].fcurves)
                row['source_first_frame'] = source_start
                row['source_last_frame'] = source_end
                _mapped_curve_count(row, source_start, source_end)
            selected_keys = sum(row['source_keys'] for row in action_rows)
            if selected_keys > library_keys:
                _fail('VALIDATION_FAILED',
                      'Rigid source window produced more keys than the frozen library')
            trim_report = {
                'source_window': {'start': source_start, 'end': source_end},
                'requested': window['requested'],
                'library_keys': library_keys,
                'selected_keys': selected_keys,
                'curve_count': curve_count,
                'boundary_policy': 'Native Bezier subdivision; necessary split/boundary handles FREE',
                'baked': False,
                'outside_keys_retained': False,
            }

        for actor_row in actor_rows:
            actor_row['new_uuid'] = _base._new_uuid(existing_uuids)
        for actor_row in actor_rows:
            structure = actor_row['structure']
            new_uuid = actor_row['new_uuid']
            tagged = [structure['collection'], structure['root']] + \
                [row['object'] for row in structure['parts']]
            for item in tagged:
                item['rp_instance_uuid'] = new_uuid
            structure['collection']['rp_source_asset_id'] = actor_row['source_asset_id']
            structure['collection']['rp_source_template_uuid'] = actor_row['template_uuid']
            structure['collection']['rp_clip_role'] = actor_row['spec']['role']

        for category, item in _base._new_ids(bpy, before_map, before_node_trees):
            if category not in {'collections', 'objects', 'meshes', 'actions'}:
                _fail('UNSUPPORTED', 'Rigid apply introduced a disallowed native data type',
                      {'category': category, 'name': item.name})
            if getattr(item, 'use_fake_user', False):
                item.use_fake_user = False
            if getattr(item, 'asset_data', None) is not None \
                    or 'asset_id' in item.keys():
                _base._clear_asset_marks(item)

        frame_span = req['end'] - req['start']
        source_span = source_end - source_start
        ratio = frame_span / source_span
        quarter = int(req['yaw_quarter_turns'])
        if quarter == 0:
            matrix2 = ((1.0, 0.0), (0.0, 1.0))
        elif quarter == 1:
            matrix2 = ((0.0, -1.0), (1.0, 0.0))
        elif quarter == 2:
            matrix2 = ((-1.0, 0.0), (0.0, -1.0))
        else:
            matrix2 = ((0.0, 1.0), (-1.0, 0.0))
        yaw = quarter * math.pi / 2.0
        translation = [float(value) for value in req['translation']]
        scale = float(req['scale'])
        mapping = {
            'schema': 'animation-rigid-clip-mapping-v1',
            'source_start': source_start,
            'source_end': source_end,
            'target_start': req['start'],
            'target_end': req['end'],
            'frame_ratio': ratio,
            'frame_affine': [ratio, req['start'] - source_start * ratio],
            'scale': scale,
            'translation': translation,
            'yaw_quarter_turns': quarter,
            'yaw_radians': yaw,
            'quarter_matrix': [list(row) for row in matrix2],
            'similarity_matrix': [
                [scale * matrix2[0][0], scale * matrix2[0][1], 0.0, translation[0]],
                [scale * matrix2[1][0], scale * matrix2[1][1], 0.0, translation[1]],
                [0.0, 0.0, scale, translation[2]],
                [0.0, 0.0, 0.0, 1.0],
            ],
        }
        if trim_report is not None:
            mapping['source_window'] = trim_report['source_window']

        def map_frame(frame):
            return req['start'] + (float(frame) - source_start) * ratio

        for row in action_rows:
            before_curves = len(row['bag'].fcurves)
            before_keys = sum(len(curve.keyframe_points) for curve in row['bag'].fcurves)
            for curve in row['bag'].fcurves:
                _base._retime_curve(curve, map_frame)
            after_curves = len(row['bag'].fcurves)
            after_keys = sum(len(curve.keyframe_points) for curve in row['bag'].fcurves)
            if (before_curves, before_keys) != (after_curves, after_keys):
                _fail('VALIDATION_FAILED', 'Rigid retime changed curve/key counts')
            _mapped_curve_count(row, req['start'], req['end'])

        wrapper = bpy.data.collections.new(req['instance_name'])
        scene.collection.children.link(wrapper)
        anchor = bpy.data.objects.new(req['instance_name'] + '_Anchor', None)
        anchor.empty_display_type = 'PLAIN_AXES'
        anchor.empty_display_size = 0.35
        wrapper.objects.link(anchor)
        for actor_row in actor_rows:
            collection = actor_row['collection']
            wrapper.children.link(collection)
            root = actor_row['structure']['root']
            if root.parent is not None:
                _fail('CONFLICT', 'Rigid actor root is unexpectedly parented')
            root.parent = anchor
            root.parent_type = 'OBJECT'
            root.matrix_parent_inverse = Matrix.Identity(4)
        anchor.location = translation
        anchor.rotation_mode = 'XYZ'
        anchor.rotation_euler = (0.0, 0.0, yaw)
        anchor.scale = (scale, scale, scale)
        if min(anchor.scale) <= 0:
            _fail('VALIDATION_FAILED', 'Rigid anchor scale collapsed in Blender')
        layer.update()

        for actor_row in actor_rows:
            for obj in actor_row['structure']['objects']:
                if scene.objects.get(obj.name) is not obj:
                    _fail('CONFLICT', 'New rigid object is not in the active Scene',
                          {'object': obj.name})
        if scene.objects.get(anchor.name) is not anchor:
            _fail('CONFLICT', 'New rigid anchor is not in the active Scene')

        common = {
            'ac_clip_schema': REQUEST_SCHEMA,
            'ac_clip_id': clip['clip_id'],
            'ac_clip_instance_name': req['instance_name'],
            'ac_clip_source_file': clip['source']['file'],
            'ac_clip_source_sha256': clip['source']['sha256'],
            'ac_clip_library_file': clip['library']['file'],
            'ac_clip_library_sha256': library_sha,
            'ac_clip_mapping': json.dumps(mapping, sort_keys=True,
                                          ensure_ascii=False, allow_nan=False),
            'ac_clip_assets': json.dumps(asset_records, sort_keys=True,
                                         ensure_ascii=False, allow_nan=False),
            'rp_clip_schema': CLIP_SCHEMA,
            'rp_clip_id': clip['clip_id'],
            'rp_clip_instance_name': req['instance_name'],
            'rp_clip_source_file': clip['source']['file'],
            'rp_clip_source_sha256': clip['source']['sha256'],
            'rp_clip_library_file': clip['library']['file'],
            'rp_clip_library_sha256': library_sha,
            'rp_clip_mapping': json.dumps(mapping, sort_keys=True,
                                          ensure_ascii=False, allow_nan=False),
            'rp_clip_assets': json.dumps(asset_records, sort_keys=True,
                                         ensure_ascii=False, allow_nan=False),
            'rp_clip_anchor': anchor.name,
            'rp_clip_actor_roles': ','.join(row['spec']['role'] for row in actor_rows),
        }
        for key, value in common.items():
            wrapper[key] = value
        for actor_row in actor_rows:
            collection = actor_row['collection']
            for key, value in common.items():
                if key != 'rp_clip_anchor':
                    collection[key] = value
            collection['rp_clip_role'] = actor_row['spec']['role']

        new_rows = _base._new_ids(bpy, before_map, before_node_trees)
        categories = sorted({category for category, _item in new_rows})
        if categories != sorted(set(categories) & {'collections', 'objects',
                                                   'meshes', 'actions'}):
            _fail('UNSUPPORTED', 'Rigid apply introduced disallowed new IDs',
                  {'categories': categories})
        inventory_after = _inventory(bpy)
        if inventory_after != inventory_before:
            _fail('UNSUPPORTED', 'Rigid apply carried native materials/images/etc.',
                  {'before': inventory_before, 'after': inventory_after})

        after_scene = _base._scene_state(scene)
        for key in ('frame_start', 'frame_end', 'frame_current', 'frame_subframe',
                    'camera_pointer', 'camera_name'):
            if after_scene[key] != scene_before[key]:
                _fail('CONFLICT', 'Rigid apply changed Scene state',
                      {'field': key, 'before': scene_before[key],
                       'after': after_scene[key]})

        after_action_rows = []
        for row in action_rows:
            curves = list(row['bag'].fcurves)
            keys = sum(len(curve.keyframe_points) for curve in curves)
            after_action_rows.append({
                'action': row['action'].name,
                'owner': row['owner'].name,
                'role': row['owner_role'],
                'object': row['owner'].name,
                'part_id': row['part']['part_id'],
                'character_id': row['part']['character_id'],
                'slot_identifier': row['slot'].identifier,
                'slot_type': row['slot'].target_id_type,
                'curves': len(curves),
                'keys': keys,
                'first_frame': min(float(key.co.x) for curve in curves
                                   for key in curve.keyframe_points),
                'last_frame': max(float(key.co.x) for curve in curves
                                  for key in curve.keyframe_points),
            })

        actors_result = []
        for actor_row in actor_rows:
            structure = actor_row['structure']
            part_rows = []
            for part in structure['parts']:
                row = next(item for item in after_action_rows
                           if item['action'] == part['action'].name)
                part_rows.append({
                    'object': part['object'].name,
                    'source_object': part['spec']['object'],
                    'character_id': part['spec']['character_id'],
                    'part_id': part['spec']['part_id'],
                    'action': part['action'].name,
                    'slot_identifier': part['slot'].identifier,
                    'slot_type': part['slot'].target_id_type,
                    'curves': row['curves'],
                    'keys': row['keys'],
                })
            actors_result.append({
                'role': actor_row['spec']['role'],
                'collection': structure['collection'].name,
                'root': structure['root'].name,
                'anchor': anchor.name,
                'instance_uuid': actor_row['new_uuid'],
                'template_uuid': actor_row['template_uuid'],
                'source_asset_id': actor_row['source_asset_id'],
                'objects': len(structure['objects']),
                'meshes': len(structure['parts']),
                'parts': part_rows,
            })

        result = {
            'schema': RESULT_SCHEMA,
            'status': 'applied',
            'module': 'animation_curves_workflow_addon.rigid_clip_asset',
            'clip_schema': CLIP_SCHEMA,
            'clip_id': clip['clip_id'],
            'title': clip['title'],
            'instance_name': req['instance_name'],
            'collection': wrapper.name,
            'anchor': anchor.name,
            'actors': actors_result,
            'lights': [],
            'materials': [],
            'mapping': mapping,
            'trim': trim_report,
            'actions': {
                'count': len(after_action_rows),
                'curves': sum(row['curves'] for row in after_action_rows),
                'keys': sum(row['keys'] for row in after_action_rows),
                'by_owner': after_action_rows,
            },
            'assets': {
                'library': clip['library']['file'],
                'library_sha256': library_sha,
                'requested': asset_records,
            },
            'scene_inventory': {
                'before': inventory_before,
                'after': inventory_after,
                'new_id_categories': categories,
            },
            'scene': {
                'name': scene.name,
                'view_layer': layer.name,
                'fps': int(scene.render.fps),
                'fps_base': float(scene.render.fps_base),
                'unit_scale': float(scene.unit_settings.scale_length),
                'frame_start': int(scene.frame_start),
                'frame_end': int(scene.frame_end),
                'frame_current': int(scene.frame_current),
                'camera': scene_before['camera_name'],
                'unchanged': True,
            },
            'limits': [
                'One or two explicit asset-marked rigid Collections from one local frozen library.',
                'Exactly one identity parentless Empty root and an explicit MESH part list per actor.',
                'Each part owns one exclusive native Action with only location/rotation_euler curves.',
                'Whole-clip source span only; no source_window/trim.' if trim_report is None
                else 'Explicit source_window; exact native interval extraction, no bake or linearization.',
                'Uniform positive scale, 3D translation and integer quarter-turn yaw.',
                'No armature, skinning, retargeting, materials, lights, cameras, images, textures or worlds.',
                'No shared Actions, NLA, drivers, tweak mode, curve modifiers or linked/override IDs.',
                'No render or save; the public CLI persists and independently reopens the candidate.',
            ],
        }
        return result
    except BaseException as original:
        try:
            imported_ids = _top_level_ids(bpy) - before_ids
            if imported_ids:
                bpy.data.batch_remove(ids=imported_ids)
            if _top_level_ids(bpy) != before_ids:
                raise RuntimeError('BlendData ID inventory differs from before import')
        except Exception as cleanup_error:
            raise WorkflowError('CLEANUP_FAILED',
                                'Rigid clip apply failed and cleanup was incomplete',
                                {'original': str(original),
                                 'cleanup': str(cleanup_error)}) from original
        if isinstance(original, WorkflowError):
            raise
        raise WorkflowError('NATIVE_FAILURE', str(original),
                            {'type': type(original).__name__}) from original


__all__ = ['apply_rigid_clip', 'REQUEST_SCHEMA', 'CLIP_SCHEMA', 'RESULT_SCHEMA']
