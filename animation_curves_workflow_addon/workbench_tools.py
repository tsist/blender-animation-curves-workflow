"""Artist-facing controls for shared graph, layer and physical animation tools."""
from functools import lru_cache
import json
from pathlib import Path
import sys
import uuid

import bpy
from bpy.app.handlers import persistent

from . import native, production_operations, production_slot
from ._ui_kit import BackgroundJobRunner, ensure_object_mode

WORKSPACE_PROPERTY = "acw_workspace"
RUNNER = BackgroundJobRunner("animation")
GRAPH_TOOLS = (
    ("gaussian_smooth", "高斯平滑", "柔化连续动作中的局部抖动"),
    ("butterworth_smooth", "低通平滑", "平抑高频噪声"),
    ("euler_filter", "欧拉去跳变", "处理所选欧拉旋转曲线的旋转跳变"),
    ("decimate", "减少关键帧", "按比例减少所选曲线的关键帧"),
    ("stepped", "阶梯采样", "按指定步长保持当前姿态"),
)


def settings(context):
    return getattr(context.scene, WORKSPACE_PROPERTY)


def report(operator, context, message, kind="OK"):
    props = settings(context)
    props.last_report = message
    props.last_report_kind = kind
    operator.report({"ERROR"} if kind == "ERROR" else {"INFO"}, message)
    return {"CANCELLED"} if kind == "ERROR" else {"FINISHED"}


def failed(operator, context, error):
    detail = str(error)
    if not any("\u4e00" <= char <= "\u9fff" for char in detail):
        print(f"[A·动画] {detail}")
        detail = "操作未完成，请核对所选目标和动画状态"
    return report(operator, context, detail, "ERROR")


def selected_channels(active):
    anim = active.animation_data if active else None
    if anim is None or anim.action is None or anim.action_slot is None:
        raise ValueError("请选择拥有活动动作的物体或骨架")
    _, bag = production_slot._slot_bags(anim.action)[anim.action_slot.identifier]
    if bag is None:
        raise ValueError("当前动作槽内没有曲线")
    channels = [{"path": curve.data_path, "index": curve.array_index} for curve in bag.fcurves
                if curve.select and not curve.hide]
    if not channels:
        raise ValueError("请在曲线编辑器中选择目标曲线")
    return channels


class ACW_OT_graph_workbench(bpy.types.Operator):
    bl_idname = "animation_curves.graph_workbench"
    bl_label = "处理所选曲线"
    bl_description = "在独立动作中对所选曲线执行滤波，保留原动作以供比较"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = settings(context)
        active = context.active_object
        try:
            channels = selected_channels(active)
            options = {
                "decimate": {"mode": "RATIO", "factor": props.graph_reduction},
                "stepped": {"frame_step": props.graph_step},
            }.get(props.graph_tool, {})
            with ensure_object_mode(context):
                result = production_operations.apply(context, {
                    "op": "graph", "object": active.name, "tool": props.graph_tool,
                    "channels": channels, "options": options,
                })
            if result["status"] == "unchanged":
                return report(self, context, "滤波后曲线与原动作一致", "INFO")
            return report(self, context, f"已处理 {len(channels)} 条曲线")
        except (ValueError, TypeError, KeyError, RuntimeError, native.WorkflowError) as error:
            return failed(self, context, error)


class ACW_OT_additive_workbench(bpy.types.Operator):
    bl_idname = "animation_curves.additive_workbench"
    bl_label = "创建位移修正层"
    bl_description = "在独立 NLA 层内按开始、中点与结束对单轴位移施加偏移"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = settings(context)
        try:
            active = context.active_object
            if active is None:
                raise ValueError("请先选择要修正的物体或骨架")
            bone = context.active_pose_bone if props.correction_bone else None
            if props.correction_bone and bone is None:
                raise ValueError("请在姿态模式选择需要修正的骨骼")
            target = bone if bone else active
            start, end = props.correction_start, props.correction_end
            if end <= start:
                raise ValueError("结束帧须晚于开始帧")
            fields = {
                "op": "correction", "object": active.name, "start": start, "end": end,
                "blend_type": "COMBINE", "channels": [{
                    "path": target.path_from_id("location"),
                    "index": "XYZ".index(props.correction_axis),
                    "interpolation": "BEZIER",
                    "keys": [{"frame": start, "value": 0.0},
                             {"frame": (start + end) / 2.0, "value": props.correction_offset},
                             {"frame": end, "value": 0.0}],
                }],
            }
            with ensure_object_mode(context):
                result = production_operations.apply(context, fields)
            props.correction_layer = result["layer"]
            return report(self, context, f"已创建位移修正层，影响帧 {start:g}–{end:g}")
        except (ValueError, TypeError, KeyError, RuntimeError, native.WorkflowError) as error:
            return failed(self, context, error)


