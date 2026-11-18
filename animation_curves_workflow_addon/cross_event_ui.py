"""Explicit capture and resynchronization of one saved two-rig event."""
import bpy,json
from bpy.props import StringProperty,FloatProperty
from . import cross_event_store as store
_REGISTERED_CLASSES=[]
class ACW_OT_cross_record_parameters(bpy.types.Operator):
 bl_idname='animation_curves.cross_record_parameters';bl_label='捕获/替换双角色灯光事件记录'
 light_object:StringProperty(name='灯光对象',default='CueObject')
 light_frame:FloatProperty(name='灯源键',default=73)
 def invoke(self,context,event):return context.window_manager.invoke_props_dialog(self,width=620,confirm_text='预览捕获内容')
 def draw(self,context):
  for k in ('light_object','light_frame'):self.layout.prop(self,k)
  self.layout.label(text='从当前有效双人记录关联默认灯energy键；不修改动画')
 def execute(self,context):
  try:
   expected=store.capture_origin(context,self.light_object,self.light_frame)
   return bpy.ops.animation_curves.cross_record_confirm('INVOKE_DEFAULT',frozen=json.dumps(expected,ensure_ascii=False,allow_nan=False))
  except Exception as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}
class ACW_OT_cross_record_confirm(bpy.types.Operator):
 bl_idname='animation_curves.cross_record_confirm';bl_label='确认保存事件关联（不改动画）';bl_options={'REGISTER','UNDO'}
 frozen:StringProperty(options={'HIDDEN'})
 def invoke(self,context,event):
  try:
   exp=json.loads(self.frozen)
   if store.capture_origin(context,exp['snapshot']['params']['light_object'],exp['snapshot']['params']['light_frame'])!=exp:raise ValueError('捕获预览已失效')
   return context.window_manager.invoke_props_dialog(self,width=700,confirm_text='确认捕获记录')
  except Exception as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}
 def draw(self,context):
  exp=json.loads(self.frozen);l=self.layout
  l.label(text='事件：'+exp['snapshot']['params']['event'])
  for r in exp['snapshot']['rows']:l.label(text=f"{r['object']} / {r['action']} / {r['slot'][0]} / {r['path']}[{r['index']}] @{r['frame']:g}")
  if exp['raw']:
   l.label(text=exp['prior_status'])
   old=store.parse(exp['raw']);l.label(text='将替换现有记录：'+old['event']+'；不是自动追踪/重绑')
   l.label(text='现有记录的所有关联将被本次显式选择替代；原生动画不改')
  l.label(text='记录只在保存工程后持久；未知格式记录不会被覆盖')
 def execute(self,context):
  try:store.capture(context,json.loads(self.frozen));self.report({'INFO'},'事件已捕获，请保存工程以持久');return {'FINISHED'}
  except Exception as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}
class ACW_OT_cross_record_inspect(bpy.types.Operator):
 bl_idname='animation_curves.cross_record_inspect';bl_label='检查已保存关联'
 def execute(self,context):
  try:
   r,_=store.inspect(context);self.report({'INFO'},f"关联有效：{r['event']} revision {r['revision']}");return {'FINISHED'}
  except Exception as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}
class ACW_OT_cross_record_shift(bpy.types.Operator):
 bl_idname='animation_curves.cross_record_shift';bl_label='接续已保存事件改时'
 delta:FloatProperty(name='共同平移帧数',default=6)
 def invoke(self,context,event):return context.window_manager.invoke_props_dialog(self,width=620,confirm_text='生成最终预览')
 def draw(self,context):self.layout.prop(self,'delta');self.layout.label(text='两head.Y与灯energy取自记录；旧双人记录不随动更新')
 def execute(self,context):
  try:return bpy.ops.animation_curves.cross_record_shift_confirm('INVOKE_DEFAULT',frozen=json.dumps(store.sync_origin(context,self.delta),ensure_ascii=False,allow_nan=False))
  except Exception as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}
class ACW_OT_cross_record_shift_confirm(bpy.types.Operator):
 bl_idname='animation_curves.cross_record_shift_confirm';bl_label='确认三动作与事件记录共同改时';bl_options={'REGISTER','UNDO'}
 frozen:StringProperty(options={'HIDDEN'})
 def invoke(self,context,event):
  try:
   exp=json.loads(self.frozen)
   if store.sync_origin(context,exp['edit']['params']['delta'])!=exp:raise ValueError('事件预览已失效')
   return context.window_manager.invoke_props_dialog(self,width=750,confirm_text='复制三动作并更新事件记录')
  except Exception as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}
 def draw(self,context):
  exp=json.loads(self.frozen);r=exp['record'];d=exp['edit']['params']['delta'];l=self.layout
  l.label(text=f"{r['event']} / revision {r['revision']} → {r['revision']+1}")
  for row in r['rows']:
   l.label(text=f"{row['object']} / {row['action']} / {row['slot'][0]}")
   l.label(text=f"{row['path']}[{row['index']}] {row['frame']:g} → {row['frame']+d:g}")
   cs=row['witness']['layers'][0]['strips'][0]['bags'][0][1]
   c=next(x for x in cs if x['data_path']==row['path'] and x['array_index']==row['index'])
   times=[k['co'][0]for k in c['keys']];ix=times.index(row['frame'])
   l.label(text=f'相邻锁键 {times[ix-1]:g} / {times[ix+1]:g}；其余键/Bezier不改')
  l.label(text=f"保持B/灯相对A偏移 {r['offsets']}；不保证邻区间形状不变")
  l.label(text='Undo恢复三绑定、记录及旧动作求值，可能丢失已动画通道未打键输入')
  l.label(text='建议先另存；不自动保存。没有音乐/空间接触锁定')
 def execute(self,context):
  try:store.execute(context,json.loads(self.frozen));self.report({'INFO'},'三动作与记录已同步，请保存工程');return {'FINISHED'}
  except Exception as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}
class ACW_PT_cross_record(bpy.types.Panel):
 bl_label='双角色灯光事件 · 保存与接续';bl_idname='ACW_PT_cross_record';bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='动画曲线'
 def draw(self,context):
  l=self.layout
  for cls in (ACW_OT_cross_record_parameters,ACW_OT_cross_record_inspect,ACW_OT_cross_record_shift):l.operator(cls.bl_idname)
  l.label(text='已有Cue时禁止旧入口改写；Inspect仍可用')
def register():
 for c in (ACW_OT_cross_record_parameters,ACW_OT_cross_record_confirm,ACW_OT_cross_record_inspect,ACW_OT_cross_record_shift,ACW_OT_cross_record_shift_confirm,ACW_PT_cross_record):bpy.utils.register_class(c);_REGISTERED_CLASSES.append(c)
def unregister():
 while _REGISTERED_CLASSES:bpy.utils.unregister_class(_REGISTERED_CLASSES.pop())
