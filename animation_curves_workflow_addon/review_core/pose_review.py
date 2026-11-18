"""Four-time evaluated pose review; changes transient frame only, restores in finally.
No save, render, curve write, support solve, or apply capability.
"""
import math,hashlib,json
from .spec.leg_contract import signed_flexion

class Rejected(ValueError):pass
def need(ok,msg):
 if not ok:raise Rejected(msg)
def finite_vec(v):
 v=list(v);need(len(v)==3 and all(type(x) in (int,float) and math.isfinite(x) for x in v),'finite vector');return v
def rigid(matrix):
 rows=[list(r) for r in matrix]
 need(len(rows)==4 and all(len(r)==4 for r in rows) and all(math.isfinite(x) for r in rows for x in r),'finite transform')
 need(all(abs(a-b)<1e-5 for a,b in zip(rows[3],(0,0,0,1))),'affine transform')
 cols=[[rows[r][c] for r in range(3)] for c in range(3)]
 for i in range(3):
  for j in range(3):need(abs(sum(a*b for a,b in zip(cols[i],cols[j]))-(1 if i==j else 0))<1e-5,'scale/shear unsupported')
 a,b,c=cols;det=a[0]*(b[1]*c[2]-b[2]*c[1])-b[0]*(a[1]*c[2]-a[2]*c[1])+c[0]*(a[1]*b[2]-a[2]*b[1])
 need(abs(det-1)<1e-5,'mirror unsupported')
def knee(hip,upper_tail,lower_head,end,axis):
 hip,upper_tail,lower_head,end,axis=map(finite_vec,(hip,upper_tail,lower_head,end,axis))
 need(max(abs(a-b) for a,b in zip(upper_tail,lower_head))<1e-5,'disconnected evaluated knee')
 angle=signed_flexion(hip,lower_head,end,axis)
 need(-1e-4<=angle<=150+1e-4,'evaluated flexion outside 0..150')
 return {'signed_flexion_deg':angle,'hip':hip,'knee':lower_head,'end':end,'positive_axis':axis}
def summarize(samples,both_no_action):
 need(bool(samples),'no samples')
 encoded=[json.dumps(s['evaluated'],sort_keys=True,allow_nan=False) for s in samples]
 # No-action branch requires actual identical geometry, never infers from rig only.
 if both_no_action:need(all(v==encoded[0] for v in encoded),'NO_ACTION evaluated geometry changed')
 return 'STATIC_EVALUATED_AT_SAMPLES' if both_no_action else 'ANIMATED_SAMPLES_EVALUATED'
def evaluate(bpy,witness,discover):
 from mathutils import Vector
 need(witness.get('may_apply') is False and witness.get('structure_recognized') is True,'review witness')
 current=discover(bpy,witness['instance_uuid'],witness['times']);need(current==witness,'discovery drift')
 scene=bpy.context.scene;layer=bpy.context.view_layer;window=bpy.context.window
 frame,sub=scene.frame_current,scene.frame_subframe
 selected=tuple(o.name for o in layer.objects if o.select_get());active=layer.objects.active
 samples=[]
 try:
  for t in dict.fromkeys(witness['times']):
   scene.frame_set(math.floor(t),subframe=t-math.floor(t))
   need(bpy.context.scene==scene and bpy.context.view_layer==layer,'context drift')
   dg=bpy.context.evaluated_depsgraph_get();rig=bpy.data.objects[witness['rig']];er=rig.evaluated_get(dg)
   rigid(er.matrix_world);rigid(bpy.data.objects[witness['root']].evaluated_get(dg).matrix_world)
   legs={};authored={}
   for side in ('L','R'):
    thigh=er.pose.bones['thigh.'+side];shin=er.pose.bones['shin.'+side];rigid(thigh.matrix);rigid(shin.matrix)
    world=er.matrix_world
    axis=world.to_3x3() @ thigh.matrix.to_3x3() @ Vector((1,0,0))
    legs[side]=knee(world@thigh.head,world@thigh.tail,world@shin.head,world@shin.tail,axis)
    authored[side]={}
    for name in ('thigh.'+side,'shin.'+side):
     pb=rig.pose.bones[name];authored[side][name]={'rotation_mode':pb.rotation_mode,'euler':list(pb.rotation_euler),'quaternion':list(pb.rotation_quaternion),'axis_angle':list(pb.rotation_axis_angle),'location':list(pb.location),'scale':list(pb.scale)}
   meshes={}
   for part,name in witness['parts'].items():
    eo=bpy.data.objects[name].evaluated_get(dg);rigid(eo.matrix_world);mesh=None
    try:
     mesh=eo.to_mesh();need(mesh is not None and len(mesh.vertices)>0,'missing evaluated mesh')
     verts=[finite_vec(eo.matrix_world@v.co) for v in mesh.vertices]
     row={'vertices':verts,'bbox_min':[min(v[i] for v in verts) for i in range(3)],'bbox_max':[max(v[i] for v in verts) for i in range(3)]}
     if part in ('shin.L','shin.R'):
      need(len(verts)==24,'shin cap topology');cap=verts[len(verts)//2:]
      row['distal_cap_min_z']=min(v[2] for v in cap);row['distal_cap_max_z']=max(v[2] for v in cap)
     meshes[part]=row
    finally:
     if mesh is not None:eo.to_mesh_clear()
   samples.append({'time':t,'authored_channels':authored,'evaluated':{'legs':legs,'meshes':meshes}})
 finally:
  scene.frame_set(frame,subframe=sub)
  need(bpy.context.scene==scene and bpy.context.view_layer==layer and bpy.context.window==window,'context not restored')
  need(scene.frame_current==frame and abs(scene.frame_subframe-sub)<1e-7,'frame restore')
  need(tuple(o.name for o in layer.objects if o.select_get())==selected and layer.objects.active==active,'selection changed')
 need(discover(bpy,witness['instance_uuid'],witness['times'])==witness,'post discovery drift')
 both=all(witness['bindings'][key]['status']=='NO_ACTION' for key in ('root','rig'))
 state=summarize(samples,both)
 return {'status':'SAMPLED_POSE_REVIEW_ONLY','motion_status':state,'samples':samples,'may_apply':False,'may_write_keys':False,'continuous_interval_qualified':False,'contact_qualified':False,'ground_assessed':False,'frame_restored':True,'view_layer_unchanged':True}
