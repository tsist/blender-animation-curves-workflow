"""Opt-in read-only complete rigid-mesh inventory at the actual saved pose.

No frame change, view-layer update, hiding, filtering or evaluated copies.
Hidden meshes are retained and must satisfy the SAME animation/matrix gates.
All meshes are conservatively solid geometry; lifecycle/role exclusions are
not supported. This is not spatial edit acceptance or trajectory validation.
"""
import math
from .contract import WorkflowError
from . import spatial_broadphase

def reject(message,details=None):raise WorkflowError('UNSUPPORTED',message,details)
def conflict(message,details=None):raise WorkflowError('CONFLICT',message,details)
TRANSFORMS={'location','rotation_euler','scale'}
BOOLS={'hide_render','hide_viewport'}
MAX_KEYS=2_000_000

def check_channels(rows,current,evaluated):
    """Exact native current property/evaluation linkage; never resets state."""
    if len(rows)>16:reject('Rigid mesh channel count exceeds16')
    seen=set();bool_paths=set();keys=0
    for row in rows:
        p,i=row['path'],row['index']
        if p not in TRANSFORMS|BOOLS or type(i)is not int or not 0<=i<(1 if p in BOOLS else 3):reject('Unsupported rigid mesh animation channel')
        if (p,i) in seen:reject('Duplicate rigid mesh channel')
        seen.add((p,i))
        if row['muted'] or row['modifiers'] or row['samples']:reject('Modified/muted/sampled rigid curve unsupported')
        if type(row['key_count'])is not int or row['key_count']<1:reject('Empty rigid curve unsupported')
        keys+=row['key_count']
        if keys>MAX_KEYS:reject('Rigid key budget exceeded')
        a,b=current[(p,i)],evaluated[(p,i)]
        finite_values([b])
        if p in BOOLS:
            bool_paths.add(p)
            if type(a)is not bool or b not in (0,1) or row['interpolations']!=['CONSTANT'] or row['bool_values_valid']is not True:reject('Visibility requires native BOOL, exact0/1 CONSTANT keys')
            a=float(a)
        else:finite_values([a])
        if a!=b:conflict('Saved native mesh property differs from animation evaluation; no reset',{'path':p,'index':i,'current':a,'evaluated':b})
    if bool_paths and bool_paths!=BOOLS:reject('Visibility animation requires paired global channels')
    return keys


def finite_values(values):
    for value in values:
        try:ok=type(value) in (int,float) and math.isfinite(value)
        except OverflowError:ok=False
        if not ok:reject('Nonfinite native transform/matrix/vertex')


