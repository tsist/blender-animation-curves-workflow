"""Explicit in-memory NO_ACTION write. No save, worker, transport, or selection edit."""
import json
from . import practice_gate,practice_plan,practice_state

def create(bpy,rig_name,instance_uuid,frames,knee_start=10.,knee_peak=45.):
 context=bpy.context
 practice_gate.need(context.mode=='OBJECT'and context.scene and not context.scene.library,'Local Object-mode scene required')
 arm=bpy.data.objects.get(rig_name)
 practice_gate.need(arm is not None and arm.type=='ARMATURE'and arm.get('ac_instance_uuid')==instance_uuid,'Explicit rig/UUID mismatch')
 practice_gate.need(context.view_layer.objects.get(rig_name)==arm,'Rig outside current view layer')
 # Full geometry/marker/constraint ownership gate, explicitly permits unsaved/dirty.
 f=list(frames);practice_gate.need(len(f)==3,'Exactly three times')
 w=practice_gate.discover(bpy,instance_uuid,[f[0],f[1],f[1],f[2]])
 practice_gate.need(w['rig']==rig_name,'Explicit rig changed')
 col=bpy.data.collections[w['collection']];meta=json.loads(col['ac_template_metadata'])
 plan=practice_plan.build(meta['params'],f,knee_start,knee_peak)
 saved_context=practice_state.context_state(bpy);saved_ad=practice_state.ad_state(arm.animation_data)
 old_ad=arm.animation_data;pose={p.name:p.matrix_basis.copy()for p in arm.pose.bones};created=None
 try:
  created=bpy.data.actions.new('AC_Practice_'+instance_uuid[:8])
  created['ac_practice_owner']='owned-fk-practice-v1';created['ac_instance_uuid']=instance_uuid
  slot=created.slots.new(id_type='OBJECT',name=arm.name);layer=created.layers.new('FK practice');strip=layer.strips.new(type='KEYFRAME');bag=strip.channelbag(slot,ensure=True)
  for row in plan['curves']:
   pb=arm.pose.bones[row['bone']];path=pb.path_from_id('rotation_euler')
   fc=bag.fcurves.new(data_path=path,index=row['index']);fc.extrapolation='CONSTANT';fc.keyframe_points.add(3)
   for key,t,value in zip(fc.keyframe_points,plan['times'],row['values']):key.co=(t,value);key.interpolation='LINEAR'
   fc.update()
  # Bind only after all keys exist; recheck no external Action appeared during creation.
  practice_gate.need(((old_ad is None and arm.animation_data is None)or(old_ad is not None and arm.animation_data is not None and practice_gate.rna_pointer(arm.animation_data)==practice_gate.rna_pointer(old_ad)and old_ad.action is None)),'Action binding changed')
  arm.animation_data_create();arm.animation_data.action=created;arm.animation_data.action_slot=slot
  arm.animation_data.action_blend_type='REPLACE';arm.animation_data.action_influence=1.
  practice_gate.need(created.users==1,'Practice action must have one user')
  practice_state.verify_action(arm,created,slot,plan)
  practice_gate.need(practice_state.context_state(bpy)==saved_context,'Context changed during practice write')
  return {'action':created.name,'instance_uuid':instance_uuid,'plan':plan,'native_validated':False}
 except BaseException as error:
  cleanup=[]
  try:
   if created is not None:
    practice_gate.need(created.get('ac_practice_owner')=='owned-fk-practice-v1'and created.get('ac_instance_uuid')==instance_uuid,'Created Action ownership changed')
    ad=arm.animation_data
    if ad is not None and ad.action==created:ad.action=None
    practice_gate.need(created.users==0,'Created Action has other users; retained')
    bpy.data.actions.remove(created)
  except BaseException as exc:cleanup.append(repr(exc))
  try:
   practice_state.restore_ad(arm,saved_ad)
  except BaseException as exc:cleanup.append(repr(exc))
  try:
   for name,matrix in pose.items():arm.pose.bones[name].matrix_basis=matrix
   practice_state.restore_context(bpy,saved_context)
   practice_gate.need(all(arm.pose.bones[n].matrix_basis==m for n,m in pose.items()),'Pose rollback readback failed')
  except BaseException as exc:cleanup.append(repr(exc))
  if cleanup:raise RuntimeError('Practice failed: '+repr(error)+'; rollback incomplete: '+repr(cleanup))from error
  raise
