"""Bounded Blender native adapter. No operators, file I/O, or scene activation."""
import math
import re

import bpy

from .contract import WorkflowError

EPSILON = 1e-6
_COLLECTIONS = {'OBJECT': 'objects', 'LIGHT': 'lights', 'CAMERA': 'cameras',
                'MATERIAL': 'materials', 'MATERIAL_NODETREE': 'materials'}
_ID_TYPES = {'OBJECT': 'OBJECT', 'LIGHT': 'LIGHT', 'CAMERA': 'CAMERA',
             'MATERIAL': 'MATERIAL', 'MATERIAL_NODETREE': 'NODETREE'}
_MANAGED = {'mw_id', 'mw_state_v1', 'mw_state_v2', 'lw_owner',
            'lighting_workflow_v1', '_toon_scope_id', '_toon_role', '_toon_state',
            '_toon_engine', '_toon_group_id', '_toon_core_id', '_toon_action_id',
            '_toon_object_id', '_toon_mesh_id'}
_MANAGED_NODE = _MANAGED | {'_toon_node_id'}
_SOCKET_PATH = re.compile(r'^nodes\["(?:[^"\\]|\\.)+"\]\.inputs\[\d+\]\.default_value$')


def fail(code, message, details=None):
    raise WorkflowError(code, message, details or {})


def finite(value):
    value = float(value)
    if not math.isfinite(value):
        fail('UNSUPPORTED', 'Non-finite native animation data')
    return value


def effective_users(item):
    return int(item.users) - int(bool(item.use_fake_user))


def local(item):
    if item.library or item.override_library:
        fail('UNSUPPORTED', 'Linked or override animation data is read-only', {'id': item.name})


def _plain(value, depth=0):
    """Bounded RNA witnesses without pointers or transient memory identifiers."""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return finite(value)
    if isinstance(value, bpy.types.ID):
        return {'id_type': value.bl_rna.identifier, 'name': value.name}
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v, depth + 1) for v in value)
    if depth > 6:
        fail('UNSUPPORTED', 'Native animation witness nesting exceeds supported depth')
    if hasattr(value, 'to_list'):
        return _plain(value.to_list(), depth + 1)
    if isinstance(value, dict) or hasattr(value, 'keys') and not hasattr(value, 'bl_rna'):
        return {str(k): _plain(value[k], depth + 1) for k in sorted(value.keys())}
    try:
        return [_plain(v, depth + 1) for v in value]
    except TypeError:
        fail('UNSUPPORTED', 'Unsupported native animation witness value')


def rna_values(item, exclude=()):
    out = {}
    for prop in item.bl_rna.properties:
        name = prop.identifier
        if name == 'rna_type' or name in exclude:
            continue
        if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}:
            out[name] = _plain(getattr(item, name))
        elif prop.type == 'COLLECTION':
            out[name] = [rna_values(v) for v in getattr(item, name)]
    return out


def custom_state(item):
    return {k: _plain(item[k]) for k in sorted(item.keys())}


def resolve_context(manifest):
    requested = manifest['context']
    scene = bpy.data.scenes.get(requested['scene'])
    if scene is None:
        fail('NOT_FOUND', 'Scene does not exist: ' + requested['scene'])
    layer = scene.view_layers.get(requested['view_layer'])
    if layer is None:
        fail('NOT_FOUND', 'View layer does not exist: ' + requested['view_layer'])
    local(scene)
    return scene, layer


def _related(owner, kind, scene, material=None):
    items = [owner]
    if material is not None:
        items.append(material)
        if material.node_tree:
            items.append(material.node_tree)
    if kind == 'OBJECT':
        if getattr(owner, 'data', None):
            items.append(owner.data)
    else:
        for obj in scene.objects:
            if kind in {'LIGHT', 'CAMERA'} and obj.data == owner:
                items.append(obj)
            elif material and any(slot.material == material for slot in obj.material_slots):
                items.append(obj)
    return items


