"""Edit one bound Action Slot while preserving all other bindings."""

import bpy

from . import core, native
from .contract import _text, normalize_manifest
from .production_drivers import require_undriven


_POSE_OWNERSHIP = {'ac_instance_uuid', 'ac_template_owner', 'ac_practice_owner'}


def _editable_object(obj):
    native.local(obj)
    # Attached meshes, constraints and drivers legitimately reference their
    # rig. Its scene and collection membership determine instance ownership.
    if len(obj.users_collection) > 1 or len(obj.users_scene) > 1:
        native.fail('CONFLICT', 'Shared Object data must be isolated before editing animation',
                    {'object': obj.name})


def _slot_bags(action):
    if len(action.layers) != 1 or len(action.layers[0].strips) != 1:
        native.fail('UNSUPPORTED', 'Expected one Action layer and one keyframe strip')
    strip = action.layers[0].strips[0]
    if strip.type != 'KEYFRAME':
        native.fail('UNSUPPORTED', 'Expected a keyframe Action strip')
    return {slot.identifier: (slot, strip.channelbag(slot)) for slot in action.slots}


def apply(manifest, *, action_name, slot_identifier, base_track=None):
    """Read or edit the named active or explicitly managed NLA base Slot.

    Edits bind a native Action copy to the named object only. All other users,
    including users of the same Slot, keep their original Action and Slot.
    Manifest frames are Action-local, including for a retimed NLA clip.
    """
    from . import production_layers

    _text(action_name, 'action_name')
    _text(slot_identifier, 'slot_identifier')
    if base_track is not None:
        _text(base_track, 'base_track')
    manifest = normalize_manifest(manifest)
    if 'event_shift' in manifest or 'derived_handle_scope' in manifest:
        native.fail('INVALID_REQUEST', 'Slot edits accept explicit key operations')
    scene, layer = native.resolve_context(manifest)
    owner_spec = manifest['targets'][0]['owner']
    if owner_spec['type'] != 'OBJECT' or any(
        target['owner'] != owner_spec for target in manifest['targets']
    ):
        native.fail('INVALID_REQUEST', 'Select one object per Action Slot request')
    obj = layer.objects.get(owner_spec['name'])
    if obj is None:
        native.fail('NOT_FOUND', 'Target object is outside the requested view layer')
    native.local(obj)
    anim = obj.animation_data
    if anim and anim.use_tweak_mode:
        native.fail('CONFLICT', 'Exit NLA tweak mode before using the active Action Slot')
    clip = None
    receipt = {}
    if base_track is None:
        if not anim or not anim.action or not anim.action_slot:
            native.fail('NOT_FOUND', 'Target object has no active Action Slot')
        action, slot = anim.action, anim.action_slot
    else:
        state, clip = production_layers.resolve_base_slot(obj, base_track)
        action, slot = clip.action, clip.action_slot
        receipt['binding'] = {
            'base_track': base_track, 'frame_domain': 'ACTION',
            **production_layers._clip_timing(clip),
        }
    native.local(action)
    if action.name != action_name or slot.identifier != slot_identifier:
        native.fail('CONFLICT', 'The selected Action or Slot changed')
    markers = set(action.keys()) | set(obj.keys())
    if markers & native._MANAGED:
        native.fail('UNSUPPORTED', 'Managed target requires its domain adapter')
    bags = _slot_bags(action)
    slot, bag = bags[slot_identifier]
    if slot.target_id_type != 'OBJECT' or bag is None:
        native.fail('UNSUPPORTED', 'Expected an object Slot with an existing channelbag')

    rows = {}
    for spec in manifest['targets']:
        address = (spec['data_path'], spec['array_index'])
        path, index = address
        if path.startswith('pose.'):
            if obj.type != 'ARMATURE':
                native.fail('UNSUPPORTED', 'Pose channels require an armature object')
            if (markers | set(obj.data.keys())) & (native._MANAGED | _POSE_OWNERSHIP):
                native.fail('UNSUPPORTED', 'Managed pose target requires its domain adapter')
            value = native.finite(production_layers._channel_vector(obj, path, index)[index])
        else:
            value = native._path_value(obj, spec)
        rows[spec['id']] = {
            'owner': obj, 'spec': spec, 'action': action, 'slot': slot, 'bag': bag,
            'curve': bag.fcurves.find(address[0], index=address[1]),
            'address': address, 'base_value': value, 'id_type': 'OBJECT',
        }

    def readback():
        return native.inspect_rows(manifest, scene, layer, list(rows.values()))

    if not manifest['operations']:
        return {**receipt, 'status': 'inspected', 'source_action': action.name,
                'slot_identifier': slot.identifier, 'inspection': readback()}

    _editable_object(obj)
    allowed = {}
    for operation in manifest['operations']:
        row = rows[operation['target']]
        frames = allowed.setdefault(row['address'], set())
        operation_frames = [operation['frame']]
        if operation['op'] == 'move_key':
            operation_frames.append(operation['to_frame'])
        if clip and any(not clip.action_frame_start <= frame <= clip.action_frame_end
                        for frame in operation_frames):
            native.fail('INVALID_REQUEST', 'Base Slot edit is outside the clip Action frame range',
                        {'target': operation['target'], 'frames': operation_frames})
        frames.update(operation_frames)
        curve = row['curve']
        if curve and (curve.lock or curve.mute or len(curve.modifiers) or len(curve.sampled_points)):
            native.fail('CONFLICT', 'Edited channels must be unlocked, unmuted keyframes without modifiers or samples')

    require_undriven(obj, allowed)
    source_state = {name: native.bag_state(row[1]) for name, row in bags.items()}
    locks = []
    for lock in manifest['locks']:
        row = rows[lock['target']]
        curve = row['curve']
        key = native.find_key(curve, lock['frame']) if curve else None
        locks.append((lock, native.curve_value(curve, lock['frame'], row['base_value']), key is not None))

    binding = native.binding_state(obj) if clip is None else None
    candidate = action.copy()
    try:
        candidate.use_fake_user = False
        candidate_bags = _slot_bags(candidate)
        candidate_slot, candidate_bag = candidate_bags[slot_identifier]
        for row in rows.values():
            row.update(candidate_bag=candidate_bag, action=candidate, slot=candidate_slot, bag=candidate_bag)
        for operation in manifest['operations']:
            core._apply_operation(rows[operation['target']], operation)
        for row in rows.values():
            path, index = row['address']
            row['curve'] = candidate_bag.fcurves.find(path, index=index)

        for lock, original_value, had_key in locks:
            row = rows[lock['target']]
            curve = row['curve']
            value = native.curve_value(curve, lock['frame'], row['base_value'])
            key = native.find_key(curve, lock['frame']) if curve else None
            if abs(value - original_value) > lock['tolerance'] or (had_key and key is None):
                native.fail('CONFLICT', 'Locked animation sample changed', {'target': lock['target']})

        if set(candidate_bags) != set(source_state):
            native.fail('VALIDATION_FAILED', 'Slot inventory changed on the candidate')
        for name, (_, other_bag) in candidate_bags.items():
            candidate_state = native.bag_state(other_bag)
            if name == slot_identifier:
                core._protected_curves(source_state[name], candidate_state, allowed)
            elif candidate_state != source_state[name]:
                native.fail('CONFLICT', 'A neighboring Action Slot changed', {'slot': name})
        if source_state != {name: native.bag_state(row[1]) for name, row in bags.items()}:
            native.fail('CONFLICT', 'Source Action curves changed during the edit')

        receipt.update(status='applied', source_action=action.name, candidate_action=candidate.name,
                       slot_identifier=candidate_slot.identifier, owner=obj.name, inspection=readback())
        if clip is None:
            anim.action = candidate
            anim.action_slot = candidate_slot
            if anim.action != candidate or anim.action_slot != candidate_slot:
                native.fail('VALIDATION_FAILED', 'Target did not bind the candidate Slot')
        else:
            production_layers.validate_base_slot_edit(anim, state, bag, candidate_bag, allowed)
            production_layers._base_range(candidate, candidate_slot)
            production_layers.rebind_base_slot(obj, state, clip, candidate, candidate_slot)
        return receipt
    except Exception:
        if binding is not None and anim.action == candidate:
            native.restore_binding(obj, binding)
        if clip is None or clip.action != candidate:
            bpy.data.actions.remove(candidate)
        raise
