"""Ephemeral, explicit current-frame inspection; no persistent scene properties."""
import bpy
from .knee_check import inspect
LABELS={'OK':'正常','REVERSE':'反折','OVERFLEX':'过屈','OFF_PLANE':'偏离铰链平面','DISCONNECTED':'关节脱开','DEGENERATE':'退化几何','MISSING':'未找到/已改名','DISABLED':'禁用/影响不足','ALTERED':'配置改变'}
class ACW_OT_check_knees(bpy.types.Operator):
    bl_idname='animation_curves.check_current_knees'
    bl_label='当前帧双膝只读体检'
    bl_description='只读实际求值几何；不切帧、不修复、不保存；只选择一个自有两段腿人形骨架'
    def execute(self,context):
        report=inspect(context)
        name=report.get('rig_name','未选角色')
        def draw(menu,ctx):
            layout=menu.layout
            layout.label(text='本次检查快照：'+name)
            layout.label(text='帧 %s + 子帧 %.4f'%(report['frame'],report['subframe']))
            if report['status']!='MEASURED':layout.label(text='不支持/缺失：'+report['reason'],icon='ERROR')
            for row in report['knees']:
                box=layout.box()
                angle='不可测' if row['physical_deg'] is None else '%.3f°'%row['physical_deg']
                box.label(text=('左膝' if row['side']=='L' else '右膝')+'：物理角 '+angle+' / '+LABELS.get(row['geometry'],row['geometry']))
                box.label(text='模板膝限位配置：'+LABELS.get(row['constraint'],row['constraint']))
            layout.label(text='仅当前姿态；非全片、接触或碰撞验收')
            layout.label(text='换帧后须重新检查；负 Euler 不等于反折')
        context.window_manager.popup_menu(draw,title='双膝只读体检',icon='INFO')
        return {'FINISHED'}
_CLASSES=(ACW_OT_check_knees,)
_REGISTERED=[]
def register():
    if _REGISTERED:raise RuntimeError('Knee UI already registered')
    try:
        for cls in _CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
    except Exception:unregister();raise

def unregister():
    while _REGISTERED:bpy.utils.unregister_class(_REGISTERED[-1]);_REGISTERED.pop()
