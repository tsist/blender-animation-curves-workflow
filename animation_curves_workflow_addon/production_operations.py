"""Shared entry for Blender 5.3 production animation operations."""

from . import native, production_graph, production_layers, production_motion, production_pose, production_rotation, production_slot


def _object(context, name):
    if not isinstance(name, str) or not name:
        native.fail('INVALID_REQUEST', 'An explicit object name is required')
    obj = context.view_layer.objects.get(name)
    if obj is None:
        native.fail('NOT_FOUND', 'Object is outside the active view layer', {'object': name})
    return obj


def apply(context, request):
    if not isinstance(request, dict):
        native.fail('INVALID_REQUEST', 'Animation production request must be an object')
    fields = {
        'slot': ({'manifest', 'action_name', 'slot_identifier'}, {'base_track'}),
        'correction': ({'object', 'start', 'end', 'channels'}, {'layer_name', 'blend_type'}),
        'layer_state': ({'object', 'layer_name'}, {'mute', 'influence'}),
        'transfer': ({'source', 'target', 'start', 'end'},
                     {'offset', 'source_bone', 'target_bone', 'layer_name', 'tolerance'}),
        'rotate': ({'object', 'mode'}, {'bone_name', 'tolerance', 'substeps'}),
        'graph': ({'object', 'tool', 'channels'}, {'options'}),
        'pose': ({'object', 'tool', 'bones'}, {'frame', 'options'}),
        'motion_paths': ({'object', 'bones', 'enabled'}, {'options'}),
        'pose_asset': ({'object', 'tool', 'bones', 'asset_name'}, {'frame'}),
    }
    operation = request.get('op')
    if not isinstance(operation, str) or operation not in fields:
        native.fail('INVALID_REQUEST', 'Unknown animation production operation')
    required, optional = fields[operation]
    params = {key: value for key, value in request.items() if key != 'op'}
    if required - params.keys() or params.keys() - required - optional:
        native.fail('INVALID_REQUEST', 'Wrong fields for animation production operation',
                    {'required': sorted(required), 'optional': sorted(optional)})
    if operation == 'slot':
        return production_slot.apply(**params)
    if operation == 'transfer':
        params['source'] = _object(context, params['source'])
        params['target'] = _object(context, params['target'])
        return production_motion.transfer(context, **params)
    target = _object(context, params.pop('object'))
    if operation == 'correction':
        return production_layers.apply(target, **params)
    if operation == 'layer_state':
        return production_layers.configure(target, **params)
    if operation == 'graph':
        return production_graph.apply(context, target, **params)
    if operation == 'pose':
        return production_pose.apply(context, target, **params)
    if operation == 'motion_paths':
        return production_pose.motion_paths(context, target, **params)
    if operation == 'pose_asset':
        return production_pose.pose_asset(context, target, **params)
    return production_rotation.convert(context, target, **params)
