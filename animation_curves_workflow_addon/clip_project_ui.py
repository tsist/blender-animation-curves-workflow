"""Thin multi-event project panel sharing clip_project.inspect/review/execute."""
import json

import bpy
from bpy.props import BoolProperty, FloatProperty, PointerProperty, StringProperty

from . import clip_project
from . import clip_project_contract as contract
from .contract import WorkflowError

SETTINGS_PROPERTY = "acw_project_settings"
REPORT_OWNER_KEY = "acw_project_report_owner"
REPORT_OWNER_VALUE = "animation-event-project-v1"
_CACHE = {}
_OVERVIEWS = {}
_REGISTERED_CLASSES = []


def _settings(context):
    return getattr(context.scene, SETTINGS_PROPERTY)


def _available(context):
    return bool(context.scene and context.view_layer
                and hasattr(context.scene, SETTINGS_PROPERTY))


def _label(operator, context, message):
    try:
        _settings(context).last_report = message[:500]
    except Exception:
        pass
    try:
        operator.report({"INFO"}, message)
    except Exception:
        pass
    try:
        if context.area:
            context.area.tag_redraw()
    except Exception:
        pass


def _failed(operator, context, error):
    code = error.code if isinstance(error, WorkflowError) else type(error).__name__
    message = str(code) + "：" + str(error)
    try:
        _settings(context).last_report = message[:500]
    except Exception:
        pass
    try:
        operator.report({"ERROR"}, message)
    except Exception:
        pass
    return {"CANCELLED"}


def _project_context(context):
    return {"scene": context.scene.name, "view_layer": context.view_layer.name}


def _request(context):
    settings = _settings(context)
    if not settings.advanced:
        scene_context = _project_context(context)
        book, _, _ = clip_project._load_book(context.scene, scene_context)
        event_id = settings.event_id
        entry = book["events"].get(event_id)
        if not entry:
            raise WorkflowError("NOT_FOUND", "请刷新事件列表并选择一项")
        request = {"schema": contract.SCHEMA, "context": scene_context, "event_id": event_id}
        if settings.change_lock:
            return {**request, "action": "set_lock", "locked": not entry["locked"]}
        record = entry["record"]
        old = clip_project._record_event_times(record)
        clip = {"schema": clip_project.CLIP_SCHEMA, "action": "retime", "context": scene_context,
                "event": {"id": event_id, "start": old["start"], "contact": old["contact"],
                          "end": old["end"], "target_contact": float(settings.target_contact)},
                "participants": record["participants"], "locks": record["locks"]}
        return contract.normalize_request({**request, "action": "retime", "clip": clip})
    name = settings.request_text.strip()
    text = bpy.data.texts.get(name) if name else None
    if text is None:
        raise WorkflowError(
            "NOT_FOUND", "请先指定包含 animation-event-project-v1 JSON 的文本块")
    return contract.loads(text.as_string())


def _compact(value):
    out = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    legacy = out.get("legacy_review")
    if isinstance(legacy, dict):
        out["legacy_review"] = {
            "schema": legacy.get("schema"),
            "action": legacy.get("action"),
            "review_sha256": legacy.get("review_sha256"),
            "counts": legacy.get("counts"),
            "warp": legacy.get("warp"),
        }
    legacy_result = out.get("legacy_result")
    if isinstance(legacy_result, dict):
        witness = legacy_result.get("key_witness")
        if isinstance(witness, dict):
            changes = witness.pop("handle_changes", [])
            witness["handle_change_count"] = len(changes)
            witness["handle_change_examples"] = changes[:24]
        contacts = legacy_result.get("contacts")
        if isinstance(contacts, dict):
            contacts.pop("rows", None)
    for row in out.get("events", []):
        diagnostics = row.get("diagnostics")
        if isinstance(diagnostics, dict):
            diagnostics.pop("errors", None)
    return out


def _publish(operator, context, report, summary):
    settings = _settings(context)
    name = settings.report_text
    text = bpy.data.texts.get(name) if name else None
    if text is None or text.get(REPORT_OWNER_KEY) != REPORT_OWNER_VALUE:
        text = bpy.data.texts.new("多事件工程 · 检查报告")
        text[REPORT_OWNER_KEY] = REPORT_OWNER_VALUE
        settings.report_text = text.name
    body = json.dumps(_compact(report), ensure_ascii=False, indent=2,
                      allow_nan=False)
    text.clear()
    text.write(body)
    _label(operator, context, summary)
    return {"FINISHED"}


