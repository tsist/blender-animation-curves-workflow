"""Pure pre-transaction whitelist. Not permission to mutate a Blender scene."""
import math

def validate_updates(updates, *, start, end, source_sha256, snapshot_sha256,
                     expected_source_sha256, expected_snapshot_sha256):
    for value in (source_sha256,snapshot_sha256,expected_source_sha256,expected_snapshot_sha256):
        if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
            raise ValueError('Exact SHA256 values required')
    if source_sha256!=expected_source_sha256 or snapshot_sha256!=expected_snapshot_sha256:
        raise ValueError('Frozen source/snapshot mismatch')
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in (start,end)) or not start<end:
        raise ValueError('Invalid domain')
    paths={'pose.bones["thigh.L"].rotation_quaternion','pose.bones["shin.L"].rotation_quaternion'}
    seen=set();frames={}
    for row in updates:
        if set(row)!={'data_path','array_index','frame','before','after'}:
            raise ValueError('Unexpected update schema')
        path,index,t=row['data_path'],row['array_index'],row['frame']
        if path not in paths or type(index)!=int or index not in range(4):raise ValueError('Curve outside eight-curve whitelist')
        if any(type(row[k]) not in (float,int) or not math.isfinite(row[k]) for k in ('frame','before','after')):
            raise ValueError('Nonfinite update')
        if not start<t<end:raise ValueError('Endpoint/outside-domain update')
        key=(path,index,t)
        if key in seen:raise ValueError('Duplicate address')
        seen.add(key);frames.setdefault(t,set()).add((path,index))
    if not seen or any(len(v)!=8 for v in frames.values()):raise ValueError('Incomplete quaternion frame')
    return {'status':'HOST_WHITELIST_ONLY_NOT_NATIVE_ADMITTED','updates':len(seen),'frames':len(frames),
            'new_keys_permitted':False,'new_curves_permitted':False}
