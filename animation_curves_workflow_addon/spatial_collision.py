"""Bounded pure convex-polyhedron SAT. No scene inventory or trajectory claim."""
import math
from .contract import WorkflowError
from .spatial_candidate import number

MAX_VERTICES = 64
MAX_FACES = 128
MAX_PROJECTION_WORK = 2_000_000


def unsupported(message):
    raise WorkflowError('UNSUPPORTED', message)


def sub(a,b):return tuple(x-y for x,y in zip(a,b))
def dot(a,b):return sum(x*y for x,y in zip(a,b))
def cross(a,b):return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def norm(a):return math.hypot(*a)
def unit(a):
    length=norm(a)
    if length == 0:unsupported('Degenerate direction')
    return tuple(x/length for x in a)


def mesh(vertices, faces):
    if not isinstance(vertices,list) or not 4 <= len(vertices) <= MAX_VERTICES:
        unsupported('Convex mesh vertex budget is 4..64')
    if not isinstance(faces,list) or not 4 <= len(faces) <= MAX_FACES:
        unsupported('Convex mesh face budget is 4..128')
    if any(not isinstance(v,(list,tuple)) or len(v)!=3 for v in vertices):
        unsupported('Mesh vertices must be three-number points')
    vs=[tuple(number(x) for x in v) for v in vertices]
    if len(set(vs))!=len(vs):unsupported('Duplicate mesh vertices')
    extent=max(max(v[i] for v in vs)-min(v[i] for v in vs) for i in range(3))
    tolerance=max(1e-8,extent*1e-10)
    center=tuple(sum(v[i] for v in vs)/len(vs) for i in range(3))
    normals=[]; edges={}; used=set()
    for face in faces:
        if not isinstance(face,list) or not 3<=len(face)<=MAX_VERTICES or any(type(i)is not int or not 0<=i<len(vs) for i in face) or len(set(face))!=len(face):
            unsupported('Malformed polygon indices')
        used.update(face)
        points=[vs[i] for i in face]
        raw=cross(sub(points[1],points[0]),sub(points[2],points[0]))
        if norm(raw)<=tolerance*tolerance:unsupported('Degenerate face')
        normal=unit(raw)
        if any(abs(dot(normal,sub(p,points[0])))>tolerance for p in points):unsupported('Nonplanar face')
        # Every other vertex of a strictly convex polygon must lie on the
        # same interior side of each directed edge. Reject collinear corners.
        winding=[]
        for j,a in enumerate(points):
            b=points[(j+1)%len(points)]
            if norm(sub(b,a))<=tolerance:unsupported('Degenerate edge')
            edge_normal=unit(cross(normal,sub(b,a)))
            distances=[dot(edge_normal,sub(p,a)) for k,p in enumerate(points) if k not in (j,(j+1)%len(points))]
            if not (all(d>tolerance for d in distances) or all(d < -tolerance for d in distances)):
                unsupported('Nonconvex or crossing face')
            winding.append(distances[0]>0)
            key=tuple(sorted((face[j],face[(j+1)%len(face)])))
            edges[key]=edges.get(key,0)+1
        if len(set(winding))!=1:unsupported('Inconsistent polygon order')
        if dot(normal,sub(center,points[0]))>0:normal=tuple(-x for x in normal)
        if dot(normal,sub(center,points[0])) >= -tolerance:
            unsupported('Zero-volume or numerically thin mesh')
        if any(dot(normal,sub(p,points[0]))>tolerance for p in vs):unsupported('Mesh is not convex')
        normals.append(normal)
    if used!=set(range(len(vs))) or any(n!=2 for n in edges.values()) or len(vs)-len(edges)+len(faces)!=2:
        unsupported('Mesh must be a closed single convex boundary')
    return {'vertices':vs,'normals':normals,'edges':[unit(sub(vs[b],vs[a])) for a,b in edges], 'tolerance':tolerance}


def compare(vertices_a,faces_a,vertices_b,faces_b):
    """Return separation or SAT overlap, never Euclidean clearance.

    Parallel cross axes alone may be skipped. Near-parallel nonzero axes are
    retained, avoiding an unsound arbitrary angular cutoff.
    """
    a,b=mesh(vertices_a,faces_a),mesh(vertices_b,faces_b)
    return _compare_prepared(a,b)