class ACWProjectSettings(bpy.types.PropertyGroup):
    event_id: StringProperty(name="当前事件", maxlen=128, options={"SKIP_SAVE"})
    target_contact: FloatProperty(name="新接触帧", min=-1000000, max=1000000,
                                  precision=4, options={"SKIP_SAVE"})
    change_lock: BoolProperty(name="切换所选事件的时间锁", options={"SKIP_SAVE"})
    advanced: BoolProperty(name="高级请求", options={"SKIP_SAVE"})
    request_text: StringProperty(
        name="请求文本块", maxlen=256,
        description="包含 animation-event-project-v1 JSON 的 Blender 文本块名称")
    last_report: StringProperty(name="最近结果", options={"HIDDEN"})
    report_text: StringProperty(name="报告文本", options={"HIDDEN"})


class ACW_OT_project_inspect(bpy.types.Operator):
    bl_idname = "animation_curves.project_inspect"
    bl_label = "只读检查多事件工程"
    bl_description = "读取共享事件存储；不修改动画、不创建或更新记录"

    @classmethod
    def poll(cls, context):
        return _available(context)

    def execute(self, context):
        try:
            report = clip_project.inspect(context, {"schema": contract.SCHEMA, "action": "inspect",
                                                     "context": _project_context(context)})
        except Exception as exc:
            return _failed(self, context, exc)
        _OVERVIEWS[context.scene.as_pointer()] = report["events"]
        summary = "检查：" + str(report.get("status")) + \
                  "；事件 " + str(report["counts"]["events"]) + \
                  "，关系 " + str(report["counts"]["relations"])
        return _publish(self, context, report, summary)


class ACW_OT_project_review(bpy.types.Operator):
    bl_idname = "animation_curves.project_review"
    bl_label = "预览多事件工程操作"
    bl_description = "只读复核、关系/几何与既有单事件核心计划；不修改场景"

    @classmethod
    def poll(cls, context):
        return _available(context)

    def execute(self, context):
        try:
            request = _request(context)
            review = clip_project.review(context, request)
        except Exception as exc:
            return _failed(self, context, exc)
        _CACHE[context.scene.as_pointer()] = review
        summary = "预览：" + review["action"] + \
                  "；事件 " + str(review["counts"]["events"])
        if review.get("proposed_book"):
            summary += "；revision→" + str(review["proposed_book"]["revision"])
        return _publish(self, context, review, summary)


class ACW_OT_project_select_event(bpy.types.Operator):
    bl_idname = "animation_curves.project_select_event"
    bl_label = "选择动画事件"
    event_id: StringProperty(options={"SKIP_SAVE"})

    def execute(self, context):
        rows = _OVERVIEWS.get(context.scene.as_pointer(), ())
        event = next((row for row in rows if row["event_id"] == self.event_id), None)
        if not event:
            return _failed(self, context, WorkflowError("NOT_FOUND", "请刷新事件列表"))
        settings = _settings(context)
        settings.event_id = self.event_id
        settings.target_contact = event["contact"]
        return {"FINISHED"}


class ACW_OT_project_execute(bpy.types.Operator):
    bl_idname = "animation_curves.project_execute"
    bl_label = "提交预览中的多事件工程操作"
    bl_description = "新鲜度复核后提交；失败/取消回滚既有单事件绑定与工程存储"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _available(context)

    def execute(self, context):
        review = _CACHE.get(context.scene.as_pointer())
        if review is None:
            return _failed(self, context, WorkflowError(
                "NOT_FOUND", "请先执行“预览多事件工程操作”"))
        try:
            if _request(context) != review["request"]:
                raise WorkflowError("CONFLICT", "请求 JSON 已改变；请重新预览")
            result = clip_project.execute(context, review)
        except Exception as exc:
            return _failed(self, context, exc)
        _CACHE.pop(context.scene.as_pointer(), None)
        _OVERVIEWS.pop(context.scene.as_pointer(), None)
        summary = "已提交：" + result["action"] + \
                  "；事件 " + str(result["counts"]["events"]) + \
                  "；请另存候选工程"
        return _publish(self, context, result, summary)


