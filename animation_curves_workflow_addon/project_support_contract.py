"""Pure source-bound role annotations and explicit horizontal finite-face proof.

No exclusions, pose changes, acceptance, generalized contact or trajectory claim.
Caller must independently verify the native report receipt/source chain.
"""
from fractions import Fraction as F
from . import project_compatibility as identity
from .contract import WorkflowError
from .spatial_precision_report import f32,ratio

def fail(msg):raise WorkflowError('CONFLICT',msg)
def annotate(project_raw,layout_raw,report_raw,*,project_sha,layout_sha,report_sha,source_sha):
    p=identity.read(project_raw,project_sha);l=identity.read(layout_raw,layout_sha);r=identity.read(report_raw,report_sha)
    try:
        if p['persistent_layout_sha256']!=layout_sha or r['source_sha256']!=source_sha or r['status']!='passed' or r['source_unchanged']is not True or r['no_blend_output']is not True:fail('Project/layout/native source identity mismatch')
        entities=l['entities'];effects=p['effect_ids'];names=r['inventory']['mesh_inventory']
        if not isinstance(entities,list) or not 1<=len(entities)<=128 or not isinstance(effects,list) or len(effects)>128 or len(set(effects))!=len(effects):fail('Role inventory budget/uniqueness')
        rows={}
        for entity in entities:
            name=entity['id'];role=entity['role']
            if type(name)is not str or not name or name in rows or role not in {'actor','prop','equipment','environment'}:fail('Unknown or duplicate layout role')
            rows[name]={'declaration':'layout_entity','layout_role':role,'collision_policy':'not_inferred_from_role'}
        for name in effects:
            if type(name)is not str or not name or name in rows:fail('Effect/layout identity overlap')
            rows[name]={'declaration':'project_effect','collision_policy':'not_automatically_excluded'}
        if type(names)is not list or any(type(n)is not str or not n for n in names) or len(set(names))!=len(names) or set(rows)!=set(names) or set(r['inventory']['meshes'])!=set(names) or set(r['inventory']['witnesses'])!=set(names):fail('Complete native mesh role reconciliation failed')
        physical=p['physical_ids']
        if type(physical)is not list or any(type(n)is not str or not n for n in physical) or len(set(physical))!=len(physical) or not set(physical)<=set(x['id'] for x in entities):fail('Animated physical subset does not reconcile')
        return {'status':'project_role_annotations_not_accepted','source_sha256':source_sha,'project_sha256':project_sha,'layout_sha256':layout_sha,'report_sha256':report_sha,'roles':rows,'layout_count':len(entities),'effect_count':len(effects),'animated_physical_subset':p['physical_ids'],'excluded_meshes':[],'pending':['explicit_collision_role_policy','contact_or_event_acceptance','trajectory','user_acceptance']}
    except (KeyError,TypeError,AttributeError) as e:raise WorkflowError('INVALID_REQUEST','Malformed project role evidence') from e

def contact(report_raw,*,report_sha,source_sha,participant,support,participant_face,support_face,frame):
    r=identity.read(report_raw,report_sha)
    try:
        if r['status']!='passed' or r['source_sha256']!=source_sha or r['source_unchanged']is not True or r['no_blend_output']is not True or type(frame)not in (int,float) or frame!=r['frame']:fail('Saved-pose identity mismatch')
        if participant==support:fail('Distinct participant/support required')
        inv=r['inventory'];names=inv['mesh_inventory']
        if participant not in names or support not in names:fail('Explicit owner not in inventory')
        for name in (participant,support):
            if inv['witnesses'][name]['geometry_kind']!='cached_native_world_with_local_agreement':fail('Contact only supports observed original cached native geometry')
        a,b=inv['meshes'][participant],inv['meshes'][support]
        if type(participant_face)is not int or type(support_face)is not int or not 0<=participant_face<len(a['faces']) or not 0<=support_face<len(b['faces']):fail('Explicit face index invalid')
        if not 4<=len(a['vertices'])<=64 or not 4<=len(b['vertices'])<=64:fail('Contact vertex budget')
        if any(type(v)is not list or len(v)!=3 for v in a['vertices']+b['vertices']):fail('Expected three-coordinate mesh points')
        if type(a['faces'])is not list or type(b['faces'])is not list or len(a['faces'])>128 or len(b['faces'])>128:fail('Face count budget')
        av=[tuple(f32(x) for x in v) for v in a['vertices']];bv=[tuple(f32(x) for x in v) for v in b['vertices']]
        ai,bi=a['faces'][participant_face],b['faces'][support_face]
        if not 3<=len(ai)<=64 or len(bi)!=4 or len(set(ai))!=len(ai) or len(set(bi))!=4:fail('Finite face topology unsupported')
        if any(type(i)is not int or not 0<=i<len(av) for i in ai) or any(type(i)is not int or not 0<=i<len(bv) for i in bi):fail('Face references invalid')
        patch=[av[i] for i in ai];face=[bv[i] for i in bi];z=face[0][2]
        if any(v[2]!=z for v in face+patch):fail('Explicit faces do not share exact horizontal plane')
        if any(v[2]<z for v in av) or any(v[2]>z for v in bv):fail('Supporting half-space proof failed')
        lo=[min(v[i] for v in face) for i in range(2)];hi=[max(v[i] for v in face) for i in range(2)]
        if lo[0]>=hi[0] or lo[1]>=hi[1] or {(v[0],v[1]) for v in face}!={(x,y) for x in (lo[0],hi[0]) for y in (lo[1],hi[1])}:fail('Support is not an exact finite horizontal rectangle')
        # Both face boundaries must be simple convex ordered polygons, not only point sets.
        for polygon in (face,patch):
            sides=[]
            for i,u in enumerate(polygon):
                v=polygon[(i+1)%len(polygon)];signs=[]
                for j,w in enumerate(polygon):
                    if j in (i,(i+1)%len(polygon)):continue
                    cross=(v[0]-u[0])*(w[1]-u[1])-(v[1]-u[1])*(w[0]-u[0])
                    if cross==0:fail('Degenerate or collinear face boundary')
                    signs.append(cross>0)
                if len(set(signs))!=1:fail('Nonconvex/crossing face boundary')
                sides.append(signs[0])
            if len(set(sides))!=1:fail('Inconsistent face winding')
        margin=min(min(v[0]-lo[0],hi[0]-v[0],v[1]-lo[1],hi[1]-v[1]) for v in patch)
        if margin<=0:fail('Contact patch not strictly within support face')
        area=abs(sum(patch[i][0]*patch[(i+1)%len(patch)][1]-patch[(i+1)%len(patch)][0]*patch[i][1] for i in range(len(patch))))/2
        if area<=0:fail('Contact area is empty')
        return {'status':'saved_pose_finite_face_certificate_not_accepted','source_sha256':source_sha,'report_sha256':report_sha,'frame':frame,'participant':participant,'support':support,'participant_face':participant_face,'support_face':support_face,'gap':ratio(F(0)),'minimum_rectangle_edge_margin':ratio(margin),'patch_area':ratio(area),'half_space_nonpenetration':True,'claim':'Explicit observed original mesh faces share finite area on exact horizontal plane; modeled saved pose only','not_claimed':['source_measured_body_contact','whole_scene_safe','trajectory','edit_acceptance','user_acceptance']}
    except (KeyError,TypeError,IndexError,OverflowError) as e:raise WorkflowError('INVALID_REQUEST','Malformed finite-face evidence') from e
