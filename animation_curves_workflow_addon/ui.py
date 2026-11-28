"""Small native channel editor using the same protected core as blenderctl.

This development panel exposes the channel operations currently implemented by
the shared core. It neither replaces Graph Editor nor claims that the later
character/event workflow is complete. No preferences or source files are saved.
"""
import json
import math

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty

from . import core, event_ui_state, clip_event
from .contract import WorkflowError


SETTINGS_PROPERTY = 'acw_settings'
REPORT_OWNER_KEY = 'acw_ui_report_owner'
REPORT_OWNER_VALUE = 'animation-curves-workflow-v1'

# Callback enum strings have module lifetime, as required by Blender's enum API.
_OBJECT_CHANNELS = (
    ('location_x', '局部位置 X', '物体的局部 X 位置'),
    ('location_y', '局部位置 Y', '物体的局部 Y 位置'),
    ('location_z', '局部位置 Z', '物体的局部 Z 位置'),
    ('rotation_x', '局部旋转 X', '欧拉旋转 X；不改变物体的旋转模式'),
    ('rotation_y', '局部旋转 Y', '欧拉旋转 Y；不改变物体的旋转模式'),
    ('rotation_z', '局部旋转 Z', '欧拉旋转 Z；不改变物体的旋转模式'),
    ('scale_x', '局部缩放 X', '物体的局部 X 缩放'),
    ('scale_y', '局部缩放 Y', '物体的局部 Y 缩放'),
    ('scale_z', '局部缩放 Z', '物体的局部 Z 缩放'),
)
_MATERIAL_CHANNELS = (
    ('material_base_r', '活动材质 Base Color R', '唯一 Principled 节点的未链接 Base Color 红色分量'),
    ('material_base_g', '活动材质 Base Color G', '唯一 Principled 节点的未链接 Base Color 绿色分量'),
    ('material_base_b', '活动材质 Base Color B', '唯一 Principled 节点的未链接 Base Color 蓝色分量'),
)
_OBJECT_CHANNELS = _OBJECT_CHANNELS + _MATERIAL_CHANNELS
_LIGHT_CHANNELS = _OBJECT_CHANNELS + (
    ('light_energy', '灯光强度', '所选灯光数据的原生强度'),
)
_CAMERA_CHANNELS = _OBJECT_CHANNELS + (
    ('camera_lens', '相机焦距', '所选相机数据的焦距，单位毫米'),
)
_CHANNEL_SPECS = {
    'location_x': ('OBJECT', 'location', 0),
    'location_y': ('OBJECT', 'location', 1),
    'location_z': ('OBJECT', 'location', 2),
    'rotation_x': ('OBJECT', 'rotation_euler', 0),
    'rotation_y': ('OBJECT', 'rotation_euler', 1),
    'rotation_z': ('OBJECT', 'rotation_euler', 2),
    'scale_x': ('OBJECT', 'scale', 0),
    'scale_y': ('OBJECT', 'scale', 1),
    'scale_z': ('OBJECT', 'scale', 2),
    'light_energy': ('LIGHT', 'energy', 0),
    'camera_lens': ('CAMERA', 'lens', 0),
}


def _channel_items(_settings, context):
    obj = getattr(context, 'active_object', None) if context else None
    if obj and obj.type == 'LIGHT':
        return _LIGHT_CHANNELS
    if obj and obj.type == 'CAMERA':
        return _CAMERA_CHANNELS
    return _OBJECT_CHANNELS


def _settings(context):
    return getattr(context.scene, SETTINGS_PROPERTY)


def _available(context):
    return bool(context.scene and context.view_layer and context.mode == 'OBJECT'
                and getattr(context, 'active_object', None)
                and hasattr(context.scene, SETTINGS_PROPERTY))


