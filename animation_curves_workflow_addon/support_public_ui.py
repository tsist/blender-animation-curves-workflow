"""Reference-only private candidate workflow. No arbitrary rig/scene claims."""
import bpy
import os
from pathlib import Path
import sys
from .support_job import JOB, ACTIVE
from .support_ui import redraw_view3d
from . import read_ui_cleanup as cleanup
SOURCE='acw_support_source';OUTPUT='acw_support_output';PYTHON='acw_support_python'
_REGISTERED=[];_PROPERTIES=[]
def tick():
 try:JOB.poll()
 except BaseException as exc:JOB._retain_cleanup('tick',exc)
 redraw_view3d()
 return .25 if JOB.state in ACTIVE or cleanup.held(JOB) else None
class ACW_OT_support_prepare(bpy.types.Operator):
 bl_idname='animation_curves.support_prepare';bl_label='冻结参考支撑输入'
 @classmethod
 def poll(cls,c):return JOB.state=='EMPTY'
 def execute(self,c):
  try:JOB.prepare(getattr(c.window_manager,SOURCE),getattr(c.window_manager,OUTPUT),{'blender':bpy.app.binary_path,'host_python':getattr(c.window_manager,PYTHON)})
  except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
  return {'FINISHED'}
class ACW_OT_support_create(bpy.types.Operator):
 bl_idname='animation_curves.support_create';bl_label='生成私有支撑候选'
 @classmethod
 def poll(cls,c):return JOB.state=='PREPARED'
 def execute(self,c):
  try:
   JOB.start()
   if not bpy.app.timers.is_registered(tick):bpy.app.timers.register(tick,first_interval=.25)
  except Exception as e:
   message=cleanup.failure(JOB,bpy.app.timers,tick,e)
   self.report({'ERROR'},message);return {'CANCELLED'}
  return {'FINISHED'}
class ACW_OT_support_cancel(bpy.types.Operator):
 bl_idname='animation_curves.support_cancel';bl_label='取消并等待清理'
 @classmethod
 def poll(cls,c):return JOB.state in ('PREPARED',*ACTIVE)
 def execute(self,c):
  JOB.cancel()
  if JOB.state in ACTIVE and not bpy.app.timers.is_registered(tick):bpy.app.timers.register(tick,first_interval=.25)
  return {'FINISHED'}
class ACW_OT_support_release(bpy.types.Operator):
 bl_idname='animation_curves.support_release';bl_label='释放会话（保留输出）'
 @classmethod
 def poll(cls,c):return JOB.state not in ('EMPTY',*ACTIVE)
 def execute(self,c):JOB.release();return {'FINISHED'}
class ACW_PT_support_public(bpy.types.Panel):
 bl_idname='ACW_PT_support_public';bl_label='参考工程左腿支撑'
 bl_space_type='VIEW_3D';bl_region_type='UI'
 def draw(self,c):
  l=self.layout;l.label(text='仅验证过的无脚参考源；470–500帧')
  l.label(text='读取磁盘文件，忽略当前未保存修改')
  col=l.column();col.enabled=JOB.state=='EMPTY';col.prop(c.window_manager,SOURCE);col.prop(c.window_manager,OUTPUT);col.prop(c.window_manager,PYTHON)
  l.operator('animation_curves.support_prepare');l.operator('animation_curves.support_create')
  l.operator('animation_curves.support_cancel');l.operator('animation_curves.support_release');l.label(text='状态：'+JOB.state)
  if JOB.error:l.label(text=JOB.error[:100])
  if JOB.result:l.label(text='已保存并冷开；非接触锁定/艺术终版')
  if JOB.session:l.label(text='输出：'+str(JOB.session))
_CLASSES=(ACW_OT_support_prepare,ACW_OT_support_create,ACW_OT_support_cancel,ACW_OT_support_release)


def default_input(property_name):
 if os.name != 'nt':
  return ''
 if property_name == PYTHON:
  candidate = Path(sys.prefix) / 'bin' / 'python.exe'
 elif property_name == SOURCE:
  from .support_payload import sample_source
  candidate = sample_source(Path(__file__).parent)
 else:
  return ''
 return str(candidate) if candidate.is_file() else ''


def register():
 if _REGISTERED or _PROPERTIES:raise RuntimeError('Support public UI already owned')
 if any(hasattr(bpy.types.WindowManager,n)for n in (SOURCE,OUTPUT,PYTHON)):raise RuntimeError('Existing property owner')
 try:
  for n,label,subtype in [(SOURCE,'原始参考 .blend','FILE_PATH'),(OUTPUT,'输出目录','DIR_PATH'),(PYTHON,'受测 Python（精确哈希）','FILE_PATH')]:
   setattr(bpy.types.WindowManager,n,bpy.props.StringProperty(name=label,subtype=subtype,default=default_input(n)));_PROPERTIES.append(n)
  for cls in _CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
 except Exception:unregister();raise

def unregister():
 cleanup.close_for_unload(JOB,bpy.app.timers,tick)
 if bpy.app.timers.is_registered(tick):bpy.app.timers.unregister(tick)
 while _REGISTERED:bpy.utils.unregister_class(_REGISTERED[-1]);_REGISTERED.pop()
 while _PROPERTIES:delattr(bpy.types.WindowManager,_PROPERTIES.pop())
