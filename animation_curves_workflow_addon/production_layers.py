"""Independent NLA correction layers for native transform animation."""

import json
import math

import bpy

from . import core, native
from .contract import _number
from .production_slot import _editable_object, _slot_bags


_STATE = '_acw_production_layers_v1'
_TRANSFORMS = {'location', 'rotation_euler', 'rotation_quaternion', 'rotation_axis_angle', 'scale'}
_CLIP_TIMING = ('action_frame_start', 'action_frame_end', 'scale', 'repeat', 'frame_start', 'frame_end')


def _state(obj):
    raw = obj.get(_STATE)
    if raw is None:
        return {'base': None, 'references': [], 'layers': {}}
    try:
        state = json.loads(raw)
    except (TypeError, ValueError) as error:
        native.fail('CONFLICT', 'Invalid correction layer metadata', {'error': str(error)})
    if (
        not isinstance(state, dict)
        or not {'base', 'references', 'layers'} <= state.keys()
        or (state['base'] is not None and not isinstance(state['base'], str))
        or not isinstance(state['references'], list)
        or any(not isinstance(name, str) for name in state['references'])
        or not isinstance(state['layers'], dict)
    ):
        native.fail('CONFLICT', 'Invalid correction layer inventory')
    return state


def _track(anim, name):
    track = anim.nla_tracks.get(name)
    if track is None or len(track.strips) != 1 or not track.strips[0].action:
        native.fail('CONFLICT', 'An owned NLA track or action clip changed', {'track': name})
    return track, track.strips[0]


def _clip_binding(clip):
    return {'action': clip.action.name, 'slot': clip.action_slot.identifier}


def _base_clip(anim, state):
    track, clip = _track(anim, state['base'])
    if clip.action_slot is None or (
        'base_binding' in state and state['base_binding'] != _clip_binding(clip)
    ):
        native.fail('CONFLICT', 'Managed base Action or Slot binding changed', {'track': track.name})
    return track, clip


def _clip_timing(clip):
    return {key: getattr(clip, key) for key in _CLIP_TIMING}


def resolve_base_slot(obj, track_name):
    state = _state(obj)
    if track_name != state['base'] or not state['layers']:
        native.fail('NOT_FOUND', 'Named track is outside the managed NLA base', {'track': track_name})
    _inventory(obj, state)
    anim = obj.animation_data
    track, clip = _base_clip(anim, state)
    if anim.nla_tracks[0] != track:
        native.fail('CONFLICT', 'Managed base track moved above a correction or reference',
                    {'track': track_name})
    if (
        track.lock or clip.type != 'CLIP' or clip.use_animated_time or clip.use_reverse
        or clip.use_animated_influence
        or clip.use_auto_blend or clip.blend_in or clip.blend_out
        or clip.fcurves or clip.modifiers
    ):
        native.fail('CONFLICT', 'Managed base needs an unlocked clip with static time and full influence',
                    {'track': track_name})
    return state, clip


def rebind_base_slot(obj, state, clip, action, slot):
    """Commit one owned strip binding and roll back native timing on failure."""
    old_action, old_slot = clip.action, clip.action_slot
    timing = _clip_timing(clip)
    old_metadata = obj[_STATE]
    last_identifier = clip.last_slot_identifier
    sync_length = clip.use_sync_length
    try:
        clip.action = action
        clip.action_slot = slot
        if (clip.action != action or clip.action_slot != slot or timing != _clip_timing(clip)
                or clip.use_sync_length != sync_length):
            native.fail('VALIDATION_FAILED', 'Blender changed the managed base Slot binding or clip timing')
        state['base_binding'] = _clip_binding(clip)
        obj[_STATE] = json.dumps(state, sort_keys=True)
    except Exception:
        clip.action = old_action
        clip.action_slot = old_slot
        if clip.use_sync_length != sync_length:
            clip.use_sync_length = sync_length
        if _clip_timing(clip) != timing:
            for key in _CLIP_TIMING[:-2]:
                if getattr(clip, key) != timing[key]:
                    setattr(clip, key, timing[key])
            clip.frame_start_raw = timing['frame_start']
            clip.frame_end_raw = timing['frame_end']
        if clip.last_slot_identifier != last_identifier:
            clip.last_slot_identifier = last_identifier
        obj[_STATE] = old_metadata
        raise


def _animated_addresses(bag):
    return {(curve.data_path, curve.array_index) for curve in bag.fcurves
            if not curve.mute and not curve.is_empty}


