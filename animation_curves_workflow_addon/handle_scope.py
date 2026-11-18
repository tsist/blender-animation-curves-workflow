"""Opt-in exact-witness scope for native derived handle coordinates only."""
import copy
from .contract import WorkflowError,_object,_array,_text,_number
POLICY='EXPLICIT_AUTO_HANDLE_COORDINATES_V1'
CLIP_POLICY='EXPLICIT_AUTO_HANDLE_DOMAIN_V2'
CHANNEL_POLICY='EXPLICIT_AUTO_HANDLE_CHANNEL_DOMAIN_V3'
LOCAL_POLICY='EXPLICIT_AUTO_HANDLE_LOCAL_KEYS_V4'
AUTO={'AUTO','AUTO_CLAMPED'}

def validate_coordinates(key):
    for field in ('handle_left','handle_right'):
        value=key.get(field) if isinstance(key,dict) else None
        if not isinstance(value,list) or len(value)!=2:
            raise WorkflowError('INVALID_REQUEST','Native handle coordinates require exactly two finite numbers')
        for number in value:_number(number,field)

def normalize(value,target_ids):
    if not isinstance(value,dict):
        raise WorkflowError('INVALID_REQUEST','derived_handle_scope must be an object')
    policy=value.get('policy')
    if policy==CHANNEL_POLICY:
        _object(value,{'schema','policy','targets'},{'schema','policy','targets'},'derived_handle_scope')
        if value['schema']!='1.0':
            raise WorkflowError('INVALID_REQUEST','Unsupported derived-handle scope schema')
        out=copy.deepcopy(value);seen=set()
        for target in _array(out['targets'],'derived handle targets',256,1):
            target=_text(target,'derived handle target',128)
            if target not in target_ids:
                raise WorkflowError('INVALID_REQUEST','Unknown derived-handle target')
            if target in seen:
                raise WorkflowError('INVALID_REQUEST','Duplicate derived-handle target')
            seen.add(target)
        return out
    _object(value,{'schema','policy','keys'},{'schema','policy','keys'},'derived_handle_scope')
    if value['schema']!='1.0' or value['policy'] not in {POLICY,CLIP_POLICY,LOCAL_POLICY}:
        raise WorkflowError('INVALID_REQUEST','Unsupported derived-handle scope policy')
    out=copy.deepcopy(value);seen=set()
    for row in _array(out['keys'],'derived handle keys',4096,1):
        _object(row,{'target','expected_key'},{'target','expected_key'},'derived handle key')
        target=_text(row['target'],'derived handle target',128)
        if target not in target_ids:raise WorkflowError('INVALID_REQUEST','Unknown derived-handle target')
        key=row['expected_key']
        if not isinstance(key,dict) or not isinstance(key.get('co'),list) or len(key['co'])!=2:
            raise WorkflowError('INVALID_REQUEST','Complete native key witness required')
        frame=_number(key['co'][0],'derived handle frame');_number(key['co'][1],'derived handle value');validate_coordinates(key)
        if (target,frame) in seen:raise WorkflowError('INVALID_REQUEST','Duplicate derived-handle key')
        seen.add((target,frame))
    return out

def prepare(value,keys_by_target,operations,epsilon=1e-6):
    value=normalize(value,set(keys_by_target));out={}
    channel_policy=value['policy']==CHANNEL_POLICY
    clip_policy=value['policy']==CLIP_POLICY
    local_policy=value['policy']==LOCAL_POLICY
    touched={}
    for operation in operations:
        if operation.get('op')!='move_key':raise WorkflowError('UNSUPPORTED','Derived scope only supports explicit event key moves')
        touched.setdefault(operation['target'],set()).update((operation['frame'],operation['to_frame']))
    moved_targets=set(touched)
    if channel_policy:
        for target in value['targets']:
            if target not in moved_targets:
                raise WorkflowError('INVALID_REQUEST','Derived channel scope must address an actually retimed channel')
            keys=keys_by_target[target]
            if any(k.get('interpolation')!='BEZIER' for k in keys):
                raise WorkflowError('UNSUPPORTED','Clip channel handle scope requires an entirely BEZIER curve')
            if any(k.get('handle_left_type') not in AUTO or k.get('handle_right_type') not in AUTO for k in keys):
                raise WorkflowError('UNSUPPORTED','Clip channel handle scope requires automatic handles on the whole curve')
            frames=out.setdefault(target,set())
            for key in keys:
                frame=key['co'][0]
                if any(abs(frame-f)<=epsilon for f in touched[target]):
                    continue
                frames.add(frame)
        return out
    for item in value['keys']:
        target=item['target'];expected=item['expected_key'];frame=expected['co'][0]
        if target not in moved_targets:raise WorkflowError('INVALID_REQUEST','Derived scope must address an actually retimed channel')
        if any(abs(frame-f)<=epsilon for f in touched[target]):
            raise WorkflowError('INVALID_REQUEST','Derived scope cannot overlap explicitly moved source or destination keys')
        keys=keys_by_target[target]
        if clip_policy or local_policy:
            # V2 is the clip-event-local Bezier policy.  It still only allows
            # automatic handle *coordinates* to change on untouched keys; the
            # curve must remain entirely native Bezier with automatic handles.
            if any(k.get('interpolation')!='BEZIER' for k in keys):
                raise WorkflowError('UNSUPPORTED','Clip handle scope requires an entirely BEZIER curve')
            if clip_policy and any(k.get('handle_left_type') not in AUTO or k.get('handle_right_type') not in AUTO for k in keys):
                raise WorkflowError('UNSUPPORTED','Clip handle scope requires automatic handles on the whole curve')
        elif any(k.get('interpolation') not in {'LINEAR','CONSTANT'} for k in keys):
            raise WorkflowError('UNSUPPORTED','Derived scope requires an entirely LINEAR/CONSTANT curve, including incoming segments')
        matches=[k for k in keys if abs(k['co'][0]-frame)<=epsilon]
        if len(matches)!=1 or matches[0]!=expected:
            raise WorkflowError('CONFLICT','Derived-handle full native witness is stale or ambiguous',{'target':target,'frame':frame})
        if any(expected.get(field) not in AUTO for field in ('handle_left_type','handle_right_type')):
            raise WorkflowError('UNSUPPORTED','Manual FREE/ALIGNED/VECTOR handles cannot enter automatic coordinate scope')
        frames=out.setdefault(target,set())
        if any(abs(f-frame)<=epsilon for f in frames):raise WorkflowError('CONFLICT','Ambiguous derived-handle scope frames')
        frames.add(frame)
    return out

def compare_key(before,after):
    """Return exact coordinate delta only; every other field must remain equal."""
    validate_coordinates(before);validate_coordinates(after)
    left=copy.deepcopy(before);right=copy.deepcopy(after)
    coords={field:{'before':left.pop(field),'after':right.pop(field)} for field in ('handle_left','handle_right')}
    if left!=right:raise WorkflowError('CONFLICT','Explicit automatic handle scope cannot change key time/value/type/interpolation or other metadata')
    return {k:v for k,v in coords.items() if v['before']!=v['after']}
