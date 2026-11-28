"""Two explicit saved-source operations; independent output, no live apply or Undo."""
import bpy,json
from bpy.props import FloatProperty,StringProperty
from .read_job import JOB
from . import support_solver as ss,support_ui,read_ui_cleanup as cleanup

def timer():
 if not bpy.app.timers.is_registered(support_ui.tick):bpy.app.timers.register(support_ui.tick,first_interval=.25)
def eligible():return JOB.state=='SUCCEEDED'and JOB.result is not None and JOB.result.get('status')=='PRECHECK_ELIGIBLE'
class ACW_OT_cap_precheck(bpy.types.Operator):
 bl_idname='animation_curves.cap_precheck';bl_label='预检双腿固定端盖补偿'
 start:FloatProperty(name='开始帧',default=1);end:FloatProperty(name='结束帧',default=25);plane:FloatProperty(name='水平支撑面世界Z（米）',default=0)
 output:StringProperty(name='现有私有输出目录',subtype='DIR_PATH');expected:StringProperty(options={'HIDDEN'})
 @classmethod
 def poll(cls,c):return JOB.state=='EMPTY'and c.mode=='OBJECT'
 def request_options(self):return {'start':self.start,'end':self.end,'plane_z_m':self.plane,'mode':'PRECHECK'}
 def invoke(self,c,event):
  try:self.expected=json.dumps(ss.freeze(bpy,self.request_options(),'0'*32)['stable_payload'],sort_keys=True)
  except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
  return c.window_manager.invoke_props_dialog(self,width=690)
 def draw(self,c):
  l=self.layout;l.label(text='仅已保存干净源；双腿rest、无姿态Action、root纯平移LINEAR/FREE')
  for name in('start','end','plane','output'):l.prop(self,name)
  l.label(text='首版root旋转零/尺度一；场景单位1米；不改变root或支撑目标')
  l.label(text='1/32帧有限采样、1/4帧写键；不是步行/通用IK/连续证明')
  l.label(text='预检不创建动作；通过后仍须另行确认生成新工程')
 def execute(self,c):
  try:
   r=ss.freeze(bpy,self.request_options(),'0'*32);old=json.loads(self.expected)
   # Parameters may be edited in this dialog; source/context/resources may not.
   stable=r['stable_payload'];old['parameters']=stable['parameters']
   if old!=stable:raise ValueError('源/身份/姿态/资源变化，请重新打开')
   JOB.prepare(bpy,(),bpy.path.abspath(self.output),solver=self.request_options());JOB.start();timer()
  except Exception as e:self.report({'ERROR'},cleanup.failure(JOB,bpy.app.timers,support_ui.tick,e));return {'CANCELLED'}
  return {'FINISHED'}
class ACW_OT_cap_generate(bpy.types.Operator):
 bl_idname='animation_curves.cap_generate';bl_label='生成独立补偿工程（不改当前场景）'
 expected:StringProperty(options={'HIDDEN'});parent:StringProperty(options={'HIDDEN'})
 @classmethod
 def poll(cls,c):return eligible()and c.mode=='OBJECT'
 def invoke(self,c,event):
  try:
   if not eligible():raise ValueError('没有有效预检')
   self.expected=json.dumps(JOB.result['request']['stable_payload'],sort_keys=True);self.parent=str(JOB.session.parent)
   stable=json.loads(self.expected);opts=dict(stable['parameters'],mode='GENERATE',approved_stable=stable);ss.freeze(bpy,opts,'0'*32)
  except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
  return c.window_manager.invoke_props_dialog(self,width=730)
 def draw(self,c):
  l=self.layout;stable=json.loads(self.expected)
  l.label(text='输出：'+self.parent);l.label(text='新的独占ACW会话/review/candidate.blend；绝不覆盖原工程')
  l.label(text='双thigh.X/shin.X四通道；完整预检后生成，保root及非目标')
  l.label(text='源：'+stable['source']);l.label(text='源SHA：'+stable['source_sha256'][:24])
  l.label(text='磁盘文件不能Ctrl+Z；不自动导入/绑定/打开，不改变当前场景')
  l.label(text='生成后同进程磁盘重开验证；非独立进程/连续数学证明')
 def execute(self,c):
  try:
   stable=json.loads(self.expected)
   if not eligible()or JOB.result['request']['stable_payload']!=stable:raise ValueError('预检结果已变化')
   opts=dict(stable['parameters'],mode='GENERATE',approved_stable=stable);ss.freeze(bpy,opts,'0'*32)
   ss.validate_result(JOB.result,JOB.request,JOB.digest,JOB.session/'review')
   JOB.release();JOB.prepare(bpy,(),self.parent,solver=opts);JOB.start();timer()
  except Exception as e:self.report({'ERROR'},cleanup.failure(JOB,bpy.app.timers,support_ui.tick,e));return {'CANCELLED'}
  return {'FINISHED'}
class ACW_OT_cap_copy(bpy.types.Operator):
 bl_idname='animation_curves.cap_copy';bl_label='复制已验证工程路径'
 def execute(self,c):
  try:
   if JOB.state!='SUCCEEDED'or not JOB.result or JOB.result.get('status')!='SAMPLED_CANDIDATE_READY':raise ValueError('无有效生成结果')
   ss.validate_result(JOB.result,JOB.request,JOB.digest,JOB.session/'review');c.window_manager.clipboard=JOB.result['candidate']
  except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
  return {'FINISHED'}
class ACW_PT_cap_solver(bpy.types.Panel):
 bl_idname='ACW_PT_cap_solver';bl_label='双腿固定端盖 · 独立工程'
 bl_space_type='VIEW_3D';bl_region_type='UI'
 def draw(self,c):
  l=self.layout;l.operator('animation_curves.cap_precheck');l.operator('animation_curves.cap_generate');l.label(text='任务：'+JOB.state)
  l.operator('animation_curves.cancel_readonly');l.operator('animation_curves.release_readonly')
  r=JOB.result
  if r and r.get('request',{}).get('kind')=='BILATERAL_CAP_WORKFLOW':
   l.label(text={'PRECHECK_ELIGIBLE':'预检可尝试生成，尚无工程','PRECHECK_REJECTED':'区间不可达：未生成工程','SAMPLED_CANDIDATE_READY':'新工程有限采样通过（非步行）'}[r['status']])
   if r['status']=='SAMPLED_CANDIDATE_READY':l.label(text=r['candidate']);l.operator('animation_curves.cap_copy')
  if JOB.error:l.label(text=JOB.error[:100])
CLASSES=(ACW_OT_cap_precheck,ACW_OT_cap_generate,ACW_OT_cap_copy);_REGISTERED=[]
def register():
 try:
  for cls in CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
 except Exception:unregister();raise
def unregister():
 while _REGISTERED:bpy.utils.unregister_class(_REGISTERED[-1]);_REGISTERED.pop()
