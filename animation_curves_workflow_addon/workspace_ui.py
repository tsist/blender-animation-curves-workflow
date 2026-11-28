"""The four-stage Animation Curves workbench."""
from types import SimpleNamespace

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, IntProperty,
                       PointerProperty, StringProperty)

from . import (
    clip_asset_ui, clip_event_ui, clip_project_ui, cross_event_ui, cue_ui, pose_event_store_ui,
    pose_event_ui, pose_ui, practice_ui, read_job, recording_ui, support_diagnostic_ui, support_job,
    support_public_ui, support_solver_ui, support_ui, templates_ui, ui, workbench_tools,
)
from ._ui_kit import (
    HUDManager, IconPack, SidebarDock, draw_heavy_workspace_header, get_theme,
    primary_button, progress_card, prop_column, section_panel, status_banner,
)

DOMAIN = "animation"
THEME = get_theme(DOMAIN)
_ICONS = IconPack(DOMAIN)
_REGISTERED = []
_OWNS_PROPERTY = False

STAGES = (
    ("POSE", "姿态", "姿态与关键帧曲线"),
    ("SUPPORT", "支撑", "脚底支撑与物理修正"),
    ("EVENTS", "事件", "事件编排与音乐卡点"),
    ("CLIPS", "片段", "片段叠加与预览录制"),
)
LEVELS = (
    ("DAILY", "日常", "显示高频制作操作"),
    ("ADVANCED", "进阶", "显示完整控制与报告"),
)


class ACWWorkspaceSettings(bpy.types.PropertyGroup):
    stage: EnumProperty(name="阶段", items=STAGES, default="POSE")
    level: EnumProperty(name="面板档位", items=LEVELS, default="DAILY")
    graph_tool: EnumProperty(name="曲线工具", items=workbench_tools.GRAPH_TOOLS)
    graph_reduction: FloatProperty(name="减少比例", min=0.01, max=1.0, default=0.35, subtype="FACTOR")
    graph_step: IntProperty(name="步长（帧）", min=1, max=1000, default=2)
    correction_start: FloatProperty(name="开始帧", default=1)
    correction_end: FloatProperty(name="结束帧", default=25)
    correction_offset: FloatProperty(name="中点位移", default=.1, subtype="DISTANCE")
    correction_axis: EnumProperty(name="局部轴", items=tuple((axis, axis, "") for axis in "XYZ"), default="Z")
    correction_bone: BoolProperty(name="修正当前姿态骨")
    correction_layer: StringProperty(name="修正层")
    correction_mute: BoolProperty(name="静音")
    correction_influence: FloatProperty(name="权重", default=1, min=0, max=1, subtype="FACTOR")
    bridge_map: StringProperty(name="骨架语义映射", subtype="FILE_PATH")
    bridge_directory: StringProperty(name="输出目录", subtype="DIR_PATH")
    bridge_program: StringProperty(name="Cascadeur 程序", subtype="FILE_PATH")
    bridge_export_only: BoolProperty(name="仅导出 FBX")
    bridge_ballistic: BoolProperty(name="指定腾空段")
    bridge_takeoff: IntProperty(name="起跳帧", default=11)
    bridge_landing: IntProperty(name="落地帧", default=38)
    bridge_report: StringProperty(name="运行报告", subtype="FILE_PATH", options={"SKIP_SAVE"})
    bridge_candidate: StringProperty(name="候选工程", subtype="FILE_PATH", options={"SKIP_SAVE"})
    last_report: StringProperty(options={"SKIP_SAVE"})
    last_report_kind: StringProperty(default="INFO", options={"SKIP_SAVE"})


def _details(layout, context, view, title=None, icon="NONE"):
    _, body = section_panel(layout, "ACW_SEC_" + view.bl_idname, title or view.bl_label,
                            icon=icon, default_closed=True)
    if body is not None:
        view.draw(SimpleNamespace(layout=body), context)


def _stage_title(layout, text, icon):
    layout.label(text=text, icon_value=_ICONS.action_id(icon))


def _pose(layout, context, props, advanced):
    _stage_title(layout, "姿态与关键帧曲线", "curve_filter")
    row = layout.row(align=True)
    row.operator("animation_curves.create_template", text="创建简形人形", icon="ARMATURE_DATA").kind = (
        "humanoid_fk_two_segment_v2"
    )
    row.operator("animation_curves.create_practice_action", text="创建练习动作", icon="ACTION")
    if context.mode == "POSE":
        layout.operator("animation_curves.capture_pose_batch", text="为所选骨骼记录姿态", icon="KEY_HLT")
        layout.operator("animation_curves.edit_pose_axis", text="编辑姿态骨关键帧", icon="BONE_DATA")
    else:
        row = layout.row(align=True)
        row.operator("animation_curves.edit_key", text="设置当前帧关键帧", icon="KEY_HLT").action = "set_key"
        row.operator("animation_curves.read_current_value", text="", icon="EYEDROPPER")
        if context.active_object:
            row = layout.row(align=True)
            row.prop(ui._settings(context), "channel", text="通道")
            row.prop(ui._settings(context), "interpolation", text="缓动")
    col = prop_column(layout, align=True)
    col.prop(props, "graph_tool")
    if props.graph_tool == "decimate":
        col.prop(props, "graph_reduction")
    elif props.graph_tool == "stepped":
        col.prop(props, "graph_step")
    primary_button(layout, "animation_curves.graph_workbench", "处理所选曲线", icon="GRAPH",
                   icon_value=_ICONS.action_id("curve_filter"))
    if advanced:
        for view in (ui.ACW_PT_channel_editor, pose_ui.ACW_PT_pose_channel,
                     templates_ui.ACW_PT_templates, practice_ui.ACW_PT_practice):
            _details(layout, context, view)