def validate_base_slot_edit(anim, state, before, after, allowed):
    """Protect the fallback channels expected by the existing correction stack."""
    lost = _animated_addresses(before) - _animated_addresses(after)
    for name in [*state['references'], *state['layers']]:
        _, clip = _track(anim, name)
        if clip.action_slot is None or (
            name in state['layers'] and clip.action.name != state['layers'][name]
        ):
            native.fail('CONFLICT', 'A neighboring correction Slot binding changed', {'track': name})
        _, bag = _slot_bags(clip.action)[clip.action_slot.identifier]
        if bag is None:
            native.fail('CONFLICT', 'A neighboring correction Slot has no channelbag', {'track': name})
        addresses = _animated_addresses(bag)
        if name in state['references'] and allowed.keys() & addresses:
            native.fail('CONFLICT', 'An existing correction reference controls the requested channel',
                        {'track': name})
        if name in state['layers'] and lost & addresses:
            native.fail('CONFLICT', 'Cannot remove the last base key of a corrected channel',
                        {'track': name})


def _channel_vector(obj, path, index):
    if not isinstance(path, str) or type(index) is not int:
        native.fail('INVALID_REQUEST', 'Channel requires a transform path and integer array index')
    prefix, separator, prop = path.rpartition('.')
    receiver = obj
    if separator:
        try:
            receiver = obj.path_resolve(prefix)
        except (ValueError, TypeError, AttributeError):
            native.fail('NOT_FOUND', 'Pose transform path does not resolve', {'path': path})
        if not isinstance(receiver, bpy.types.PoseBone):
            native.fail('UNSUPPORTED', 'Correction channels require an object or pose bone transform')
    if prop not in _TRANSFORMS or receiver.path_from_id(prop) != path:
        native.fail('UNSUPPORTED', 'Expected a canonical native transform path', {'path': path})
    active_rotation = {'QUATERNION': 'rotation_quaternion', 'AXIS_ANGLE': 'rotation_axis_angle'}
    if prop.startswith('rotation_') and prop != active_rotation.get(receiver.rotation_mode, 'rotation_euler'):
        native.fail('CONFLICT', 'The channel does not match the current rotation mode', {'path': path})
    vector = getattr(receiver, prop)
    if not 0 <= index < len(vector):
        native.fail('INVALID_REQUEST', 'Transform array index out of range', {'path': path, 'index': index})
    return vector


def _new_action(obj, name):
    action = bpy.data.actions.new(name)
    slot = action.slots.new('OBJECT', obj.name)
    strip = action.layers.new('Animation').strips.new(type='KEYFRAME')
    return action, slot, strip.channelbag(slot, ensure=True)


def _new_clip(anim, name, action, slot, start, end, extrapolation, *, before=None, source_range=None):
    tracks = anim.nla_tracks
    previous = tracks[-1] if tracks else None
    if before is not None:
        position = tracks.find(before)
        if position < 1:
            native.fail('CONFLICT', 'Cannot place the reference below the named layer')
        previous = tracks[position - 1]
    track = tracks.new(prev=previous)
    try:
        track.name = name
        clip = track.strips.new(track.name, math.floor(start), action)
        clip.action_slot = slot
        clip.blend_type = 'REPLACE'
        clip.extrapolation = extrapolation
        if source_range is not None:
            clip.action_frame_start, clip.action_frame_end = source_range
        clip.frame_start = start
        clip.frame_end = end
        if source_range is not None and (
            (clip.action_frame_start, clip.action_frame_end) != source_range
            or (clip.frame_start, clip.frame_end) != (start, end)
            or clip.scale != 1 or clip.repeat != 1
        ):
            native.fail('CONFLICT', 'Blender did not retain the base Action Slot time mapping')
        return track, clip
    except Exception:
        tracks.remove(track)
        raise


def _weight(clip, influence):
    clip.use_animated_influence = True
    curve = next(curve for curve in clip.fcurves if curve.data_path == 'influence')
    if len(curve.keyframe_points) != 1:
        native.fail('CONFLICT', 'Correction influence was hand-animated')
    curve.keyframe_points[0].co.y = influence
    curve.update()
    clip.influence = influence


def _edit_rows(obj, channels, start, end):
    if not isinstance(channels, list) or not channels:
        native.fail('INVALID_REQUEST', 'Provide a nonempty list of correction channels')
    rows = {}
    for row in channels:
        if not isinstance(row, dict) or set(row) != {'path', 'index', 'keys'}:
            native.fail('INVALID_REQUEST', 'Each correction channel requires path, index and keys')
        path, index = row['path'], row['index']
        value = native.finite(_channel_vector(obj, path, index)[index])
        address = (path, index)
        if address in rows:
            native.fail('INVALID_REQUEST', 'Duplicate correction channel', {'channel': address})
        if not isinstance(row['keys'], list) or not row['keys']:
            native.fail('INVALID_REQUEST', 'Each correction channel needs at least one key')
        keys = {}
        for key in row['keys']:
            if not isinstance(key, dict) or set(key) != {'frame', 'value'}:
                native.fail('INVALID_REQUEST', 'Each correction key needs frame and value')
            frame = _number(key['frame'], 'key.frame')
            if not start <= frame <= end or frame in keys:
                native.fail('INVALID_REQUEST', 'Duplicate correction key or key outside layer frame range')
            keys[frame] = _number(key['value'], 'key.value')
        rows[address] = (value, keys)
    rotation_paths = {path for path, _ in rows if path.rsplit('.', 1)[-1]
                      in ('rotation_quaternion', 'rotation_axis_angle')}
    for path in rotation_paths:
        frame_sets = [set(rows.get((path, index), (None, {}))[1]) for index in range(4)]
        if not frame_sets[0] or any(frames != frame_sets[0] for frames in frame_sets[1:]):
            native.fail('INVALID_REQUEST', 'Quaternion and axis-angle corrections require all four values at each frame',
                        {'path': path})
    return rows


