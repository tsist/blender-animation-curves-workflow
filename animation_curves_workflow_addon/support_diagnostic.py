"""Isolated read-only diagnostic experiment, not installed plugin or solver."""
import math,json
from . import pose_gate,pose_channel as pc,root_input
from .leg_contract import signed_flexion

def need(ok,s):
 if not ok:raise ValueError(s)
def uniform(m,eps=1e-6):
 c=[m.to_3x3().col[i]for i in range(3)];ls=[v.length for v in c]
 need(all(math.isfinite(v)and v>0 for v in ls),'invalid world scale')
 need(m.to_3x3().determinant()>0 and max(ls)-min(ls)<eps*max(ls)and all(abs(c[i].dot(c[j]))<eps*ls[i]*ls[j]for i in range(3)for j in range(i)),'nonuniform/shear/mirror '+repr((ls,[c[i].dot(c[j])for i in range(3)for j in range(i)])))
 return sum(ls)/3

def state(bpy):
 s=bpy.context.scene
 return {'frame':s.frame_current,'subframe':s.frame_subframe,'dirty':bpy.data.is_dirty,'active':bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,'selected':[o.name for o in bpy.context.selected_objects],'mode':bpy.context.mode,'actions':{a.name:(pc.action_witness(a),a.use_fake_user)for a in bpy.data.actions},'poses':{o.name:[(p.name,list(p.location),list(p.rotation_euler),list(p.scale))for p in o.pose.bones]for o in bpy.data.objects if o.type=='ARMATURE'},'roots':{o.name:root_input.witness(o,bpy.context)for o in bpy.data.objects if o.type=='EMPTY'and o.get('ac_instance_uuid')}}

def inspect(bpy,uid,side,start,end,plane_z_m=0.,step=1/32,checkpoint=None):
 need(bpy.app.background,'Background private-process experiment only; no live GUI use')
 need(side in ('L','R'),'side')
 need(all(type(x)in(int,float)and math.isfinite(x)for x in (start,end,plane_z_m,step)),'finite parameters')
 need(-1e6<=start<end<=1e6 and step==1/32,'bounded supported interval/step')
 count=(end-start)/step;need(count.is_integer()and count+1<=4097,'sample budget/exact grid')
 w=pose_gate.discover(bpy,uid,allow_root_location=True);arm=w['rig'];root=w['root'];scene=bpy.context.scene
 unit=scene.unit_settings.scale_length;fps=scene.render.fps/scene.render.fps_base
 need(math.isfinite(unit)and unit>0 and math.isfinite(fps)and fps>0,'units/fps');need(w['metadata']['meters_per_blender_unit']==unit,'unit mismatch')
 # LINEAR only in this experiment: no misleading sampling across CONSTANT jumps.
 for obj in (root,arm):
  if obj.animation_data and obj.animation_data.action:
   a=obj.animation_data.action;slot=obj.animation_data.action_slot;bag=a.layers[0].strips[0].channelbag(slot)
   for fc in bag.fcurves:
    need(not fc.modifiers and not fc.sampled_points,'curve effect')
    need(all(k.interpolation=='LINEAR'and k.handle_left_type==k.handle_right_type=='FREE'for k in fc.keyframe_points),'only LINEAR FREE sampled input')
    if obj==arm:need(fc.data_path.startswith('pose.bones[')and fc.data_path.endswith('].rotation_euler')and fc.array_index in (0,1,2),'pose rotation input only')
 mesh=next(o for o in bpy.data.objects if o.parent==arm and o.get('ac_part')=='shin.'+side)
 need(len(mesh.data.vertices)==24,'distal topology');ids=list(range(12,24))
 # pose_gate validates all original vertices, faces, exact weights and modifier target.
 before=state(bpy);scale=uniform(arm.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world);H=w['metadata']['height_blender_units']*unit*scale
 limits={'drift_m':.002*H,'height_m':.001*H,'speed_m_s':.01*H,'path_m':.004*H};rows=[]
 try:
  for i in range(int(count)+1):
   t=start+i*step;scene.frame_set(math.floor(t),subframe=t-math.floor(t))
   if checkpoint:checkpoint('sample_'+str(i))
   dg=bpy.context.evaluated_depsgraph_get();er=arm.evaluated_get(dg)
   need(abs(uniform(er.matrix_world)-scale)<1e-6*scale,'scale changed')
   th=er.pose.bones['thigh.'+side];sh=er.pose.bones['shin.'+side]
   for p in (th,sh):need(abs(uniform(p.matrix,1e-5)-1)<1e-5,'nonrigid bone')
   angle=signed_flexion(er.matrix_world@th.head,er.matrix_world@sh.head,er.matrix_world@sh.tail,er.matrix_world.to_3x3()@th.matrix.to_3x3().col[0])
   eo=mesh.evaluated_get(dg);m=eo.to_mesh()
   try:
    need(len(m.vertices)==24 and [tuple(p.vertices)for p in m.polygons]==[tuple(p.vertices)for p in mesh.data.polygons],'evaluated topology changed')
    vertices=[[float(x)*unit for x in eo.matrix_world@v.co]for v in m.vertices];need(all(math.isfinite(x)for p in vertices for x in p),'nonfinite geometry');cap=[vertices[j]for j in ids]
    rows.append({'t':t,'cap':cap,'centroid':[sum(p[j]for p in cap)/12 for j in range(3)],'cap_min_z':min(p[2]-plane_z_m for p in cap),'cap_max_z':max(p[2]-plane_z_m for p in cap),'shin_min_z':min(p[2]-plane_z_m for p in vertices),'lowest_id':min(range(24),key=lambda j:vertices[j][2]),'physical_knee_deg':angle})
   finally:eo.to_mesh_clear()
 finally:scene.frame_set(before['frame'],subframe=before['subframe'])
 need(state(bpy)==before,'read-only state restoration mismatch');pose_gate.discover(bpy,uid,allow_root_location=True)
 initial=rows[0]['cap'];paths=[0.]*12;drift=speed=0.
 for i,r in enumerate(rows):
  for j,p in enumerate(r['cap']):
   drift=max(drift,math.dist(p[:2],initial[j][:2]))
   if i:
    d=math.dist(p[:2],rows[i-1]['cap'][j][:2]);paths[j]+=d;speed=max(speed,d*fps/step)
 mn=min(r['cap_min_z']for r in rows);mx=max(r['cap_max_z']for r in rows);penetration=min(r['shin_min_z']for r in rows);reasons=[]
 if drift>limits['drift_m']:reasons.append('SLIDING')
 if max(paths)>limits['path_m']:reasons.append('PATH_LENGTH')
 if speed>limits['speed_m_s']:reasons.append('TANGENTIAL_SPEED')
 if mx>limits['height_m']:reasons.append('CAP_LIFT')
 if mn < -limits['height_m']or penetration < -limits['height_m']:reasons.append('PENETRATION')
 if any(not -1e-3<=r['physical_knee_deg']<=150.001 for r in rows):reasons.append('KNEE_RANGE')
 return {'status':'SAMPLED_SUPPORT_FAIL'if reasons else'SAMPLED_SUPPORT_PASS','reasons':reasons,'height_m':H,'unit_m':unit,'world_scale':scale,'limits':limits,'fixed_ids':ids,'max_drift_m':drift,'max_path_m':max(paths),'peak_speed_m_s':speed,'cap_range_m':[mn,mx],'shin_min_z_m':penetration,'rows':rows,'state_restored':True,'continuous_proof':False,'may_write':False}