def extract(scene,frame,hidden_pose_evidence=None,source_topology_reference=None):
    import bpy
    from mathutils import Matrix,Vector
    from . import native
    finite_values([frame])
    if bpy.context.scene is not scene or frame!=float(scene.frame_current)+float(scene.frame_subframe):conflict('Rigid inventory requires current scene/frame')
    native.local(scene)
    if scene.is_evaluated or scene.original!=scene:reject('Expected original scene ID')
    if not 1<=len(scene.objects)<=256:reject('Scene object budget exceeded')
    objects=list(scene.objects)
    if any(o.type not in {'MESH','CAMERA','LIGHT','EMPTY'} for o in objects):reject('Unsupported geometry-producing scene object')
    if any(o.instance_type!='NONE' or o.instance_collection is not None for o in objects):reject('Scene instancing requires expanded inventory')
    meshes=sorted((o for o in objects if o.type=='MESH'),key=lambda o:o.name)
    if not 1<=len(meshes)<=spatial_broadphase.MAX_MESHES:reject('Complete rigid mesh inventory exceeds128')
    hidden_policy=None;topology_identity=None
    if hidden_pose_evidence is None and source_topology_reference is not None:reject('Topology reference requires explicit hidden-pose mode')
    if hidden_pose_evidence is not None:
        from . import hidden_pose_evidence as evidence
        import hashlib
        from pathlib import Path
        if type(source_topology_reference)is not tuple or len(source_topology_reference)!=2:reject('Hidden mode requires explicit opened-source topology bytes and SHA')
        if type(hidden_pose_evidence)is not tuple or len(hidden_pose_evidence)!=2:reject('Expected exact diagnostic bytes and SHA tuple')
        source_path=Path(bpy.data.filepath)
        if not source_path.is_file():reject('Hidden-pose mode requires actual saved input bytes')
        hidden_policy=evidence.parse(hidden_pose_evidence[0],hidden_pose_evidence[1],hashlib.sha256(source_path.read_bytes()).hexdigest(),frame,[o.name for o in meshes if o.hide_render or o.hide_viewport])
    payload={};witnesses={};total_keys=0;prepared=[];material_links=0
    # Reject unsupported structure throughout the scene BEFORE any FCurve.evaluate.
    for o in meshes:
        native.local(o);native.local(o.data)
        if o.is_evaluated or o.original!=o or o.data.is_evaluated or o.data.original!=o.data:reject('Evaluated/non-original mesh IDs unsupported')
        if o.mode!='OBJECT' or o.data.is_editmode:reject('Edit-mode mesh state unsupported')
        if o.parent or o.constraints or o.modifiers or o.rigid_body or o.rigid_body_constraint or o.data.shape_keys or o.data.animation_data is not None:reject('Unsupported mesh deformation/dependency',{'owner':o.name})
        material_links+=len(o.data.materials)+len(o.material_slots)
        if material_links>4096:reject('Aggregate material binding budget exceeded')
        if any(m is not None for m in o.data.materials) or any(slot.material is not None for slot in o.material_slots):reject('Material-dependent geometry is outside this initial inventory backend',{'owner':o.name})
        if o.field is not None and o.field.type!='NONE':reject('Active force-field dependency unsupported',{'owner':o.name})
        if o.rotation_mode!='XYZ' or list(o.delta_location)!=[0,0,0] or list(o.delta_rotation_euler)!=[0,0,0] or list(o.delta_rotation_quaternion)!=[1,0,0,0] or list(o.delta_scale)!=[1,1,1]:reject('Rigid inventory requires XYZ/default delta transforms',{'owner':o.name})
        for field in ('location','rotation_euler','rotation_quaternion','rotation_axis_angle','scale','delta_location','delta_rotation_euler','delta_rotation_quaternion','delta_scale'):finite_values(getattr(o,field))
        for field in ('matrix_world','matrix_local','matrix_basis','matrix_parent_inverse'):finite_values(v for row in getattr(o,field) for v in row)
        if not 4<=len(o.data.vertices)<=64 or not 4<=len(o.data.polygons)<=128:reject('Per-mesh convex budget exceeded',{'owner':o.name})
        if any(not 3<=len(p.vertices)<=64 for p in o.data.polygons):reject('Native polygon corner budget exceeded',{'owner':o.name})
        for v in o.data.vertices:finite_values(v.co)
        action,slot,bag=native.action_binding(o,'OBJECT',writing=False)
        rows=[];current={};curves=[];seen=set()
        if bag:
            if action.is_evaluated or action.original!=action or native.effective_users(action)!=1 or len(bag.fcurves)>16:reject('Shared/non-original Action or excessive channels')
            for c in bag.fcurves:
                p,i=c.data_path,c.array_index
                if p not in TRANSFORMS|BOOLS or type(i)is not int or not 0<=i<(1 if p in BOOLS else 3):reject('Unknown rigid geometry animation address',{'owner':o.name,'path':p})
                if (p,i) in seen:reject('Duplicate native rigid channel')
                seen.add((p,i))
                if c.mute or len(c.modifiers) or len(c.sampled_points):reject('Modified/muted/sampled curves rejected before evaluation')
                if not 1<=len(c.keyframe_points)<=MAX_KEYS-total_keys:reject('Empty curve or aggregate key scan exceeds budget')
                for key in c.keyframe_points:finite_values(key.co)
                interpolations=sorted(set(k.interpolation for k in c.keyframe_points))
                bool_valid=all(k.co.y in (0,1) for k in c.keyframe_points) if p in BOOLS else None
                if p in BOOLS and (interpolations!=['CONSTANT'] or not bool_valid):reject('Visibility curve is not exact0/1 CONSTANT')
                value=getattr(o,p);current[(p,i)]=value if p in BOOLS else value[i]
                rows.append({'path':p,'index':i,'muted':False,'modifiers':0,'samples':0,'key_count':len(c.keyframe_points),'interpolations':interpolations,'bool_values_valid':bool_valid})
                curves.append(c);total_keys+=len(c.keyframe_points)
            visibility={p for p,i in seen if p in BOOLS}
            if visibility and visibility!=BOOLS:reject('Both known visibility channels required; equal schedules not certified')
        prepared.append((o,action,slot,curves,rows,current))
    if hidden_policy is not None:
        current_geometry={o.name:{'vertices':[list(v.co) for v in o.data.vertices],'faces':[list(p.vertices) for p in o.data.polygons]} for o in meshes}
        topology_identity=evidence.verify_topology_reference(source_topology_reference[0],source_topology_reference[1],hidden_policy['source_sha256'],current_geometry)
    for o,action,slot,curves,rows,current in prepared:
        evaluated={(c.data_path,c.array_index):c.evaluate(frame) for c in curves}
        try:check_channels(rows,current,evaluated)
        except WorkflowError as e:raise WorkflowError(e.code,str(e),{'owner':o.name,**e.details}) from e
        expected=Matrix.LocRotScale(Vector(o.location),o.rotation_euler.to_quaternion(),Vector(o.scale))
        finite_values(v for row in expected for v in row)
        matrix_error=max(abs(o.matrix_world[i][j]-expected[i][j]) for i in range(4) for j in range(4))
        reconstructed=hidden_policy is not None and o.name in hidden_policy['owners']
        if matrix_error>1e-6 and not reconstructed:conflict('Saved rigid world matrix differs from validated local transform beyond tolerance',{'owner':o.name,'max_error':matrix_error,'tolerance':1e-6})
        geometry_matrix=expected if reconstructed else o.matrix_world
        vertices=[list(geometry_matrix@v.co) for v in o.data.vertices]
        reconstruction=None
        if reconstructed:
            reconstruction=evidence.verify_owner(hidden_policy,o.name,{'location':list(o.location),'rotation':list(o.rotation_euler),'scale':list(o.scale)},[list(v) for v in o.matrix_world],[list(v) for v in expected],[list(v.co) for v in o.data.vertices],vertices)
        for vertex in vertices:finite_values(vertex)
        payload[o.name]={'vertices':vertices,'faces':[list(p.vertices) for p in o.data.polygons]}
        witnesses[o.name]={'geometry_kind':'reconstructed_conservative_local_trs' if reconstructed else 'cached_native_world_with_local_agreement','reconstruction':reconstruction,'geometry_matrix':[list(v) for v in geometry_matrix],'owner':o.name,'data':o.data.name,'action':action.name if action else None,'slot_identifier':slot.identifier if slot else None,'slot_type':slot.target_id_type if slot else None,'slot_handle':slot.handle if slot else None,'matrix_world':[list(v) for v in o.matrix_world],'matrix_max_error':matrix_error,'matrix_comparison_tolerance':1e-6,'location':list(o.location),'rotation_euler':list(o.rotation_euler),'scale':list(o.scale),'hide_render':o.hide_render,'hide_viewport':o.hide_viewport,'channel_summaries':rows}
    return {'opened_source_topology':topology_identity,'hidden_pose_evidence_sha256':hidden_policy['diagnostic_sha256'] if hidden_policy else None,'status':'rigid_inventory_not_accepted','frame':frame,'mesh_inventory':[o.name for o in meshes],'meshes':payload,'witnesses':witnesses,'native_key_scan_count':total_keys,'visibility_policy':'all_meshes_retained_as_conservative_solid_geometry_no_lifecycle_exclusions','bool_schedule_equality':'not_certified','pending':['convex_collision_kernel','nonmesh_preservation','lifecycle_role_semantics','native_edit_readback','cold_validation','user_acceptance']}
