"""Native clip reuse UI. The shared core owns creation and rollback."""
import json
from pathlib import Path
import bpy
from bpy.props import StringProperty, FloatProperty, FloatVectorProperty, IntProperty, EnumProperty, BoolProperty


class ACW_OT_clip_asset_apply(bpy.types.Operator):
    bl_idname = 'animation_curves.clip_asset_apply'
    bl_label = '复用动作素材'
    bl_description = '将动作素材添加为可编辑的新实例'
    bl_options = {'REGISTER', 'UNDO'}

    filepath: StringProperty(name='动作素材', subtype='FILE_PATH')
    filter_glob: StringProperty(default='*.clip.json;*.json', options={'HIDDEN'})
    instance_name: StringProperty(name='实例名称', default='Reuse_01', maxlen=48)
    start: IntProperty(name='起始帧', default=1, min=1, max=999999)
    end: IntProperty(name='结束帧', default=288, min=2, max=1000000)
    scale: FloatProperty(name='统一尺度', default=1.2, min=0.1, max=10)
    translation: FloatVectorProperty(name='站位偏移', size=3, default=(3, 1, 0), subtype='TRANSLATION')
    yaw: EnumProperty(name='朝向', items=(('0', '0°', ''), ('1', '90°', ''), ('2', '180°', ''), ('3', '270°', '')), default='1')
    set_playback_range: BoolProperty(name='播放区间使用片段范围', default=True)
    use_source_window: BoolProperty(name='仅复用源片段选段', default=False)
    source_start: FloatProperty(name='源起始帧', default=48, min=0.001)
    source_end: FloatProperty(name='源结束帧', default=96, min=0.002)
    project_source_placement: BoolProperty(
        name='四人素材沿用源时长与站位',
        description='使用四人素材的原时长、站位和朝向，保留绝对锁定点',
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return bool(context.scene and context.view_layer and context.mode == 'OBJECT' and not context.scene.library)

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout
        for name in ('instance_name', 'start', 'end', 'scale', 'translation', 'yaw', 'set_playback_range'):
            layout.prop(self, name)
        layout.prop(self, 'project_source_placement')
        layout.prop(self, 'use_source_window')
        if self.use_source_window:
            layout.prop(self, 'source_start')
            layout.prop(self, 'source_end')

    def execute(self, context):
        from . import clip_asset
        try:
            path = Path(bpy.path.abspath(self.filepath)).resolve(strict=True)
            clip = json.loads(path.read_text(encoding='utf-8-sig'))
            request = {'schema': 'animation-native-clip-apply-v1', 'clip': clip,
                       'instance_name': self.instance_name, 'start': self.start, 'end': self.end,
                       'scale': self.scale, 'translation': list(self.translation),
                       'yaw_quarter_turns': int(self.yaw)}
            if clip.get('schema') == 'animation-native-project-clip-v1' and self.project_source_placement:
                request.update(start=clip['timing']['start'], end=clip['timing']['end'],
                               scale=1.0, translation=[0.0, 0.0, 0.0], yaw_quarter_turns=0)
            if self.use_source_window:
                request['source_window'] = {'start': self.source_start, 'end': self.source_end}
            result = clip_asset.apply_clip(context, request)
            if self.set_playback_range:
                context.scene.frame_start = int(request['start'])
                context.scene.frame_end = int(request['end'])
            self.report({'INFO'}, f'已添加 {len(result["actors"])} 名角色，可继续编辑原生曲线')
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class ACW_PT_clip_asset(bpy.types.Panel):
    bl_idname = 'ACW_PT_clip_asset'
    bl_label = '动作素材复用'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '动画曲线'

    def draw(self, context):
        layout = self.layout
        layout.operator('animation_curves.clip_asset_apply', icon='ASSET_MANAGER')
        layout.label(text='完整或选段 · 统一尺度与站位')
        layout.label(text='细改使用原生 Graph Editor')


_CLASSES = (ACW_OT_clip_asset_apply, ACW_PT_clip_asset)
_REGISTERED = []


def register():
    if _REGISTERED:
        raise RuntimeError('Clip asset UI is already registered')
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
    except Exception:
        unregister()
        raise


def unregister():
    while _REGISTERED:
        bpy.utils.unregister_class(_REGISTERED[-1])
        _REGISTERED.pop()