class ACW_OT_set_layer_workbench(bpy.types.Operator):
    bl_idname = "animation_curves.set_layer_workbench"
    bl_label = "应用修正层权重"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = settings(context)
        try:
            active = context.active_object
            if active is None or not props.correction_layer:
                raise ValueError("请创建或选择受管修正层")
            with ensure_object_mode(context):
                production_operations.apply(context, {
                    "op": "layer_state", "object": active.name, "layer_name": props.correction_layer,
                    "mute": props.correction_mute, "influence": props.correction_influence,
                })
            return report(self, context, "已更新修正层权重")
        except (ValueError, TypeError, KeyError, RuntimeError, native.WorkflowError) as error:
            return failed(self, context, error)


@lru_cache(maxsize=2)
def project_root(binary):
    roots = (*Path(__file__).resolve().parents, *Path(binary).resolve().parents)
    return next((root for root in roots if (root / "tools/bridges/cascadeur_bridge/bridge.py").is_file()), None)


def _path(raw):
    return Path(bpy.path.abspath(raw)).resolve() if raw else None


class ACW_OT_cascadeur_workbench(bpy.types.Operator):
    bl_idname = "animation_curves.cascadeur_workbench"
    bl_label = "后台运行物理动画交换"
    bl_description = "从当前工作副本导出动作，使用本机 Cascadeur 润色并生成独立候选"

    def execute(self, context):
        props = settings(context)
        try:
            if RUNNER.running:
                raise ValueError("物理动画交换正在运行")
            root = project_root(bpy.app.binary_path)
            if root is None:
                raise ValueError("未找到本机 Blender AI 动画交换组件")
            mapping = _path(props.bridge_map)
            if mapping is None or not mapping.is_file():
                raise ValueError("请从文件选择器选择骨架语义映射")
            app = _path(props.bridge_program) or root / "cascadeur/cascadeur.exe"
            if not props.bridge_export_only and not app.is_file():
                raise ValueError("请选择本机 Cascadeur 程序")
            python = Path(sys.prefix) / "bin/python.exe"
            if not python.is_file():
                raise ValueError("未找到 Blender 自带的 Python")
            parent = _path(props.bridge_directory) or Path(bpy.app.tempdir).resolve()
            if not parent.is_dir():
                raise ValueError("请选择存在的交换输出目录")
            job = parent / ("animation-cascadeur-" + uuid.uuid4().hex[:12])
            job.mkdir()
            snapshot, output = job / "input.blend", job / "roundtrip"
            request, status, result = (job / f"{name}.json" for name in ("request", "status", "result"))
            with ensure_object_mode(context):
                if bpy.ops.wm.save_as_mainfile(filepath=str(snapshot), copy=True) != {"FINISHED"}:
                    raise ValueError("无法创建本次交换使用的独立副本")
            argv = [str(python), "-X", "utf8", str(root / "tools/bridges/cascadeur_bridge/bridge.py"),
                    "--source", str(snapshot), "--map", str(mapping), "--output", str(output),
                    "--blender", bpy.app.binary_path]
            if props.bridge_export_only:
                argv.append("--export-only")
            else:
                argv.extend(("--start-cascadeur", str(app), "--polish"))
                if props.bridge_ballistic:
                    argv.extend(("--airborne", str(props.bridge_takeoff), str(props.bridge_landing)))
            request.write_text(json.dumps({"argv": argv, "output": str(output), "project": str(root)},
                                          ensure_ascii=False), encoding="utf-8")
            scene_id = context.scene.as_pointer()

            def finish(ok, payload):
                scene = next((item for item in bpy.data.scenes if item.as_pointer() == scene_id), None)
                if scene is None:
                    return
                workspace = getattr(scene, WORKSPACE_PROPERTY)
                workspace.bridge_report = payload.get("report") or str(job / "bridge.log")
                workspace.bridge_candidate = payload.get("candidate", "")
                workspace.last_report = "动画交换已完成" if ok else payload.get("error", "动画交换未完成")
                workspace.last_report_kind = "OK" if ok else "ERROR"

            RUNNER.start(
                [str(python), "-X", "utf8", str(Path(__file__).with_name("bridge_worker.py")),
                 str(request), str(status), str(result)],
                label="物理动画交换", status_file=status, result_file=result, on_complete=finish,
            )
            return report(self, context, "已启动物理动画交换", "RUNNING")
        except (OSError, ValueError, RuntimeError) as error:
            return failed(self, context, error)


class ACW_OT_cancel_bridge(bpy.types.Operator):
    bl_idname = "animation_curves.cancel_bridge_workbench"
    bl_label = "取消物理动画交换"

    def execute(self, context):
        RUNNER.cleanup()
        return report(self, context, "已取消物理动画交换", "WARN")


@persistent
def reset_bridge(_):
    RUNNER.cleanup()


CLASSES = (ACW_OT_graph_workbench, ACW_OT_additive_workbench, ACW_OT_set_layer_workbench,
           ACW_OT_cascadeur_workbench, ACW_OT_cancel_bridge)