class ACW_OT_project_show_report(bpy.types.Operator):
    bl_idname = "animation_curves.project_show_report"
    bl_label = "查看多事件工程报告"

    @classmethod
    def poll(cls, context):
        if not _available(context):
            return False
        name = _settings(context).report_text
        text = bpy.data.texts.get(name) if name else None
        return bool(text and text.get(REPORT_OWNER_KEY) == REPORT_OWNER_VALUE)

    def execute(self, context):
        settings = _settings(context)
        name = settings.report_text
        text = bpy.data.texts.get(name) if name else None
        if text is None or text.get(REPORT_OWNER_KEY) != REPORT_OWNER_VALUE:
            return _failed(self, context, WorkflowError(
                "NOT_FOUND", "本插件的多事件工程报告已不存在"))
        context.area.type = "TEXT_EDITOR"
        context.area.spaces.active.text = text
        return {"FINISHED"}


class ACW_PT_project(bpy.types.Panel):
    bl_label = "多事件工程 · 时间 / 锁 / 关系"
    bl_idname = "ACW_PT_project"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    def draw(self, context):
        layout = self.layout
        if not _available(context):
            layout.label(text="当前场景不可用", icon="INFO")
            return
        settings = _settings(context)
        layout.operator("animation_curves.project_inspect", text="刷新事件与卡点", icon="FILE_REFRESH")
        events = _OVERVIEWS.get(context.scene.as_pointer(), ())
        for event in sorted(events, key=lambda row: row["contact"]):
            row = layout.row(align=True)
            label = f'{event["event_id"]}  ·  {event["contact"]:.2f}'
            row.operator("animation_curves.project_select_event", text=label,
                         icon="LOCKED" if event["locked"] else "UNLOCKED",
                         depress=settings.event_id == event["event_id"]).event_id = event["event_id"]
            if event["status"] != "ready":
                row.label(text="需检查", icon="ERROR")
        layout.prop(settings, "advanced")
        if settings.advanced:
            layout.prop_search(settings, "request_text", bpy.data, "texts")
        elif settings.event_id:
            layout.label(text=settings.event_id)
            layout.prop(settings, "change_lock")
            if not settings.change_lock:
                layout.prop(settings, "target_contact")
        row = layout.row(align=True)
        row.operator("animation_curves.project_review", text="预览")
        layout.operator("animation_curves.project_execute",
                        text="提交预览中的工程操作", icon="CHECKMARK")
        layout.operator("animation_curves.project_show_report", text="查看 JSON 报告")
        if settings.last_report:
            layout.label(text=settings.last_report[:120])


_CLASSES = (ACWProjectSettings, ACW_OT_project_inspect, ACW_OT_project_review,
            ACW_OT_project_select_event, ACW_OT_project_execute, ACW_OT_project_show_report)


def _remove_owned_scene_settings():
    if not hasattr(bpy.types.Scene, SETTINGS_PROPERTY):
        return
    prop = bpy.types.Scene.bl_rna.properties.get(SETTINGS_PROPERTY)
    if (ACWProjectSettings not in _REGISTERED_CLASSES or prop is None
            or prop.fixed_type.identifier != ACWProjectSettings.__name__):
        raise RuntimeError("Scene.acw_project_settings ownership changed; "
                           "refusing to remove it")
    delattr(bpy.types.Scene, SETTINGS_PROPERTY)


def register():
    if _REGISTERED_CLASSES:
        raise RuntimeError("Project UI classes are already registered")
    if hasattr(bpy.types.Scene, SETTINGS_PROPERTY):
        raise RuntimeError("Scene.acw_project_settings already exists; "
                           "refusing to replace it")
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED_CLASSES.append(cls)
        setattr(bpy.types.Scene, SETTINGS_PROPERTY,
                PointerProperty(type=ACWProjectSettings))
    except Exception:
        _remove_owned_scene_settings()
        while _REGISTERED_CLASSES:
            bpy.utils.unregister_class(_REGISTERED_CLASSES.pop())
        raise


def unregister():
    _CACHE.clear()
    _OVERVIEWS.clear()
    _remove_owned_scene_settings()
    while _REGISTERED_CLASSES:
        bpy.utils.unregister_class(_REGISTERED_CLASSES.pop())
