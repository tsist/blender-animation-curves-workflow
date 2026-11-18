"""Minimal View3D sidebar for the native viewport recorder candidate."""

from __future__ import annotations

from typing import Any

import bpy

try:
    from . import native_recording as native
except ImportError:  # Standalone self-check outside an add-on package.
    import native_recording as native


class ANIMATION_NATIVE_PT_viewport_recording(bpy.types.Panel):
    """Public start/cancel buttons and an honest viewport-size check."""

    bl_label = "原生视口录屏"
    bl_idname = "ANIMATION_NATIVE_PT_viewport_recording"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "动画曲线"

    @classmethod
    def poll(cls, context: Any) -> bool:
        return (
            not bpy.app.background
            and context.area is not None
            and context.area.type == "VIEW_3D"
        )

    def draw(self, context: Any) -> None:
        layout = self.layout
        area = context.area
        regions = [r for r in area.regions if r.type == "WINDOW"] if area else []
        if regions:
            region = regions[0]
            enough = (
                region.width >= native.core.CAPTURE_WIDTH
                and region.height >= native.core.CAPTURE_HEIGHT
            )
            row = layout.row()
            row.label(
                text=(
                    f"视口 {region.width}×{region.height} "
                    f"（至少 {native.core.CAPTURE_WIDTH}×"
                    f"{native.core.CAPTURE_HEIGHT}）"
                ),
                icon="CHECKMARK" if enough else "ERROR",
            )
        else:
            layout.label(text="当前没有可录制的三维视口", icon="ERROR")

        layout.label(text="录制实际播放，输出原生 1080p")
        layout.label(text="显示模式：白模实体 / 实体视口颜色（无光影） / 当前视口")
        layout.label(text="开始前先确认视口角色已显示")
        session = native.active_summary()
        if session is None:
            column = layout.column(align=True)
            white = column.operator(
                "animation_native.record_viewport",
                text="白模实体录屏",
                icon="MESH_CUBE",
            )
            white.display_mode = native.core.DISPLAY_MODE_SOLID_WHITE_UNLIT
            object_color = column.operator(
                "animation_native.record_viewport",
                text="实体视口颜色录屏",
                icon="OBJECT_DATA",
            )
            object_color.display_mode = (
                native.core.DISPLAY_MODE_SOLID_OBJECT_UNLIT)
            current = column.operator(
                "animation_native.record_viewport",
                text="当前视口录屏",
                icon="VIEW3D",
            )
            current.display_mode = native.core.DISPLAY_MODE_CURRENT
            layout.label(text="白模：单色白；实体颜色：Object.color；当前：保持视口")
        else:
            box = layout.box()
            box.label(text=f"状态：{session['phase']}")
            mode = session.get("display_mode")
            if mode == native.core.DISPLAY_MODE_SOLID_WHITE_UNLIT:
                mode_text = "白模实体"
            elif mode == native.core.DISPLAY_MODE_SOLID_OBJECT_UNLIT:
                mode_text = "实体视口颜色"
            elif mode == native.core.DISPLAY_MODE_CURRENT:
                mode_text = "当前视口"
            else:
                mode_text = str(mode)
            box.label(text=f"显示模式：{mode_text}")
            box.label(
                text=f"已录帧数：{session['encoded']}/{session['expected']}"
            )
            box.label(text=f"视口更新：{session['draws']}")
            if session.get("cancel_requested"):
                box.label(text="正在取消录屏")
            layout.operator(
                "animation_native.cancel_recording",
                text="取消录屏",
                icon="CANCEL",
            )
        layout.label(text="输出目录及编码器可在操作参数中设置")
        layout.label(text="录屏报告会标明漏帧情况")


_CLASSES = (ANIMATION_NATIVE_PT_viewport_recording,)


def register() -> None:
    native.register()
    registered: list[type] = []
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            registered.append(cls)
    except BaseException:
        for cls in reversed(registered):
            try:
                bpy.utils.unregister_class(cls)
            except BaseException:
                pass
        native.unregister()
        raise


def unregister() -> None:
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except BaseException:
            pass
    native.unregister()