def _path_value(owner, spec):
    kind, path, index = spec['owner']['type'], spec['data_path'], spec['array_index']
    allowed = {'OBJECT': {'location', 'rotation_euler', 'rotation_quaternion', 'scale'},
               'LIGHT': {'energy', 'color'}, 'CAMERA': {'lens', 'ortho_scale'},
               'MATERIAL': {'diffuse_color'}}
    if kind == 'MATERIAL_NODETREE':
        if not _SOCKET_PATH.fullmatch(path):
            fail('UNSUPPORTED', 'Only explicit material input socket default_value paths are supported')
        try:
            socket = owner.path_resolve(path[:-len('.default_value')])
        except (ValueError, TypeError, AttributeError) as exc:
            fail('NOT_FOUND', 'Material socket path does not resolve', {'path': path, 'error': str(exc)})
        if not isinstance(socket, bpy.types.NodeSocket) or socket.is_linked:
            fail('UNSUPPORTED', 'Target must be an unlinked native input socket')
        if socket.path_from_id('default_value') != path or socket.is_property_readonly('default_value'):
            fail('UNSUPPORTED', 'Socket default value is not an editable canonical path')
    elif kind == 'OBJECT' and path.startswith('pose.'):
        from . import pose_batch,pose_event,clip_event
        if pose_event.active():pose_event.native_target(owner,path,index)
        elif pose_batch.active():pose_batch.native_target(owner,path,index)
        elif clip_event.active():clip_event.native_target(owner,path,index)
        else:
            from .pose_channel import native_target
            native_target(owner,path,index)
    elif path not in allowed[kind]:
        fail('UNSUPPORTED', 'Path is outside the explicit channel support boundary', {'path': path})
    if kind == 'OBJECT':
        if path == 'rotation_euler' and owner.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
            fail('UNSUPPORTED', 'Euler channels require an Euler rotation mode')
        if path == 'rotation_quaternion' and owner.rotation_mode != 'QUATERNION':
            fail('UNSUPPORTED', 'Quaternion channels require QUATERNION rotation mode')
    try:
        value = owner.path_resolve(path)
    except (ValueError, TypeError, AttributeError) as exc:
        fail('NOT_FOUND', 'Target data path does not resolve', {'path': path, 'error': str(exc)})
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if index != 0:
            fail('INVALID_REQUEST', 'Scalar channels require array_index 0')
        return finite(value)
    try:
        if index >= len(value) or isinstance(value[index], bool):
            fail('INVALID_REQUEST', 'Channel array index is outside numeric property')
        return finite(value[index])
    except TypeError:
        fail('UNSUPPORTED', 'Only numeric scalar or numeric array channels are supported')


def _check_managed_nodes(related):
    """Keep marked shader nodes under their owning workflow's edit adapter."""
    trees = set()
    for item in related:
        tree = item if isinstance(item, bpy.types.NodeTree) else getattr(item, 'node_tree', None)
        if tree is None or tree in trees:
            continue
        trees.add(tree)
        for node in tree.nodes:
            markers = sorted(set(node.keys()) & _MANAGED_NODE)
            if markers:
                fail('UNSUPPORTED', 'Managed node data requires its domain adapter',
                     {'tree': tree.name, 'node': node.name, 'markers': markers})


