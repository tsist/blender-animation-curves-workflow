"""Pure project/census compatibility binding, never an editability certificate.

Caller must verify the successful census receipt and actual source bytes first.
Hashes bind supplied bytes, not their truth, authorization or native freshness.
"""
import hashlib,json,math,re
from .contract import WorkflowError

def fail(message):raise WorkflowError('INVALID_REQUEST',message)
def conflict(message):raise WorkflowError('CONFLICT',message)
def digest(v):
    if not isinstance(v,str) or re.fullmatch('[0-9a-f]{64}',v) is None:fail('Expected SHA256')
    return v

def read(raw,expected):
    if type(raw)is not bytes or len(raw)>64*1024*1024:fail('Expected bounded JSON bytes')
    if hashlib.sha256(raw).hexdigest()!=digest(expected):conflict('Artifact bytes changed')
    def pairs(rows):
        result={}
        for k,v in rows:
            if k in result:fail('Duplicate JSON object key')
            result[k]=v
        return result
    def validate(v,depth=0):
        if depth>32:fail('JSON depth budget exceeded')
        if type(v)is float and not math.isfinite(v):fail('Nonfinite JSON number')
        if type(v)is dict:
            for value in v.values():validate(value,depth+1)
        elif type(v)is list:
            for value in v:validate(value,depth+1)
        elif v is not None and type(v)not in (bool,int,float,str):fail('Unsupported JSON type')
    try:
        result=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:fail('Nonfinite JSON'));validate(result);return result
    except (ValueError,UnicodeError,RecursionError) as e:fail('Malformed JSON: '+str(e))

def integer(x):
    if type(x)is not int:fail('Expected integer frame')
    return x

def assess(project_bytes,census_bytes,*,project_sha256,census_sha256,source_sha256,frame):
    """Bind source shots, lifecycle owners and observed native Action/slot labels.

    Even an empty unsupported list cannot authorize a write: unassessed gates
    remain explicit. This API does not read external paths or mutate inputs.
    """
    try:return _assess(read(project_bytes,project_sha256),read(census_bytes,census_sha256),digest(source_sha256),integer(frame),project_sha256,census_sha256)
    except (KeyError,TypeError,IndexError,AttributeError) as e:fail('Malformed project/census schema: '+str(e))

