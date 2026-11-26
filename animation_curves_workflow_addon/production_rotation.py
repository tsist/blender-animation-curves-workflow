"""Isolated Blender 5.3 rotation-mode conversion with sampled pose verification."""

import math

import bpy
from mathutils import Quaternion, Vector

from . import native
from .contract import _number
from .production_motion import _find_bone, _residual, _rotation_values, _world
from .production_slot import _editable_object, _slot_bags


_MODES = {'QUATERNION', 'AXIS_ANGLE', 'XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'}


def _property(mode):
    return {'QUATERNION': 'rotation_quaternion', 'AXIS_ANGLE': 'rotation_axis_angle'}.get(mode, 'rotation_euler')


def _quaternion(receiver, mode):
    if mode == 'QUATERNION':
        return receiver.rotation_quaternion.normalized()
    if mode == 'AXIS_ANGLE':
        angle, *values = receiver.rotation_axis_angle
        axis = Vector(values)
        return Quaternion(axis, angle) if axis.length_squared else Quaternion()
    return receiver.rotation_euler.to_quaternion()


def _refine(receiver, bag, mode, rotations):
    path = receiver.path_from_id(_property(mode))
    for curve in tuple(bag.fcurves):
        if curve.data_path == path:
            bag.fcurves.remove(curve)
    parts = []
    prior = None
    for frame, rotation in sorted(rotations.items()):
        _, values, prior = _rotation_values(rotation, mode, prior)
        if not parts:
            parts = [bag.fcurves.new(path, index=index) for index in range(len(values))]
        for curve, value in zip(parts, values):
            key = curve.keyframe_points.insert(frame, value, options={'FAST'})
            key.interpolation = 'LINEAR'
            key.handle_left_type = key.handle_right_type = 'FREE'
    for curve in parts:
        curve.update()


