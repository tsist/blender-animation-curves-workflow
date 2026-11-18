"""Pure existing-key compiler from trusted native inspections, not a commit API.

Native dependency/geometry/world-transform and identity revalidation remain
mandatory in the future guarded adapter. No standalone operation is authorized
by this compiler's output.
"""
import copy
import math
import struct
from fractions import Fraction
from .bindings import capture
from .contract import WorkflowError
from .spatial_candidate import number


def native_number(value):
    value=number(value)
    try:stored=struct.unpack('f',struct.pack('f',value))[0]
    except (OverflowError,struct.error):
        raise WorkflowError('UNSUPPORTED','Requested value exceeds native float32')
    if not math.isfinite(stored) or stored!=value:
        raise WorkflowError('UNSUPPORTED','Requested value is not exactly native float32')
    return value


def compile_existing(inspection,participants,frame,translation):
    try:return _compile(inspection,participants,frame,translation)
    except (KeyError,TypeError,ValueError,OverflowError,AttributeError,IndexError) as exc:
        raise WorkflowError('INVALID_REQUEST','Malformed trusted inspection or spatial selector') from exc


def _compile(inspection,participants,frame,translation):
    frame=native_number(frame)
    if not isinstance(participants,list) or not 1<=len(participants)<=8 or any(not isinstance(x,str) or not x for x in participants) or len(set(participants))!=len(participants):
        raise WorkflowError('INVALID_REQUEST','Explicit unique participant names required')
    if not isinstance(translation,(list,tuple)) or len(translation)!=3:
        raise WorkflowError('INVALID_REQUEST','Explicit world XYZ translation required')
    delta=[number(v) for v in translation]
    witnesses=[];operations=[];changes=[];actions={};owner_bindings={}
    rows=inspection['targets']
    ids=[r['id'] for r in rows]
    if len(ids)!=len(set(ids)):raise WorkflowError('CONFLICT','Inspection target IDs must be unique')
    for owner in participants:
        for axis in range(3):
            selected=[r for r in rows if r['owner']=={'type':'OBJECT','name':owner} and r['data_path']=='location' and r['array_index']==axis]
            if len(selected)!=1:raise WorkflowError('CONFLICT','Exactly one inspected XYZ location channel required')
            row=selected[0]
            witness=capture(inspection,row['id'],frame)
            key=witness['expected_key']
            if key['co'][0]!=frame:raise WorkflowError('CONFLICT','Existing key time must match exactly')
            if row['slot_type']!='OBJECT':raise WorkflowError('CONFLICT','OBJECT slot required')
            identity=(row['action_name'],row['slot_identifier'],row['slot_type'])
            if owner_bindings.setdefault(owner,identity)!=identity:
                raise WorkflowError('CONFLICT','Participant channels disagree on native binding')
            prior=actions.setdefault(row['action_name'],owner)
            if prior!=owner:raise WorkflowError('UNSUPPORTED','Shared Action ownership is unsupported')
            if key['handle_left_type']!='FREE' or key['handle_right_type']!='FREE':
                raise WorkflowError('UNSUPPORTED','Initial spatial compiler requires FREE handles')
            if key['interpolation'] not in ('LINEAR','CONSTANT','BEZIER'):
                raise WorkflowError('UNSUPPORTED','Unsupported interpolation')
            before=native_number(key['co'][1]);after=native_number(before+delta[axis])
            if Fraction(after)-Fraction(before)!=Fraction(delta[axis]):
                raise WorkflowError('UNSUPPORTED','Numeric precision loses requested spatial displacement')
            expected=copy.deepcopy(key)
            expected['co'][1]=after
            # co_ui is an RNA view of co, not independently settable metadata.
            if key['co_ui']!=key['co']:raise WorkflowError('UNSUPPORTED','Unexpected native co_ui alias')
            expected['co_ui'][1]=after
            for handle in ('handle_left','handle_right'):
                coords=key[handle]
                if not isinstance(coords,list) or len(coords)!=2:raise WorkflowError('INVALID_REQUEST','Complete handle coordinates required')
                native_number(coords[0]);old=native_number(coords[1]);new=native_number(old+delta[axis])
                if Fraction(new)-Fraction(old)!=Fraction(delta[axis]):raise WorkflowError('UNSUPPORTED','Numeric precision loses handle displacement')
                expected[handle][1]=new
            witnesses.append(witness)
            if after!=before:
                operations.append({'op':'set_key','target':row['id'],'frame':frame,'value':after,'interpolation':key['interpolation']})
                changes.append({'target':row['id'],'before':copy.deepcopy(key),'expected_after':expected})
    return {'status':'compiled_not_accepted','frame':frame,'participants':list(participants),'translation':delta,
            'witnesses':witnesses,'operations':operations,'expected_changes':changes,
            'pending':['native_world_transform_validation','geometry_collision','fresh_witness_comparison','native_readback','preview_acceptance','cold_verification']}
