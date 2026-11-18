"""Report-only affine arithmetic envelopes and exact native-hull separation.

Never certifies native planarity/convexity, overlap, contact or edit acceptance.
"""
import math,struct
from fractions import Fraction as F
from . import spatial_collision as kernel
from .contract import WorkflowError
MAX_MESHES=128
MAX_PROJECTION_WORK=2_000_000
MAX_VALIDATION_WORK=2_000_000
U=F(1,2**24);GAMMA7=7*U/(1-7*U);UNDERFLOW=F(7,2**149)/(1-7*U)
def reject(msg):raise WorkflowError('UNSUPPORTED',msg)
def f32(v):
    try:ok=type(v) in (int,float) and math.isfinite(v) and struct.unpack('f',struct.pack('f',v))[0]==v
    except (OverflowError,struct.error):ok=False
    if not ok:reject('Expected finite exact binary32 native coordinate')
    return F(v)
def ratio(v):return {'numerator':str(v.numerator),'denominator':str(v.denominator)}
def point(v):
    if not isinstance(v,list) or len(v)!=3:reject('Expected three native coordinates')
    return tuple(f32(x) for x in v)
def prepare(local,faces,matrix,actual):
    if not isinstance(matrix,list) or len(matrix)!=4 or any(not isinstance(row,list) or len(row)!=4 for row in matrix):reject('Expected native4x4 matrix')
    m=[[f32(v) for v in row] for row in matrix]
    if m[3]!=[0,0,0,1]:reject('Projective matrix unsupported')
    if not isinstance(local,list) or not isinstance(actual,list) or not 4<=len(local)<=64 or len(actual)!=len(local):reject('Native vertex budget mismatch')
    lp=[point(v) for v in local];ap=[point(v) for v in actual]
    kernel.mesh(local,faces) # Preserve strict original local convexity/topology.
    affine=[];evidence=[]
    for v,a in zip(lp,ap):
        xyz=[];row=[]
        for i in range(3):
            products=[m[i][j]*v[j] for j in range(3)]+[m[i][3]]
            exact=sum(products,F(0));bound=GAMMA7*sum(map(abs,products),F(0))+UNDERFLOW;residual=abs(a[i]-exact)
            if residual>bound:reject('Observed world vertex exceeds binary32 affine arithmetic envelope')
            approximate=float(exact)
            if not math.isfinite(approximate):reject('Affine reference overflow')
            xyz.append(approximate);row.append({'residual':ratio(residual),'bound':ratio(bound)})
        affine.append(xyz);evidence.append(row)
    model=kernel.mesh(affine,faces)
    return {'model':model,'actual':ap,'actual_float':actual,'affine':affine,'arithmetic_evidence':evidence}
def aabb_axis(a,b):
    for i in range(3):
        alo=min(v[i] for v in a['actual']);ahi=max(v[i] for v in a['actual']);blo=min(v[i] for v in b['actual']);bhi=max(v[i] for v in b['actual'])
        if blo>ahi or alo>bhi:
            axis=[0.,0.,0.];axis[i]=1.
            return axis,max(blo-ahi,alo-bhi)
    return None

def compare(a,b):
    early=aabb_axis(a,b)
    if early:return {'relation':'separated_declared_vertex_hulls','axis':early[0],'exact_projection_gap':ratio(early[1]),'method':'exact_aabb'}
    if kernel.work_bound(a['model'],b['model'])>MAX_PROJECTION_WORK:reject('Exact projection work budget exceeded')
    axes=list(a['model']['normals'])+list(b['model']['normals'])
    for x in a['model']['edges']:
        for y in b['model']['edges']:
            q=kernel.cross(x,y)
            if any(q):axes.append(q) # Any nonzero finite dyadic axis is sound; no near-parallel cutoff.
    for axis in axes:
        if any(not math.isfinite(v) for v in axis):reject('Nonfinite reference axis')
        q=tuple(F(v) for v in axis)
        aa=[sum((x*y for x,y in zip(v,q)),F(0)) for v in a['actual']];bb=[sum((x*y for x,y in zip(v,q)),F(0)) for v in b['actual']]
        gap=max(min(bb)-max(aa),min(aa)-max(bb))
        if gap>0:return {'relation':'separated_declared_vertex_hulls','axis':list(axis),'exact_projection_gap':ratio(gap),'method':'exact_dyadic_projection'}
    return {'relation':'uncertain_not_separated_by_tested_axes','claim':'No native contact/overlap/convexity or clearance conclusion'}