def action_binding(owner, expected_type, writing=False):
    ad = owner.animation_data
    if ad and (len(ad.nla_tracks) or len(ad.drivers) or ad.use_tweak_mode):
        fail('UNSUPPORTED', 'NLA, drivers, or tweak mode require a separately validated adapter')
    if ad and (ad.action_blend_type != 'REPLACE' or abs(ad.action_influence - 1.0) > EPSILON):
        fail('UNSUPPORTED', 'Active animation data requires REPLACE and full influence')
    if not ad or not ad.action:
        return None, None, None
    action, slot = ad.action, ad.action_slot
    local(action)
    markers = sorted(set(action.keys()) & _MANAGED)
    if markers:
        fail('UNSUPPORTED', 'Managed Action requires its domain adapter', {'markers': markers})
    if len(action.slots) != 1 or not slot or slot != action.slots[0] or slot.target_id_type != expected_type:
        fail('UNSUPPORTED', 'Action requires exactly one correctly typed active slot')
    if len(action.layers) != 1 or len(action.layers[0].strips) != 1:
        fail('UNSUPPORTED', 'Action requires one layer and one strip')
    strip = action.layers[0].strips[0]
    if strip.type != 'KEYFRAME':
        fail('UNSUPPORTED', 'Only KEYFRAME action strips are supported')
    if hasattr(action.layers[0], 'influence') and abs(action.layers[0].influence - 1.0) > EPSILON:
        fail('UNSUPPORTED', 'Action layer influence must be one')
    if getattr(action.layers[0], 'mute', False):
        fail('UNSUPPORTED', 'Muted Action layers require a separately validated adapter')
    bag = strip.channelbag(slot)
    if bag is None:
        fail('UNSUPPORTED', 'Active Action slot has no channelbag')
    if hasattr(strip, 'channelbags') and len(strip.channelbags) != 1:
        fail('UNSUPPORTED', 'Unexpected additional channelbags')
    return action, slot, bag


def resolve_targets(manifest, writing=False):
    scene, layer = resolve_context(manifest)
    rows = []
    for spec in manifest['targets']:
        kind, name = spec['owner']['type'], spec['owner']['name']
        item = getattr(bpy.data, _COLLECTIONS[kind]).get(name)
        if item is None:
            fail('NOT_FOUND', 'Target owner does not exist', {'owner': spec['owner']})
        material = item if kind in {'MATERIAL', 'MATERIAL_NODETREE'} else None
        if kind == 'MATERIAL_NODETREE':
            if not material.use_nodes or material.node_tree is None:
                fail('UNSUPPORTED', 'Material requires an existing node tree')
            item = material.node_tree
        if kind == 'OBJECT' and (scene.objects.get(name) != item or layer.objects.get(name) != item):
            fail('CONFLICT', 'Target object is outside the requested scene/view layer')
        related = _related(item, kind, scene, material)
        for related_id in related:
            local(related_id)
            markers = sorted(set(related_id.keys()) & _MANAGED)
            if markers:
                fail('UNSUPPORTED', 'Managed material/lighting/toon data requires its domain adapter',
                     {'id': related_id.name, 'markers': markers})
        if kind in {'LIGHT', 'MATERIAL', 'MATERIAL_NODETREE'}:
            _check_managed_nodes(related)
        if writing:
            if effective_users(item) > 1 or material and effective_users(material) > 1:
                fail('CONFLICT', 'Shared owner ID cannot be isolated by copying only its Action', {'owner': spec['owner']})
        value = _path_value(item, spec)
        if kind == 'OBJECT' and spec['data_path'].startswith('pose.'):
            from . import pose_batch,pose_event,clip_event
            if pose_event.active():pose_event.validate_manifest(item,spec,manifest)
            elif pose_batch.active():pose_batch.validate_manifest(item,spec,manifest)
            elif clip_event.active():clip_event.validate_manifest(item,spec,manifest)
            else:
                from .pose_channel import validate_manifest
                validate_manifest(item,spec,manifest)
        action, slot, bag = action_binding(item, _ID_TYPES[kind], writing)
        if writing and action and effective_users(action) > 1 and spec['sharing'] != 'isolate_action':
            fail('CONFLICT', 'Shared Action requires explicit isolate_action', {'target': spec['id']})
        curve = bag.fcurves.find(spec['data_path'], index=spec['array_index']) if bag else None
        if writing and curve and (len(curve.modifiers) or len(curve.sampled_points) or curve.mute or curve.lock):
            fail('UNSUPPORTED', 'Edited channels must be unlocked, unmuted keyframes without modifiers/samples')
        rows.append({'spec': spec, 'owner': item, 'material': material, 'action': action,
                     'slot': slot, 'bag': bag, 'curve': curve, 'base_value': value,
                     'id_type': _ID_TYPES[kind]})
    return scene, layer, rows


