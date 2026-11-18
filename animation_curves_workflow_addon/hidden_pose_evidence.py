"""Explicit sampled-pose evidence, never a reconstruction/approval inference."""
import hashlib,json,math
from .contract import WorkflowError

def fail(message):raise WorkflowError('CONFLICT',message)
def finite(values):
    for v in values:
        try:ok=type(v) in (int,float) and math.isfinite(v)
        except OverflowError:ok=False
        if not ok:fail('Invalid hidden-pose finite coordinate')
def parse(raw,expected_sha,source_sha,frame,hidden_names):
    if type(raw)is not bytes or len(raw)>4_000_000 or hashlib.sha256(raw).hexdigest()!=expected_sha:fail('Hidden-pose diagnostic byte identity mismatch')
    def pairs(rows):
        out={}
        for k,v in rows:
            if k in out:fail('Duplicate hidden-pose evidence key')
            out[k]=v
        return out
    try:d=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda x:fail('Nonfinite evidence'))
    except (ValueError,TypeError,RecursionError) as e:raise WorkflowError('INVALID_REQUEST','Invalid hidden-pose JSON') from e
    nodes=[0]
    def valid(v,depth=0):
        nodes[0]+=1
        if depth>16 or nodes[0]>200000:fail('Hidden-pose JSON structure budget exceeded')
        if type(v) in (int,float):finite([v])
        elif isinstance(v,dict):
            for x in v.values():valid(x,depth+1)
        elif isinstance(v,list):
            for x in v:valid(x,depth+1)
        elif v is not None and type(v) not in (str,bool):fail('Invalid evidence JSON type')
    valid(d)
    finite([frame])
    if frame!=1:fail('Evidence qualifies saved frame1 only')
    if not isinstance(d,dict) or d.get('status')!='passed' or d.get('claim')!='temporary_copy_pose_observation_only' or d.get('source_sha256')!=source_sha or d.get('source_unchanged')is not True or d.get('no_blend_output')is not True:fail('Hidden-pose diagnostic/source mismatch')
    checks=d.get('checks');owners=d.get('owners')
    if not isinstance(checks,list) or not checks or any(not isinstance(c,dict) or c.get('passed')is not True for c in checks) or not any(c.get('name')=='saved_frame1' for c in checks):fail('Unpassed hidden-pose evidence')
    if not isinstance(owners,dict) or not 1<=len(owners)<=128 or sorted(owners)!=sorted(hidden_names):fail('Exact complete hidden-owner scope mismatch')
    return {'source_sha256':source_sha,'diagnostic_sha256':expected_sha,'frame':frame,'owners':owners}

def verify_owner(policy,name,local,raw_matrix,composed_matrix,local_vertices,composed_vertices):
    try:
        row=policy['owners'][name]
        if local!=row['source_local'] or raw_matrix!=row['raw_world'] or composed_matrix!=row['reconstructed_world'] or local_vertices!=row['vertices_local']:fail('Declared hidden pose/raw geometry drift')
        native_vertices=row['native_copy_vertices'];native_matrix=row['native_copy_world']
        if not 4<=len(native_vertices)<=64 or len(native_vertices)!=len(composed_vertices) or len(native_matrix)!=4:fail('Hidden native-copy shape mismatch')
        if any(len(v)!=3 for v in native_vertices+composed_vertices) or any(len(v)!=4 for v in native_matrix+composed_matrix):fail('Hidden native-copy coordinate shape mismatch')
        for values in native_vertices+composed_vertices+native_matrix+composed_matrix:finite(values)
        matrix_error=max(abs(a-b) for x,y in zip(native_matrix,composed_matrix) for a,b in zip(x,y))
        vertex_error=max(abs(a-b) for x,y in zip(native_vertices,composed_vertices) for a,b in zip(x,y))
        if matrix_error>1e-6 or vertex_error>1e-5:fail('Reconstructed geometry exceeds observed native-copy tolerance')
        return {'diagnostic_sha256':policy['diagnostic_sha256'],'geometry_kind':'reconstructed_conservative_local_trs','native_copy_matrix_max_error':matrix_error,'native_copy_vertex_max_error':vertex_error,'matrix_tolerance':1e-6,'vertex_tolerance':1e-5}
    except (KeyError,TypeError,ValueError,OverflowError) as e:raise WorkflowError('INVALID_REQUEST','Malformed hidden-pose owner evidence') from e

def verify_topology_reference(raw,expected_sha,source_sha,actual_meshes):
    """Caller must establish pristine source-open provenance independently."""
    if type(raw)is not bytes or len(raw)>4_000_000 or hashlib.sha256(raw).hexdigest()!=expected_sha:fail('Source topology byte identity mismatch')
    def pairs(rows):
        out={}
        for k,v in rows:
            if k in out:fail('Duplicate topology field')
            out[k]=v
        return out
    try:
        d=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda x:fail('Nonfinite topology'))
        if not isinstance(d,dict) or set(d)!={'schema','source_sha256','meshes'} or d['schema']!='OPENED_SOURCE_TOPOLOGY_V1' or d['source_sha256']!=source_sha:fail('Source topology identity mismatch')
        meshes=d['meshes']
        if not isinstance(meshes,dict) or not 1<=len(meshes)<=128 or set(meshes)!=set(actual_meshes):fail('Complete topology inventory mismatch')
        for name,row in meshes.items():
            if not isinstance(row,dict) or set(row)!={'vertices','faces'}:fail('Unsupported topology fields')
            vertices,faces=row['vertices'],row['faces']
            if not isinstance(vertices,list) or not 4<=len(vertices)<=64 or not isinstance(faces,list) or not 4<=len(faces)<=128:fail('Source topology budget mismatch')
            for v in vertices:
                if not isinstance(v,list) or len(v)!=3:fail('Source vertex shape mismatch')
                finite(v)
            for face in faces:
                if not isinstance(face,list) or not 3<=len(face)<=64 or any(type(i)is not int or not 0<=i<len(vertices) for i in face) or len(set(face))!=len(face):fail('Source polygon indices invalid')
            if row!=actual_meshes[name]:fail('Live geometry differs from source topology reference')
        return {'source_sha256':source_sha,'topology_sha256':expected_sha,'mesh_count':len(meshes),'provenance':'caller_must_verify_pristine_source_open'}
    except (ValueError,TypeError,KeyError,RecursionError,OverflowError) as e:raise WorkflowError('INVALID_REQUEST','Malformed source topology reference') from e
