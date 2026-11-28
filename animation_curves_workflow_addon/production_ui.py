"""Invoke the shared production request from Animation Curves in Blender."""

import json

import bpy

from . import production_operations


class ACW_OT_production_operation(bpy.types.Operator):
    bl_idname = 'acw.production_operation'
    bl_label = '执行动画制作请求'
    bl_description = '执行动作局部修正、迁移、曲线编辑或旋转转换'
    bl_options = {'REGISTER', 'UNDO'}

    request_text: bpy.props.StringProperty(name='请求文本', default='Animation Production Request')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        text = bpy.data.texts.get(self.request_text)
        if text is None:
            self.report({'ERROR'}, '请选择包含动画制作请求的文本数据块')
            return {'CANCELLED'}
        try:
            request = json.loads(text.as_string())
            result = production_operations.apply(context, request)
        except (ValueError, RuntimeError, TypeError, production_operations.native.WorkflowError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, result.get('status', 'Applied') + ': ' + str(
            result.get('layer') or result.get('owner') or result.get('slot_identifier') or request['op']
        ))
        return {'FINISHED'}


class ACW_PT_production_operations(bpy.types.Panel):
    bl_label = '动画制作请求'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    def draw(self, context):
        self.layout.operator(ACW_OT_production_operation.bl_idname)


_REGISTERED_CLASSES = []


def register():
    for cls in (ACW_OT_production_operation,):
        bpy.utils.register_class(cls)
        _REGISTERED_CLASSES.append(cls)


def unregister():
    while _REGISTERED_CLASSES:
        bpy.utils.unregister_class(_REGISTERED_CLASSES[-1])
        _REGISTERED_CLASSES.pop()
