"""Read-only explicit support request UI; deliberately no candidate writer yet."""
import json
import bpy
from bpy.props import FloatProperty, StringProperty, EnumProperty
from .read_job import JOB,ACTIVE
from . import read_ui_cleanup as cleanup
def tick():
    try:JOB.poll()
    except BaseException as exc:
        JOB.cleanup_errors.append('tick:'+repr(exc));JOB.error=repr(exc);JOB.state='CLEANUP_PENDING'
    redraw_view3d()
    return .25 if JOB.state in ACTIVE or cleanup.held(JOB) else None


def redraw_view3d():
    # Polling changes Python state only; explicitly invalidate visible panels.
    # No scene update/frame_set/operator is performed here.
    try:
        windows=tuple(bpy.context.window_manager.windows)
    except (ReferenceError,RuntimeError,AttributeError):windows=()
    for window in windows:
        try:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':area.tag_redraw()
        except (ReferenceError,RuntimeError,AttributeError):
            # UI invalidation is best effort; never stop the owned-job poller.
            continue
from .support_input import request
from .templates_native import OWNER
from .review_core import service
_ENUM_ITEMS=[('NONE','请选择模板实例','No explicit instance selected')]
def instance_items(self,context):return _ENUM_ITEMS


class ACW_OT_inspect_support_input(bpy.types.Operator):
    bl_idname='animation_curves.inspect_support_input'
    bl_label='冻结四时刻并只读审阅'
    instance: EnumProperty(name='模板实例（不改变场景选择）',items=instance_items)
    output: StringProperty(name='只读报告输出目录',subtype='DIR_PATH')
    entry: FloatProperty(name='进入帧',default=1)
    plant: FloatProperty(name='支撑开始',default=5)
    release: FloatProperty(name='支撑结束',default=25)
    exit: FloatProperty(name='退出帧',default=31)
    @classmethod
    def poll(cls,context):
        obj=context.active_object
        return JOB.state=='EMPTY' and context.mode=='OBJECT'
    def invoke(self,context,event):
        global _ENUM_ITEMS
        try:
            rows=service.explicit_options(bpy,[self.entry,self.plant,self.release,self.exit])
            if not rows:raise ValueError('没有可识别的已保存干净模板实例')
            _ENUM_ITEMS=[('NONE','请选择模板实例','多实例须明确选择')]+[(json.dumps(r,sort_keys=True),r['rig_name']+' · '+r['instance_uuid'][:8],r['source']) for r in rows]
            active=getattr(context.active_object,'name',None)
            chosen=next((r for r in rows if r['rig_name']==active),rows[0] if len(rows)==1 else None)
            self.instance=json.dumps(chosen,sort_keys=True) if chosen else 'NONE'
        except Exception as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self)
    def execute(self,context):
        try:
            identity=json.loads(self.instance)
            if type(identity)is not dict or set(identity)!={'rig_name','instance_uuid','source','source_sha256'}:raise ValueError('明确选择模板实例')
            JOB.prepare(bpy,(self.entry,self.plant,self.release,self.exit),bpy.path.abspath(self.output),explicit=identity)
            JOB.start()
            if not bpy.app.timers.is_registered(tick):bpy.app.timers.register(tick,first_interval=.25)
        except Exception as exc:
            message=cleanup.failure(JOB,bpy.app.timers,tick,exc)
            self.report({'ERROR'},message);return {'CANCELLED'}
        self.report({'INFO'},'已冻结请求并启动只读审阅；不生成动作')
        return {'FINISHED'}

class ACW_PT_support_input(bpy.types.Panel):
    bl_idname='ACW_PT_support_input';bl_label='左腿支撑输入检查'
    bl_space_type='VIEW_3D';bl_region_type='UI'
    def draw(self,context):
        self.layout.operator('animation_curves.inspect_support_input')
        self.layout.label(text='状态：'+JOB.state)
        self.layout.operator('animation_curves.cancel_readonly')
        self.layout.operator('animation_curves.release_readonly')
        if JOB.error:self.layout.label(text=JOB.error[:100])
        if JOB.result and JOB.result.get('status')=='REQUEST_BOUND_SAMPLED_REVIEW_ONLY':
            self.layout.label(text='请求绑定采样审阅通过：'+JOB.digest[:12])
            self.layout.label(text='仅 '+str(len(JOB.result['pose']['samples']))+' 个采样，不代表连续区间或地面接触')

class ACW_OT_cancel_readonly(bpy.types.Operator):
    bl_idname='animation_curves.cancel_readonly';bl_label='取消只读审阅'
    @classmethod
    def poll(cls,c):return JOB.state in ('PREPARED',*ACTIVE)
    def execute(self,c):
        JOB.cancel()
        if JOB.state in ACTIVE and not bpy.app.timers.is_registered(tick):bpy.app.timers.register(tick,first_interval=.25)
        return {'FINISHED'}
class ACW_OT_release_readonly(bpy.types.Operator):
    bl_idname='animation_curves.release_readonly';bl_label='释放只读会话（保留证据）'
    @classmethod
    def poll(cls,c):return JOB.state not in ('EMPTY',*ACTIVE)
    def execute(self,c):JOB.release();return {'FINISHED'}

_REGISTERED=[]
def register():
    if _REGISTERED:raise RuntimeError('Support input UI already registered')
    try:
        for cls in (ACW_OT_inspect_support_input,ACW_OT_cancel_readonly,ACW_OT_release_readonly):
            bpy.utils.register_class(cls);_REGISTERED.append(cls)
    except Exception:
        unregister();raise
def unregister():
    cleanup.close_for_unload(JOB,bpy.app.timers,tick)
    if bpy.app.timers.is_registered(tick):bpy.app.timers.unregister(tick)
    while _REGISTERED:
        bpy.utils.unregister_class(_REGISTERED[-1]);_REGISTERED.pop()