def _target(context):
    if not _available(context):
        raise WorkflowError('INVALID_REQUEST', '请在物体模式中选择当前视图层的目标')
    obj = context.active_object
    if context.view_layer.objects.get(obj.name) is not obj:
        raise WorkflowError('CONFLICT', '目标不在当前视图层中')
    channel = _settings(context).channel
    if channel in {'material_base_r', 'material_base_g', 'material_base_b'}:
        material = obj.active_material
        if material is None:
            raise WorkflowError('INVALID_REQUEST', '当前物体没有活动材质')
        if not material.use_nodes or material.node_tree is None:
            raise WorkflowError('UNSUPPORTED', '活动材质必须已有节点树；不自动创建节点')
        tree = material.node_tree
        nodes = [node for node in tree.nodes if node.bl_idname == 'ShaderNodeBsdfPrincipled']
        if len(nodes) != 1:
            raise WorkflowError('UNSUPPORTED', '活动材质必须恰有一个 Principled 节点；不猜测目标')
        socket = nodes[0].inputs.get('Base Color')
        if socket is None or socket.is_linked:
            raise WorkflowError('UNSUPPORTED', 'Base Color 输入不存在或已链接；不修改链接')
        path = socket.path_from_id('default_value')
        try:
            resolved = tree.path_resolve(socket.path_from_id())
            value = tree.path_resolve(path)
            if resolved != socket or len(value) != 4 or not all(math.isfinite(float(v)) for v in value):
                raise ValueError('invalid Base Color identity/value')
        except Exception as error:
            raise WorkflowError('CONFLICT', 'Base Color 选择已失效或值无效；请重新选择') from error
        index = {'material_base_r': 0, 'material_base_g': 1, 'material_base_b': 2}[channel]
        return ({'id': 'selected_channel',
                 'owner': {'type': 'MATERIAL_NODETREE', 'name': material.name},
                 'data_path': path, 'array_index': index, 'sharing': 'reject'}, tree)
    spec = _CHANNEL_SPECS.get(channel)
    if spec is None:
        raise WorkflowError('INVALID_REQUEST', '请为当前目标重新选择通道')
    owner_type, path, index = spec
    if owner_type == 'LIGHT' and obj.type != 'LIGHT':
        raise WorkflowError('INVALID_REQUEST', '灯光强度通道需要选择灯光物体')
    if owner_type == 'CAMERA' and obj.type != 'CAMERA':
        raise WorkflowError('INVALID_REQUEST', '焦距通道需要选择相机物体')
    if path == 'rotation_euler' and obj.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
        raise WorkflowError('UNSUPPORTED', '当前物体采用四元数或轴角旋转；请在原生编辑器中处理，不自动转换')
    owner = obj if owner_type == 'OBJECT' else obj.data
    return ({'id': 'selected_channel', 'owner': {'type': owner_type, 'name': owner.name},
             'data_path': path, 'array_index': index, 'sharing': 'reject'}, owner)


def _source_frame(context):
    settings = _settings(context)
    return float(context.scene.frame_current_final if settings.use_current_frame else settings.frame)


def _manifest(context, operation):
    target, _owner = _target(context)
    return {'schema': '1.0', 'operation': operation,
            'context': {'scene': context.scene.name, 'view_layer': context.view_layer.name},
            'targets': [target], 'operations': [], 'samples': [_source_frame(context)], 'locks': []}


def _write_report(context, report):
    """Update only our marked Text; a matching user-owned name is never reused."""
    settings = _settings(context)
    text = bpy.data.texts.get(settings.report_text) if settings.report_text else None
    if text is None or text.get(REPORT_OWNER_KEY) != REPORT_OWNER_VALUE:
        text = bpy.data.texts.new('动画曲线 · 检查报告')
        text[REPORT_OWNER_KEY] = REPORT_OWNER_VALUE
        settings.report_text = text.name
    body = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    text.clear()
    text.write(body)
    return text.name


def _publish(operator, context, result, summary):
    """Best-effort display only: it must never negate an already committed edit."""
    try:
        settings = _settings(context)
        settings.last_report = summary
    except Exception:
        pass
    try:
        _write_report(context, result)
    except Exception:
        try:_settings(context).last_report = summary + '；报告写入失败'
        except Exception:pass
    try:operator.report({'INFO'}, summary)
    except Exception:pass
    try:
        if context.area:context.area.tag_redraw()
    except Exception:pass
    return {'FINISHED'}


def _failed(operator, context, error):
    code = error.code if isinstance(error, WorkflowError) else type(error).__name__
    message = str(code) + '：' + str(error)
    if context.scene and hasattr(context.scene, SETTINGS_PROPERTY):
        _settings(context).last_report = message
    operator.report({'ERROR'}, message)
    return {'CANCELLED'}


