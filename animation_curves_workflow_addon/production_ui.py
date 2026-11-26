"""Invoke the shared production request from Animation Curves in Blender."""

import json

import bpy

from . import production_operations


class ACW_OT_production_operation(bpy.types.Operator):
    bl_idname = 'acw.production_operation'
    bl_label = 'Run Production Operation'
    bl_description = 'Run a Blender 5.3 correction, motion transfer, Slot edit or rotation conversion'
    bl_options = {'REGISTER', 'UNDO'}

    request_text: bpy.props.StringProperty(name='Request Text', default='Animation Production Request')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        text = bpy.data.texts.get(self.request_text)
        if text is None:
            self.report({'ERROR'}, 'Choose an existing Text block containing the JSON request')
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
    bl_label = 'Production Operations'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Animation Curves'

    def draw(self, context):
        self.layout.operator(ACW_OT_production_operation.bl_idname)


_REGISTERED_CLASSES = []


def register():
    for cls in (ACW_OT_production_operation, ACW_PT_production_operations):
        bpy.utils.register_class(cls)
        _REGISTERED_CLASSES.append(cls)


def unregister():
    while _REGISTERED_CLASSES:
        bpy.utils.unregister_class(_REGISTERED_CLASSES[-1])
        _REGISTERED_CLASSES.pop()
