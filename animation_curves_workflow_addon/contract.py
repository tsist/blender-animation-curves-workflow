"""Host-safe initial channel editing contract; no bpy import or arbitrary eval."""
import copy
import math

class WorkflowError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

def _fail(message):
    raise WorkflowError('INVALID_REQUEST', message)

def _object(value, allowed, required, label):
    if not isinstance(value, dict):
        _fail(label + ' must be an object')
    missing = set(required) - value.keys()
    unknown = value.keys() - set(allowed)
    if missing or unknown:
        _fail(label + ': missing=' + repr(sorted(missing)) + ', unknown=' + repr(sorted(unknown)))

def _text(value, label, maximum=512):
    if not isinstance(value, str) or not value or len(value) > maximum or '\x00' in value:
        _fail(label + ' must be a nonempty bounded string')
    return value

def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        _fail(label + ' must be a finite number')
    try:
        result = float(value)
    except (OverflowError, ValueError):
        _fail(label + ' exceeds the supported finite numeric range')
    if not math.isfinite(result):
        _fail(label + ' must be a finite number')
    return result

def _choice(value, choices, label):
    if not isinstance(value, str) or value not in choices:
        _fail(label + ' must be one of ' + ', '.join(sorted(choices)))
    return value

def _array(value, label, maximum, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        _fail(label + ' requires ' + str(minimum) + '..' + str(maximum) + ' items')
    return value

OWNER_TYPES = {'OBJECT', 'LIGHT', 'CAMERA', 'MATERIAL', 'MATERIAL_NODETREE'}
MAX_PUBLIC_LOCKS = 512

def normalize_manifest(value, *, lock_limit=MAX_PUBLIC_LOCKS):
    """Normalize explicit channel edits or an internally compiled clip manifest."""
    _object(value, {'schema', 'operation', 'context', 'targets', 'operations', 'samples', 'locks', 'event_shift', 'derived_handle_scope'},
            {'schema', 'operation', 'context', 'targets'}, 'manifest')
    if value['schema'] != '1.0':
        _fail('Expected schema 1.0 and operation edit|inspect')
    _choice(value['operation'], {'edit', 'inspect'}, 'operation')
    out = copy.deepcopy(value)
    _object(out['context'], {'scene', 'view_layer'}, {'scene', 'view_layer'}, 'context')
    for key in ('scene', 'view_layer'):
        _text(out['context'][key], 'context.' + key, 256)
    identifiers = set()
    addresses = set()
    for target in _array(out['targets'], 'targets', 256, 1):
        _object(target, {'id', 'owner', 'data_path', 'array_index', 'sharing'},
                {'id', 'owner', 'data_path'}, 'target')
        identifier = _text(target['id'], 'target.id', 128)
        if identifier in identifiers:
            _fail('Duplicate target id: ' + identifier)
        identifiers.add(identifier)
        _object(target['owner'], {'type', 'name'}, {'type', 'name'}, 'owner')
        _choice(target['owner']['type'], OWNER_TYPES, 'owner.type')
        _text(target['owner']['name'], 'owner.name', 256)
        _text(target['data_path'], 'data_path')
        index = target.setdefault('array_index', 0)
        if type(index) is not int or not 0 <= index <= 63:
            _fail('array_index must be an integer 0..63 (scalar channels use 0)')
        _choice(target.setdefault('sharing', 'reject'), {'reject', 'isolate_action'}, 'sharing')
        address = (target['owner']['type'], target['owner']['name'], target['data_path'], index)
        if address in addresses:
            _fail('Two target ids address the same native channel')
        addresses.add(address)
    operations = out.setdefault('operations', [])
    for operation in _array(operations, 'operations', 4096):
        if not isinstance(operation, dict):
            _fail('operation must be an object')
        kind = operation.get('op')
        fields = {'set_key': ({'op', 'target', 'frame', 'value', 'interpolation'}, {'op', 'target', 'frame', 'value'}),
                  'move_key': ({'op', 'target', 'frame', 'to_frame'}, {'op', 'target', 'frame', 'to_frame'}),
                  'delete_key': ({'op', 'target', 'frame'}, {'op', 'target', 'frame'})}
        _choice(kind, set(fields), 'operation.op')
        allowed, required = fields[kind]
        _object(operation, allowed, required, 'operation')
        _text(operation['target'], 'operation.target', 128)
        if operation['target'] not in identifiers:
            _fail('Unknown target: ' + str(operation['target']))
        for key in ('frame', 'value', 'to_frame'):
            if key in operation:
                operation[key] = _number(operation[key], 'operation.' + key)
        if kind == 'set_key':
            _choice(operation.setdefault('interpolation', 'LINEAR'), {'LINEAR', 'BEZIER', 'CONSTANT'}, 'interpolation')
    if out['operation'] == 'inspect' and operations:
        _fail('inspect cannot contain mutation operations')
    out['samples'] = [_number(frame, 'sample frame') for frame in _array(out.get('samples', []), 'samples', 512)]
    out['locks'] = copy.deepcopy(_array(out.get('locks', []), 'locks', lock_limit))
    for lock in out['locks']:
        _object(lock, {'target', 'frame', 'tolerance'}, {'target', 'frame'}, 'lock')
        _text(lock['target'], 'lock.target', 128)
        if lock['target'] not in identifiers:
            _fail('Unknown lock target')
        lock['frame'] = _number(lock['frame'], 'lock.frame')
        lock['tolerance'] = _number(lock.get('tolerance', 1e-6), 'lock.tolerance')
        if lock['tolerance'] < 0:
            _fail('lock tolerance cannot be negative')
    if 'event_shift' in out:
        if out['operation'] != 'edit' or operations:
            _fail('event_shift requires edit with no explicit operations')
        from .events import plan_event_shift
        plan_event_shift(out['event_shift'], identifiers)
    if 'derived_handle_scope' in out:
        scope=out['derived_handle_scope']
        clip_scope=(isinstance(scope,dict)
                    and scope.get('policy') in {
                        'EXPLICIT_AUTO_HANDLE_DOMAIN_V2',
                        'EXPLICIT_AUTO_HANDLE_CHANNEL_DOMAIN_V3',
                        'EXPLICIT_AUTO_HANDLE_LOCAL_KEYS_V4'})
        if out['operation']!='edit':
            _fail('derived_handle_scope requires an explicit edit')
        if 'event_shift' in out:
            if clip_scope:
                _fail('Clip automatic-handle policy requires explicit local move operations, not event_shift')
        elif not (clip_scope and operations
                  and all(operation['op']=='move_key' for operation in operations)):
            _fail('derived_handle_scope requires explicit event_shift edit or the clip-local move policy')
        from .handle_scope import normalize as normalize_handle_scope
        out['derived_handle_scope']=normalize_handle_scope(out['derived_handle_scope'],identifiers)
    return out