class ACWSettings(bpy.types.PropertyGroup):
    event_name: StringProperty(name='事件名称', default='impact', maxlen=128)
    event_locked: BoolProperty(name='事件改时锁（不锁原生编辑器）', default=False)
    event_state: StringProperty(name='事件绑定状态', options={'HIDDEN'})
    pose_event_state: StringProperty(name='双角色持久事件', options={'HIDDEN'})
    cross_domain_event_state: StringProperty(name='双角色灯光事件', options={'HIDDEN'})
    channel: EnumProperty(name='通道', items=_channel_items)
    use_current_frame: BoolProperty(name='使用当前帧', default=True,
                                   description='包含当前子帧；取消勾选可输入源帧')
    frame: FloatProperty(name='源帧', default=1.0)
    to_frame: FloatProperty(name='移至帧', default=2.0)
    value: FloatProperty(name='数值', default=0.0)
    rotation_value: FloatProperty(name='旋转值', subtype='ANGLE', default=0.0)
    interpolation: EnumProperty(name='插值', items=(
        ('LINEAR', '线性', '线性插值'),
        ('BEZIER', '贝塞尔', '贝塞尔插值；非目标手柄仍受保护'),
        ('CONSTANT', '恒定', '恒定插值')), default='LINEAR')
    report_text: StringProperty(name='报告文本', options={'HIDDEN'})
    last_report: StringProperty(name='最近结果', options={'HIDDEN'})


class ACW_OT_edit_key(bpy.types.Operator):
    bl_idname = 'animation_curves.edit_key'
    bl_label = '编辑关键帧'
    bl_description = '通过共享核心编辑所选通道，保护非目标键和原生手改'
    bl_options = {'REGISTER', 'UNDO'}

    action: EnumProperty(items=(('set_key', '设置关键帧', ''),
                               ('move_key', '移动关键帧', ''),
                               ('delete_key', '删除关键帧', '')), default='set_key')

    @classmethod
    def poll(cls, context):
        return _available(context)

    def execute(self, context):
        try:
            settings = _settings(context)
            manifest = _manifest(context, 'edit')
            operation = {'op': self.action, 'target': 'selected_channel',
                         'frame': _source_frame(context)}
            if self.action == 'set_key':
                operation['value'] = float(settings.rotation_value if settings.channel.startswith('rotation_')
                                           else settings.value)
                operation['interpolation'] = settings.interpolation
            elif self.action == 'move_key':
                operation['to_frame'] = float(settings.to_frame)
                manifest['samples'].append(float(settings.to_frame))
            manifest['operations'].append(operation)
            result = core.apply_manifest(manifest)
        except Exception as error:
            return _failed(self, context, error)
        summary = {'set_key': '已设置关键帧', 'move_key': '已移动关键帧',
                   'delete_key': '已删除关键帧'}[self.action]
        return _publish(self, context, result, summary)


class ACW_OT_inspect_channel(bpy.types.Operator):
    bl_idname = 'animation_curves.inspect_channel'
    bl_label = '检查所选通道'
    bl_description = '读取原生通道和求值报告，不修改动画'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _available(context)

    def execute(self, context):
        try:
            result = core.inspect(_manifest(context, 'inspect'))
        except Exception as error:
            return _failed(self, context, error)
        return _publish(self, context, result, '已生成通道检查报告')


class ACW_OT_read_current_value(bpy.types.Operator):
    bl_idname = 'animation_curves.read_current_value'
    bl_label = '读取当前值'
    bl_description = '读取当前帧的原生属性值到输入栏，不插入关键帧'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _available(context)

    def execute(self, context):
        try:
            target, owner = _target(context)
            value = owner.path_resolve(target['data_path'])
            if not isinstance(value, (int, float)):
                value = value[target['array_index']]
            value = float(value)
            if not math.isfinite(value):
                raise WorkflowError('CONFLICT', '当前属性不是有限数值')
            settings = _settings(context)
            if settings.channel.startswith('rotation_'):
                settings.rotation_value = value
            else:
                settings.value = value
            settings.last_report = '已读取当前帧数值'
        except Exception as error:
            return _failed(self, context, error)
        return {'FINISHED'}


