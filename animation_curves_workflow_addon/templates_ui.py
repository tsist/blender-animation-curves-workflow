"""Separate template controls; no scene properties or automatic creation."""
import bpy
from bpy.props import EnumProperty, FloatProperty
from . import templates_native
from ._ui_kit import apply_collection_theme, apply_node_theme, ensure_object_mode

class ACW_OT_create_template(bpy.types.Operator):
    bl_idname='animation_curves.create_template'
    bl_label='创建简形角色'
    bl_options={'REGISTER','UNDO'}
    kind: EnumProperty(name='模板',items=(('cylinder_v1','双节圆柱','3骨，无四肢'),('humanoid_fk_two_segment_v2','简形人形 FK','18骨；每腿大腿/小腿两段，无脚；物理膝0–150°')))
    height_m: FloatProperty(name='身高（米制数值）',default=1.8,min=.3,max=3.0)
    @classmethod
    def poll(cls,context):
        return bool(context.scene and context.view_layer and not context.scene.library)
    def invoke(self,context,event):
        return context.window_manager.invoke_props_dialog(self)
    def execute(self,context):
        try:
            with ensure_object_mode(context):
                handle=templates_native.create_template(context,self.kind,{'height_m':self.height_m})
            apply_collection_theme(handle['collection'], 'animation')
            materials = {slot.material for obj in handle['parts'].values()
                         for slot in obj.material_slots if slot.material}
            for material in materials:
                if material.node_tree:
                    for node in material.node_tree.nodes:
                        apply_node_theme(node, 'animation')
        except Exception as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}
        self.report({'INFO'},'已创建新实例 '+handle['instance_uuid'][:8]+'；保留原选择与时间轴')
        return {'FINISHED'}

class ACW_PT_templates(bpy.types.Panel):
    bl_idname='ACW_PT_templates'
    bl_label='简形角色模板'
    bl_space_type='VIEW_3D';bl_region_type='UI'
    def draw(self,context):
        layout=self.layout
        layout.operator('animation_curves.create_template',text='创建圆柱角色').kind='cylinder_v1'
        layout.operator('animation_curves.create_template',text='创建两段腿人形（无脚）').kind='humanoid_fk_two_segment_v2'
        layout.label(text='正面 -Y / 上方 +Z')
        layout.label(text='胸前方片 / 脸前竖长片')
        layout.label(text='Solid 颜色请用材质或物体')
        layout.label(text='物理膝 0–150°；无脚')
        layout.label(text='FK 无自动 IK；不生成动作')
        layout.operator('animation_curves.check_current_knees')

_CLASSES=(ACW_OT_create_template,)
_REGISTERED=[]
def register():
    if _REGISTERED:raise RuntimeError('Template UI is already registered')
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls);_REGISTERED.append(cls)
    except Exception:
        unregister();raise

def unregister():
    while _REGISTERED:
        bpy.utils.unregister_class(_REGISTERED[-1]);_REGISTERED.pop()
