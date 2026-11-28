"""Native pose editing, motion path and Pose Library operations."""

from contextlib import contextmanager
import math

import bpy

from . import native
from .production_drivers import require_undriven
from .production_slot import _editable_object, _slot_bags


def _bones(owner, names):
    _editable_object(owner)
    if owner.type != 'ARMATURE':
        native.fail('INVALID_REQUEST', 'The target must be an armature')
    if (not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names)
            or len(set(names)) != len(names)):
        native.fail('INVALID_REQUEST', 'Specify a nonempty list of distinct bone names')
    missing = [name for name in names if owner.pose.bones.get(name) is None]
    if missing:
        native.fail('NOT_FOUND', 'Pose bones are missing', {'bones': missing})
    return [owner.pose.bones[name] for name in names]


def _select_bones(owner, bones):
    names = {bone.name for bone in bones}
    for bone in owner.pose.bones:
        bone.select = bone.name in names
    owner.data.bones.active = bones[0].bone


@contextmanager
def _pose_context(context, owner, bones, frame=None, extra_bones=()):
    scene, layer = context.scene, context.view_layer
    active, selected = layer.objects.active, tuple(context.selected_objects)
    if context.mode != 'OBJECT' and (context.mode != 'POSE' or active != owner
                                     or context.objects_in_mode != (owner,)):
        native.fail('CONFLICT', 'Close other edit modes before invoking native pose tools')
    old_active_bone = owner.data.bones.active
    selected_bones = [bone for bone in owner.pose.bones if bone.select]
    visible_bones = set(bones) | set(extra_bones)
    old_visibility = {bone: (bone.hide, bone.bone.hide, bone.bone.hide_select) for bone in visible_bones}
    solo = [collection for collection in owner.data.collections_all if collection.is_solo]
    collections = {}
    for bone in visible_bones:
        for leaf in bone.bone.collections:
            collection = leaf
            while collection is not None:
                collections[collection] = collection.is_visible
                collection = collection.parent
    original_frame = scene.frame_current, scene.frame_subframe
    auto_keying = scene.tool_settings.use_keyframe_insert_auto
    entered = context.mode == 'OBJECT'
    try:
        for obj in selected:
            obj.select_set(False, view_layer=layer)
        owner.select_set(True, view_layer=layer)
        layer.objects.active = owner
        if entered:
            bpy.ops.object.mode_set(mode='POSE')
        scene.tool_settings.use_keyframe_insert_auto = False
        for collection in solo:
            collection.is_solo = False
        for collection in collections:
            collection.is_visible = True
        for bone in visible_bones:
            bone.hide = bone.bone.hide = bone.bone.hide_select = False
        _select_bones(owner, bones)
        if set(context.selected_pose_bones_from_active_object or []) != set(bones):
            native.fail('CONFLICT', 'Pose selection differs from the requested bones')
        if frame is not None:
            if type(frame) not in (int, float) or not math.isfinite(frame):
                native.fail('INVALID_REQUEST', 'Pose frame must be a finite number')
            integer = math.floor(frame)
            scene.frame_set(integer, subframe=frame - integer)
        yield
    finally:
        for bone, (hide_pose, hide_bone, hide_select) in old_visibility.items():
            bone.hide, bone.bone.hide, bone.bone.hide_select = hide_pose, hide_bone, hide_select
        for collection, visible in collections.items():
            collection.is_visible = visible
        for collection in solo:
            collection.is_solo = True
        selected_names = {bone.name for bone in selected_bones}
        for bone in owner.pose.bones:
            bone.select = bone.name in selected_names
        owner.data.bones.active = old_active_bone
        scene.tool_settings.use_keyframe_insert_auto = auto_keying
        if entered and owner.mode == 'POSE':
            bpy.ops.object.mode_set(mode='OBJECT')
        owner.select_set(False, view_layer=layer)
        for obj in selected:
            obj.select_set(True, view_layer=layer)
        layer.objects.active = active
        scene.frame_set(original_frame[0], subframe=original_frame[1])