def key_state(key):
    return rna_values(key)


def curve_state(curve):
    return {'data_path': curve.data_path, 'array_index': int(curve.array_index),
            'properties': rna_values(curve, {'keyframe_points', 'sampled_points', 'modifiers', 'is_valid', 'is_empty'}),
            'group': curve.group.name if curve.group else None,
            'keys': [key_state(k) for k in curve.keyframe_points],
            'sampled_points': [rna_values(p) for p in curve.sampled_points],
            'modifiers': [rna_values(m) for m in curve.modifiers]}


def bag_state(bag):
    return [curve_state(curve) for curve in sorted(bag.fcurves, key=lambda c: (c.data_path, c.array_index))] if bag else []


def action_state(action):
    if action is None:
        return None
    slot = action.slots[0]
    bag = action.layers[0].strips[0].channelbag(slot)
    return {'properties': custom_state(action), 'curves': bag_state(bag)}


def binding_state(owner):
    ad = owner.animation_data
    return {'had_animation_data': bool(ad), 'action': ad.action if ad else None,
            'slot': ad.action_slot if ad and ad.action else None,
            'settings': {name: getattr(ad, name) for name in ('action_blend_type', 'action_extrapolation', 'action_influence')}
            if ad else {}}


def restore_binding(owner, state):
    if not state['had_animation_data']:
        owner.animation_data_clear()
        return
    ad = owner.animation_data_create()
    ad.action = state['action']
    if state['action']:
        ad.action_slot = state['slot']
    for key, value in state['settings'].items():
        setattr(ad, key, value)


def clone_action(row):
    if row['action']:
        action = row['action'].copy()
        slot = action.slots[0]
        bag = action.layers[0].strips[0].channelbag(slot)
        return action, slot, bag
    action = bpy.data.actions.new('Animation Curves candidate')
    try:
        slot = action.slots.new(id_type=row['id_type'], name=row['owner'].name)
        layer = action.layers.new('Animation')
        strip = layer.strips.new(type='KEYFRAME')
        bag = strip.channelbag(slot, ensure=True)
        if slot.target_id_type != row['id_type'] or bag is None:
            fail('VALIDATION_FAILED', 'Native Action slot/channelbag creation failed')
        return action, slot, bag
    except BaseException:
        bpy.data.actions.remove(action)
        raise


def find_key(curve, frame):
    keys = [key for key in curve.keyframe_points if abs(float(key.co.x) - frame) <= EPSILON]
    if len(keys) > 1:
        fail('UNSUPPORTED', 'Ambiguous coincident keyframes')
    return keys[0] if keys else None


def curve_value(curve, frame, fallback):
    return finite(curve.evaluate(frame)) if curve else fallback


def inspect_rows(manifest, scene, layer, rows):
    targets = []
    for row in rows:
        spec, curve = row['spec'], row['curve']
        targets.append({'id': spec['id'], 'owner': dict(spec['owner']),
                        'data_path': spec['data_path'], 'array_index': spec['array_index'],
                        'has_action': row['action'] is not None,
                        'action_name': row['action'].name if row['action'] else None,
                        'slot_identifier': row['slot'].identifier if row['slot'] else None,
                        'slot_type': row['id_type'] if row['slot'] else None,
                        'channel': curve_state(curve) if curve else None,
                        'samples': [{'frame': frame, 'value': curve_value(curve, frame, row['base_value']),
                                     'evaluation': 'fcurve' if curve else 'unanimated_property'}
                                    for frame in manifest['samples']]})
    return {'schema': 'animation-curves-inspection-v1',
            'context': {'scene': scene.name, 'view_layer': layer.name,
                        'fps': int(scene.render.fps), 'fps_base': finite(scene.render.fps_base)},
            'targets': targets,
            'evaluation_boundary': 'Native FCurve values; not constraints, world transforms, or rendered appearance'}