def report(inventory,topology,participants,hidden_copy_evidence=None):
    names=inventory['mesh_inventory']
    if not isinstance(names,list) or any(type(n)is not str or not n for n in names) or not 1<=len(names)<=MAX_MESHES or len(set(names))!=len(names) or set(names)!=set(inventory['meshes']) or set(names)!=set(topology):reject('Complete bounded inventory required')
    if not isinstance(participants,list) or not 1<=len(participants)<=8 or len(set(participants))!=len(participants) or not set(participants)<=set(names):reject('Explicit bounded participant set required')
    if set(inventory['witnesses'])!=set(names):reject('Exact witness inventory required')
    validation_work=0
    for name in names:
        row=topology[name]
        if type(row)is not dict or set(row)!={'vertices','faces'} or type(row['vertices'])is not list or type(row['faces'])is not list:reject('Malformed source topology')
        if not 4<=len(row['vertices'])<=64 or not 4<=len(row['faces'])<=128 or any(type(f)is not list or not 3<=len(f)<=64 for f in row['faces']):reject('Topology structural budget exceeded')
        if set(inventory['meshes'][name])!={'vertices','faces'} or inventory['meshes'][name]['faces']!=row['faces']:reject('Supplied geometry face topology differs from validated reference')
        validation_work+=2*sum(len(f)**2+len(row['vertices']) for f in row['faces'])
        if validation_work>MAX_VALIDATION_WORK:reject('Aggregate local+affine validation work budget exceeded')
    hidden=[name for name in names if inventory['witnesses'][name].get('geometry_kind')=='reconstructed_conservative_local_trs']
    evidence=None
    if hidden:
        from . import hidden_pose_evidence as hp
        if type(hidden_copy_evidence)is not tuple or len(hidden_copy_evidence)!=2:reject('Exact observed hidden-copy evidence required; reconstructed vertices are not native observations')
        evidence=hp.parse(hidden_copy_evidence[0],hidden_copy_evidence[1],inventory['opened_source_topology']['source_sha256'],inventory['frame'],hidden)
        if evidence['diagnostic_sha256']!=inventory['hidden_pose_evidence_sha256']:reject('Hidden copy provenance mismatch')
    elif hidden_copy_evidence is not None:reject('Unexpected hidden-copy evidence')
    prepared={};provenance={}
    for name in names:
        witness=inventory['witnesses'][name];matrix=witness['geometry_matrix'];actual=inventory['meshes'][name]['vertices']
        if name in hidden:
            hp.verify_owner(evidence,name,{'location':witness['location'],'rotation':witness['rotation_euler'],'scale':witness['scale']},witness['matrix_world'],matrix,topology[name]['vertices'],actual)
            row=evidence['owners'][name];matrix=row['native_copy_world'];actual=row['native_copy_vertices']
            provenance[name]={'kind':'observed_temporary_visible_copy_saved_local_pose','diagnostic_sha256':evidence['diagnostic_sha256'],'not_original_hidden_cached_pose':True}
        else:
            if witness.get('geometry_kind')!='cached_native_world_with_local_agreement':reject('Unknown geometry provenance')
            provenance[name]={'kind':'observed_original_cached_world_pose'}
        prepared[name]=prepare(topology[name]['vertices'],topology[name]['faces'],matrix,actual)
    pairs=[(a,b) for i,a in enumerate(sorted(names)) for b in sorted(names)[i+1:] if a in participants or b in participants]
    if len(pairs)>32768:reject('Pair budget exceeded')
    work=sum(kernel.work_bound(prepared[a]['model'],prepared[b]['model']) for a,b in pairs if aabb_axis(prepared[a],prepared[b]) is None)
    if work>MAX_PROJECTION_WORK:reject('Aggregate exact projection work budget exceeded before comparison')
    results=[{'a':a,'b':b,'geometry_provenance':{'a':provenance[a],'b':provenance[b]},**compare(prepared[a],prepared[b])} for a,b in pairs]
    return {'status':'precision_collision_diagnostics_not_accepted','coordinate_units':'Blender_coordinate_units_not_implicitly_metres','mesh_count':len(names),'participants':participants,'pairs':results,'excluded_meshes':[],'reserved_projection_work':work,'reserved_validation_work':validation_work,'arithmetic_model':{'unit_roundoff':ratio(U),'gamma7':ratio(GAMMA7),'absolute_underflow_allowance':ratio(UNDERFLOW)},'meshes':{name:{'provenance':provenance[name],'observed_vertices':v['actual_float'],'affine_reference_vertices':v['affine'],'arithmetic_evidence':v['arithmetic_evidence']} for name,v in prepared.items()},'limitations':['Hidden_pairs_refer_to_observed_temporary_copies_not_original_hidden_cached_pose','No_native_planarity_or_convexity_certificate','Unseparated_is_uncertain_not_overlap','No_contact_or_Euclidean_clearance','No_edit_trajectory_or_user_acceptance']}
