"""Pure support request gate. Input validity is never native qualification."""
import math

def request(*, instance_uuid, side, times, bone_names):
    if not isinstance(instance_uuid,str) or len(instance_uuid)!=32 or any(c not in '0123456789abcdef' for c in instance_uuid):
        raise ValueError('Missing exact template instance identity')
    if side != 'L':
        raise ValueError('Only the independently tested left-leg policy is available')
    if len(times)!=4 or any(type(v) not in (float,int) or not math.isfinite(v) for v in times):
        raise ValueError('Four finite frame values required')
    a,b,c,d=times
    if not a < b <= c < d or not math.isfinite(d-a):
        raise ValueError('Expected entry < plant <= release < exit')
    names=set(bone_names)
    if len(names)!=18 or not {'thigh.L','shin.L','thigh.R','shin.R'} <= names or {'foot.L','foot.R'} & names:
        raise ValueError('Expected the 18-bone footless two-segment template')
    return {'schema':1,'instance_uuid':instance_uuid,'side':side,'times':list(times),
            'status':'INPUTS_ONLY_NOT_NATIVE_QUALIFIED','may_write_keys':False,
            'required_next':['frozen_source_snapshot','actual_mesh_and_knee_qualification',
                             'proposal_whitelist','transaction_rollback','independent_cold']}
