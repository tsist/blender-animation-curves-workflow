"""Sidebar GUI for the P2 full-clip capture/retime event."""
import json

import bpy
from bpy.props import EnumProperty, FloatProperty, PointerProperty, StringProperty

from . import clip_event
from .contract import WorkflowError

SETTINGS_PROPERTY = "acw_clip_settings"
REPORT_OWNER_KEY = "acw_clip_report_owner"
REPORT_OWNER_VALUE = "animation-clip-event-v1"
_CACHE = {}
_REGISTERED_CLASSES = []


def _settings(context):
    return getattr(context.scene, SETTINGS_PROPERTY)


def _available(context):
    return bool(context.scene and context.view_layer
                and hasattr(context.scene, SETTINGS_PROPERTY))


def _label(operator, context, message):
    try:
        _settings(context).last_report = message
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


def _report_text(context, report):
    settings = _settings(context)
    text = bpy.data.texts.get(settings.report_text) if settings.report_text else None
    if text is None or text.get(REPORT_OWNER_KEY) != REPORT_OWNER_VALUE:
        text = bpy.data.texts.new("完整片段事件 · 检查报告")
        text[REPORT_OWNER_KEY] = REPORT_OWNER_VALUE
        settings.report_text = text.name
    body = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    text.clear()
    text.write(body)
    return text.name


def _publish(operator, context, report, summary):
    try:
        _report_text(context, report)
    except Exception as exc:
        summary = summary + "；报告写入失败：" + str(exc)
    _label(operator, context, summary)
    return {"FINISHED"}


def _compact_result(result):
    """Keep the Text report small; full technical evidence stays in CLI JSON."""
    out = json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
    key_witness = out.get("key_witness")
    if isinstance(key_witness, dict):
        changes = key_witness.pop("handle_changes", [])
        key_witness["handle_change_examples"] = changes[:24]
    contacts = out.get("contacts")
    if isinstance(contacts, dict):
        contacts.pop("rows", None)
    warp = out.get("warp")
    if isinstance(warp, dict):
        warp.pop("rows", None)
    protection = out.get("protection")
    if isinstance(protection, dict):
        derived = protection.pop("derived_handle_changes", None)
        if isinstance(derived, list):
            protection["derived_handle_change_count"] = len(derived)
            protection["derived_handle_change_examples"] = derived[:16]
    return out


def _locks(settings):
    value = settings.locks.strip()
    if not value:
        return []
    out = []
    for part in value.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    return out


def _object_names(settings):
    value = settings.object_names.replace("，", ",")
    return [part.strip() for part in value.split(",") if part.strip()]