def _inventory(obj, state):
    anim = obj.animation_data
    if not anim:
        if obj.get(_STATE) is not None:
            native.fail('CONFLICT', 'Correction track inventory was removed')
        return set()
    if anim.use_tweak_mode or anim.drivers or not anim.use_nla:
        native.fail('CONFLICT', 'Correction layers need enabled NLA outside tweak mode without drivers')
    names = [name for name in [state['base'], *state['references'], *state['layers']] if name is not None]
    if set(names) != set(anim.nla_tracks.keys()) or any(track.is_solo for track in anim.nla_tracks):
        native.fail('CONFLICT', 'Unmanaged or solo NLA tracks need an explicit composition plan')
    if state['layers'] and anim.action:
        native.fail('CONFLICT', 'Active Action was assigned above managed correction layers')
    if anim.action:
        if anim.action_blend_type != 'REPLACE' or anim.action_extrapolation != 'HOLD' or anim.action_influence != 1:
            native.fail('UNSUPPORTED', 'Active base Action requires full REPLACE with HOLD extrapolation')
        native.local(anim.action)
        if anim.action.use_frame_range or anim.action_slot is None:
            native.fail('UNSUPPORTED', 'Base Action requires a native active Slot and its natural frame range')
    covered = set()
    clips = [anim.action] if anim.action else []
    slots = [anim.action_slot] if anim.action else []
    for name in [state['base'], *state['references']]:
        if name:
            track, clip = _base_clip(anim, state) if name == state['base'] else _track(anim, name)
            if (track.mute or clip.mute or clip.action_slot is None or clip.blend_type != 'REPLACE'
                    or clip.extrapolation != 'HOLD'):
                native.fail('CONFLICT', 'Correction base or reference track is no longer active', {'track': name})
            clips.append(clip.action)
            slots.append(clip.action_slot)
    for action, slot in zip(clips, slots):
        _, bag = _slot_bags(action)[slot.identifier]
        if bag is None:
            native.fail('CONFLICT', 'Base Action Slot is missing its channelbag')
        covered.update(_animated_addresses(bag))
    return covered


def _base_range(action, slot):
    _, bag = _slot_bags(action)[slot.identifier]
    if bag is None:
        native.fail('CONFLICT', 'Active base Slot is missing its channelbag')
    curves = [curve for curve in bag.fcurves if not curve.mute and not curve.is_empty]
    frames = []
    vector_axes = {}
    for curve in curves:
        if curve.extrapolation != 'CONSTANT' or curve.modifiers or curve.sampled_points:
            native.fail('UNSUPPORTED', 'Base Slot needs constant extrapolation and unmodified keyframes')
        if curve.data_path.rsplit('.', 1)[-1] in ('rotation_quaternion', 'rotation_axis_angle'):
            vector_axes.setdefault(curve.data_path, set()).add(curve.array_index)
        frames.extend(float(key.co.x) for key in curve.keyframe_points)
    if any(axes != {0, 1, 2, 3} for axes in vector_axes.values()):
        native.fail('UNSUPPORTED', 'A partial quaternion or axis-angle base can change under NLA blending')
    if not frames:
        native.fail('UNSUPPORTED', 'The active base Slot has no source key range')
    left, right = min(frames), max(frames)
    return left, right if right > left else left + 1


def _write(bag, rows):
    for (path, index), (_, keys) in rows.items():
        row = {'candidate_bag': bag, 'spec': {'id': path, 'data_path': path, 'array_index': index}}
        curve = bag.fcurves.find(path, index=index)
        if curve and (curve.lock or curve.mute or curve.modifiers or curve.sampled_points):
            native.fail('CONFLICT', 'Correction channel has hand-edited curve controls', {'path': path})
        for frame, value in sorted(keys.items()):
            core._apply_operation(row, {'op': 'set_key', 'frame': frame, 'value': value,
                                        'interpolation': 'LINEAR'})