def _run(operator, **params):
    if not operator.poll() or 'FINISHED' not in operator('EXEC_DEFAULT', **params):
        native.fail('VALIDATION_FAILED', 'Blender rejected the pose operation', {'tool': operator.idname()})


_BBONE_PROPERTIES = (
    'bbone_curveinx', 'bbone_curveoutx', 'bbone_curveinz', 'bbone_curveoutz',
    'bbone_rollin', 'bbone_rollout', 'bbone_scalein', 'bbone_scaleout', 'bbone_easein', 'bbone_easeout',
)


def _numbers(value):
    if type(value) in (bool, int, float):
        return (float(value),)
    if hasattr(value, '__len__') and not isinstance(value, (str, bytes, dict)):
        values = tuple(value)
        if all(type(item) in (bool, int, float) for item in values):
            return values
    return ()


def _pose_values(bones):
    result = {}
    for bone in bones:
        rotation = {'QUATERNION': 'rotation_quaternion', 'AXIS_ANGLE': 'rotation_axis_angle'
                    }.get(bone.rotation_mode, 'rotation_euler')
        for prop in ('location', rotation, 'scale', *_BBONE_PROPERTIES):
            for index, number in enumerate(_numbers(getattr(bone, prop))):
                result[(bone.path_from_id(prop), index)] = number
        for prop, value in bone.items():
            path = f'{bone.path_from_id()}["{bpy.utils.escape_identifier(prop)}"]'
            for index, number in enumerate(_numbers(value)):
                result[(path, index)] = number
    return result


def _edit(context, owner, bones, frame, edit, *, extra_bones=()):
    animation = owner.animation_data
    if animation and (animation.use_tweak_mode or animation.nla_tracks):
        native.fail('CONFLICT', 'Pose editing requires an active Action outside NLA')
    source = animation.action if animation else None
    source_slot = animation.action_slot if animation else None
    if source is not None:
        native.local(source)
        bags = _slot_bags(source)
        if source_slot is None or bags[source_slot.identifier][1] is None:
            native.fail('NOT_FOUND', 'Pose editing needs a native active Action Slot')
    candidate = None
    binding = native.binding_state(owner)
    try:
        with _pose_context(context, owner, bones, frame, extra_bones):
            before = _pose_values(bones)
            edit()
            after = _pose_values(bones)
            changes = {address for address in before if abs(after[address] - before[address]) > 1e-7}
            if not changes:
                return {'status': 'unchanged', 'channels': 0}
            complete_rotations = {path for path, _ in changes
                                  if path.endswith(('.rotation_quaternion', '.rotation_axis_angle'))}
            changes.update(address for address in before if address[0] in complete_rotations)
            require_undriven(owner, changes)
            # Create and bind the candidate before edits so a shared Action
            # never receives the native keyframe insertion.
            if source is not None:
                candidate = source.copy()
                candidate.use_fake_user = False
                slot, bag = _slot_bags(candidate)[source_slot.identifier]
            else:
                from .production_layers import _new_action
                candidate, slot, bag = _new_action(owner, 'Pose Edit')
            for path, index in sorted(changes):
                curve = bag.fcurves.find(path, index=index)
                if curve is None:
                    curve = bag.fcurves.new(path, index=index)
                elif curve.lock or curve.mute or curve.modifiers or curve.sampled_points:
                    native.fail('CONFLICT', 'Pose edit targets a locked, muted or modified curve', {'path': path})
                curve.keyframe_points.insert(context.scene.frame_current_final, after[path, index])
                curve.update_autoflags(owner)
                curve.update()
            animation = owner.animation_data_create()
            animation.action = candidate
            animation.action_slot = slot
        if source is not None:
            source.use_fake_user = True
        return {'status': 'applied', 'action': candidate.name, 'source_action': source.name if source else None,
                'slot_identifier': slot.identifier, 'channels': len(changes)}
    except Exception:
        native.restore_binding(owner, binding)
        if candidate is not None:
            bpy.data.actions.remove(candidate)
        raise