def convert(context, obj, *, mode, bone_name=None, tolerance=2e-3, substeps=8):
    """Convert one active native rotation representation, preserving other curve data."""
    if not isinstance(mode, str) or mode not in _MODES or type(substeps) is not int or not 1 <= substeps <= 8:
        native.fail('INVALID_REQUEST', 'Expected a Blender rotation mode and 1..8 verification substeps')
    tolerance = _number(tolerance, 'tolerance')
    if tolerance <= 0:
        native.fail('INVALID_REQUEST', 'Conversion tolerance must be positive')
    _editable_object(obj)
    if context.view_layer.objects.get(obj.name) != obj:
        native.fail('CONFLICT', 'Rotation target must be in the requested view layer')
    if bone_name is not None:
        native.local(obj.data)
    bone = _find_bone(obj, bone_name)
    receiver = bone if bone is not None else obj
    old_mode = receiver.rotation_mode
    if mode == old_mode:
        return {'status': 'unchanged', 'mode': mode}
    animation = obj.animation_data
    if not animation or not animation.action or not animation.action_slot:
        native.fail('NOT_FOUND', 'Rotation conversion requires an active Action Slot')
    if animation.nla_tracks or animation.drivers or animation.use_tweak_mode:
        native.fail('CONFLICT', 'Additional Action strips or drivers can depend on this rotation mode')
    if receiver.constraints:
        native.fail('UNSUPPORTED', 'Constrained rotation conversion needs an independent basis check')
    action = animation.action
    native.local(action)
    if (set(action.keys()) | set(obj.keys())) & native._MANAGED:
        native.fail('UNSUPPORTED', 'Managed target requires its domain adapter')
    slot_identifier = animation.action_slot.identifier
    bags = _slot_bags(action)
    slot, bag = bags[slot_identifier]
    if bag is None:
        native.fail('NOT_FOUND', 'Rotation target Slot has no channelbag')
    original_path = receiver.path_from_id(_property(old_mode))
    result_path = receiver.path_from_id(_property(mode))
    if result_path != original_path and any(curve.data_path == result_path for curve in bag.fcurves):
        native.fail('CONFLICT', 'Destination rotation channels already contain authoring')
    curves = [curve for curve in bag.fcurves if curve.data_path == original_path]
    if not curves or any(curve.lock or curve.mute or curve.modifiers or curve.sampled_points
                         or curve.extrapolation != 'CONSTANT' for curve in curves):
        native.fail('UNSUPPORTED', 'Conversion requires unlocked active rotation curves with constant extrapolation')

    source_state = {name: native.bag_state(row[1]) for name, row in bags.items()}
    start, end = map(float, action.frame_range)
    keys = {float(key.co.x) for curve in curves for key in curve.keyframe_points}
    fit_frames = sorted(keys | {
        frame / substeps for frame in range(math.floor(start * substeps), math.ceil(end * substeps) + 1)
    })
    frames = sorted(set(fit_frames) | {fit_frames[0] - 1, fit_frames[-1] + 1} |
                    {(left + right) / 2 for left, right in zip(fit_frames, fit_frames[1:])})
    scene = context.scene
    previous_frame = scene.frame_current, scene.frame_subframe
    original_rotation = {prop: tuple(getattr(receiver, prop)) for prop in map(_property, (old_mode, mode))}

    rotations = {}

    def sample(capture_rotation=False):
        result = {}
        for frame in frames:
            integer = math.floor(frame)
            scene.frame_set(integer, subframe=frame - integer)
            depsgraph = context.evaluated_depsgraph_get()
            result[frame] = _world(obj, bone_name, depsgraph)
            if capture_rotation:
                evaluated = obj.evaluated_get(depsgraph)
                source = evaluated.pose.bones[bone_name] if bone_name is not None else evaluated
                rotations[frame] = _quaternion(source, old_mode)
        scene.frame_set(previous_frame[0], subframe=previous_frame[1])
        return result

    binding = native.binding_state(obj)
    candidate = None
    try:
        original = sample(capture_rotation=True)
        candidate = action.copy()
        candidate.use_fake_user = False
        animation.action = candidate
        animation.action_slot = _slot_bags(candidate)[slot_identifier][0]
        receiver.convert_rotation_mode(mode, bake=True)
        after = sample()
        maximum = max(_residual(original[frame], after[frame]) for frame in frames)
        refined = maximum > tolerance
        if refined:
            _, target_bag = _slot_bags(candidate)[slot_identifier]
            _refine(receiver, target_bag, mode, {frame: rotations[frame] for frame in fit_frames})
            after = sample()
            maximum = max(_residual(original[frame], after[frame]) for frame in frames)
        if maximum > tolerance:
            native.fail('VALIDATION_FAILED', f'Baked conversion pose error {maximum:.6g} exceeds {tolerance:.6g}',
                        {'maximum_error': maximum, 'tolerance': tolerance})
        candidate_bags = _slot_bags(candidate)
        if set(candidate_bags) != set(source_state) or animation.action_slot.identifier != slot_identifier:
            native.fail('VALIDATION_FAILED', 'Rotation conversion changed Slot inventory or binding')
        for name, (_, new_bag) in candidate_bags.items():
            before_state, after_state = source_state[name], native.bag_state(new_bag)
            if name == slot_identifier:
                paths = {original_path, result_path}
                before_state = [curve for curve in before_state if curve['data_path'] not in paths]
                after_state = [curve for curve in after_state if curve['data_path'] not in paths]
            if before_state != after_state:
                native.fail('CONFLICT', 'An unrelated curve or Slot changed during rotation conversion',
                            {'slot': name})
        if source_state != {name: native.bag_state(row[1]) for name, row in bags.items()}:
            native.fail('CONFLICT', 'Original shared Action changed during rotation conversion')
        return {'status': 'applied', 'owner': obj.name, 'bone': bone_name, 'from': old_mode, 'to': mode,
                'action': candidate.name, 'slot_identifier': slot_identifier, 'samples': len(frames),
                'max_error': maximum, 'subframe_refinement': refined}
    except Exception:
        native.restore_binding(obj, binding)
        receiver.rotation_mode = old_mode
        for prop, value in original_rotation.items():
            setattr(receiver, prop, value)
        if candidate is not None:
            bpy.data.actions.remove(candidate)
        raise
    finally:
        scene.frame_set(previous_frame[0], subframe=previous_frame[1])