class ACW_OT_show_report(bpy.types.Operator):
    bl_idname = 'animation_curves.show_report'
    bl_label = '查看检查报告'
    bl_description = '在当前区域打开本插件生成的文本报告'

    @classmethod
    def poll(cls, context):
        if not context.area or not context.scene or not hasattr(context.scene, SETTINGS_PROPERTY):
            return False
        name = _settings(context).report_text
        text = bpy.data.texts.get(name) if name else None
        return bool(text and text.get(REPORT_OWNER_KEY) == REPORT_OWNER_VALUE)

    def execute(self, context):
        text = bpy.data.texts.get(_settings(context).report_text)
        if text is None or text.get(REPORT_OWNER_KEY) != REPORT_OWNER_VALUE:
            return _failed(self, context, WorkflowError('NOT_FOUND', '本插件的检查报告已不存在'))
        context.area.type = 'TEXT_EDITOR'
        context.area.spaces.active.text = text
        return {'FINISHED'}



def _event_state(context):
    raw=_settings(context).event_state
    state=json.loads(raw) if raw else event_ui_state.empty()
    if state.get('schema')!='event-panel-state-v1':
        raise WorkflowError('INVALID_REQUEST','事件状态版本不支持')
    return state

def _event_context(context):
    return {'scene':context.scene.name,'view_layer':context.view_layer.name}

def _clip_guard(context):
    if clip_event.has_record(context.scene):
        raise WorkflowError('UNSUPPORTED','已有完整片段事件记录；旧单键事件入口不会修改同一动画，请使用“完整片段事件”面板')

def _event_target(context):
    target,_owner=_target(context)
    target['id']=event_ui_state.target_id(target)
    return target

class ACW_OT_event_capture(bpy.types.Operator):
    bl_idname='animation_curves.event_capture'
    bl_label='捕获所选通道到事件'
    bl_description='捕获当前原生关键帧完整见证；已有绑定不会被静默覆盖'
    bl_options={'REGISTER','UNDO'}
    @classmethod
    def poll(cls,context):return _available(context)
    def execute(self,context):
        try:
            _clip_guard(context)
            settings=_settings(context);target=_event_target(context);state=_event_state(context)
            request=_manifest(context,'inspect');request['targets']=[target]
            inspection=core.inspect(request)
            state=event_ui_state.capture(state,settings.event_name,_source_frame(context),target,inspection,settings.event_locked)
            settings.event_state=json.dumps(state,ensure_ascii=False,allow_nan=False)
            return _publish(self,context,{'state':state,'inspection':inspection},'已捕获事件绑定；未修改动画')
        except Exception as exc:return _failed(self,context,exc)

class ACW_OT_event_inspect(bpy.types.Operator):
    bl_idname='animation_curves.event_inspect'
    bl_label='检查全部事件绑定'
    bl_description='比较当前原生关键帧与已捕获见证；差异不会自动采用'
    @classmethod
    def poll(cls,context):return _available(context)
    def execute(self,context):
        try:
            state=_event_state(context);inspection=core.inspect(event_ui_state.manifest(state,_event_context(context)))
            rows=event_ui_state.inspect_state(state,inspection)
            drift=sum(r['status']!='unchanged' for r in rows)
            return _publish(self,context,{'inspection':inspection,'bindings':rows},'绑定检查：'+str(len(rows))+' 项，差异 '+str(drift)+' 项')
        except Exception as exc:return _failed(self,context,exc)

class ACW_OT_event_rebind(bpy.types.Operator):
    bl_idname='animation_curves.event_rebind'
    bl_label='明确采用当前所选键'
    bl_description='只更新所选事件/通道的当前见证；不改动画，不更新其他锁定事件'
    bl_options={'REGISTER','UNDO'}
    @classmethod
    def poll(cls,context):return _available(context)
    def invoke(self,context,event):return context.window_manager.invoke_confirm(self,event)
    def execute(self,context):
        try:
            _clip_guard(context)
            state=_event_state(context);settings=_settings(context);target=_event_target(context)
            inspection=core.inspect(event_ui_state.manifest(state,_event_context(context)))
            updated=event_ui_state.rebind(state,settings.event_name,target['id'],inspection)
            settings.event_state=json.dumps(updated,ensure_ascii=False,allow_nan=False)
            return _publish(self,context,{'before':state,'adopted':updated},'已明确采用所选键；未修改动画')
        except Exception as exc:return _failed(self,context,exc)

