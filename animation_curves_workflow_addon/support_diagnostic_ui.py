"""Explicit clean-source support diagnostic; live scene never sampled."""
import bpy,json
from bpy.props import FloatProperty,StringProperty,EnumProperty
from .read_job import JOB
from . import support_ui,read_ui_cleanup as cleanup,support_diagnostic_service as ds
class ACW_OT_support_diagnostic(bpy.types.Operator):
 bl_idname='animation_curves.support_diagnostic';bl_label='只读检查小腿端盖支撑区间'
 side:EnumProperty(name='支撑侧',items=[('L','左小腿',''),('R','右小腿','')],default='L')
 start:FloatProperty(name='支撑开始帧',default=1)
 end:FloatProperty(name='支撑结束帧',default=25)
 plane:FloatProperty(name='固定水平面世界Z（米）',default=0)
 output:StringProperty(name='现有私有报告目录',subtype='DIR_PATH')
 expected:StringProperty(options={'HIDDEN'})
 @classmethod
 def poll(cls,c):return JOB.state=='EMPTY'and c.mode=='OBJECT'
 def invoke(self,c,event):
  try:
   request=ds.freeze(bpy,{'side':self.side,'start':self.start,'end':self.end,'plane_z_m':self.plane},'0'*32)
   self.expected=json.dumps({'source':request['source'],'sha':request['source_sha256'],'witness':request['witness']},sort_keys=True)
  except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
  return c.window_manager.invoke_props_dialog(self,width=650)
 def draw(self,c):
  l=self.layout;l.label(text='只读：已保存干净工程→私有副本，不读取未保存输入')
  for p in('side','start','end','plane','output'):l.prop(self,p)
  l.label(text='只支持静态水平平面上的平盖支撑；无脚/无IK/不修复滑移')
  l.label(text='固定1/32帧，最多4097点；LINEAR/FREE；非连续时间证明')
  l.label(text='阈值按角色高度H：漂移0.002H；离面/穿地0.001H')
  l.label(text='路程0.004H；相邻采样平均速度0.01H/秒，不是瞬时峰值')
 def execute(self,c):
  try:
   opts={'side':self.side,'start':self.start,'end':self.end,'plane_z_m':self.plane};r=ds.freeze(bpy,opts,'0'*32)
   if json.loads(self.expected)!={'source':r['source'],'sha':r['source_sha256'],'witness':r['witness']}:raise ValueError('源/角色/姿态已变化，请重新打开')
   JOB.prepare(bpy,(),bpy.path.abspath(self.output),diagnostic=opts);JOB.start()
   if not bpy.app.timers.is_registered(support_ui.tick):bpy.app.timers.register(support_ui.tick,first_interval=.25)
  except Exception as e:self.report({'ERROR'},cleanup.failure(JOB,bpy.app.timers,support_ui.tick,e));return {'CANCELLED'}
  self.report({'INFO'},'已启动私有副本只读采样；不会修改动作');return {'FINISHED'}
class ACW_PT_support_diagnostic(bpy.types.Panel):
 bl_idname='ACW_PT_support_diagnostic';bl_label='小腿端盖支撑 · 只读采样'
 bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='动画曲线'
 def draw(self,c):
  l=self.layout;l.operator('animation_curves.support_diagnostic');l.label(text='只读任务：'+JOB.state)
  l.operator('animation_curves.cancel_readonly');l.operator('animation_curves.release_readonly')
  if JOB.result and JOB.result.get('status')=='REQUEST_BOUND_SUPPORT_DIAGNOSTIC':
   d=JOB.result['diagnostic'];l.label(text='采样发现支撑风险（不修复）' if d['status']=='SAMPLED_SUPPORT_FAIL' else '采样阈值通过（非连续证明）');l.label(text=', '.join(d['reasons'])or'仅采样阈值通过')
   l.label(text='漂移米：'+format(d['max_drift_m'],'.6g'));l.label(text='报告：'+str(JOB.session/'review/REPORT.zh-CN.md'))
  if JOB.error:l.label(text=JOB.error[:100])
CLASSES=(ACW_OT_support_diagnostic,ACW_PT_support_diagnostic)
_REGISTERED=[]
def register():
 if _REGISTERED:raise RuntimeError('Diagnostic UI already registered')
 try:
  for cls in CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
 except Exception:unregister();raise
def unregister():
 while _REGISTERED:bpy.utils.unregister_class(_REGISTERED[-1]);_REGISTERED.pop()
