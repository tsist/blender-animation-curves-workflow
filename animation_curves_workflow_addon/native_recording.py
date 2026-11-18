"""Plugin-native real viewport recording candidate for Blender 5.2.

This is the Blender-facing half of the candidate.  It is intentionally not a
renderer and not a general capture framework:

* playback is started with ``bpy.ops.screen.animation_play``;
* the default route reads final window pixels with
  ``bpy.types.Window.screenshot(region=..., use_alpha=False)`` from a modal
  timer driven by real playback;
* the old SpaceView3D POST_PIXEL active-framebuffer route remains only as an
  explicit legacy diagnostic; in Blender 5.2 Material view it captured the
  overlay framebuffer (camera border only, no materials);
* exactly 1920x1080 native pixels are cropped from a viewport that must be at
  least 1920x1080; no scale/upscale/interpolation is present;
* a bounded queue feeds a bytes-only thread that owns no Blender API access;
* exactly one ffmpeg Popen is owned and reaped by the main thread.

The module does not launch the GUI, save a blend, write preferences, reset or
open a project, or stop playback it did not start.  It is a candidate for the
main thread to integrate and verify in a real GUI.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bpy
import gpu

try:
    from . import recording_core as core
    from . import screen_capture_core as capture_core
except ImportError:  # Standalone self-check outside an add-on package.
    import recording_core as core
    import screen_capture_core as capture_core


_REQUIRED_WIDTH = core.CAPTURE_WIDTH
_REQUIRED_HEIGHT = core.CAPTURE_HEIGHT
_ACTIVE_SESSION: "NativeRecordingSession | None" = None
_REGISTERED = False
_REGISTERED_CLASSES: list[type] = []
_HANDLERS_INSTALLED = False


@dataclass
class Preflight:
    """Validated, read-only snapshot of the GUI/context admission."""

    window: Any
    screen: Any
    area: Any
    region: Any
    space: Any
    scene: Any
    window_ptr: int
    screen_ptr: int
    area_ptr: int
    region_ptr: int
    scene_ptr: int
    start_frame: int
    end_frame: int
    expected_count: int
    fps_num: Any
    fps_den: Any
    fps_value: float
    fps_text: str
    output_root: Path
    ffmpeg_path: Path
    crop_x: int
    crop_y: int
    crop_early: core.Crop
    hide_overlays: bool
    flip_vertical: bool
    queue_frames: int
    crf: int
    timeout_seconds: float
    region_size: tuple[int, int]
    window_size: tuple[int, int]
    area_xy: tuple[int, int]
    region_xy: tuple[int, int]
    capture_source: str
    capture_route: dict[str, Any]
    display_mode: str
    display_state: dict[str, Any]
    source_path: Path
    source_sha256_start: str
    view_snapshot: dict[str, Any]
    operator_options: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_pointer": self.window_ptr,
            "screen_pointer": self.screen_ptr,
            "area_pointer": self.area_ptr,
            "region_pointer": self.region_ptr,
            "scene_pointer": self.scene_ptr,
            "window_size": list(self.window_size),
            "region_size": list(self.region_size),
            "area_xy": list(self.area_xy),
            "region_xy": list(self.region_xy),
            "capture_source": self.capture_source,
            "capture_route": dict(self.capture_route),
            "display_mode": self.display_mode,
            "display_state": dict(self.display_state),
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "expected_count": self.expected_count,
            "fps_num": self.fps_num,
            "fps_den": self.fps_den,
            "fps_value": self.fps_value,
            "fps_text": self.fps_text,
            "output_root": str(self.output_root),
            "ffmpeg_path": str(self.ffmpeg_path),
            "crop_x": self.crop_x,
            "crop_y": self.crop_y,
            "crop_early": self.crop_early.as_dict(),
            "hide_overlays": self.hide_overlays,
            "flip_vertical": self.flip_vertical,
            "queue_frames": self.queue_frames,
            "crf": self.crf,
            "timeout_seconds": self.timeout_seconds,
            "source_path": str(self.source_path),
            "source_sha256_start": self.source_sha256_start,
            "view": dict(self.view_snapshot),
            "operator_options": dict(self.operator_options),
            "required_native_size": [_REQUIRED_WIDTH, _REQUIRED_HEIGHT],
        }


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name)
    except BaseException:
        return default


def _display_state(space: Any) -> dict[str, Any]:
    shading = _safe_attr(space, "shading")
    overlay = _safe_attr(space, "overlay")

    def value(owner: Any, name: str) -> Any:
        raw = _safe_attr(owner, name, None)
        if isinstance(raw, (str, bytes, bytearray)):
            return raw
        try:
            return [float(item) for item in raw]
        except BaseException:
            pass
        return raw

    return {
        "shading_type": value(shading, "type"),
        "light": value(shading, "light"),
        "color_type": value(shading, "color_type"),
        "single_color": value(shading, "single_color"),
        "show_shadows": value(shading, "show_shadows"),
        "show_cavity": value(shading, "show_cavity"),
        "show_specular_highlight": value(shading, "show_specular_highlight"),
        "background_type": value(shading, "background_type"),
        "background_color": value(shading, "background_color"),
        "show_object_outline": value(shading, "show_object_outline"),
        "object_outline_color": value(shading, "object_outline_color"),
        "overlay_show_overlays": value(overlay, "show_overlays"),
    }


def preflight(context: Any, props: Any) -> Preflight:
    """Validate every admission condition without writing anything.

    This is called by the public operator's ``invoke``.  It intentionally
    fails closed for background runs, small viewports, active playback, and
    unresolved ffmpeg.  It does not resize the window or change a preference.
    """
    if bpy.app.background:
        raise core.PreflightError(
            "GUI required: background Blender cannot provide final window "
            "pixels or real viewport draws"
        )
    window = _safe_attr(context, "window")
    if window is None:
        raise core.PreflightError("no active Blender window")
    screen = _safe_attr(window, "screen")
    if screen is None:
        raise core.PreflightError("no active Blender screen")
    area = _safe_attr(context, "area")
    if area is None or _safe_attr(area, "type") != "VIEW_3D":
        raise core.PreflightError(
            "start the recorder from a 3D viewport panel or a VIEW_3D context"
        )
    regions = [region for region in area.regions if region.type == "WINDOW"]
    if len(regions) != 1:
        raise core.PreflightError(
            f"expected exactly one 3D viewport WINDOW region, found {len(regions)}"
        )
    region = regions[0]
    if region.width < _REQUIRED_WIDTH or region.height < _REQUIRED_HEIGHT:
        raise core.PreflightError(
            "real 3D viewport WINDOW region is {0}x{1}; the recorder needs "
            ">= {2}x{3} native pixels. This candidate will not upscale and "
            "will not change the system resolution or viewport layout.".format(
                region.width, region.height, _REQUIRED_WIDTH, _REQUIRED_HEIGHT
            )
        )
    capture_source = str(
        getattr(props, "capture_source", "WINDOW_SCREENSHOT") or ""
    ).upper()
    if capture_source not in {
        "WINDOW_SCREENSHOT", "ACTIVE_FRAMEBUFFER_LEGACY"
    }:
        raise core.PreflightError(
            f"unsupported capture_source: {capture_source!r}"
        )
    display_mode = str(
        getattr(props, "display_mode", core.DISPLAY_MODE_SOLID_WHITE_UNLIT)
        or ""
    ).upper()
    if display_mode not in {
        core.DISPLAY_MODE_CURRENT,
        core.DISPLAY_MODE_SOLID_WHITE_UNLIT,
        core.DISPLAY_MODE_SOLID_OBJECT_UNLIT,
    }:
        raise core.PreflightError(
            f"unsupported display_mode: {display_mode!r}"
        )
    if capture_source == "WINDOW_SCREENSHOT" and not callable(
            _safe_attr(window, "screenshot", None)):
        raise core.PreflightError(
            "Window.screenshot is unavailable; this Blender build cannot "
            "capture final window pixels"
        )
    if bool(_safe_attr(screen, "is_animation_playing", False)):
        raise core.PreflightError(
            "animation playback is already active; refusing to stop or "
            "record playback that this operator did not start"
        )
    space = _safe_attr(area, "spaces", None)
    space = space.active if space is not None else None
    if space is None or _safe_attr(space, "type") != "VIEW_3D":
        raise core.PreflightError("the active area is not a View3D space")
    region_3d = _safe_attr(space, "region_3d")
    if region_3d is None:
        raise core.PreflightError("the 3D viewport has no region_3d")
    scene = _safe_attr(window, "scene")
    if scene is None:
        raise core.PreflightError("no active scene")
    if not bpy.data.filepath:
        raise core.PreflightError(
            "the scene has no saved blend path; save it before recording"
        )
    source_path = Path(bpy.data.filepath)
    if not source_path.is_file():
        raise core.PreflightError(f"saved blend is not readable: {source_path}")

    fps_num = scene.render.fps
    fps_den = scene.render.fps_base
    fps_text = core.fps_text(fps_num, fps_den)
    fps_val = core.fps_value(fps_num, fps_den)

    scene_start = int(scene.frame_start)
    scene_end = int(scene.frame_end)
    start_prop = int(getattr(props, "start_frame", -1))
    end_prop = int(getattr(props, "end_frame", -1))
    start = scene_start if start_prop < 0 else start_prop
    end = scene_end if end_prop < 0 else end_prop
    if start < scene_start or end > scene_end:
        raise core.PreflightError(
            f"requested frames {start}-{end} are outside scene range "
            f"{scene_start}-{scene_end}"
        )
    if start > end:
        raise core.PreflightError("start_frame must be <= end_frame")
    if bool(getattr(scene, "use_preview_range", False)):
        preview_start = int(scene.frame_preview_start)
        preview_end = int(scene.frame_preview_end)
        if start < preview_start or end > preview_end:
            raise core.PreflightError(
                f"scene preview range {preview_start}-{preview_end} does not "
                f"cover requested range {start}-{end}; disable preview range "
                "or select a covered range"
            )
    expected_count = end - start + 1
    if expected_count > 100_000:
        raise core.PreflightError("requested frame range is too large")

    raw_output = str(getattr(props, "output_root", "") or "").strip()
    if raw_output:
        resolved_output = bpy.path.abspath(raw_output)
    else:
        resolved_output = bpy.path.abspath("//native_recordings")
    if not resolved_output:
        raise core.PreflightError("could not resolve output_root")
    output_root = Path(resolved_output).expanduser()
    if not output_root.is_absolute():
        output_root = (Path.cwd() / output_root).resolve()

    ffmpeg_path = Path(core.resolve_ffmpeg(
        str(getattr(props, "ffmpeg_path", "") or "")
    ))
    crop_x = int(getattr(props, "crop_x", -1))
    crop_y = int(getattr(props, "crop_y", -1))
    crop_early = core.resolve_crop(
        int(region.width), int(region.height), crop_x, crop_y
    )
    area_xy = (int(area.x), int(area.y))
    region_xy = (int(region.x), int(region.y))
    if capture_source == "WINDOW_SCREENSHOT":
        try:
            capture_core.window_region(
                area_xy[0], area_xy[1], region_xy[0], region_xy[1],
                int(region.width), int(region.height),
                0, 0, int(region.width), int(region.height),
                int(window.width), int(window.height),
            )
            capture_core.window_region(
                area_xy[0], area_xy[1], region_xy[0], region_xy[1],
                int(region.width), int(region.height),
                crop_early.x, crop_early.y, crop_early.width,
                crop_early.height, int(window.width), int(window.height),
            )
        except BaseException as exc:
            raise core.PreflightError(
                "Window.screenshot geometry is invalid: " + repr(exc)
            ) from exc
    queue_frames = int(getattr(props, "queue_frames", core.DEFAULT_QUEUE_FRAMES))
    if not 1 <= queue_frames <= 32:
        raise core.PreflightError("queue_frames must be between 1 and 32")
    crf = int(getattr(props, "crf", core.DEFAULT_CRF))
    if not 0 <= crf <= 51:
        raise core.PreflightError("crf must be between 0 and 51")
    timeout_prop = float(getattr(props, "timeout_seconds", 0) or 0)
    if timeout_prop > 0:
        timeout_seconds = min(max(timeout_prop, 10.0), 3600.0)
    else:
        timeout_seconds = min(
            max(120.0, (expected_count / fps_val) * 20.0 + 60.0),
            3600.0,
        )
    hide_overlays = bool(getattr(props, "hide_overlays", False))
    flip_vertical = bool(getattr(props, "flip_vertical", True))

    overlay = _safe_attr(space, "overlay")
    shading = _safe_attr(space, "shading")
    camera = None
    if bool(_safe_attr(space, "use_local_camera", False)):
        camera = _safe_attr(space, "camera")
    else:
        camera = _safe_attr(scene, "camera")
    view_snapshot = {
        "shading_type": _safe_attr(shading, "type"),
        "show_overlays": bool(_safe_attr(overlay, "show_overlays", False)),
        "view_perspective": _safe_attr(region_3d, "view_perspective"),
        "use_local_camera": bool(_safe_attr(space, "use_local_camera", False)),
        "camera": _safe_attr(camera, "name"),
        "local_view": _safe_attr(space, "local_view", None) is not None,
        "show_region_ui": bool(_safe_attr(space, "show_region_ui", False)),
        "show_region_toolbar": bool(_safe_attr(space, "show_region_toolbar", False)),
        "render_resolution": [int(scene.render.resolution_x),
                              int(scene.render.resolution_y)],
        "render_resolution_percentage": int(scene.render.resolution_percentage),
        "scene_range": [scene_start, scene_end],
        "scene_current_frame": int(scene.frame_current),
        "use_preview_range": bool(getattr(scene, "use_preview_range", False)),
        "preview_range": [
            int(getattr(scene, "frame_preview_start", scene_start)),
            int(getattr(scene, "frame_preview_end", scene_end)),
        ],
        "capture_source": capture_source,
        "area_xy": list(area_xy),
        "region_xy": list(region_xy),
        "display_mode": display_mode,
        "display_state": _display_state(space),
    }
    if capture_source == "WINDOW_SCREENSHOT":
        capture_route = {
            "method": "bpy.types.Window.screenshot",
            "capture_trigger": "modal timer driven by real animation playback",
            "window_region": "owned 3D View3D WINDOW region + explicit crop",
            "pixel_format": "RGBA memoryview, use_alpha=False, rows bottom-to-top",
            "native_window_pixels": True,
            "active_framebuffer_legacy": False,
            "gpu_offscreen": False,
            "render_render": False,
            "render_opengl": False,
            "playblast": False,
            "frame_set_screenshot_loop": False,
            "encoder": "ffmpeg rawvideo rgb24 via exact Popen",
            "encoder_fixed_argv_only": True,
            "arbitrary_script": False,
            "shell": False,
            "crop_origin": "viewport_lower_left",
            "crop_size": [_REQUIRED_WIDTH, _REQUIRED_HEIGHT],
            "mcp_reference": (
                "runtime/blender-lab-mcp/.../"
                "get_screenshot_of_area_as_image_toolcode.py uses "
                "bpy.ops.screen.screenshot_area"
            ),
            "source_reference": (
                "blender main source/blender/editors/screen/screendump.cc: "
                "WM_redraw_windows then WM_window_pixels_read; screenshot_area "
                "crops final window pixels. Architecture reference only."
            ),
            "root_cause": (
                "Hypothesis pending probe: Blender 5.2 Material view "
                "POST_PIXEL active framebuffer is overlay-only. Independent "
                "possibility: read_color UBYTE buffer format/value range and "
                "numpy uint8 conversion. Window.screenshot is the independent "
                "final-window candidate."
            ),
        }
    else:
        capture_route = {
            "method": "gpu.state.active_framebuffer_get().read_color",
            "draw": "SpaceView3D POST_PIXEL",
            "framebuffer_format": "UBYTE",
            "native_window_pixels": False,
            "active_framebuffer_legacy": True,
            "known_limitation": (
                "The recorded failure showed black plus camera border. Whether "
                "that is overlay-only or UBYTE/float conversion is still a "
                "hypothesis; window_screenshot_probe records buffer metadata/"
                "ranges. Diagnostic only."
            ),
            "gpu_offscreen": False,
            "render_render": False,
            "render_opengl": False,
            "playblast": False,
            "frame_set_screenshot_loop": False,
            "encoder": "ffmpeg rawvideo rgb24 via exact Popen",
            "encoder_fixed_argv_only": True,
            "arbitrary_script": False,
            "shell": False,
            "crop_origin": "viewport_lower_left",
            "crop_size": [_REQUIRED_WIDTH, _REQUIRED_HEIGHT],
        }
    operator_options = {
        "start_frame": start_prop,
        "end_frame": end_prop,
        "crop_x": crop_x,
        "crop_y": crop_y,
        "hide_overlays": hide_overlays,
        "flip_vertical": flip_vertical,
        "queue_frames": queue_frames,
        "crf": crf,
        "timeout_seconds": timeout_prop,
        "capture_source": capture_source,
        "display_mode": display_mode,
    }
    source_sha256_start = core.sha256_file(source_path)
    return Preflight(
        window=window,
        screen=screen,
        area=area,
        region=region,
        space=space,
        scene=scene,
        window_ptr=window.as_pointer(),
        screen_ptr=screen.as_pointer(),
        area_ptr=area.as_pointer(),
        region_ptr=region.as_pointer(),
        scene_ptr=scene.as_pointer(),
        start_frame=start,
        end_frame=end,
        expected_count=expected_count,
        fps_num=fps_num,
        fps_den=fps_den,
        fps_value=fps_val,
        fps_text=fps_text,
        output_root=output_root,
        ffmpeg_path=ffmpeg_path,
        crop_x=crop_x,
        crop_y=crop_y,
        crop_early=crop_early,
        hide_overlays=hide_overlays,
        flip_vertical=flip_vertical,
        queue_frames=queue_frames,
        crf=crf,
        timeout_seconds=timeout_seconds,
        region_size=(int(region.width), int(region.height)),
        window_size=(int(window.width), int(window.height)),
        area_xy=area_xy,
        region_xy=region_xy,
        capture_source=capture_source,
        capture_route=capture_route,
        display_mode=display_mode,
        display_state=_display_state(space),
        source_path=source_path,
        source_sha256_start=source_sha256_start,
        view_snapshot=view_snapshot,
        operator_options=operator_options,
    )


def _buffer_to_rgb24(buf: Any, width: int, height: int, channels: int,
                     flip_vertical: bool) -> bytes:
    """Convert a UBYTE framebuffer buffer to top-down RGB24 bytes.

    Blender's framebuffer rows are documented as starting at the lower-left
    corner.  The vertical flip is a row reorder only; no pixel interpolation
    or resampling occurs.
    """
    import numpy as np

    if channels not in (3, 4):
        raise core.FrameReadError("framebuffer channels must be 3 or 4")
    expected = width * height * channels
    candidates = []
    try:
        candidates.append(np.frombuffer(buf, dtype=np.uint8))
    except BaseException:
        pass
    try:
        candidates.append(np.asarray(buf, dtype=np.uint8).reshape(-1))
    except BaseException:
        pass
    array = None
    for candidate in candidates:
        if candidate.size == expected:
            array = candidate
            break
    if array is None:
        try:
            values = buf.to_list()
        except BaseException as exc:
            raise core.FrameReadError(
                "could not convert real framebuffer pixels: "
                + repr(exc)
            ) from exc
        array = np.asarray(values, dtype=np.uint8).reshape(-1)
    if array.size != expected:
        raise core.FrameReadError(
            f"framebuffer returned {array.size} bytes; expected {expected}"
        )
    if channels == 4:
        array = array.reshape(height, width, 4)[:, :, :3]
    else:
        array = array.reshape(height, width, 3)
    if flip_vertical:
        array = array[::-1]
    return array.tobytes()


def _read_framebuffer_rgb(fb: Any, x: int, y: int, width: int, height: int,
                          flip_vertical: bool) -> tuple[bytes, int]:
    """Read RGB directly from the bound onscreen framebuffer.

    Three channels is attempted first to reduce readback bandwidth.  Some
    drivers expose only RGBA readback; four channels is a format fallback, not
    a different capture route.  Both failures are reported with the GPU error.
    """
    last_error: BaseException | None = None
    for channels in (3, 4):
        try:
            buf = fb.read_color(
                int(x), int(y), int(width), int(height), channels, 0, "UBYTE"
            )
            return (
                _buffer_to_rgb24(buf, width, height, channels, flip_vertical),
                channels,
            )
        except BaseException as exc:
            last_error = exc
    raise core.FrameReadError(
        "gpu framebuffer read_color failed for 3 and 4 channels: "
        + repr(last_error)
    )


class _ViewGuard:
    """Temporarily apply a View3D display preset and restore owned fields.

    Only View3D shading/overlay fields are touched.  Scene materials, lights,
    render engine, meshes and preferences are never modified.  If another
    actor changes a field after this guard applied it, restore leaves that
    user value alone and reports it as an external change.
    """

    def __init__(self, area: Any, space: Any, hide_overlays: bool,
                 display_mode: str = core.DISPLAY_MODE_SOLID_WHITE_UNLIT):
        self.area = area
        self.space = space
        self.overlay = _safe_attr(space, "overlay")
        self.shading = _safe_attr(space, "shading")
        self.hide_overlays = bool(hide_overlays)
        self.display_mode = str(display_mode)
        if self.display_mode == core.DISPLAY_MODE_SOLID_WHITE_UNLIT:
            self.preset = {
                "shading": dict(core.SOLID_WHITE_PRESET["shading"]),
                "overlay": dict(core.SOLID_WHITE_PRESET["overlay"]),
            }
        elif self.display_mode == core.DISPLAY_MODE_SOLID_OBJECT_UNLIT:
            self.preset = {
                "shading": dict(core.SOLID_OBJECT_UNLIT_PRESET["shading"]),
                "overlay": dict(core.SOLID_OBJECT_UNLIT_PRESET["overlay"]),
            }
        elif self.display_mode == core.DISPLAY_MODE_CURRENT:
            self.preset = (
                {"overlay": {"show_overlays": False}}
                if self.hide_overlays else {}
            )
        else:
            self.preset = {}
        self.fields_before: dict[str, Any] = {}
        self.values_applied: dict[str, Any] = {}
        self.fields_after_restore: dict[str, Any] = {}
        self.changed_fields: list[dict[str, Any]] = []
        self.unchanged_fields: list[str] = []
        self.unsupported_fields: list[str] = []
        self.restored_fields: list[str] = []
        self.external_changes: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.changed = False
        self.restored = False
        self._restore_attempted = False

    @staticmethod
    def _sequence(value: Any) -> tuple[float, ...] | None:
        if isinstance(value, (str, bytes, bytearray)):
            return None
        try:
            return tuple(float(item) for item in value)
        except BaseException:
            return None

    @classmethod
    def _plain(cls, value: Any) -> Any:
        sequence = cls._sequence(value)
        if sequence is not None:
            return list(sequence)
        return value

    @classmethod
    def _equal(cls, left: Any, right: Any) -> bool:
        left_sequence = cls._sequence(left)
        right_sequence = cls._sequence(right)
        if left_sequence is not None or right_sequence is not None:
            if left_sequence is None or right_sequence is None:
                return False
            if len(left_sequence) != len(right_sequence):
                return False
            return all(abs(a - b) <= 1e-6
                       for a, b in zip(left_sequence, right_sequence))
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return abs(float(left) - float(right)) <= 1e-6
        return left == right

    def _owner(self, field: str) -> Any:
        if field.startswith("shading."):
            return self.shading
        if field.startswith("overlay."):
            return self.overlay
        return None

    def _apply_field(self, field: str, value: Any) -> None:
        owner = self._owner(field)
        name = field.split(".", 1)[1]
        if owner is None:
            self.unsupported_fields.append(field)
            return
        try:
            before = getattr(owner, name)
        except BaseException as exc:
            self.unsupported_fields.append(field)
            self.errors.append(f"{field} read: {exc!r}")
            return
        before_plain = self._plain(before)
        value_plain = self._plain(value)
        self.fields_before[field] = before_plain
        self.values_applied[field] = value_plain
        if self._equal(before, value):
            self.unchanged_fields.append(field)
            return
        try:
            setattr(owner, name, value)
        except BaseException as exc:
            self.errors.append(f"{field} apply: {exc!r}")
            return
        self.changed_fields.append({
            "field": field,
            "before": before_plain,
            "applied": value_plain,
        })

    def apply(self) -> None:
        if self.space is None:
            self.errors.append("View3D space is unavailable")
            return
        if self.display_mode in {
            core.DISPLAY_MODE_SOLID_WHITE_UNLIT,
            core.DISPLAY_MODE_SOLID_OBJECT_UNLIT,
        }:
            if self.shading is None:
                self.unsupported_fields.append("shading")
            else:
                for name, value in self.preset.get("shading", {}).items():
                    self._apply_field("shading." + name, value)
            if self.overlay is None:
                self.unsupported_fields.append("overlay")
            else:
                for name, value in self.preset.get("overlay", {}).items():
                    self._apply_field("overlay." + name, value)
        elif self.display_mode == core.DISPLAY_MODE_CURRENT:
            if self.hide_overlays:
                if self.overlay is None:
                    self.unsupported_fields.append("overlay")
                else:
                    self._apply_field(
                        "overlay.show_overlays",
                        self.preset["overlay"]["show_overlays"],
                    )
        else:
            self.errors.append(
                f"unknown display_mode: {self.display_mode!r}")
        self.changed = bool(self.changed_fields)
        try:
            self.area.tag_redraw()
        except BaseException as exc:
            self.errors.append("tag_redraw apply: " + repr(exc))

    def restore(self) -> None:
        if self._restore_attempted:
            return
        self._restore_attempted = True
        if not self.changed_fields:
            self.restored = not self.errors
            return
        for entry in reversed(self.changed_fields):
            field = entry["field"]
            owner = self._owner(field)
            name = field.split(".", 1)[1]
            if owner is None:
                self.errors.append(f"{field} restore: owner unavailable")
                continue
            try:
                current = getattr(owner, name)
            except BaseException as exc:
                self.errors.append(f"{field} restore read: {exc!r}")
                continue
            if not self._equal(current, entry["applied"]):
                self.external_changes.append({
                    "field": field,
                    "applied": entry["applied"],
                    "current": self._plain(current),
                })
                self.fields_after_restore[field] = self._plain(current)
                continue
            try:
                setattr(owner, name, entry["before"])
                self.restored_fields.append(field)
            except BaseException as exc:
                self.errors.append(f"{field} restore write: {exc!r}")
            try:
                self.fields_after_restore[field] = self._plain(
                    getattr(owner, name))
            except BaseException as exc:
                self.errors.append(f"{field} restore verify: {exc!r}")
        try:
            self.area.tag_redraw()
        except BaseException as exc:
            self.errors.append("tag_redraw restore: " + repr(exc))
        self.restored = (
            not self.external_changes
            and not self.errors
            and len(self.restored_fields) == len(self.changed_fields)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "display_mode": self.display_mode,
            "hide_overlays_requested": self.hide_overlays,
            "preset": {
                group: {name: self._plain(value)
                        for name, value in fields.items()}
                for group, fields in self.preset.items()
            },
            "fields_before": dict(self.fields_before),
            "values_applied": dict(self.values_applied),
            "fields_after_restore": dict(self.fields_after_restore),
            "changed_fields": list(self.changed_fields),
            "unchanged_fields": list(self.unchanged_fields),
            "restored_fields": list(self.restored_fields),
            "external_changes": list(self.external_changes),
            "unsupported_fields": list(self.unsupported_fields),
            "errors": list(self.errors),
            "changed": self.changed,
            "restored": self.restored,
        }


class NativeRecordingSession:
    """One owned GUI recording session driven by the modal operator."""

    def __init__(self, pf: Preflight, sink: core.RawVideoSink,
                 stats: core.RecordingStats, job_dir: Path,
                 ffmpeg_argv: list[str]):
        self.pf = pf
        self.sink = sink
        self.stats = stats
        self.job_dir = Path(job_dir)
        self.ffmpeg_argv = list(ffmpeg_argv)
        self._capture_source = pf.capture_source
        self._capture_route = dict(pf.capture_route)
        self._display_mode = pf.display_mode
        self._phase = "init"
        self._phase_started = time.monotonic()
        self._started_monotonic = time.monotonic()
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._accepting = False
        self._finalized = False
        self._finalizing = False
        self._fatal: str | None = None
        self._fatal_detail: str | None = None
        self._cancel_requested: str | None = None
        self._draw_handle: Any = None
        self._timer: Any = None
        self._started_playback = False
        self._stopped_playback = False
        self._playback_started_at: str | None = None
        self._playback_stopped_at: str | None = None
        self._frame_set_calls = 0
        self._cleanup_errors: list[str] = []
        self._view_guard = _ViewGuard(
            pf.area, pf.space, pf.hide_overlays, pf.display_mode)
        self._report_path: Path | None = None
        self._status: str | None = None
        self._reason: str | None = None
        self._end_seen_at: float | None = None
        self._last_channels_used: int | None = None
        self._last_window_pixel_kind: str | None = None
        self._capture_seconds: list[float] = []
        self._priming_capture_at: float | None = None
        self._priming_capture_done = False
        self._last_capture_attempt_frame: int | None = None

    def start(self) -> None:
        if bpy.app.background:
            raise core.PreflightError("GUI required for native viewport recording")
        self._view_guard.apply()
        try:
            if self._capture_source == "ACTIVE_FRAMEBUFFER_LEGACY":
                self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
                    self._draw, (), "WINDOW", "POST_PIXEL"
                )
            wm = bpy.context.window_manager
            interval = (0.05 if self._capture_source == "ACTIVE_FRAMEBUFFER_LEGACY"
                        else 0.01)
            self._timer = wm.event_timer_add(interval, window=self.pf.window)
        except BaseException:
            if self._draw_handle is not None:
                try:
                    bpy.types.SpaceView3D.draw_handler_remove(
                        self._draw_handle, "WINDOW")
                except BaseException:
                    pass
                self._draw_handle = None
            self._view_guard.restore()
            raise
        self._phase = "start_pending"
        self._phase_started = time.monotonic()

    def request_cancel(self, reason: str = "user_cancel") -> None:
        if self._finalized or self._cancel_requested is not None:
            return
        self._cancel_requested = str(reason)
        print(f"ANIMATION_NATIVE_RECORD cancel_requested={reason}", flush=True)

    def abort(self, reason: str) -> Path | None:
        self.request_cancel(reason)
        return self.finalize(core.STATUS_CANCELLED, reason)

    def status_summary(self) -> dict[str, Any]:
        return {
            "active": not self._finalized,
            "phase": self._phase,
            "job_dir": str(self.job_dir),
            "capture_source": self._capture_source,
            "display_mode": self._display_mode,
            "encoded": self.stats.encoded_frame_count(),
            "expected": self.stats.expected_count,
            "draws": self.stats.draws_total,
            "fatal": self._fatal,
            "cancel_requested": self._cancel_requested,
            "started_playback": self._started_playback,
            "stopped_playback": self._stopped_playback,
        }

    def _set_fatal(self, reason: str, detail: str | None = None) -> None:
        if self._fatal is None:
            self._fatal = str(reason)
            self._fatal_detail = detail
            self._accepting = False
            print(f"ANIMATION_NATIVE_RECORD fatal={reason}", flush=True)

    def _remove_timer(self) -> None:
        if self._timer is None:
            return
        try:
            bpy.context.window_manager.event_timer_remove(self._timer)
        except BaseException as exc:
            self._cleanup_errors.append("timer remove: " + repr(exc))
        finally:
            self._timer = None

    def _stop_playback(self) -> None:
        if not self._started_playback:
            return
        try:
            if bool(self.pf.screen.is_animation_playing):
                with bpy.context.temp_override(
                    window=self.pf.window,
                    screen=self.pf.screen,
                    area=self.pf.area,
                    region=self.pf.region,
                ):
                    bpy.ops.screen.animation_cancel(restore_frame=False)
                self._stopped_playback = True
                self._playback_stopped_at = datetime.now(timezone.utc).isoformat()
        except BaseException as exc:
            self._cleanup_errors.append("stop own playback: " + repr(exc))

    def _begin_priming(self) -> None:
        if bool(self.pf.screen.is_animation_playing):
            self._set_fatal(
                "playback became active before the recorder started it; "
                "not stopping it"
            )
            return
        self._phase = "priming"
        self._phase_started = time.monotonic()
        self._accepting = True
        self._priming_capture_at = (
            time.monotonic() + 0.20
            if self._capture_source == "WINDOW_SCREENSHOT" else None
        )
        self._priming_capture_done = False
        try:
            # The single allowed frame_set: set the start point once.  No
            # per-frame frame_set exists anywhere in this module.
            self._frame_set_calls += 1
            self.pf.scene.frame_set(self.pf.start_frame)
        except BaseException as exc:
            self._set_fatal(
                "initial frame_set failed: " + repr(exc),
                traceback.format_exc(),
            )
            return
        try:
            self.pf.area.tag_redraw()
        except BaseException as exc:
            self._set_fatal(
                "initial redraw request failed: " + repr(exc),
                traceback.format_exc(),
            )

    def _start_playback(self) -> None:
        if bool(self.pf.screen.is_animation_playing):
            self._set_fatal(
                "playback active before animation_play; not stopping it"
            )
            return
        try:
            with bpy.context.temp_override(
                window=self.pf.window,
                screen=self.pf.screen,
                area=self.pf.area,
                region=self.pf.region,
            ):
                result = bpy.ops.screen.animation_play(sync=False)
        except BaseException as exc:
            self._set_fatal(
                "bpy.ops.screen.animation_play failed: " + repr(exc),
                traceback.format_exc(),
            )
            return
        if "FINISHED" not in result:
            self._set_fatal(f"animation_play returned {result!r}")
            return
        self._started_playback = True
        self._playback_started_at = datetime.now(timezone.utc).isoformat()
        self._phase = "playing"
        self._phase_started = time.monotonic()
        print(
            "ANIMATION_NATIVE_RECORD playback_started "
            f"frames={self.pf.start_frame}-{self.pf.end_frame}",
            flush=True,
        )

    def _capture_window_frame(self) -> None:
        """Capture final window pixels during priming/real playback.

        This is the default route.  It is called by the modal timer, not by
        per-frame frame_set, and only a new scene frame is read from
        Window.screenshot.  The timer is running because real animation
        playback is owned by this session.
        """
        if self._capture_source != "WINDOW_SCREENSHOT":
            return
        if not self._accepting or self._finalized or self._finalizing:
            return
        try:
            window = self.pf.window
            area = self.pf.area
            region = self.pf.region
            scene = self.pf.scene
            if (int(area.x), int(area.y)) != self.pf.area_xy:
                self._set_fatal("3D area origin changed during recording")
                return
            if (int(region.x), int(region.y)) != self.pf.region_xy:
                self._set_fatal("3D WINDOW region origin changed during recording")
                return
            if (int(region.width), int(region.height)) != self.pf.region_size:
                self._set_fatal("3D WINDOW region size changed during recording")
                return
            if (int(window.width), int(window.height)) != self.pf.window_size:
                self._set_fatal("Blender window size changed during recording")
                return
            frame = int(scene.frame_current)
            subframe = float(scene.frame_subframe)
            crop = core.resolve_crop(
                int(region.width), int(region.height),
                self.pf.crop_x, self.pf.crop_y,
            )
            viewport = (
                int(area.x) + int(region.x),
                int(area.y) + int(region.y),
                int(region.width),
                int(region.height),
            )
            decision = self.stats.classify_draw(
                frame,
                subframe,
                viewport,
                (crop.x, crop.y, crop.width, crop.height),
            )
            self._last_capture_attempt_frame = frame
            if decision.fatal:
                self._set_fatal(decision.reason)
                return
            if not decision.accept:
                return
            if not self.sink.queue_has_capacity():
                self.stats.record_queue_drop(
                    frame, subframe, "bounded queue full before window screenshot")
                return
            early_rc = self.sink.early_exit_code()
            if early_rc is not None:
                self._set_fatal(f"ffmpeg exited early with code {early_rc}")
                return
            region_arg = capture_core.window_region(
                int(area.x), int(area.y),
                int(region.x), int(region.y),
                int(region.width), int(region.height),
                crop.x, crop.y, crop.width, crop.height,
                int(window.width), int(window.height),
            )
            started = time.monotonic()
            mem = window.screenshot(region=region_arg, use_alpha=False)
            rgb, pixel_kind = capture_core.window_screenshot_rgba_to_rgb24(
                mem, crop.width, crop.height, self.pf.flip_vertical)
            seconds = time.monotonic() - started
            frame_after = int(scene.frame_current)
            self._last_channels_used = 4
            self._last_window_pixel_kind = pixel_kind
            offered = self.sink.offer(
                frame,
                subframe,
                viewport,
                (crop.x, crop.y, crop.width, crop.height),
                rgb,
                4,
                capture_seconds=seconds,
                frame_after=frame_after,
            )
            if not offered:
                self.stats.record_queue_drop(
                    frame, subframe, "bounded queue rejected window screenshot")
                return
            self._capture_seconds.append(seconds)
            if len(self._capture_seconds) > 1000:
                del self._capture_seconds[:-1000]
        except BaseException as exc:
            self._set_fatal(
                "Window.screenshot capture failed: " + repr(exc),
                traceback.format_exc(),
            )

    def _draw(self) -> None:
        if not self._accepting or self._finalized or self._finalizing:
            return
        if self._phase not in ("priming", "playing"):
            return
        try:
            ctx = bpy.context
            window = _safe_attr(ctx, "window")
            if window is None or window.as_pointer() != self.pf.window_ptr:
                return
            screen = _safe_attr(ctx, "screen") or _safe_attr(window, "screen")
            if screen is None or screen.as_pointer() != self.pf.screen_ptr:
                return
            area = _safe_attr(ctx, "area")
            if area is None or area.as_pointer() != self.pf.area_ptr:
                return
            region = _safe_attr(ctx, "region")
            if region is None or region.as_pointer() != self.pf.region_ptr:
                return
            scene = _safe_attr(ctx, "scene") or _safe_attr(window, "scene")
            if scene is None or scene.as_pointer() != self.pf.scene_ptr:
                return

            frame = int(scene.frame_current)
            subframe = float(scene.frame_subframe)
            viewport = gpu.state.viewport_get()
            if len(viewport) != 4:
                raise core.FrameReadError(
                    f"gpu.state.viewport_get returned {viewport!r}")
            vx, vy, vw, vh = (int(value) for value in viewport)
            region_width = int(region.width)
            region_height = int(region.height)
            if (vw, vh) != (region_width, region_height):
                self._set_fatal(
                    "GPU viewport {0}x{1} does not match the owned 3D "
                    "WINDOW region {2}x{3}; refusing to crop an unknown "
                    "framebuffer area".format(
                        vw, vh, region_width, region_height)
                )
                return
            crop = core.resolve_crop(
                vw, vh, self.pf.crop_x, self.pf.crop_y
            )
            decision = self.stats.classify_draw(
                frame,
                subframe,
                (vx, vy, vw, vh),
                (crop.x, crop.y, crop.width, crop.height),
            )
            if decision.fatal:
                self._set_fatal(decision.reason)
                return
            if not decision.accept:
                return
            if not self.sink.queue_has_capacity():
                self.stats.record_queue_drop(
                    frame, subframe, "bounded queue full before read")
                return
            early_rc = self.sink.early_exit_code()
            if early_rc is not None:
                self._set_fatal(f"ffmpeg exited early with code {early_rc}")
                return
            framebuffer = gpu.state.active_framebuffer_get()
            rgb, channels = _read_framebuffer_rgb(
                framebuffer,
                vx + crop.x,
                vy + crop.y,
                crop.width,
                crop.height,
                self.pf.flip_vertical,
            )
            self._last_channels_used = channels
            offered = self.sink.offer(
                frame,
                subframe,
                (vx, vy, vw, vh),
                (crop.x, crop.y, crop.width, crop.height),
                rgb,
                channels,
            )
            if not offered:
                self.stats.record_queue_drop(
                    frame, subframe, "bounded queue rejected after read")
        except BaseException as exc:
            self._set_fatal(
                "draw capture failed: " + repr(exc),
                traceback.format_exc(),
            )

    def tick(self) -> tuple[bool, str | None, str | None]:
        if self._finalized:
            return True, self._status, self._reason
        if self._fatal is not None:
            path = self.finalize(core.STATUS_FAILED, self._fatal)
            return True, self._status, self._reason
        if self._cancel_requested is not None:
            path = self.finalize(
                core.STATUS_CANCELLED, self._cancel_requested
            )
            return True, self._status, self._reason
        now = time.monotonic()
        if self._phase == "start_pending":
            self._begin_priming()
            return False, None, None
        if self._phase == "priming":
            early_rc = self.sink.early_exit_code()
            if early_rc is not None:
                self._set_fatal(f"ffmpeg exited during priming rc={early_rc}")
            elif self.sink.write_failed():
                self._set_fatal(
                    "encoder writer failed during priming: "
                    + "; ".join(self.sink.errors_snapshot())
                )
            elif self._capture_source == "WINDOW_SCREENSHOT":
                if (not self._priming_capture_done
                        and self._priming_capture_at is not None
                        and now >= self._priming_capture_at):
                    self._priming_capture_done = True
                    self._capture_window_frame()
                elif self.stats.encoded_frame_count() >= 1:
                    self._start_playback()
                elif bool(self.pf.screen.is_animation_playing):
                    self._set_fatal(
                        "playback active during priming but not started by recorder"
                    )
                elif now - self._phase_started > 5.0:
                    self._set_fatal(
                        "initial final-window screenshot was not encoded within 5s"
                    )
            elif self.stats.encoded_frame_count() >= 1:
                self._start_playback()
            elif bool(self.pf.screen.is_animation_playing):
                self._set_fatal(
                    "playback active during priming but not started by recorder"
                )
            elif now - self._phase_started > 3.0:
                self._set_fatal(
                    "initial native POST_PIXEL draw was not captured within 3s"
                )
            if self._fatal is not None:
                self.finalize(core.STATUS_FAILED, self._fatal)
                return True, self._status, self._reason
            return False, None, None
        if self._phase == "playing":
            return self._tick_playing(now)
        self._set_fatal(f"unknown session phase {self._phase!r}")
        self.finalize(core.STATUS_FAILED, self._fatal or "unknown phase")
        return True, self._status, self._reason

    def _tick_playing(self, now: float) -> tuple[bool, str | None, str | None]:
        early_rc = self.sink.early_exit_code()
        if early_rc is not None:
            self._set_fatal(f"ffmpeg exited early with code {early_rc}")
        if self.sink.write_failed():
            self._set_fatal(
                "encoder writer failed: "
                + "; ".join(self.sink.errors_snapshot())
            )
        if self._capture_source == "WINDOW_SCREENSHOT":
            current_frame = int(self.pf.scene.frame_current)
            if self._last_capture_attempt_frame != current_frame:
                self._capture_window_frame()
        snapshot = self.stats.snapshot()
        if snapshot["size_changes"]:
            self._set_fatal("real viewport size/origin changed during playback")
        if self._fatal is not None:
            self.finalize(core.STATUS_FAILED, self._fatal)
            return True, self._status, self._reason

        playing = bool(self.pf.screen.is_animation_playing)
        current = int(self.pf.scene.frame_current)
        max_seen = snapshot["max_seen_frame"]
        if not playing:
            # animation_play may not flip the flag until its first UI tick.
            # Give the owned start a short grace period before treating a
            # false flag as an external stop.
            if now - self._phase_started < 0.5:
                return False, None, None
            if snapshot["technical_complete"]:
                self.finalize(core.STATUS_COMPLETE, "playback ended")
            elif max_seen is not None and max_seen >= self.pf.end_frame - 1:
                self.finalize(
                    core.STATUS_GAPS,
                    "playback ended before every expected frame was encoded",
                )
            elif snapshot["encoded_count"] > 0:
                self.finalize(
                    core.STATUS_FAILED,
                    f"playback stopped externally at frame {current}",
                )
            else:
                self.finalize(
                    core.STATUS_FAILED,
                    "playback ended without an encoded frame",
                )
            return True, self._status, self._reason

        if snapshot["technical_complete"]:
            self._stop_playback()
            self.finalize(core.STATUS_COMPLETE, "all expected frames encoded")
            return True, self._status, self._reason

        if snapshot["end_seen"]:
            if self._end_seen_at is None:
                self._end_seen_at = now
            elif now - self._end_seen_at > 0.30:
                self._stop_playback()
                self.finalize(
                    core.STATUS_GAPS,
                    "end frame observed; missing frames are reported, not filled",
                )
                return True, self._status, self._reason
        elif max_seen is not None and max_seen >= self.pf.end_frame:
            self._end_seen_at = self._end_seen_at or now

        if (max_seen is not None and max_seen > current + 0.5):
            self._stop_playback()
            if snapshot["encoded_count"] > 0:
                self.finalize(
                    core.STATUS_GAPS,
                    "playback wrapped or jumped backwards; missing frames are reported",
                )
            else:
                self.finalize(
                    core.STATUS_FAILED,
                    "playback wrapped before any frame was encoded",
                )
            return True, self._status, self._reason

        if now - self._started_monotonic > self.pf.timeout_seconds:
            self._set_fatal(
                f"recording timed out after {self.pf.timeout_seconds:.1f}s")
            self.finalize(core.STATUS_FAILED, self._fatal)
            return True, self._status, self._reason
        if (snapshot["encoded_count"] == 0
                and now - self._phase_started > 5.0):
            self._set_fatal("no native frame was encoded within 5s of playback")
            self.finalize(core.STATUS_FAILED, self._fatal)
            return True, self._status, self._reason
        return False, None, None

    def finalize(self, status: str, reason: str) -> Path | None:
        if self._finalized:
            return self._report_path
        if self._finalizing:
            return self._report_path
        self._finalizing = True
        self._accepting = False
        self._status = status
        self._reason = str(reason)
        cleanup_errors: list[str] = []
        self._remove_timer()
        self._stop_playback()
        if self._draw_handle is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(
                    self._draw_handle, "WINDOW")
            except BaseException as exc:
                cleanup_errors.append("draw handler remove: " + repr(exc))
            finally:
                self._draw_handle = None
        self._view_guard.restore()
        cleanup_errors.extend(self._cleanup_errors)
        cleanup_errors.extend(self._view_guard.errors)

        sink_summary: dict[str, Any]
        try:
            sink_summary = self.sink.finish(
                reason, wait_timeout=30.0, join_timeout=5.0
            )
        except BaseException as exc:
            cleanup_errors.append("sink finish: " + repr(exc))
            sink_summary = {
                "started": True,
                "reason": reason,
                "argv": list(self.ffmpeg_argv),
                "pid": None,
                "returncode": None,
                "reaped": False,
                "writer_thread_alive": None,
                "frames_offered_to_queue": None,
                "frames_written": None,
                "queue_maxsize": self.pf.queue_frames,
                "queue_drops": self.stats.queue_drops,
                "metadata_error": None,
                "errors": [repr(exc)],
                "video_path": str(self.job_dir / "viewport-1920x1080.mp4"),
                "video_exists": (self.job_dir / "viewport-1920x1080.mp4").is_file(),
                "video_bytes": None,
                "samples_path": str(self.job_dir / "samples.jsonl"),
                "samples_exists": (self.job_dir / "samples.jsonl").is_file(),
                "samples_lines": None,
                "log_path": str(self.job_dir / "ffmpeg.log"),
                "log_exists": (self.job_dir / "ffmpeg.log").is_file(),
                "elapsed_seconds": None,
                "popen_ownership": "exact_popen_child_only",
            }
        snapshot = self.stats.snapshot()
        source_end = None
        source_changed = None
        try:
            source_end = core.sha256_file(self.pf.source_path)
            source_changed = source_end != self.pf.source_sha256_start
        except BaseException as exc:
            cleanup_errors.append("source end hash: " + repr(exc))
        encoder_errors = list(sink_summary.get("errors", []))
        encoder_ok = (
            sink_summary.get("returncode") == 0
            and bool(sink_summary.get("reaped"))
            and not encoder_errors
        )
        final_status = status
        if status == core.STATUS_CANCELLED:
            final_status = core.STATUS_CANCELLED
        elif status == core.STATUS_FAILED:
            final_status = core.STATUS_FAILED
        elif snapshot["encoded_count"] == 0:
            final_status = core.STATUS_FAILED
        elif not encoder_ok or cleanup_errors:
            final_status = core.STATUS_ERRORS
        elif (snapshot["technical_complete"]
              and status == core.STATUS_COMPLETE):
            final_status = core.STATUS_COMPLETE
        else:
            final_status = core.STATUS_GAPS

        finished_at = datetime.now(timezone.utc).isoformat()
        elapsed = time.monotonic() - self._started_monotonic
        try:
            playing_final = bool(self.pf.screen.is_animation_playing)
        except BaseException:
            playing_final = None
        extra = {
            "playback": {
                "started_by_recorder": self._started_playback,
                "stopped_by_recorder": self._stopped_playback,
                "started_at": self._playback_started_at,
                "stopped_at": self._playback_stopped_at,
                "scene_frame_final": _safe_attr(self.pf.scene, "frame_current"),
                "screen_is_animation_playing_final": playing_final,
                "frame_set_calls": self._frame_set_calls,
                "frame_set_policy": "one initial start frame only; no per-frame frame_set",
            },
            "view": {
                "guard": self._view_guard.as_dict(),
                "snapshot": dict(self.pf.view_snapshot),
            },
            "source": {
                "path": str(self.pf.source_path),
                "sha256_start": self.pf.source_sha256_start,
                "sha256_end": source_end,
                "changed_during_recording": source_changed,
            },
            "capture": {
                "source": self._capture_source,
                "display_mode": self._display_mode,
                "method": self._capture_route.get("method"),
                "attempts": snapshot["draws_total"],
                "encoded": snapshot["encoded_count"],
                "capture_seconds": capture_core.timing_summary(
                    self._capture_seconds),
                "last_capture_attempt_frame": self._last_capture_attempt_frame,
                "displayed_frame_claim": "not_assessed",
                "frame_timing_source": "samples.jsonl scene_frame/frame_after",
                "window_screenshot_frames_encoded": (
                    snapshot["encoded_count"]
                    if self._capture_source == "WINDOW_SCREENSHOT" else 0
                ),
            },
            "gpu": {
                "framebuffer_frames_encoded": (
                    snapshot["encoded_count"]
                    if self._capture_source == "ACTIVE_FRAMEBUFFER_LEGACY" else 0
                ),
                "window_screenshot_frames_encoded": (
                    snapshot["encoded_count"]
                    if self._capture_source == "WINDOW_SCREENSHOT" else 0
                ),
                "channels_used_last": self._last_channels_used,
                "window_pixel_kind": self._last_window_pixel_kind,
                "actual_gpu_review_required": True,
            },
            "fatal_detail": self._fatal_detail,
            "report_path": None,
        }
        try:
            report = core.build_report(
                status=final_status,
                reason=self._reason,
                job_dir=self.job_dir,
                preflight=self.pf.as_dict(),
                stats=snapshot,
                encoder=sink_summary,
                capture_route=self._capture_route,
                cleanup_errors=cleanup_errors,
                started_at=self._started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed,
                extra=extra,
            )
            self._report_path = core.write_json_unique(
                self.job_dir, "report", report)
        except BaseException as exc:
            cleanup_errors.append("finalize/report: " + repr(exc))
            final_status = core.STATUS_FAILED
            print(
                "ANIMATION_NATIVE_RECORD finalize_report_failed "
                + repr(exc),
                flush=True,
            )
            try:
                fallback = {
                    "schema": "animation-native-viewport-recording-v1",
                    "status": core.STATUS_FAILED,
                    "reason": self._reason,
                    "job_dir": str(self.job_dir),
                    "candidate": True,
                    "visual_acceptance": "not_assessed",
                    "recording_pass": False,
                    "gpu_readback_verified": False,
                    "technical_complete": False,
                    "frames": snapshot,
                    "encoder": sink_summary,
                    "cleanup": {"errors": cleanup_errors,
                                "cleanup_ok": False},
                    "honesty": {
                        "frames_filled": False,
                        "frames_interpolated": False,
                        "frames_upscaled": False,
                        "missing_frames_are_not_filled": True,
                    },
                    "qualification": {
                        "raw_candidate_only": True,
                        "requires_main_thread_gui_review": True,
                        "recording_pass": False,
                        "gpu_readback_verified": False,
                    },
                }
                self._report_path = core.write_json_unique(
                    self.job_dir, "report", fallback)
            except BaseException as fallback_exc:
                cleanup_errors.append(
                    "fallback report: " + repr(fallback_exc))
        finally:
            self._status = final_status
            self._finalized = True
            self._finalizing = False
            global _ACTIVE_SESSION
            if _ACTIVE_SESSION is self:
                _ACTIVE_SESSION = None
            print(
                "ANIMATION_NATIVE_RECORD finalized "
                f"status={final_status} encoded={snapshot['encoded_count']}"
                f"/{snapshot['expected_count']} report={self._report_path}",
                flush=True,
            )
        return self._report_path


def active_summary() -> dict[str, Any] | None:
    session = _ACTIVE_SESSION
    return session.status_summary() if session is not None else None


class ANIMATION_NATIVE_OT_record_viewport(bpy.types.Operator):
    """Start one real viewport playback recording from the current 3D area."""

    bl_idname = "animation_native.record_viewport"
    bl_label = "Start Native Viewport Recording"
    bl_description = (
        "Record final 1920x1080 window pixels during native playback. "
        "Requires a 3D viewport WINDOW region at least 1920x1080; never upscales"
    )
    bl_options = {"REGISTER"}

    capture_source: bpy.props.EnumProperty(
        name="Capture Source",
        description="Final-window pixels are the default; legacy framebuffer is diagnostic only",
        items=(
            (
                "WINDOW_SCREENSHOT",
                "Window Screenshot",
                "bpy.types.Window.screenshot final window pixels (default)",
            ),
            (
                "ACTIVE_FRAMEBUFFER_LEGACY",
                "Active Framebuffer (legacy)",
                "POST_PIXEL active framebuffer; known overlay-only in Material view",
            ),
        ),
        default="WINDOW_SCREENSHOT",
    )
    display_mode: bpy.props.EnumProperty(
        name="Display Mode",
        description=(
            "白模实体：临时单色白实体，无材质/光影；"
            "实体视口颜色：使用 Object.color，无光影；"
            "当前视口：保持现有显示"
        ),
        items=(
            (
                core.DISPLAY_MODE_SOLID_WHITE_UNLIT,
                "白模实体",
                "实体视口，无材质/光影（默认）",
            ),
            (
                core.DISPLAY_MODE_SOLID_OBJECT_UNLIT,
                "实体视口颜色（无光影）",
                "实体视口使用 Object.color，无光影",
            ),
            (
                core.DISPLAY_MODE_CURRENT,
                "当前视口",
                "保持当前视口显示；兼容旧 hide_overlays",
            ),
        ),
        default=core.DISPLAY_MODE_SOLID_WHITE_UNLIT,
    )
    output_root: bpy.props.StringProperty(
        name="Output Root",
        description="New job subdirectory is created below this path; empty uses //native_recordings",
        default="",
        subtype="DIR_PATH",
    )
    ffmpeg_path: bpy.props.StringProperty(
        name="FFmpeg",
        description=(
            "Existing ffmpeg executable; empty uses ANIMATION_FFMPEG, "
            "the known local default, then PATH. Only fixed rawvideo args "
            "are passed; scripts and arbitrary commands are refused"
        ),
        default="",
        subtype="FILE_PATH",
    )
    start_frame: bpy.props.IntProperty(
        name="Start Frame",
        description="-1 uses scene frame_start; set before playback only",
        default=-1,
        min=-1,
    )
    end_frame: bpy.props.IntProperty(
        name="End Frame",
        description="-1 uses scene frame_end",
        default=-1,
        min=-1,
    )
    crop_x: bpy.props.IntProperty(
        name="Crop X",
        description="Offset from viewport lower-left; -1 centers the 1920x1080 crop",
        default=-1,
        min=-1,
    )
    crop_y: bpy.props.IntProperty(
        name="Crop Y",
        description="Offset from viewport lower-left; -1 centers the 1920x1080 crop",
        default=-1,
        min=-1,
    )
    hide_overlays: bpy.props.BoolProperty(
        name="Hide Overlays",
        description="Temporarily hide viewport overlays; restore on stop",
        default=False,
    )
    flip_vertical: bpy.props.BoolProperty(
        name="Flip Vertical",
        description="Convert bottom-up window/readback rows to top-down video rows",
        default=True,
    )
    queue_frames: bpy.props.IntProperty(
        name="Queue Frames",
        description="Bounded frame queue; overflow drops are reported, never filled",
        default=core.DEFAULT_QUEUE_FRAMES,
        min=1,
        max=32,
    )
    crf: bpy.props.IntProperty(
        name="CRF",
        description="libx264 CRF",
        default=core.DEFAULT_CRF,
        min=0,
        max=51,
    )
    timeout_seconds: bpy.props.IntProperty(
        name="Timeout Seconds",
        description="0 computes a bounded automatic timeout",
        default=0,
        min=0,
        max=3600,
    )

    @classmethod
    def poll(cls, context: Any) -> bool:
        return (
            not bpy.app.background
            and _ACTIVE_SESSION is None
            and _safe_attr(context, "area") is not None
            and _safe_attr(context.area, "type") == "VIEW_3D"
        )

    def execute(self, context: Any) -> set[str]:
        self.report(
            {"ERROR"},
            "Use the View3D panel button; this operator requires GUI invoke/modal",
        )
        return {"CANCELLED"}

    def invoke(self, context: Any, event: Any) -> set[str]:
        global _ACTIVE_SESSION
        if _ACTIVE_SESSION is not None:
            self.report({"ERROR"}, "A native viewport recording is already active")
            return {"CANCELLED"}
        try:
            pf = preflight(context, self)
        except BaseException as exc:
            self.report({"ERROR"}, f"Native recording refused: {exc}")
            return {"CANCELLED"}

        job_dir: Path | None = None
        sink: core.RawVideoSink | None = None
        stats: core.RecordingStats | None = None
        session: NativeRecordingSession | None = None
        try:
            job_dir = core.new_job_directory(pf.output_root)
            stats = core.RecordingStats(pf.start_frame, pf.end_frame)
            sink = core.RawVideoSink(
                job_dir=job_dir,
                ffmpeg_path=pf.ffmpeg_path,
                output_path=job_dir / "viewport-1920x1080.mp4",
                stats=stats,
                width=_REQUIRED_WIDTH,
                height=_REQUIRED_HEIGHT,
                fps_num=pf.fps_num,
                fps_den=pf.fps_den,
                queue_frames=pf.queue_frames,
                crf=pf.crf,
            )
            argv = sink.start()
            session = NativeRecordingSession(pf, sink, stats, job_dir, argv)
            session.start()
            _ACTIVE_SESSION = session
            self._session = session
            try:
                context.window_manager.modal_handler_add(self)
            except BaseException as exc:
                try:
                    session.finalize(
                        core.STATUS_FAILED,
                        "modal_handler_add failed: " + repr(exc),
                    )
                except BaseException:
                    pass
                self.report(
                    {"ERROR"},
                    "Native recording modal setup failed: " + repr(exc),
                )
                return {"CANCELLED"}
            self.report(
                {"INFO"},
                "Native viewport recording started; job: " + str(job_dir),
            )
            print(
                "ANIMATION_NATIVE_RECORD started "
                f"job={job_dir} frames={pf.start_frame}-{pf.end_frame} "
                f"region={pf.region_size} crop_early={pf.crop_early.as_dict()} "
                f"display_mode={pf.display_mode}",
                flush=True,
            )
            return {"RUNNING_MODAL"}
        except BaseException as exc:
            cleanup_errors: list[str] = []
            sink_summary: dict[str, Any] | None = None
            if sink is not None:
                try:
                    sink_summary = sink.finish(
                        "setup_failed", wait_timeout=10.0)
                except BaseException as cleanup_exc:
                    cleanup_errors.append(repr(cleanup_exc))
            if job_dir is not None:
                try:
                    if stats is None:
                        stats = core.RecordingStats(
                            pf.start_frame, pf.end_frame)
                    if sink_summary is None:
                        sink_summary = {
                            "started": sink is not None,
                            "reason": "setup_failed",
                            "returncode": None,
                            "reaped": False,
                            "errors": [],
                            "video_path": str(
                                job_dir / "viewport-1920x1080.mp4"),
                            "video_exists": (
                                job_dir / "viewport-1920x1080.mp4").is_file(),
                            "samples_path": str(job_dir / "samples.jsonl"),
                            "samples_exists": (
                                job_dir / "samples.jsonl").is_file(),
                            "log_path": str(job_dir / "ffmpeg.log"),
                            "log_exists": (job_dir / "ffmpeg.log").is_file(),
                            "popen_ownership": "exact_popen_child_only",
                        }
                    else:
                        sink_summary = dict(sink_summary)
                    sink_summary["errors"] = (
                        [repr(exc)]
                        + list(sink_summary.get("errors", []))
                    )
                    report = core.build_report(
                        status=core.STATUS_FAILED,
                        reason="setup_failed: " + repr(exc),
                        job_dir=job_dir,
                        preflight=pf.as_dict(),
                        stats=stats.snapshot(),
                        encoder=sink_summary,
                        cleanup_errors=cleanup_errors,
                        started_at=datetime.now(timezone.utc).isoformat(),
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        elapsed_seconds=0.0,
                    )
                    core.write_json_unique(job_dir, "report", report)
                except BaseException as report_exc:
                    cleanup_errors.append("failure report: " + repr(report_exc))
            self.report(
                {"ERROR"},
                "Native recording setup failed: " + repr(exc),
            )
            return {"CANCELLED"}

    def modal(self, context: Any, event: Any) -> set[str]:
        session = getattr(self, "_session", None)
        if session is None:
            return {"CANCELLED"}
        if event.type == "TIMER":
            try:
                done, status, reason = session.tick()
            except BaseException as exc:
                try:
                    session.finalize(
                        core.STATUS_FAILED,
                        "modal timer failed: " + repr(exc),
                    )
                except BaseException:
                    pass
                done, status, reason = True, core.STATUS_FAILED, repr(exc)
            if done:
                summary = session.status_summary()
                message = (
                    f"Native recording {status}: encoded "
                    f"{summary['encoded']}/{summary['expected']} frames; "
                    f"job {summary['job_dir']}"
                )
                if status == core.STATUS_FAILED:
                    self.report({"ERROR"}, message)
                    return {"CANCELLED"}
                if status == core.STATUS_CANCELLED:
                    self.report({"WARNING"}, message)
                    return {"FINISHED"}
                if status == core.STATUS_COMPLETE:
                    self.report({"INFO"}, message)
                else:
                    self.report({"WARNING"}, message)
                return {"FINISHED"}
            return {"RUNNING_MODAL"}
        if event.type in {"ESC"}:
            session.request_cancel("esc")
            return {"RUNNING_MODAL"}
        return {"PASS_THROUGH"}

    def cancel(self, context: Any) -> None:
        session = getattr(self, "_session", None)
        if session is not None:
            session.abort("operator_cancelled")


class ANIMATION_NATIVE_OT_cancel_recording(bpy.types.Operator):
    """Cancel the recorder-owned native viewport playback session."""

    bl_idname = "animation_native.cancel_recording"
    bl_label = "Cancel Native Viewport Recording"
    bl_description = "Stop the playback started by this recorder and finalize a partial report"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: Any) -> bool:
        return _ACTIVE_SESSION is not None

    def execute(self, context: Any) -> set[str]:
        session = _ACTIVE_SESSION
        if session is None:
            self.report({"INFO"}, "No native viewport recording is active")
            return {"CANCELLED"}
        session.request_cancel("user_cancel")
        self.report({"INFO"}, "Native recording cancel requested")
        return {"FINISHED"}

    def invoke(self, context: Any, event: Any) -> set[str]:
        return self.execute(context)


def _abort_active(reason: str) -> None:
    session = _ACTIVE_SESSION
    if session is None:
        return
    try:
        session.abort(reason)
    except BaseException as exc:
        print(
            "ANIMATION_NATIVE_RECORD abort_failed "
            f"reason={reason} error={exc!r}",
            flush=True,
        )


def _on_load_pre(*_args: Any) -> None:
    _abort_active("file_load_started")


def _on_exit_pre(*_args: Any) -> None:
    _abort_active("blender_exit")


def _install_handlers() -> None:
    global _HANDLERS_INSTALLED
    if _HANDLERS_INSTALLED:
        return
    if _on_load_pre not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_on_load_pre)
    if _on_exit_pre not in bpy.app.handlers.exit_pre:
        bpy.app.handlers.exit_pre.append(_on_exit_pre)
    _HANDLERS_INSTALLED = True


def _remove_handlers() -> None:
    global _HANDLERS_INSTALLED
    for handler, collection in (
        (_on_load_pre, bpy.app.handlers.load_pre),
        (_on_exit_pre, bpy.app.handlers.exit_pre),
    ):
        try:
            if handler in collection:
                collection.remove(handler)
        except BaseException:
            pass
    _HANDLERS_INSTALLED = False


def register() -> None:
    global _REGISTERED
    if _REGISTERED:
        raise RuntimeError("native_recording is already registered")
    classes = (
        ANIMATION_NATIVE_OT_record_viewport,
        ANIMATION_NATIVE_OT_cancel_recording,
    )
    registered: list[type] = []
    try:
        for cls in classes:
            bpy.utils.register_class(cls)
            registered.append(cls)
        _install_handlers()
        _REGISTERED_CLASSES[:] = registered
        _REGISTERED = True
    except BaseException:
        for cls in reversed(registered):
            try:
                bpy.utils.unregister_class(cls)
            except BaseException:
                pass
        _remove_handlers()
        raise


def unregister() -> None:
    global _REGISTERED
    if not _REGISTERED:
        return
    _abort_active("addon_unregister")
    _remove_handlers()
    for cls in reversed(list(_REGISTERED_CLASSES)):
        try:
            bpy.utils.unregister_class(cls)
        except BaseException:
            pass
    _REGISTERED_CLASSES.clear()
    _REGISTERED = False
