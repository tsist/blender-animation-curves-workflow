"""Explicit owned-instance practice entry; operator properties do not dirty the scene."""
import bpy,json
from bpy.props import EnumProperty,FloatProperty,StringProperty,BoolProperty
from . import practice_writer
from .practice_identity import REGISTRY
def items(self,context):return REGISTRY.items
def changed(self,context):
 # Only an explicitly invoked dialog may change the frozen identity. Restoring
 # redo enum values is not a new selection and must never rewrite this string.
 if self.dialog_open:self.frozen_identity=REGISTRY.freeze(self.rig_name)
class ACW_OT_practice_action(bpy.types.Operator):
 bl_idname='animation_curves.create_practice_action';bl_label='创建独立 FK 练习动作';bl_options={'REGISTER','UNDO'}
 rig_name:EnumProperty(name='目标自有角色',items=items,update=changed)
 dialog_open:BoolProperty(name='对话框选择状态',default=False,options={'HIDDEN','SKIP_SAVE'})
 frozen_identity:StringProperty(name='冻结目标',default='',options={'HIDDEN'})
 start:FloatProperty(name='开始帧',default=1)
 peak:FloatProperty(name='中间帧',default=13)
 end:FloatProperty(name='结束帧',default=25)
 knee_start:FloatProperty(name='起止膝屈曲角',default=10,min=0,max=150)
 knee_peak:FloatProperty(name='中间膝屈曲角',default=45,min=0,max=150)
 @classmethod
 def poll(cls,c):return bool(c.scene and c.mode=='OBJECT'and not c.scene.library)
 def invoke(self,c,event):
  rows=[{'rig_name':o.name,'instance_uuid':o.get('ac_instance_uuid')}for o in bpy.data.objects if o.type=='ARMATURE'and o.get('ac_template_owner')=='animation-curves-two-segment-template-v2']
  REGISTRY.refresh(rows);self.dialog_open=True;self.frozen_identity=''
  active=getattr(c.active_object,'name',None);chosen=next((r for r in rows if r['rig_name']==active),rows[0]if len(rows)==1 else None)
  self.rig_name=REGISTRY.encode(chosen)if chosen else 'NONE'
  changed(self,c)
  return c.window_manager.invoke_props_dialog(self,width=430)
 def cancel(self,c):self.dialog_open=False
 def draw(self,c):
  l=self.layout;l.prop(self,'rig_name');l.prop(self,'start');l.prop(self,'peak');l.prop(self,'end');l.prop(self,'knee_start');l.prop(self,'knee_peak');l.label(text='仅默认姿态且无 Action 的自有人形；不覆盖旧动作');l.label(text='FK 练习：不是 IK、接触锁定或行走动画')
 def execute(self,c):
  self.dialog_open=False
  try:
   identity=REGISTRY.validate(self.rig_name,self.frozen_identity)
   r=practice_writer.create(bpy,identity['rig_name'],identity['instance_uuid'],[self.start,self.peak,self.end],self.knee_start,self.knee_peak)
  except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
  self.report({'INFO'},'已创建 '+r['action']+'；LINEAR 三关键帧，可在原生编辑器继续编辑');return {'FINISHED'}
class ACW_PT_practice(bpy.types.Panel):
 bl_idname='ACW_PT_practice';bl_label='自有角色 FK 练习';bl_space_type='VIEW_3D';bl_region_type='UI'
 def draw(self,c):
  self.layout.operator('animation_curves.create_practice_action');self.layout.label(text='仅默认姿态；未保存的新模板也可用');self.layout.label(text='无脚 / 膝物理 0–150° / 不覆盖旧 Action')
_CLASSES=(ACW_OT_practice_action,);_REGISTERED=[]
def register():
 if _REGISTERED:raise RuntimeError('Practice UI already registered')
 try:
  for cls in _CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
 except Exception:unregister();raise

def unregister():
 while _REGISTERED:bpy.utils.unregister_class(_REGISTERED[-1]);_REGISTERED.pop()