def _request(settings, action):
    mode = getattr(settings, "participant_mode", "rig")
    if mode == "objects":
        names = _object_names(settings)
        if not 1 <= len(names) <= 8:
            raise WorkflowError("INVALID_REQUEST",
                                "普通对象模式需要1..8个显式MESH/EMPTY名称")
        participants = {
            "rigs": [], "roots": [], "lights": [], "materials": [],
            "objects": names,
        }
    else:
        values = {
            "rig_a": settings.rig_a_name.strip(),
            "rig_b": settings.rig_b_name.strip(),
            "root_a": settings.root_a_name.strip(),
            "root_b": settings.root_b_name.strip(),
            "light": settings.light_name.strip(),
            "material_a": settings.material_a_name.strip(),
            "material_b": settings.material_b_name.strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise WorkflowError("INVALID_REQUEST",
                                "请显式选择全部参与者：" + ", ".join(missing))
        participants = {
            "rigs": [values["rig_a"], values["rig_b"]],
            "roots": [values["root_a"], values["root_b"]],
            "lights": [values["light"]],
            "materials": [values["material_a"], values["material_b"]],
        }
    event = {
        "id": settings.event_id,
        "start": float(settings.start),
        "contact": float(settings.contact),
        "end": float(settings.end),
    }
    if action == "retime":
        event["target_contact"] = float(settings.target_contact)
    return {
        "schema": "animation-clip-event-v1",
        "action": action,
        "context": {"scene": settings.scene_name or bpy.context.scene.name,
                    "view_layer": settings.view_layer_name or bpy.context.view_layer.name},
        "event": event,
        "participants": participants,
        "locks": _locks(settings),
    }


def _refresh_context_names(settings, context):
    settings.scene_name = context.scene.name
    settings.view_layer_name = context.view_layer.name


class ACWClipSettings(bpy.types.PropertyGroup):
    scene_name: StringProperty(name="场景", options={"HIDDEN"})
    view_layer_name: StringProperty(name="视图层", options={"HIDDEN"})
    participant_mode: EnumProperty(name="参与者模式", items=(
        ("rig", "人形四组", "骨架+根+灯光+材质；旧请求原样可用"),
        ("objects", "普通对象", "1..8个显式MESH/EMPTY的XYZ Euler TRS通道"),
    ), default="rig")
    mode: EnumProperty(name="操作", items=(
        ("capture", "捕获", "只写事件记录，不修改动画"),
        ("retime", "改时", "按记录与当前 Action 重读并改时"),
    ), default="capture")
    event_id: StringProperty(name="事件 ID", default="block", maxlen=128)
    start: FloatProperty(name="开始帧", default=48.0)
    contact: FloatProperty(name="接触帧", default=66.0)
    end: FloatProperty(name="结束帧", default=80.0)
    target_contact: FloatProperty(name="目标接触帧", default=68.0)
    locks: StringProperty(name="锁定帧（逗号分隔）", default="112,240")
    rig_a_name: StringProperty(name="角色 A 骨架名")
    rig_b_name: StringProperty(name="角色 B 骨架名")
    root_a_name: StringProperty(name="角色 A 根名")
    root_b_name: StringProperty(name="角色 B 根名")
    light_name: StringProperty(name="格挡灯光对象名")
    material_a_name: StringProperty(name="发光材质 A 名")
    material_b_name: StringProperty(name="发光材质 B 名")
    object_names: StringProperty(name="普通对象名单", maxlen=4096,
                                 description="显式MESH/EMPTY名称，逗号分隔，1..8个")
    last_report: StringProperty(name="最近结果", options={"HIDDEN"})
    report_text: StringProperty(name="报告文本", options={"HIDDEN"})


class ACW_OT_clip_use_selection(bpy.types.Operator):
    bl_idname = "animation_curves.clip_use_selection"
    bl_label = "从当前选择填入显式参与者"
    bl_description = "只读取用户当前显式选择，不猜测材质或名称"

    @classmethod
    def poll(cls, context):
        return _available(context)

    def execute(self, context):
        settings = _settings(context)
        _refresh_context_names(settings, context)
        if getattr(settings, "participant_mode", "rig") == "objects":
            objects = [obj for obj in context.selected_objects
                       if obj.type in {"MESH", "EMPTY"}]
            if not objects:
                return _failed(self, context, WorkflowError(
                    "INVALID_REQUEST", "当前选择没有MESH/EMPTY普通对象"))
            if len(objects) > 8:
                return _failed(self, context, WorkflowError(
                    "INVALID_REQUEST", "普通对象最多8个；当前选择超过上限"))
            settings.object_names = ", ".join(obj.name for obj in objects)
            _label(self, context, "已从当前选择填入普通对象；不套用模板角色")
            return {"FINISHED"}
        rigs = [obj for obj in context.selected_objects if obj.type == "ARMATURE"]
        roots = [obj for obj in context.selected_objects if obj.type == "EMPTY"]
        lights = [obj for obj in context.selected_objects if obj.type == "LIGHT"]
        if len(rigs) >= 1:
            settings.rig_a_name = rigs[0].name
        if len(rigs) >= 2:
            settings.rig_b_name = rigs[1].name
        if len(roots) >= 1:
            settings.root_a_name = roots[0].name
        if len(roots) >= 2:
            settings.root_b_name = roots[1].name
        if len(lights) >= 1:
            settings.light_name = lights[0].name
        _label(self, context, "已从当前选择填入；材质仍须显式选择")
        return {"FINISHED"}


class ACW_OT_clip_pick_active(bpy.types.Operator):
    bl_idname = "animation_curves.clip_pick_active"
    bl_label = "从活动对象/材质填入"
    bl_description = "按显式角色从当前活动对象或活动材质填入名称；不做名称猜测"

    role: EnumProperty(items=(
        ("rig_a", "角色 A", ""), ("rig_b", "角色 B", ""),
        ("root_a", "根 A", ""), ("root_b", "根 B", ""),
        ("light", "灯光", ""),
        ("material_a", "材质 A", ""), ("material_b", "材质 B", ""),
        ("object", "普通对象", ""),
    ), default="rig_a")

    @classmethod
    def poll(cls, context):
        return _available(context) and context.active_object is not None

    def execute(self, context):
        obj = context.active_object
        settings = _settings(context)
        _refresh_context_names(settings, context)
        if self.role == "object":
            if obj.type not in {"MESH", "EMPTY"}:
                return _failed(self, context, WorkflowError(
                    "INVALID_REQUEST", "普通对象需要活动对象为MESH或EMPTY"))
            names = _object_names(settings)
            if obj.name not in names:
                if len(names) >= 8:
                    return _failed(self, context, WorkflowError(
                        "INVALID_REQUEST", "普通对象名单已达8个上限"))
                names.append(obj.name)
            settings.participant_mode = "objects"
            settings.object_names = ", ".join(names)
            _label(self, context, "已填入普通对象：" + obj.name)
            return {"FINISHED"}
        if self.role in {"rig_a", "rig_b"}:
            if obj.type != "ARMATURE":
                return _failed(self, context, WorkflowError(
                    "INVALID_REQUEST", "该角色需要活动对象为骨架"))
            value = obj.name
        elif self.role in {"root_a", "root_b"}:
            if obj.type != "EMPTY":
                return _failed(self, context, WorkflowError(
                    "INVALID_REQUEST", "该角色需要活动对象为空物体"))
            value = obj.name
        elif self.role == "light":
            if obj.type != "LIGHT":
                return _failed(self, context, WorkflowError(
                    "INVALID_REQUEST", "该角色需要活动对象为灯光"))
            value = obj.name
        else:
            material = obj.active_material
            if material is None:
                return _failed(self, context, WorkflowError(
                    "INVALID_REQUEST", "活动对象没有活动材质"))
            value = material.name
        setattr(settings, self.role + "_name", value)
        _label(self, context, "已填入 " + self.role + "：" + value)
        return {"FINISHED"}


class ACW_OT_clip_inspect(bpy.types.Operator):
    bl_idname = "animation_curves.clip_inspect"
    bl_label = "只读检查完整片段事件"
    bl_description = "不修改动画，不创建或更新事件记录"

    @classmethod
    def poll(cls, context):
        return _available(context)

    def execute(self, context):
        try:
            settings = _settings(context)
            _refresh_context_names(settings, context)
            action = settings.mode
            report = clip_event.inspect(context, _request(settings, action))
        except Exception as exc:
            return _failed(self, context, exc)
        status = report.get("status")
        summary = "检查：" + str(status)
        if report.get("counts"):
            summary += "；通道 " + str(report["counts"]["channels"]) + \
                       "，绑定 " + str(report["counts"]["bindings"])
        return _publish(self, context, report, summary)


class ACW_OT_clip_review(bpy.types.Operator):
    bl_idname = "animation_curves.clip_review"
    bl_label = "预览并准备提交"
    bl_description = "只读复核 origin、目标键和操作摘要；不修改动画或记录"

    @classmethod
    def poll(cls, context):
        return _available(context)

    def execute(self, context):
        try:
            settings = _settings(context)
            _refresh_context_names(settings, context)
            action = settings.mode
            request = _request(settings, action)
            review = clip_event.review(context, request)
        except Exception as exc:
            return _failed(self, context, exc)
        _CACHE[context.scene.as_pointer()] = review
        counts = review["counts"]
        summary = "预览：" + action + "；通道 " + str(counts["channels"]) + \
                  "，绑定 " + str(counts["bindings"]) + \
                  "，操作 " + str(counts["operations"])
        if review.get("warp"):
            summary += "；接触 " + str(review["warp"]["contact"]) + \
                       "→" + str(review["warp"]["target_contact"])
        return _publish(self, context, {
            "schema": review["schema"], "action": action,
            "review_sha256": review["review_sha256"],
            "counts": counts, "warp": review.get("warp"),
            "handle_impact": review.get("handle_impact"),
            "origin": {"structure_sha256": review["origin"]["structure_sha256"],
                       "record": review["origin"]["record"]},
        }, summary)


class ACW_OT_clip_execute(bpy.types.Operator):
    bl_idname = "animation_curves.clip_execute"
    bl_label = "提交预览中的完整片段事件"
    bl_description = "新鲜度复核后提交；失败/取消回滚自身 Action 绑定与记录；不保存文件"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _available(context)

    def execute(self, context):
        review = _CACHE.get(context.scene.as_pointer())
        if review is None:
            return _failed(self, context, WorkflowError(
                "NOT_FOUND", "请先执行“预览并准备提交”"))
        try:
            settings = _settings(context)
            _refresh_context_names(settings, context)
            current = _request(settings, review["action"])
            if current != review["request"]:
                raise WorkflowError(
                    "CONFLICT", "面板参与者/时间已改变；请重新预览")
            result = clip_event.execute(context, review)
        except Exception as exc:
            return _failed(self, context, exc)
        _CACHE.pop(context.scene.as_pointer(), None)
        summary = "已提交：" + result["action"] + \
                  "；记录 revision " + str(result["record"]["revision"]) + \
                  "；请另存候选工程"
        return _publish(self, context, _compact_result(result), summary)


class ACW_OT_clip_clear_record(bpy.types.Operator):
    bl_idname = "animation_curves.clip_clear_record"
    bl_label = "清除完整片段事件记录（不改动画）"
    bl_description = "显式维护入口：删除记录后可从当前 Action 重新捕获"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _available(context)

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        try:
            result = clip_event.clear_record(context)
        except Exception as exc:
            return _failed(self, context, exc)
        _CACHE.pop(context.scene.as_pointer(), None)
        return _publish(self, context, result, "事件记录已清除；原生动画未改")


class ACW_OT_clip_fill_record(bpy.types.Operator):
    bl_idname = "animation_curves.clip_fill_record"
    bl_label = "从记录填入事件时间"
    bl_description = "只读记录，填入事件 ID/开始/接触/结束/锁帧；不改动画"

    @classmethod
    def poll(cls, context):
        return _available(context)

    def execute(self, context):
        try:
            summary = clip_event.record_summary(context.scene)
            if summary is None:
                raise WorkflowError("NOT_FOUND", "尚无完整片段事件记录")
            settings = _settings(context)
            event = summary["event"]
            settings.mode = "retime"
            settings.event_id = event["id"]
            settings.start = event["frame"]
            settings.contact = event["contact"]
            settings.end = event["end"]
            settings.target_contact = event["contact"]
            settings.locks = ",".join(format(float(v), "g")
                                      for v in summary["locks"])
        except Exception as exc:
            return _failed(self, context, exc)
        _label(self, context, "已从记录填入；修改目标接触帧后再预览")
        return {"FINISHED"}


class ACW_OT_clip_show_report(bpy.types.Operator):
    bl_idname = "animation_curves.clip_show_report"
    bl_label = "查看完整片段事件报告"

    @classmethod
    def poll(cls, context):
        if not _available(context):
            return False
        name = _settings(context).report_text
        text = bpy.data.texts.get(name) if name else None
        return bool(text and text.get(REPORT_OWNER_KEY) == REPORT_OWNER_VALUE)

    def execute(self, context):
        name = _settings(context).report_text
        text = bpy.data.texts.get(name) if name else None
        if text is None or text.get(REPORT_OWNER_KEY) != REPORT_OWNER_VALUE:
            return _failed(self, context, WorkflowError(
                "NOT_FOUND", "本插件的完整片段事件报告已不存在"))
        context.area.type = "TEXT_EDITOR"
        context.area.spaces.active.text = text
        return {"FINISHED"}


class ACW_PT_clip_event(bpy.types.Panel):
    bl_label = "完整片段事件 · 捕获 / 改时"
    bl_idname = "ACW_PT_clip_event"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "动画曲线"

    def draw(self, context):
        layout = self.layout
        if not _available(context):
            layout.label(text="当前场景不可用", icon="INFO")
            return
        settings = _settings(context)
        if getattr(settings, "participant_mode", "rig") == "objects":
            layout.label(text="普通MESH/EMPTY · XYZ Euler TRS9 · 显式对象")
        else:
            layout.label(text="全身 Euler + 手四元数 + 根 + 光材；显式参与者")
        try:
            summary = clip_event.record_summary(context.scene)
        except Exception as exc:
            layout.label(text="记录损坏：" + str(exc)[:80], icon="ERROR")
            summary = None
        if summary:
            layout.label(text="记录 revision " + str(summary["revision"]) + \
                              " / " + summary["event"]["id"])
            layout.label(text="接触 " + format(summary["event"]["contact"], ".4g") + \
                              " · 绑定 " + str(summary["binding_count"]))
            layout.operator("animation_curves.clip_fill_record",
                            text="从记录填入事件时间")
            layout.operator("animation_curves.clip_clear_record",
                            text="清除记录以重新捕获", icon="TRASH")
        else:
            layout.label(text="尚无完整片段事件记录", icon="INFO")
        layout.prop(settings, "mode")
        layout.prop(settings, "event_id")
        row = layout.row(align=True)
        row.prop(settings, "start")
        row.prop(settings, "contact")
        row.prop(settings, "end")
        if settings.mode == "retime":
            layout.prop(settings, "target_contact")
        layout.prop(settings, "locks")
        box = layout.box()
        box.prop(settings, "participant_mode")
        if getattr(settings, "participant_mode", "rig") == "objects":
            box.label(text="普通对象名单（1..8，MESH/EMPTY）")
            box.prop(settings, "object_names")
            row = box.row(align=True)
            row.operator("animation_curves.clip_pick_active",
                         text="普通对象 ← 活动对象").role = "object"
        else:
            box.label(text="显式参与者（当前场景数据）")
            for field in ("rig_a_name", "rig_b_name", "root_a_name",
                          "root_b_name", "light_name", "material_a_name",
                          "material_b_name"):
                box.prop(settings, field)
            row = box.row(align=True)
            row.operator("animation_curves.clip_pick_active",
                         text="骨架 A ← 活动对象").role = "rig_a"
            row.operator("animation_curves.clip_pick_active",
                         text="材质 A ← 活动材质").role = "material_a"
            row = box.row(align=True)
            row.operator("animation_curves.clip_pick_active",
                         text="材质 B ← 活动材质").role = "material_b"
        layout.operator("animation_curves.clip_use_selection")
        layout.separator()
        row = layout.row(align=True)
        row.operator("animation_curves.clip_inspect", text="只读检查")
        row.operator("animation_curves.clip_review", text="预览")
        layout.operator("animation_curves.clip_execute",
                        text="提交预览中的事件", icon="CHECKMARK")
        layout.operator("animation_curves.clip_show_report", text="查看 JSON 报告")
        if settings.last_report:
            layout.label(text=settings.last_report[:120])
        layout.label(text="不自动保存；改时后请另存候选", icon="FILE_TICK")


_CLASSES = (ACWClipSettings, ACW_OT_clip_use_selection, ACW_OT_clip_pick_active,
            ACW_OT_clip_inspect,
            ACW_OT_clip_review, ACW_OT_clip_execute, ACW_OT_clip_clear_record,
            ACW_OT_clip_fill_record, ACW_OT_clip_show_report, ACW_PT_clip_event)


def _remove_owned_scene_settings():
    if not hasattr(bpy.types.Scene, SETTINGS_PROPERTY):
        return
    prop = bpy.types.Scene.bl_rna.properties.get(SETTINGS_PROPERTY)
    if (ACWClipSettings not in _REGISTERED_CLASSES or prop is None
            or prop.fixed_type.identifier != ACWClipSettings.__name__):
        raise RuntimeError("Scene.acw_clip_settings ownership changed; refusing to remove it")
    delattr(bpy.types.Scene, SETTINGS_PROPERTY)


def register():
    if _REGISTERED_CLASSES:
        raise RuntimeError("Clip event UI classes are already registered")
    if hasattr(bpy.types.Scene, SETTINGS_PROPERTY):
        raise RuntimeError("Scene.acw_clip_settings already exists; refusing to replace it")
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED_CLASSES.append(cls)
        setattr(bpy.types.Scene, SETTINGS_PROPERTY,
                PointerProperty(type=ACWClipSettings))
    except Exception:
        _remove_owned_scene_settings()
        while _REGISTERED_CLASSES:
            bpy.utils.unregister_class(_REGISTERED_CLASSES.pop())
        raise


def unregister():
    _remove_owned_scene_settings()
    while _REGISTERED_CLASSES:
        bpy.utils.unregister_class(_REGISTERED_CLASSES.pop())
