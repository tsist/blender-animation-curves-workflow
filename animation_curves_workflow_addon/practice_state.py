"""Narrow writer transaction state; preserves empty AnimData and UI context."""
import struct
from .practice_gate import need,rna_pointer
FIELDS=('action_blend_type','action_extrapolation','action_influence','use_nla','action_slot_handle','last_slot_identifier')
def ad_state(ad):
 return None if ad is None else {'pointer':rna_pointer(ad),'slot':ad.action_slot,'settings':{k:getattr(ad,k)for k in FIELDS if hasattr(ad,k)}}
def restore_ad(arm,before):
 ad=arm.animation_data
 if before is None:
  if ad is not None:
   need(ad.action is None and not ad.drivers and not ad.nla_tracks,'Unexpected animation data; retained');arm.animation_data_clear()
 else:
  need(ad is not None and rna_pointer(ad)==before['pointer']and ad.action is None,'Original empty AnimData changed')
  if ad.action_slot!=before['slot']:ad.action_slot=before['slot']
  for k,v in before['settings'].items():
   if getattr(ad,k)!=v:setattr(ad,k,v)
 need(ad_state(arm.animation_data)==before,'AnimData rollback readback failed')
def context_state(bpy):
 c=bpy.context
 return {'frame':c.scene.frame_current,'subframe':c.scene.frame_subframe,'selected':tuple(sorted(o.name for o in c.selected_objects)),'active':c.view_layer.objects.active.name if c.view_layer.objects.active else None}
def restore_context(bpy,state):
 if context_state(bpy)!=state:
  c=bpy.context
  c.scene.frame_set(state['frame'],subframe=state['subframe'])
  for o in c.view_layer.objects:o.select_set(o.name in state['selected'])
  c.view_layer.objects.active=c.view_layer.objects.get(state['active'])if state['active']else None
 need(context_state(bpy)==state,'Context rollback readback failed')
def f32(x):return struct.unpack('f',struct.pack('f',x))[0]
def verify_action(arm,a,slot,plan):
 need(arm.animation_data.action==a and arm.animation_data.action_slot==slot and len(a.slots)==1 and a.slots[0]==slot and slot.target_id_type=='OBJECT','Practice Action/slot readback')
 need(len(a.layers)==1 and len(a.layers[0].strips)==1,'Practice layer/strip readback');strip=a.layers[0].strips[0];bag=strip.channelbag(slot)
 need(strip.type=='KEYFRAME'and len(strip.channelbags)==1 and bag is not None and len(bag.fcurves)==5,'Practice channelbag readback')
 seen=set()
 for row in plan['curves']:
  path=arm.pose.bones[row['bone']].path_from_id('rotation_euler');matches=[fc for fc in bag.fcurves if fc.data_path==path and fc.array_index==row['index']];need(len(matches)==1,'Practice curve identity readback');fc=matches[0];need(not fc.modifiers and not fc.sampled_points and len(fc.keyframe_points)==3 and fc.extrapolation=='CONSTANT','Practice curve shape readback')
  for k,t,v in zip(fc.keyframe_points,plan['times'],row['values']):need(tuple(k.co)==(f32(t),f32(v))and k.interpolation=='LINEAR','Practice key float32 readback')