def _support(layout, context, props, advanced):
    _stage_title(layout, "脚底支撑与物理修正", "foot_support")
    primary_button(layout, "animation_curves.support_diagnostic", "检查脚底支撑区间", icon="VIEWZOOM",
                   icon_value=_ICONS.action_id("foot_support"))
    row = layout.row(align=True)
    row.operator("animation_curves.cap_precheck", text="预检防滑补偿")
    row.operator("animation_curves.cap_generate", text="生成补偿工程")
    if support_solver_ui.eligible():
        layout.label(text="支撑预检已通过，可以生成独立工程", icon="CHECKMARK")
    if read_job.JOB.state not in ("EMPTY", *read_job.ACTIVE):
        if read_job.JOB.result and read_job.JOB.result.get("status") == "REQUEST_BOUND_SUPPORT_DIAGNOSTIC":
            diagnostic = read_job.JOB.result["diagnostic"]
            label = "采样发现脚底支撑风险" if diagnostic["status"] == "SAMPLED_SUPPORT_FAIL" else "脚底支撑采样通过"
            layout.label(text=label, icon="INFO")
        elif read_job.JOB.error:
            layout.label(text="支撑检查未完成，进阶中可查看报告", icon="ERROR")
        row = layout.row(align=True)
        row.operator("animation_curves.release_readonly", text="结束当前支撑会话", icon="CHECKMARK")
        if read_job.JOB.result and read_job.JOB.result.get("candidate"):
            row.operator("animation_curves.cap_copy", text="复制工程路径", icon="COPYDOWN")
    _, bridge = section_panel(layout, "ACW_SEC_cascadeur", "Cascadeur 物理润色",
                              icon_value=_ICONS.action_id("cascadeur_bridge"), default_closed=True)
    if bridge is not None:
        col = prop_column(bridge, align=True)
        col.prop(props, "bridge_map")
        col.prop(props, "bridge_directory")
        if advanced:
            col.prop(props, "bridge_program")
            col.prop(props, "bridge_export_only")
            if not props.bridge_export_only:
                col.prop(props, "bridge_ballistic")
                if props.bridge_ballistic:
                    row = bridge.row(align=True)
                    row.prop(props, "bridge_takeoff")
                    row.prop(props, "bridge_landing")
        primary_button(bridge, "animation_curves.cascadeur_workbench", "启动后台交换",
                       icon_value=_ICONS.action_id("cascadeur_bridge"), icon="PLAY",
                       enabled=not workbench_tools.RUNNER.running)
        if props.bridge_report:
            row = bridge.row()
            row.enabled = False
            row.prop(props, "bridge_report")
        if props.bridge_candidate:
            row = bridge.row()
            row.enabled = False
            row.prop(props, "bridge_candidate")
    if advanced:
        for view in (support_diagnostic_ui.ACW_PT_support_diagnostic, support_solver_ui.ACW_PT_cap_solver,
                     support_ui.ACW_PT_support_input, support_public_ui.ACW_PT_support_public):
            _details(layout, context, view)


def _events(layout, context, props, advanced):
    _stage_title(layout, "事件编排与音乐卡点", "event_beat")
    row = layout.row(align=True)
    row.operator("animation_curves.pose_record_parameters", text="双角色事件", icon="CON_ARMATURE")
    row.operator("animation_curves.cross_record_parameters", text="双人加灯光", icon="LIGHT")
    primary_button(layout, "animation_curves.cue_parameters", "指定音乐拍点",
                   icon_value=_ICONS.action_id("event_beat"), icon="SOUND")
    row = layout.row(align=True)
    row.operator("animation_curves.project_inspect", text="多人事件清单", icon="VIEWZOOM")
    row.operator("animation_curves.project_review", text="审阅改时", icon="CHECKMARK")
    if advanced:
        for view in (ui.ACW_PT_event_editor, pose_event_ui.ACW_PT_pose_event,
                     pose_event_store_ui.ACW_PT_pose_record, cross_event_ui.ACW_PT_cross_record,
                     cue_ui.ACW_PT_cue, clip_project_ui.ACW_PT_project):
            _details(layout, context, view)