def _assess(p,c,source,frame,ph,ch):
    if c['status']!='passed' or c['source_sha256']!=source or c['source_unchanged'] is not True or c['no_blend_output'] is not True:conflict('Census is not successful for the supplied source')
    observed_frame=c['frame']
    if type(observed_frame)is not list or len(observed_frame)!=2 or type(observed_frame[0])is not int or type(observed_frame[1])not in (int,float) or not 0<=observed_frame[1]<1:fail('Malformed census frame/subframe')
    lo,hi=p['local_range'];integer(lo);integer(hi)
    if not lo<=frame<=hi:conflict('Requested frame outside project range')
    cameras=p['cameras'];objects=c['objects'];actions=c['actions']
    if not 1<=len(cameras)<=128 or len(objects)>512 or len(actions)>256:fail('Inventory budget exceeded')
    owners={x['identity']['name']:x for x in objects};acts={x['identity']['name']:x for x in actions}
    if len(owners)!=len(objects) or len(acts)!=len(actions):conflict('Duplicate observed native identity')
    shots=[];previous=lo-1
    for shot in cameras:
        start,end=integer(shot['start']),integer(shot['end']);name=shot['id']
        if start!=previous+1 or end<start or end>hi:conflict('Source shots must completely partition range')
        if owners[name]['type']!='CAMERA':conflict('Source shot owner is not a native camera')
        shots.append({'camera':name,'start':start,'end':end});previous=end
    if previous!=hi or p['source_cuts']!=[s['start'] for s in shots[1:]]:conflict('Source cut inventory disagrees with shots')
    expected=[(s['start'],s['camera']) for s in shots]
    observed=[(integer(m['frame']),m['camera']['name']) for m in c['markers']]
    if observed!=expected:conflict('Native marker order/frame/camera differs from source shots')
    # Bind actual observed owner -> Action -> slot. Names are labels, not UUIDs.
    native=[]
    for name,o in sorted(owners.items()):
        a=o['animation']
        if a is None or a['action'] is None:continue
        action=acts[a['action']['name']];slot=a['slot']
        matches=[s for s in action['slots'] if s['identifier']==slot['identifier'] and s['target_id_type']==slot['target_id_type']]
        if len(matches)!=1:conflict('Native Action slot label is missing or ambiguous')
        handle=matches[0]['handle']
        if type(handle)is not int:fail('Slot handle must be integer')
        curves=[v for v in action['curves'] if v['slot_handle']==handle]
        native.append({'owner':name,'owner_type':o['type'],'action':action['identity'],'slot':matches[0],'curves':curves})
    lifecycle=[]
    for name,intervals in sorted(p['active_intervals'].items()):
        if name not in owners:conflict('Lifecycle owner absent from native scene')
        prior=lo-1
        for start,end in intervals:
            integer(start);integer(end)
            if not lo<=start<=end<=hi or start<=prior:conflict('Lifecycle intervals overlap or exceed range')
            prior=end
        rows=[v for v in native if v['owner']==name]
        if len(rows)!=1:conflict('Lifecycle owner has no unique native Action/slot')
        curves=[v for v in rows[0]['curves'] if v['path'] in ('hide_render','hide_viewport')]
        if len(curves)!=2 or {v['path'] for v in curves}!={'hide_render','hide_viewport'} or any(type(v['index'])is not int or v['index']!=0 or set(v['interpolation_counts'])!={'CONSTANT'} for v in curves):conflict('Lifecycle requires paired native CONSTANT bool curves')
        lifecycle.append({'owner':name,'active_intervals':intervals,'native_action':rows[0]['action'],'native_slot':rows[0]['slot'],'curve_summaries':curves,'full_key_semantics':'unassessed_by_census'})
    gates=[]
    def gate(code,evidence):gates.append({'code':code,'status':'unsupported','evidence':evidence})
    meshes=[n for n,o in owners.items() if o['type']=='MESH']
    if len(meshes)>64:gate('spatial_mesh_count',{'observed':len(meshes),'current_limit':64})
    animated=[n for n,o in owners.items() if o['type']!='MESH' and o['animation'] is not None]
    if animated:gate('animated_nonparticipants',animated)
    hidden=[n for n,o in owners.items() if o['type']=='MESH' and (o['hide_render'] or o['hide_viewport'] or not o['visible_in_current_layer'])]
    if hidden:gate('hidden_mesh_inventory',{'observed_frame_subframe':observed_frame,'scope':'saved_pose_only','owners':hidden})
    if lifecycle:gate('public_bool_lifecycle_write',[v['owner'] for v in lifecycle])
    handles=sorted({k for a in actions for v in a['curves'] for k,count in v['handle_type_counts'].items() if count and k!='FREE'})
    if handles:gate('existing_key_spatial_FREE_contract',handles)
    return {'schema':'PROJECT_COMPATIBILITY_V1','status':'not_accepted','native_apply_supported':False,'identity':{'source_sha256':source,'project_sha256':ph,'census_sha256':ch},'requested_frame':frame,'census_observed_frame_subframe':observed_frame,'source_shot':next(s for s in shots if s['start']<=frame<=s['end']),'source_shots':shots,'source_markers':c['markers'],'native_bindings':native,'lifecycles':lifecycle,'mesh_inventory':sorted(meshes),'unsupported':gates,'unassessed':[{'code':v,'status':'unassessed'} for v in ('requested_frame_visibility','full_native_key_witnesses','complete_dependency_and_static_schema','convex_geometry_and_collision_work_budget','animated_nonparticipant_preservation','automatic_handle_delta_scope','trajectory_safety','user_acceptance')],'identity_limit':'Supplied hash identity is not receipt verification, durable native identity or consent'}
