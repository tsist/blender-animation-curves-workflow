"""Opt-in pure bounded multi-pose collision diagnostics. Not a native route.

Every mesh is convex-validated; every group pair is reported. AABB separation
can prune SAT projection work, never native inventory/dependency validation.
"""
from . import spatial_collision as sat
from .contract import WorkflowError
MAX_MESHES=128
MAX_POSES=33
MAX_PAIRS=32768
MAX_TOTAL_VERTICES=270336
MAX_TOTAL_FACES=540672
MAX_VALIDATION_WORK=2_000_000

def reject(message):raise WorkflowError('UNSUPPORTED',message)
def names(v):
    if type(v)is not list or any(type(x)is not str or not x for x in v) or len(set(v))!=len(v):reject('Expected unique inventory names')
    return set(v)
def bounds(m):return [(min(v[i] for v in m['vertices']),max(v[i] for v in m['vertices'])) for i in range(3)]
def separated(a,b,aa,bb):
    tol=max(a['tolerance'],b['tolerance'])
    for i in range(3):
        gap=max(aa[i][0]-bb[i][1],bb[i][0]-aa[i][1])
        if gap>tol:
            axis=[0.,0.,0.];axis[i]=1.
            return {'relation':'separated','separating_axis':axis,'projected_gap':gap,'tolerance':tol,'broad_phase':'aabb','claim':'single_pose_convex_sat_only'}
    return None

def report(scene_mesh_names,poses,participants):
    observed=names(scene_mesh_names);group=names(participants)
    if not 1<=len(observed)<=MAX_MESHES or not 1<=len(group)<=8 or not group<=observed:reject('Inventory/group budget mismatch')
    if type(poses)is not list or not 1<=len(poses)<=MAX_POSES:reject('Pose budget exceeded')
    ordered=sorted(observed);selected=[(a,b) for i,a in enumerate(ordered) for b in ordered[i+1:] if a in group or b in group]
    if len(selected)*len(poses)>MAX_PAIRS:reject('Aggregate pair budget exceeded')
    # Full payload count gate precedes expensive convex validation.
    vertices=faces=validation_work=0;times=set()
    for pose in poses:
        if type(pose)is not dict or set(pose)!={'frame','meshes'}:reject('Unexpected pose payload')
        t=sat.number(pose['frame'])
        if t in times:reject('Duplicate sampled frame')
        times.add(t)
        if type(pose['meshes'])is not dict or set(pose['meshes'])!=observed:reject('Incomplete pose inventory')
        for row in pose['meshes'].values():
            if type(row)is not dict or set(row)!={'vertices','faces'} or type(row['vertices'])is not list or type(row['faces'])is not list:reject('Unexpected mesh payload')
            vertices+=len(row['vertices']);faces+=len(row['faces'])
            if not 4<=len(row['vertices'])<=sat.MAX_VERTICES or not 4<=len(row['faces'])<=sat.MAX_FACES:reject('Per-mesh geometry budget exceeded')
            if any(type(f)is not list or not 3<=len(f)<=sat.MAX_VERTICES for f in row['faces']):reject('Polygon validation budget exceeded')
            validation_work+=sum(len(f)**2+len(row['vertices']) for f in row['faces'])
            if validation_work>MAX_VALIDATION_WORK:reject('Aggregate convex validation work budget exceeded')
    if vertices>MAX_TOTAL_VERTICES or faces>MAX_TOTAL_FACES:reject('Aggregate geometry validation budget exceeded')
    prepared=[];total=0;pruned=0
    for pose in poses:
        meshes={n:sat.mesh(pose['meshes'][n]['vertices'],pose['meshes'][n]['faces']) for n in ordered}
        aabbs={n:bounds(meshes[n]) for n in ordered};pairs=[]
        for a,b in selected:
            result=separated(meshes[a],meshes[b],aabbs[a],aabbs[b])
            if result is None:total+=sat.work_bound(meshes[a],meshes[b])
            else:pruned+=1
            pairs.append((a,b,result))
        prepared.append((pose['frame'],meshes,pairs))
    # Reserve all narrow-phase work across ALL samples before any SAT call.
    if total>sat.MAX_PROJECTION_WORK:reject('Aggregate sampled SAT projection budget exceeded')
    results=[]
    for frame,meshes,pairs in prepared:
        results.append({'frame':frame,'pairs':[{'a':a,'b':b,'internal_group':a in group and b in group,**(result if result is not None else sat._compare_prepared(meshes[a],meshes[b]))} for a,b,result in pairs]})
    return {'status':'collision_diagnostics_not_accepted','inventory':ordered,'participants':sorted(group),'sampled_frames':[p['frame'] for p in poses],'poses':results,'excluded_meshes':[],'aabb_separated_pair_count':pruned,'total_pair_count':len(selected)*len(poses),'projection_work_upper_bound':total,'projection_work_budget':sat.MAX_PROJECTION_WORK,'geometry_validation_counts':{'vertices':vertices,'faces':faces,'work_upper_bound':validation_work,'work_budget':MAX_VALIDATION_WORK},'scope':'explicit_sampled_poses_group_pairs_including_internal','pending':['native_complete_inventory','hidden_lifecycle_policy','dependency_validation','finite_support_face_validation','native_edit_readback','cold_validation','user_acceptance'],'native_route_enabled':False}