def apply(context, owner, *, tool, bones, frame=None, options=None):
    bones = _bones(owner, bones)
    options = {} if options is None else options
    if not isinstance(options, dict):
        native.fail('INVALID_REQUEST', 'Pose options must be an object')
    source_bones = ()
    if tool == 'mirror_paste':
        if set(options) - {'source_bones', 'factor'} or 'source_bones' not in options:
            native.fail('INVALID_REQUEST', 'Mirror Paste needs explicit source_bones in options')
        source_bones = _bones(owner, options['source_bones'])

        def edit():
            _select_bones(owner, source_bones)
            _run(bpy.ops.pose.copy)
            _select_bones(owner, bones)
            _run(bpy.ops.pose.paste, flipped=True, selected_mask=True, factor=options.get('factor', 1))
    elif isinstance(tool, str) and tool in {'breakdown', 'push', 'relax'}:
        if options.keys() - {'factor', 'prev_frame', 'next_frame', 'channels', 'axis_lock'}:
            native.fail('INVALID_REQUEST', 'Unknown pose blend option')
        operator = getattr(bpy.ops.pose, tool)

        def edit():
            _run(operator, **options)
    else:
        native.fail('INVALID_REQUEST', 'Unknown native pose tool', {'tool': tool})
    return {**_edit(context, owner, bones, frame, edit, extra_bones=source_bones), 'tool': tool}


def motion_paths(context, owner, *, bones, enabled, options=None):
    bones = _bones(owner, bones)
    options = {} if options is None else options
    if (type(enabled) is not bool or not isinstance(options, dict)
            or options.keys() - {'display_type', 'range', 'bake_location'}
            or options and not enabled):
        native.fail('INVALID_REQUEST', 'Expected a motion path enabled flag and native calculation options')
    with _pose_context(context, owner, bones):
        if enabled:
            _run(bpy.ops.pose.paths_calculate, **{'range': 'SCENE', **options})
        else:
            _run(bpy.ops.pose.paths_clear, only_selected=True)
        paths = {bone.name: len(bone.motion_path.points) for bone in bones if bone.motion_path}
        if (enabled and len(paths) != len(bones)) or (not enabled and paths):
            native.fail('VALIDATION_FAILED', 'Blender motion path state differs from the request')
    return {'status': 'applied', 'enabled': enabled, 'motion_paths': paths}


def pose_asset(context, owner, *, tool, bones, asset_name, frame=None):
    bones = _bones(owner, bones)
    if not isinstance(asset_name, str) or not asset_name:
        native.fail('INVALID_REQUEST', 'A Pose Library Action name is required')
    found = bpy.data.actions.get(asset_name)
    if tool == 'capture':
        if found is not None:
            native.fail('CONFLICT', 'An Action already has the requested Pose Library name')
        from pose_library.pose_creation import create_pose_asset, PoseCreationParams
        with _pose_context(context, owner, bones, frame):
            animation = owner.animation_data
            action = create_pose_asset(PoseCreationParams(
                owner, animation.action if animation else None,
                animation.action_slot if animation else None,
                context.scene.frame_current_final,
                frozenset(bone.name for bone in bones), asset_name,
            ))
            if action is None or action.asset_data is None:
                native.fail('VALIDATION_FAILED', 'Blender could not create the Pose Library asset')
        return {'status': 'applied', 'tool': tool, 'asset': action.name}
    if tool != 'apply':
        native.fail('INVALID_REQUEST', 'Pose Library tool must be capture or apply')
    if (found is None or found.asset_data is None or found.library is not None
            or abs(found.frame_range[1] - found.frame_range[0]) > 1e-5):
        native.fail('NOT_FOUND', 'A local single-frame Action asset is required')

    def edit():
        owner.pose.apply_pose_from_action(found, evaluation_time=found.frame_range[0])

    return {**_edit(context, owner, bones, frame, edit), 'tool': tool, 'asset': found.name}