class ACW_OT_event_apply(bpy.types.Operator):
    bl_idname='animation_curves.event_apply'
    bl_label='移动所选事件'
    bl_description='检查全部见证后通过共享核心改时；锁定事件和未点名内容受保护'
    bl_options={'REGISTER','UNDO'}
    @classmethod
    def poll(cls,context):return _available(context)
    def execute(self,context):
        try:
            _clip_guard(context)
            state=_event_state(context);settings=_settings(context);ctx=_event_context(context)
            inspection=core.inspect(event_ui_state.manifest(state,ctx))
            request=event_ui_state.edit_manifest(state,ctx,settings.event_name,settings.to_frame,inspection)
            result=core.apply_manifest(request)
        except Exception as exc:
            try:_write_report(context,{'status':'rejected','error':str(exc),'details':getattr(exc,'details',{})})
            except Exception:pass
            return _failed(self,context,exc)
        # A committed native edit is never labelled CANCELLED by a UI refresh failure.
        try:
            after=core.inspect(request)
            updated=event_ui_state.refresh_after_commit(state,result['event_plan'],after)
            settings.event_state=json.dumps(updated,ensure_ascii=False,allow_nan=False)
            result['event_state_refresh']='passed'
        except Exception as exc:
            result['event_state_refresh']={'status':'failed','error':str(exc),'next_step':'检查原生曲线与事件绑定后再继续'}
            return _publish(self,context,result,'动画已提交；绑定显示刷新失败，请重新检查')
        return _publish(self,context,result,'事件改时已提交；请在原生编辑器检查')

class ACW_OT_event_reset(bpy.types.Operator):
    bl_idname='animation_curves.event_reset'
    bl_label='清空事件绑定（不改动画）'
    bl_description='确认后仅移除本面板绑定状态；原生动画不变，可从当前关键帧重新捕获'
    bl_options={'REGISTER','UNDO'}
    @classmethod
    def poll(cls,context):return _available(context)
    def invoke(self,context,event):return context.window_manager.invoke_confirm(self,event)
    def execute(self,context):
        try:
            _clip_guard(context)
            _settings(context).event_state=json.dumps(event_ui_state.empty(),ensure_ascii=False)
        except Exception as exc:return _failed(self,context,exc)
        return _publish(self,context,{'state_reset':True,'native_animation_changed':False},'事件绑定已清空；原生动画未改，可重新捕获')


class ACW_PT_event_editor(bpy.types.Panel):
    bl_idname='ACW_PT_event_editor'
    bl_label='事件绑定与人工接续'
    bl_space_type='VIEW_3D'
    bl_region_type='UI'
    def draw(self,context):
        layout=self.layout
        if not _available(context):
            layout.label(text='请在物体模式中选择目标',icon='INFO');return
        settings=_settings(context)
        layout.label(text='显式绑定；不自动求解空间接触')
        if clip_event.has_record(context.scene):
            layout.label(text='已有完整片段事件记录；旧单键入口只读',icon='LOCKED')
        layout.prop(settings,'event_name');layout.prop(settings,'event_locked')
        layout.prop(settings,'channel');layout.prop(settings,'use_current_frame')
        if not settings.use_current_frame:layout.prop(settings,'frame')
        layout.operator('animation_curves.event_capture')
        layout.operator('animation_curves.event_inspect')
        layout.operator('animation_curves.event_rebind')
        layout.operator('animation_curves.event_reset')
        layout.prop(settings,'to_frame');layout.operator('animation_curves.event_apply')
        try:
            state=_event_state(context)
            layout.label(text=str(len(state['events']))+' 个事件 / '+str(len(state['bindings']))+' 个通道绑定')
            for event in list(state['events'].values())[:8]:
                layout.label(text=event['id']+' @ '+format(event['frame'],'.4g')+(' [锁定]' if event.get('locked') else ''))
        except Exception:layout.label(text='事件状态不可读，请查看报告',icon='ERROR')
        layout.operator('animation_curves.show_report',text='查看验证/差异报告',icon='TEXT')
        if settings.last_report:layout.label(text=settings.last_report[:100])
        layout.label(text='曲线手柄可在曲线编辑器中精修')

