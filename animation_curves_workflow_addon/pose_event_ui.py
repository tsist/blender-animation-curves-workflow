"""Explicit parameters -> frozen two-owner preview -> one Undo commit."""
import json,bpy
from bpy.props import StringProperty,FloatProperty
from . import pose_event
_REGISTERED_CLASSES=[]
class ACW_OT_pose_event_parameters(bpy.types.Operator):
 bl_idname='animation_curves.pose_event_parameters';bl_label='两个角色转头键同步改时（本次请求）'
 rig_a:StringProperty(name='角色A骨架',default='M3.A.Rig')
 rig_b:StringProperty(name='角色B骨架',default='M3.B.Rig')
 frame_a:FloatProperty(name='A源键帧',default=73)
 frame_b:FloatProperty(name='B源键帧',default=133)
 delta:FloatProperty(name='共同平移帧数',default=6)
 event:StringProperty(name='本次事件名',default='双方转头')
 def invoke(self,context,event):return context.window_manager.invoke_props_dialog(self,width=580,confirm_text='生成最终预览')
 def draw(self,context):
  for k in ('event','rig_a','rig_b','frame_a','frame_b','delta'):self.layout.prop(self,k)
  self.layout.label(text='仅两条head.Y；其余键锁定；不建立持久事件图')
 def execute(self,context):
  try:
   params={k:getattr(self,k)for k in ('event','rig_a','rig_b','frame_a','frame_b','delta')}
   expected=pose_event.origin(context,params)
   return bpy.ops.animation_curves.pose_event_confirm('INVOKE_DEFAULT',frozen=json.dumps(expected,ensure_ascii=False,allow_nan=False))
  except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
class ACW_OT_pose_event_confirm(bpy.types.Operator):
 bl_idname='animation_curves.pose_event_confirm';bl_label='确认两角色同步改时 · 全部非目标键锁定';bl_options={'REGISTER','UNDO'}
 frozen:StringProperty(options={'HIDDEN'})
 def invoke(self,context,event):
  try:
   expected=json.loads(self.frozen)
   if pose_event.origin(context,expected['params'])!=expected:raise ValueError('预览已失效')
   return context.window_manager.invoke_props_dialog(self,width=650,confirm_text='复制两个动作并同步改时')
  except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
 def draw(self,context):
  expected=json.loads(self.frozen);p=expected['params'];l=self.layout
  l.label(text='本次事件：'+p['event'])
  for i,side in enumerate(('a','b')):
   frozen=expected['rigs'][i]
   name=next(n for n,v in expected['global_origin']['actions'].items()if v['ptr']==frozen['action_ptr'])
   slot=frozen['action']['slots'][0][1]
   curves=frozen['action']['layers'][0]['strips'][0]['bags'][0][1]
   curve=next(c for c in curves if c['data_path']==pose_event.PATH and c['array_index']==1)
   times=[k['co'][0]for k in curve['keys']];ix=times.index(p['frame_'+side])
   l.label(text=f'Action: {name} / slot: {slot}')
   l.label(text=f'相邻锁键 {times[ix-1]:g} / {times[ix+1]:g}；端点 {times[0]:g} / {times[-1]:g}')
   l.label(text=f"{p['rig_'+side]} / head.Y：{p['frame_'+side]:g} → {p['frame_'+side]+p['delta']:g}")
  l.label(text=f"保留相对时差 {p['frame_b']-p['frame_a']:g} 帧；其他所有键/Bezier不改")
  l.label(text='只同步显式两键，不等于空间接触/音轨锁定或邻区间形状保持')
  l.label(text='原动作保护在当前工程内，保存后持久；不自动保存')
  l.label(text='Undo恢复旧动作求值；可能丢失已动画骨未打键手动输入')
  l.label(text='建议先另存副本；Redo恢复本次改时。取消不改工程。')
 def execute(self,context):
  try:pose_event.execute(context,json.loads(self.frozen));self.report({'INFO'},'两角色同步改时完成');return {'FINISHED'}
  except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
class ACW_PT_pose_event(bpy.types.Panel):
 bl_label='双角色转头事件 · 显式同步';bl_idname='ACW_PT_pose_event';bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='动画曲线'
 def draw(self,context):self.layout.operator(ACW_OT_pose_event_parameters.bl_idname)
def register():
 for c in (ACW_OT_pose_event_parameters,ACW_OT_pose_event_confirm,ACW_PT_pose_event):bpy.utils.register_class(c);_REGISTERED_CLASSES.append(c)
def unregister():
 while _REGISTERED_CLASSES:bpy.utils.unregister_class(_REGISTERED_CLASSES.pop())
