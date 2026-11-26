"""Transfer evaluated world motion into a native correction layer."""

from mathutils import Matrix, Vector

from . import native, production_layers
from .contract import _number


def _find_bone(obj, name):
    if name is None:
        return None
    bone = obj.pose.bones.get(name) if obj.pose else None
    if bone is None:
        native.fail('NOT_FOUND', 'Pose bone not found', {'owner': obj.name, 'bone': name})
    return bone


def _world(obj, name, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    if name is None:
        return evaluated.matrix_world.copy()
    return evaluated.matrix_world @ evaluated.pose.bones[name].matrix


def _residual(left, right):
    return max(abs(left[row][col] - right[row][col]) for row in range(4) for col in range(4))


def _basis(obj, bone, world, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    if bone is not None:
        return evaluated.convert_space(pose_bone=evaluated.pose.bones[bone.name], matrix=world,
                                       from_space='WORLD', to_space='LOCAL')
    if obj.parent is None:
        return world
    parent = obj.parent.evaluated_get(depsgraph)
    return (parent.matrix_world @ evaluated.matrix_parent_inverse).inverted() @ world


def _rotation_values(quat, mode, prior):
    if mode == 'QUATERNION':
        if prior is not None and quat.dot(prior) < 0:
            quat.negate()
        return 'rotation_quaternion', tuple(quat), quat.copy()
    if mode == 'AXIS_ANGLE':
        if prior is not None and quat.dot(prior) < 0:
            quat.negate()
        axis, angle = quat.to_axis_angle()
        return 'rotation_axis_angle', (angle, *axis), quat.copy()
    rotation = quat.to_euler(mode, prior) if prior is not None else quat.to_euler(mode)
    return 'rotation_euler', tuple(rotation), rotation


def transfer(context, source, target, *, start, end, offset=0, source_bone=None, target_bone=None,
             layer_name=None, tolerance=1e-4):
    """Read every source matrix first and put the transferred motion on the target."""
    if any(type(value) is not int for value in (start, end, offset)) or start >= end:
        native.fail('INVALID_REQUEST', 'Motion transfer requires increasing integer frames and an integer offset')
    tolerance = _number(tolerance, 'tolerance')
    if tolerance <= 0:
        native.fail('INVALID_REQUEST', 'Motion tolerance must be positive')
    if context.view_layer.objects.get(source.name) != source or context.view_layer.objects.get(target.name) != target:
        native.fail('CONFLICT', 'Source and target must be in the requested view layer')
    _find_bone(source, source_bone)
    bone = _find_bone(target, target_bone)
    receiver = bone if bone is not None else target
    if receiver.constraints or (bone is not None and bone.bone.use_connect):
        native.fail('UNSUPPORTED', 'Constrained or connected targets cannot be authored as unrestricted TRS')
    if bone is None and (
        (target.parent and target.parent_type != 'OBJECT') or target.rigid_body or
        target.delta_location.length > 0 or tuple(target.delta_scale) != (1, 1, 1) or
        Vector(target.delta_rotation_euler).length > 0 or
        tuple(target.delta_rotation_quaternion) != (1, 0, 0, 0)
    ):
        native.fail('UNSUPPORTED', 'Target has unsupported parent, rigid body or delta transforms')
    if source == target and source_bone == target_bone and offset == 0:
        native.fail('INVALID_REQUEST', 'Source and target motion addresses must differ')

    scene = context.scene
    current = scene.frame_current, scene.frame_subframe
    expected = {}
    rows = {}
    prior = None
    errors = {}
    try:
        for frame in range(start, end + 1):
            scene.frame_set(frame)
            expected[frame + offset] = _world(source, source_bone, context.evaluated_depsgraph_get())
        for frame, matrix in expected.items():
            scene.frame_set(frame)
            try:
                basis = _basis(target, bone, matrix, context.evaluated_depsgraph_get())
            except ValueError:
                native.fail('UNSUPPORTED', 'Target parent transform is singular', {'frame': frame})
            location, rotation, scale = basis.decompose()
            if _residual(basis, Matrix.LocRotScale(location, rotation, scale)) > tolerance:
                native.fail('UNSUPPORTED', 'Target local transform contains shear', {'frame': frame})
            prop, angles, prior = _rotation_values(rotation, receiver.rotation_mode, prior)
            for attribute, values in (('location', location), (prop, angles), ('scale', scale)):
                path = receiver.path_from_id(attribute)
                for index, value in enumerate(values):
                    row = rows.setdefault((path, index), {'path': path, 'index': index, 'keys': []})
                    row['keys'].append({'frame': frame, 'value': float(value)})
    finally:
        scene.frame_set(current[0], subframe=current[1])

    def verify():
        try:
            for frame, matrix in expected.items():
                scene.frame_set(frame)
                measured = _world(target, target_bone, context.evaluated_depsgraph_get())
                errors[frame] = _residual(measured, matrix)
                if errors[frame] > tolerance:
                    native.fail('VALIDATION_FAILED', 'World motion transfer exceeded tolerance',
                                {'frame': frame, 'error': errors[frame]})
        finally:
            scene.frame_set(current[0], subframe=current[1])

    receipt = production_layers.apply(target, start=start + offset, end=end + offset,
                                      channels=list(rows.values()), layer_name=layer_name,
                                      _verify=verify)
    receipt.update(source=source.name, target=target.name, source_bone=source_bone,
                   target_bone=target_bone, frame_errors=errors, max_error=max(errors.values()))
    return receipt