def apply(obj, *, start, end, channels, layer_name=None, _verify=None):
    """Create a new correction layer or apply keys to one explicitly named layer."""
    _editable_object(obj)
    start, end = _number(start, 'start'), _number(end, 'end')
    if start >= end or (layer_name is not None and (not isinstance(layer_name, str) or not layer_name)):
        native.fail('INVALID_REQUEST', 'Expected an increasing frame range and optional layer name')
    state = _state(obj)
    covered = _inventory(obj, state)
    rows = _edit_rows(obj, channels, start, end)
    if layer_name is not None and layer_name not in state['layers']:
        native.fail('NOT_FOUND', 'Named correction layer does not exist', {'layer': layer_name})

    binding = native.binding_state(obj)
    base_time = _base_range(binding['action'], binding['slot']) if binding['action'] else None
    original_metadata = obj.get(_STATE)
    anim = obj.animation_data_create()
    tracks, actions = [], []
    prior_clip = None
    try:
        if anim.action:
            base, base_slot = anim.action, anim.action_slot
            left, right = base_time
            track, base_clip = _new_clip(anim, 'Animation Base', base, base_slot, left, right, 'HOLD',
                                        source_range=base_time)
            tracks.append(track)
            state['base'] = track.name
            state['base_binding'] = _clip_binding(base_clip)
            anim.action = None

        missing = {address: (value, {start: value, end: value})
                   for address, (value, keys) in rows.items() if address not in covered}
        if missing:
            action, slot, bag = _new_action(obj, 'Correction Reference')
            actions.append(action)
            _write(bag, missing)
            track, _ = _new_clip(anim, 'Correction Reference', action, slot, start, end, 'HOLD',
                                 before=layer_name)
            tracks.append(track)
            state['references'].append(track.name)

        if layer_name is None:
            action, slot, bag = _new_action(obj, 'Performance Correction')
            actions.append(action)
            for value, keys in rows.values():
                first, last = keys[min(keys)], keys[max(keys)]
                keys.setdefault(start, first)
                keys.setdefault(end, last)
            _write(bag, rows)
            track, clip = _new_clip(anim, 'Performance Correction', action, slot, start, end, 'NOTHING')
            tracks.append(track)
            _weight(clip, 1)
            layer_name = track.name
        else:
            _, clip = _track(anim, layer_name)
            if clip.action.name != state['layers'][layer_name] or (clip.frame_start, clip.frame_end) != (start, end):
                native.fail('CONFLICT', 'Named correction clip binding or frame range changed')
            prior_clip = (clip, clip.action, clip.action_slot)
            action = clip.action.copy()
            actions.append(action)
            slot, bag = _slot_bags(action)[clip.action_slot.identifier]
            before = native.bag_state(bag)
            _write(bag, rows)
            core._protected_curves(before, native.bag_state(bag),
                                   {address: set(keys) for address, (_, keys) in rows.items()})
            clip.action = action
            clip.action_slot = slot
        state['layers'][layer_name] = clip.action.name
        obj[_STATE] = json.dumps(state, sort_keys=True)
        if _verify is not None:
            _verify()
        return {'layer': layer_name, 'action': clip.action.name, 'start': start, 'end': end,
                'channels': [{'path': path, 'index': index} for path, index in rows]}
    except Exception:
        if prior_clip:
            clip, action, slot = prior_clip
            clip.action = action
            clip.action_slot = slot
        for track in reversed(tracks):
            anim.nla_tracks.remove(track)
        native.restore_binding(obj, binding)
        for action in actions:
            bpy.data.actions.remove(action)
        if original_metadata is None:
            if _STATE in obj:
                del obj[_STATE]
        else:
            obj[_STATE] = original_metadata
        for (path, index), (value, _) in rows.items():
            if (path, index) not in covered:
                _channel_vector(obj, path, index)[index] = value
        raise


def configure(obj, layer_name, *, mute=None, influence=None):
    _editable_object(obj)
    state = _state(obj)
    _inventory(obj, state)
    if layer_name not in state['layers']:
        native.fail('NOT_FOUND', 'Named correction layer does not exist', {'layer': layer_name})
    track, clip = _track(obj.animation_data, layer_name)
    if clip.action.name != state['layers'][layer_name]:
        native.fail('CONFLICT', 'The named correction clip binding changed')
    if mute is not None and type(mute) is not bool:
        native.fail('INVALID_REQUEST', 'mute must be a boolean')
    if influence is not None:
        influence = _number(influence, 'influence')
        if not 0 <= influence <= 1:
            native.fail('INVALID_REQUEST', 'influence must be between zero and one')
        _weight(clip, influence)
    if mute is not None:
        track.mute = mute
    return {'layer': layer_name, 'mute': track.mute, 'influence': clip.influence}