def _clips(layout, context, props, advanced):
    _stage_title(layout, "片段叠加与预览录制", "additive_layer")
    row = layout.row(align=True)
    row.operator("animation_curves.clip_asset_apply", text="复用动作片段", icon="ASSET_MANAGER")
    row.operator("animation_curves.clip_review", text="审阅片段范围", icon="VIEWZOOM")
    _, layer = section_panel(layout, "ACW_SEC_additive", "位移修正层",
                             icon_value=_ICONS.action_id("additive_layer"), default_closed=False)
    if layer is not None:
        row = layer.row(align=True)
        row.prop(props, "correction_start")
        row.prop(props, "correction_end")
        col = prop_column(layer, align=True)
        col.prop(props, "correction_axis")
        col.prop(props, "correction_offset")
        if context.mode == "POSE":
            col.prop(props, "correction_bone")
        layer.operator("animation_curves.additive_workbench")
        if props.correction_layer:
            layer.prop(props, "correction_layer")
            row = layer.row(align=True)
            row.prop(props, "correction_mute")
            row.prop(props, "correction_influence")
            layer.operator("animation_curves.set_layer_workbench")
    session = recording_ui.native.active_summary()
    if session is None and recording_ui.ANIMATION_NATIVE_PT_viewport_recording.poll(context):
        action = layout.operator("animation_native.record_viewport", text="录制当前三维视口", icon="RENDER_ANIMATION")
        action.display_mode = recording_ui.native.core.DISPLAY_MODE_CURRENT
    if advanced:
        for view in (clip_event_ui.ACW_PT_clip_event, clip_project_ui.ACW_PT_project,
                     clip_asset_ui.ACW_PT_clip_asset, recording_ui.ANIMATION_NATIVE_PT_viewport_recording):
            _details(layout, context, view)


_DRAW_STAGE = {"POSE": _pose, "SUPPORT": _support, "EVENTS": _events, "CLIPS": _clips}


def _progress(layout):
    runner = workbench_tools.RUNNER
    if runner.running:
        progress_card(layout, DOMAIN, _ICONS, text=runner.detail or runner.label,
                      factor=runner.progress, cancel_operator="animation_curves.cancel_bridge_workbench")
    if read_job.JOB.state in read_job.ACTIVE:
        phase = getattr(read_job.JOB, "phase", None)
        caption = "正在独立重开核验" if phase == "cold" else "正在进行支撑采样"
        progress_card(layout, DOMAIN, _ICONS, text=caption, factor=.82 if phase == "cold" else .2,
                      cancel_operator="animation_curves.cancel_readonly")
    if support_job.JOB.state in support_job.ACTIVE:
        progress_card(layout, DOMAIN, _ICONS, text="正在验证参考支撑工程", factor=.2,
                      cancel_operator="animation_curves.support_cancel")
    session = recording_ui.native.active_summary()
    if session is not None:
        progress_card(layout, DOMAIN, _ICONS, text=f"正在录制 {session['encoded']}/{session['expected']} 帧",
                      factor=session["encoded"] / max(1, session["expected"]),
                      cancel_operator="animation_native.cancel_recording")


class ACW_PT_workbench(bpy.types.Panel):
    bl_idname = "ACW_PT_workbench"
    bl_label = ""
    bl_space_type = THEME.editor_space
    bl_region_type = "UI"
    bl_category = THEME.tab_label
    bl_options = {"HIDE_HEADER"}

    def draw(self, context):
        layout = self.layout
        props = workbench_tools.settings(context)
        draw_heavy_workspace_header(layout, DOMAIN, _ICONS, props, stage_prop="stage", level_prop="level")
        _progress(layout)
        _DRAW_STAGE[props.stage](layout, context, props, props.level == "ADVANCED")
        if props.last_report:
            status_banner(layout, context, props.last_report, kind=props.last_report_kind, icon_pack=_ICONS)


_CLASSES = (ACWWorkspaceSettings, *workbench_tools.CLASSES, ACW_PT_workbench)


def register():
    global _OWNS_PROPERTY
    property_name = workbench_tools.WORKSPACE_PROPERTY
    if _REGISTERED or hasattr(bpy.types.Scene, property_name):
        raise RuntimeError("动画工作台属性已注册")
    try:
        _ICONS.register()
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
        setattr(bpy.types.Scene, property_name, PointerProperty(type=ACWWorkspaceSettings))
        _OWNS_PROPERTY = True
        bpy.app.handlers.load_pre.append(workbench_tools.reset_bridge)
        SidebarDock.register_plugin(DOMAIN, _ICONS.icons_dir)
    except Exception:
        unregister()
        raise


def unregister():
    global _OWNS_PROPERTY
    workbench_tools.reset_bridge(None)
    if workbench_tools.reset_bridge in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(workbench_tools.reset_bridge)
    SidebarDock.unregister_plugin(DOMAIN)
    HUDManager.clear(DOMAIN)
    _ICONS.unregister()
    if _OWNS_PROPERTY:
        delattr(bpy.types.Scene, workbench_tools.WORKSPACE_PROPERTY)
        _OWNS_PROPERTY = False
    while _REGISTERED:
        bpy.utils.unregister_class(_REGISTERED[-1])
        _REGISTERED.pop()