def work_bound(a,b):
    return (len(a['normals'])+len(b['normals'])+len(a['edges'])*len(b['edges']))*(len(a['vertices'])+len(b['vertices']))


def _compare_prepared(a,b):
    if work_bound(a,b)>MAX_PROJECTION_WORK:unsupported('SAT projection work budget exceeded')
    tol=max(a['tolerance'],b['tolerance'])
    for i in range(3):
        gap=max(min(v[i] for v in a['vertices'])-max(v[i] for v in b['vertices']),
                min(v[i] for v in b['vertices'])-max(v[i] for v in a['vertices']))
        if gap>tol:
            axis=[0.,0.,0.];axis[i]=1.
            return {'relation':'separated','separating_axis':axis,'projected_gap':gap,'tolerance':tol,'claim':'single_pose_convex_sat_only','broad_phase':'aabb'}
    axes=list(a['normals'])+list(b['normals'])
    for ea in a['edges']:
        for eb in b['edges']:
            axis=cross(ea,eb)
            if norm(axis)!=0:axes.append(unit(axis))
    least=float('inf')
    # Shift the shared projection origin to reduce cancellation far from zero.
    origin=a['vertices'][0]
    for axis in axes:
        pa=[dot(sub(v,origin),axis) for v in a['vertices']]
        pb=[dot(sub(v,origin),axis) for v in b['vertices']]
        overlap=min(max(pa),max(pb))-max(min(pa),min(pb))
        if overlap < -tol:
            return {'relation':'separated','separating_axis':list(axis),'projected_gap':-overlap,'tolerance':tol,'claim':'single_pose_convex_sat_only'}
        least=min(least,overlap)
    return {'relation':'touching_within_tolerance' if least<=tol else 'overlapping', 'minimum_projection_overlap':least,'tolerance':tol,'claim':'single_pose_convex_sat_only'}


def inventory_report(scene_mesh_names, meshes, participants):
    """Reconcile a complete native mesh-name snapshot with explicit world meshes.

    No exclusion mechanism in v1. Check every pair involving the group,
    including group-internal pairs. Static-static geometry is outside the claim.
    Touching remains reported for separate finite-face acceptance, never waived.
    Native caller must reject unsupported dependency stacks before extraction.
    """
    def names(value,label):
        if not isinstance(value,list) or any(not isinstance(x,str) or not x for x in value) or len(set(value))!=len(value):
            raise WorkflowError('INVALID_REQUEST', label+' must be unique nonempty names')
        return set(value)
    observed=names(scene_mesh_names,'Scene mesh inventory')
    group=names(participants,'Participants')
    if not 1<=len(group)<=8 or not 1<=len(observed)<=64:unsupported('Scene/participant budget exceeded')
    if not group <= observed:unsupported('Participant absent from scene inventory')
    if not isinstance(meshes,dict) or set(meshes)!=observed:unsupported('Explicit meshes do not exactly cover scene inventory')
    prepared={}
    for name in sorted(observed):
        record=meshes[name]
        if not isinstance(record,dict) or set(record)!={'vertices','faces'}:unsupported('Unexpected mesh payload')
        prepared[name]=mesh(record['vertices'],record['faces'])
    pairs=[]
    ordered=sorted(observed)
    selected=[(a,b) for index,a in enumerate(ordered) for b in ordered[index+1:] if a in group or b in group]
    total_work=sum(work_bound(prepared[a],prepared[b]) for a,b in selected)
    if total_work>MAX_PROJECTION_WORK:unsupported('Total pose SAT projection work budget exceeded')
    for index,a in enumerate(ordered):
        for b in ordered[index+1:]:
            if a not in group and b not in group:continue
            result=_compare_prepared(prepared[a],prepared[b])
            pairs.append({'a':a,'b':b,'internal_group':a in group and b in group,**result})
    return {'status':'collision_diagnostics_not_accepted','inventory':ordered,'participants':sorted(group),
            'pairs':pairs,'excluded_meshes':[], 'projection_work_upper_bound':total_work, 'projection_work_budget':MAX_PROJECTION_WORK, 'scope':'single_pose_group_pairs_including_internal',
            'pending':['native_inventory_validation','finite_support_touch_validation','preview_acceptance','cold_validation']}