class ACW_PT_channel_editor(bpy.types.Panel):
    bl_idname = 'ACW_PT_channel_editor'
    bl_label = '动画曲线'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    def draw(self, context):
        layout = self.layout
        layout.label(text='通道编辑')
        if not _available(context):
            layout.label(text='请在物体模式中选择目标', icon='INFO')
            return
        settings = _settings(context)
        obj = context.active_object
        layout.label(text=obj.name, icon='OBJECT_DATA')
        layout.prop(settings, 'channel')
        layout.prop(settings, 'use_current_frame')
        if settings.use_current_frame:
            layout.label(text='当前帧：' + format(context.scene.frame_current_final, '.4g'))
        else:
            layout.prop(settings, 'frame')
        row = layout.row(align=True)
        row.prop(settings, 'rotation_value' if settings.channel.startswith('rotation_') else 'value')
        row.operator('animation_curves.read_current_value', text='', icon='EYEDROPPER')
        layout.prop(settings, 'interpolation')
        layout.operator('animation_curves.edit_key', text='设置关键帧', icon='KEY_HLT').action = 'set_key'
        layout.separator()
        layout.prop(settings, 'to_frame')
        row = layout.row(align=True)
        row.operator('animation_curves.edit_key', text='移动关键帧').action = 'move_key'
        row.operator('animation_curves.edit_key', text='删除关键帧', icon='KEY_DEHLT').action = 'delete_key'
        layout.separator()
        layout.operator('animation_curves.inspect_channel', text='检查所选通道', icon='VIEWZOOM')
        layout.operator('animation_curves.show_report', text='查看检查报告', icon='TEXT')
        layout.label(text='精细手柄调整请用原生曲线编辑器', icon='GRAPH')
        layout.label(text='多角色事件在「事件」阶段编排', icon='INFO')
        if settings.last_report:
            box = layout.box()
            box.label(text=settings.last_report[:100])


_CLASSES = (ACWSettings, ACW_OT_edit_key, ACW_OT_inspect_channel,
            ACW_OT_read_current_value, ACW_OT_show_report,
            ACW_OT_event_capture, ACW_OT_event_inspect, ACW_OT_event_rebind, ACW_OT_event_apply, ACW_OT_event_reset)
# Blender's bpy.types lookup is not an identity-preserving registry for Python
# classes. Keep the exact class objects that this module successfully registered.
_REGISTERED_CLASSES = []


def _remove_owned_scene_settings():
    if not hasattr(bpy.types.Scene, SETTINGS_PROPERTY):
        return
    prop = bpy.types.Scene.bl_rna.properties.get(SETTINGS_PROPERTY)
    if (ACWSettings not in _REGISTERED_CLASSES or prop is None
            or prop.fixed_type.identifier != ACWSettings.__name__):
        raise RuntimeError('Scene.acw_settings ownership changed; refusing to remove it')
    delattr(bpy.types.Scene, SETTINGS_PROPERTY)


def _unregister_owned_classes():
    # Remove an entry only after successful deregistration, so a failed cleanup
    # remains explicit and can be retried without guessing which classes exist.
    while _REGISTERED_CLASSES:
        cls = _REGISTERED_CLASSES[-1]
        bpy.utils.unregister_class(cls)
        _REGISTERED_CLASSES.pop()


def register():
    if _REGISTERED_CLASSES:
        raise RuntimeError('Animation Curves UI classes are already registered by this module')
    if hasattr(bpy.types.Scene, SETTINGS_PROPERTY):
        raise RuntimeError('Scene.acw_settings already exists; refusing to replace an existing property')
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED_CLASSES.append(cls)
        setattr(bpy.types.Scene, SETTINGS_PROPERTY, PointerProperty(type=ACWSettings))
    except Exception:
        _remove_owned_scene_settings()
        _unregister_owned_classes()
        raise


def unregister():
    # Only remove a property that resolves to this package's registered group.
    _remove_owned_scene_settings()
    _unregister_owned_classes()
