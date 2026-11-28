"""Pure-Python support for the native viewport recorder candidate.

This module deliberately does not import ``bpy`` or ``gpu``.  It contains the
parts that can be tested on a host:

* the fixed 1920x1080 native crop policy (never a scale/upscale),
* fresh job directories,
* the ffmpeg rawvideo command line,
* a bounded queue and a bytes-only writer thread with exact Popen ownership,
* honest draw/encoded-frame accounting.

The Blender-facing code lives in ``native_recording.py``.
"""

from __future__ import annotations

import json
import math
import os
import queue
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


CAPTURE_WIDTH = 1920
CAPTURE_HEIGHT = 1080
DEFAULT_QUEUE_FRAMES = 6
DEFAULT_CRF = 18
DEFAULT_PRESET = "ultrafast"
DEFAULT_THREADS = 2
# Main-thread-confirmed local encoder for this machine.  This is only a
# default candidate; an explicit public path or ANIMATION_FFMPEG overrides it.
DEFAULT_FFMPEG_PATH = r"F:\ComfyUI\tools\ffmpeg\bin\ffmpeg.exe"
_SCRIPT_SUFFIXES = {
    ".py", ".bat", ".cmd", ".ps1", ".sh",
    ".js", ".vbs", ".wsf", ".php", ".pl", ".rb",
}

DISPLAY_MODE_CURRENT = "CURRENT"
DISPLAY_MODE_SOLID_WHITE_UNLIT = "SOLID_WHITE_UNLIT"
DISPLAY_MODE_SOLID_OBJECT_UNLIT = "SOLID_OBJECT_UNLIT"
SOLID_WHITE_PRESET: dict[str, dict[str, Any]] = {
    "shading": {
        "type": "SOLID",
        "light": "FLAT",
        "color_type": "SINGLE",
        "single_color": (1.0, 1.0, 1.0),
        "show_shadows": False,
        "show_cavity": False,
        "show_specular_highlight": False,
        "background_type": "VIEWPORT",
        "background_color": (0.18, 0.18, 0.18),
        "show_object_outline": True,
        "object_outline_color": (0.0, 0.0, 0.0),
    },
    "overlay": {
        "show_overlays": False,
    },
}
SOLID_OBJECT_UNLIT_PRESET: dict[str, dict[str, Any]] = {
    "shading": {
        "type": "SOLID",
        "light": "FLAT",
        "color_type": "OBJECT",
        "show_shadows": False,
        "show_cavity": False,
        "show_specular_highlight": False,
        "background_type": "VIEWPORT",
        "background_color": (0.18, 0.18, 0.18),
        "show_object_outline": True,
        "object_outline_color": (0.0, 0.0, 0.0),
    },
    "overlay": {
        "show_overlays": False,
    },
}

STATUS_COMPLETE = "RAW_CAPTURE_COMPLETE_PENDING_REVIEW"
STATUS_GAPS = "RAW_CAPTURE_COMPLETE_WITH_GAPS_PENDING_REVIEW"
STATUS_ERRORS = "RAW_CAPTURE_COMPLETE_WITH_ERRORS_PENDING_REVIEW"
STATUS_CANCELLED = "CANCELLED"
STATUS_FAILED = "FAILED"


class RecordingError(RuntimeError):
    """Base error for the recorder."""


class PreflightError(RecordingError):
    """The requested recording cannot be admitted without changing the system."""


class FrameReadError(RecordingError):
    """A framebuffer read or pixel conversion failed."""


def _as_int(name: str, value: Any, *, minimum: int | None = None,
            maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecordingError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise RecordingError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise RecordingError(f"{name} must be <= {maximum}")
    return value


def _as_finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise RecordingError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RecordingError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise RecordingError(f"{name} must be finite")
    return result


def validate_viewport(viewport_width: int, viewport_height: int,
                      width: int = CAPTURE_WIDTH,
                      height: int = CAPTURE_HEIGHT) -> None:
    """Require enough real viewport pixels for an unscaled crop."""
    _as_int("viewport_width", viewport_width, minimum=1)
    _as_int("viewport_height", viewport_height, minimum=1)
    _as_int("capture_width", width, minimum=1)
    _as_int("capture_height", height, minimum=1)
    if viewport_width < width or viewport_height < height:
        raise PreflightError(
            "real viewport WINDOW region is {0}x{1}; the recorder needs at "
            "least {2}x{3} native pixels and will not upscale".format(
                viewport_width, viewport_height, width, height
            )
        )


@dataclass(frozen=True)
class Crop:
    """An explicit crop inside the native viewport, in region coordinates."""

    viewport_width: int
    viewport_height: int
    x: int
    y: int
    width: int = CAPTURE_WIDTH
    height: int = CAPTURE_HEIGHT

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def top(self) -> int:
        return self.y + self.height

    def as_dict(self) -> dict[str, int]:
        return {
            "viewport_width": self.viewport_width,
            "viewport_height": self.viewport_height,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }

    def framebuffer_rect(self, viewport_origin_x: int,
                         viewport_origin_y: int) -> tuple[int, int, int, int]:
        return (
            int(viewport_origin_x) + self.x,
            int(viewport_origin_y) + self.y,
            self.width,
            self.height,
        )


def resolve_crop(viewport_width: int, viewport_height: int,
                 crop_x: int = -1, crop_y: int = -1,
                 width: int = CAPTURE_WIDTH,
                 height: int = CAPTURE_HEIGHT) -> Crop:
    """Resolve a centered (``-1``) or explicit crop inside the viewport."""
    validate_viewport(viewport_width, viewport_height, width, height)
    _as_int("crop_x", crop_x, minimum=-1)
    _as_int("crop_y", crop_y, minimum=-1)
    if crop_x == -1:
        crop_x = (viewport_width - width) // 2
    if crop_y == -1:
        crop_y = (viewport_height - height) // 2
    if crop_x < 0 or crop_y < 0:
        raise PreflightError("crop origin must be non-negative")
    if crop_x + width > viewport_width or crop_y + height > viewport_height:
        raise PreflightError(
            "crop {0}x{1} at ({2},{3}) exceeds real viewport {4}x{5}".format(
                width, height, crop_x, crop_y, viewport_width, viewport_height
            )
        )
    return Crop(viewport_width, viewport_height, crop_x, crop_y, width, height)


def _validate_prefix(prefix: str) -> str:
    if not isinstance(prefix, str) or not prefix:
        raise RecordingError("job directory prefix must be a non-empty string")
    allowed = set("abcdefghijklmnopqrstuvwxyz"
                  "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                  "0123456789._-")
    if any(ch not in allowed for ch in prefix):
        raise RecordingError("job directory prefix contains unsupported characters")
    return prefix


def new_job_directory(output_root: str | os.PathLike[str],
                      prefix: str = "native-recording",
                      now: datetime | None = None) -> Path:
    """Create a fresh, non-overwriting job directory below ``output_root``."""
    root = Path(output_root).expanduser()
    if not str(root):
        raise PreflightError("output_root is empty")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise PreflightError(f"output_root is not a directory: {root}")
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    prefix = _validate_prefix(prefix)
    for _attempt in range(100):
        path = root / f"{prefix}-{stamp}-{secrets.token_hex(6)}"
        try:
            # Do not use tempfile.mkdtemp here: on Windows its protected 0700
            # DACL can be inaccessible to the sandbox user.  Inheriting the
            # explicit output-root ACL is both simpler and auditable.
            path.mkdir(parents=False, exist_ok=False)
            return path
        except FileExistsError:
            continue
    raise RecordingError("could not allocate a fresh job directory")


def _ffmpeg_candidate(candidate: str | os.PathLike[str],
                      label: str) -> str | None:
    """Accept a native ffmpeg executable, never a public script path."""
    path = Path(candidate).expanduser()
    if path.suffix.lower() in _SCRIPT_SUFFIXES:
        raise PreflightError(
            f"{label} points to a script ({path}); provide an ffmpeg executable")
    if path.is_file():
        return str(path.resolve())
    return None


def resolve_ffmpeg(explicit: str | os.PathLike[str] | None = "",
                   env_var: str = "ANIMATION_FFMPEG",
                   default_path: str | os.PathLike[str] | None = None) -> str:
    """Find an existing ffmpeg without installing or modifying anything.

    Precedence: explicit public path, ANIMATION_FFMPEG, this machine's known
    local default, PATH.  The returned path is used only as argv[0] for the
    fixed rawvideo encoder command; no arbitrary script or arguments are run.
    """
    if explicit:
        resolved = _ffmpeg_candidate(explicit, "explicit ffmpeg_path")
        if resolved is not None:
            return resolved
        raise PreflightError(f"explicit ffmpeg_path is not a file: {explicit}")
    env_value = os.environ.get(env_var, "")
    if env_value:
        resolved = _ffmpeg_candidate(env_value, env_var)
        if resolved is not None:
            return resolved
        raise PreflightError(
            f"{env_var} does not point to a file: {env_value}")
    known_default = (DEFAULT_FFMPEG_PATH
                     if default_path is None else default_path)
    if known_default:
        resolved = _ffmpeg_candidate(known_default, "known local ffmpeg default")
        if resolved is not None:
            return resolved
    which = shutil.which("ffmpeg")
    if which:
        resolved = _ffmpeg_candidate(which, "PATH ffmpeg")
        if resolved is not None:
            return resolved
        raise PreflightError(
            f"PATH ffmpeg is not a native executable: {which}")
    raise PreflightError(
        "ffmpeg was not found; set ANIMATION_FFMPEG or pass ffmpeg_path. "
        f"The known local default is {DEFAULT_FFMPEG_PATH}. "
        "This candidate does not install or download ffmpeg."
    )


def fps_text(fps_num: Any, fps_den: Any) -> str:
    """Return an ffmpeg-compatible rational frame rate."""
    fps_num_f = _as_finite("fps_num", fps_num)
    fps_den_f = _as_finite("fps_den", fps_den)
    if fps_num_f <= 0 or fps_den_f <= 0:
        raise RecordingError("fps numerator and denominator must be positive")
    value = fps_num_f / fps_den_f
    fraction = Fraction(value).limit_denominator(1_000_000)
    if fraction.denominator == 1:
        return str(fraction.numerator)
    return f"{fraction.numerator}/{fraction.denominator}"


def fps_value(fps_num: Any, fps_den: Any) -> float:
    num = _as_finite("fps_num", fps_num)
    den = _as_finite("fps_den", fps_den)
    if num <= 0 or den <= 0:
        raise RecordingError("fps numerator and denominator must be positive")
    return num / den


def ffmpeg_argv(ffmpeg_path: str | os.PathLike[str],
                output_path: str | os.PathLike[str],
                width: int = CAPTURE_WIDTH,
                height: int = CAPTURE_HEIGHT,
                fps_num: Any = 24,
                fps_den: Any = 1,
                crf: int = DEFAULT_CRF,
                preset: str = DEFAULT_PRESET,
                threads: int = DEFAULT_THREADS) -> list[str]:
    """Build the rawvideo-to-H.264 command.  No scale filter is allowed."""
    width = _as_int("width", width, minimum=2)
    height = _as_int("height", height, minimum=2)
    if width % 2 or height % 2:
        raise RecordingError("H.264 yuv420p output needs even dimensions")
    crf = _as_int("crf", crf, minimum=0, maximum=51)
    threads = _as_int("threads", threads, minimum=1, maximum=64)
    if not isinstance(preset, str) or not preset:
        raise RecordingError("preset must be a non-empty string")
    if any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in preset):
        raise RecordingError("preset contains unsupported characters")
    return [
        str(ffmpeg_path),
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-n",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}",
        "-framerate", fps_text(fps_num, fps_den),
        "-i", "pipe:0",
        "-an",
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-threads", str(threads),
        "-movflags", "+faststart",
        str(output_path),
    ]


@dataclass(frozen=True)
class FramePacket:
    """One accepted native draw waiting for the bytes-only writer thread."""

    sample_index: int
    scene_frame: int
    scene_subframe: float
    viewport: tuple[int, int, int, int]
    crop: tuple[int, int, int, int]
    channels_used: int
    rgb: bytes
    capture_seconds: float | None = None
    frame_after: int | None = None


@dataclass(frozen=True)
class DrawDecision:
    """Result of classifying one POST_PIXEL draw."""

    accept: bool
    reason: str
    fatal: bool = False


class RecordingStats:
    """Thread-safe, non-filling frame accounting.

    ``classify_draw`` is called by the Blender main thread.  ``record_encoded``
    is called by the writer thread after the exact bytes have reached ffmpeg.
    Duplicate and out-of-order draws are counted and ignored; missing scene
    frames are never synthesized.
    """

    def __init__(self, start_frame: int, end_frame: int,
                 max_event_lists: int = 512):
        self.start_frame = _as_int("start_frame", start_frame)
        self.end_frame = _as_int("end_frame", end_frame)
        if self.end_frame < self.start_frame:
            raise RecordingError("end_frame must be >= start_frame")
        self.expected_count = self.end_frame - self.start_frame + 1
        if self.expected_count > 100_000:
            raise RecordingError("frame range is too large for this candidate")
        self.max_event_lists = _as_int("max_event_lists", max_event_lists,
                                       minimum=1)
        self._lock = threading.RLock()
        self._seen_keys: set[tuple[int, float]] = set()
        self._encoded_keys: set[tuple[int, float]] = set()
        self._last_accepted_key: tuple[int, float] | None = None
        self._last_seen_key: tuple[int, float] | None = None
        self.draws_total = 0
        self.draw_started_monotonic: float | None = None
        self.draw_last_monotonic: float | None = None
        self.first_draw_key: tuple[int, float] | None = None
        self.last_draw_key: tuple[int, float] | None = None
        self.viewport_initial: tuple[int, int, int, int] | None = None
        self.viewport_last: tuple[int, int, int, int] | None = None
        self.crop_initial: tuple[int, int, int, int] | None = None
        self.size_changes: list[dict[str, Any]] = []
        self.duplicate_draws = 0
        self.duplicate_frames: list[int] = []
        self.out_of_order_draws = 0
        self.out_of_order_frames: list[int] = []
        self.out_of_range_draws = 0
        self.out_of_range_frames: list[int] = []
        self.queue_drops = 0
        self.dropped_frames: list[int] = []
        self.encoded_samples: list[dict[str, Any]] = []
        self.encoded_started_monotonic: float | None = None
        self.encoded_last_monotonic: float | None = None
        self.end_seen = False
        self.end_seen_monotonic: float | None = None
        self.max_seen_frame: int | None = None

    def _remember(self, rows: list[Any], value: Any) -> None:
        if len(rows) < self.max_event_lists:
            rows.append(value)

    def classify_draw(self, scene_frame: int, scene_subframe: float,
                      viewport: Sequence[int],
                      crop: Sequence[int]) -> DrawDecision:
        frame = _as_int("scene_frame", scene_frame)
        subframe = _as_finite("scene_subframe", scene_subframe)
        if len(viewport) != 4 or len(crop) != 4:
            raise RecordingError("viewport and crop must each have four values")
        vp = tuple(_as_int("viewport component", int(v)) for v in viewport)
        crop_tuple = tuple(_as_int("crop component", int(v)) for v in crop)
        if vp[2] < CAPTURE_WIDTH or vp[3] < CAPTURE_HEIGHT:
            with self._lock:
                self.draws_total += 1
                self.viewport_last = vp
            return DrawDecision(
                False,
                "real GPU viewport is {0}x{1}; need >= {2}x{3}".format(
                    vp[2], vp[3], CAPTURE_WIDTH, CAPTURE_HEIGHT),
                True,
            )
        now = time.monotonic()
        key = (frame, round(subframe, 6))
        with self._lock:
            self.draws_total += 1
            if self.draw_started_monotonic is None:
                self.draw_started_monotonic = now
            self.draw_last_monotonic = now
            if self.first_draw_key is None:
                self.first_draw_key = key
            self.last_draw_key = key
            self.viewport_last = vp
            if self.viewport_initial is None:
                self.viewport_initial = vp
                self.crop_initial = crop_tuple
            elif vp != self.viewport_initial:
                if len(self.size_changes) < 32:
                    self.size_changes.append({
                        "draw": self.draws_total,
                        "scene_frame": frame,
                        "from": list(self.viewport_initial),
                        "to": list(vp),
                    })
                return DrawDecision(False, "real viewport size/origin changed", True)
            if self.max_seen_frame is None or frame > self.max_seen_frame:
                self.max_seen_frame = frame
            if key in self._seen_keys:
                self.duplicate_draws += 1
                self._remember(self.duplicate_frames, frame)
                return DrawDecision(False, "duplicate draw")
            self._seen_keys.add(key)
            if frame < self.start_frame or frame > self.end_frame:
                self.out_of_range_draws += 1
                self._remember(self.out_of_range_frames, frame)
                if frame >= self.end_frame:
                    self.end_seen = True
                    if self.end_seen_monotonic is None:
                        self.end_seen_monotonic = now
                return DrawDecision(False, "out-of-range draw")
            if (self._last_accepted_key is not None
                    and key < self._last_accepted_key):
                self.out_of_order_draws += 1
                self._remember(self.out_of_order_frames, frame)
                return DrawDecision(False, "out-of-order draw")
            self._last_accepted_key = key
            if frame >= self.end_frame:
                self.end_seen = True
                if self.end_seen_monotonic is None:
                    self.end_seen_monotonic = now
            return DrawDecision(True, "accept")

    def record_queue_drop(self, scene_frame: int, scene_subframe: float,
                          reason: str) -> None:
        frame = _as_int("scene_frame", scene_frame)
        subframe = _as_finite("scene_subframe", scene_subframe)
        with self._lock:
            self.queue_drops += 1
            self._remember(self.dropped_frames, frame)

    def record_encoded(self, scene_frame: int, scene_subframe: float,
                       sample_index: int, channels_used: int) -> None:
        frame = _as_int("scene_frame", scene_frame)
        subframe = _as_finite("scene_subframe", scene_subframe)
        key = (frame, round(subframe, 6))
        now = time.monotonic()
        with self._lock:
            if self.encoded_started_monotonic is None:
                self.encoded_started_monotonic = now
            self.encoded_last_monotonic = now
            self._encoded_keys.add(key)
            self.encoded_samples.append({
                "sample_index": _as_int("sample_index", sample_index,
                                        minimum=0),
                "scene_frame": frame,
                "scene_subframe": subframe,
                "channels_used": _as_int("channels_used", channels_used,
                                          minimum=3, maximum=4),
            })

    def encoded_frame_count(self) -> int:
        with self._lock:
            return len(self.encoded_samples)

    def all_expected_encoded(self) -> bool:
        with self._lock:
            return {key[0] for key in self._encoded_keys} >= set(
                range(self.start_frame, self.end_frame + 1)
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            expected = list(range(self.start_frame, self.end_frame + 1))
            encoded_frames = sorted({key[0] for key in self._encoded_keys})
            missing = sorted(set(expected) - set(encoded_frames))
            extra = sorted(set(encoded_frames) - set(expected))
            encoded_keys_ordered = [
                (row["scene_frame"], round(row["scene_subframe"], 6))
                for row in self.encoded_samples
            ]
            out_of_order_encoded = sum(
                1 for prev, cur in zip(encoded_keys_ordered,
                                       encoded_keys_ordered[1:])
                if cur < prev
            )
            subframe_encoded = sum(
                1 for row in self.encoded_samples
                if abs(row["scene_subframe"]) > 1e-9
            )
            encoded_count = len(self.encoded_samples)
            unique_count = len(encoded_frames)
            technical_complete = (
                bool(encoded_count)
                and not missing
                and not extra
                and self.viewport_initial is not None
                and not self.size_changes
                and out_of_order_encoded == 0
            )
            strict_draw_order = (
                self.duplicate_draws == 0
                and self.out_of_order_draws == 0
                and self.out_of_range_draws == 0
                and not self.size_changes
            )
            return {
                "expected_start": self.start_frame,
                "expected_end": self.end_frame,
                "expected_count": len(expected),
                "encoded_count": encoded_count,
                "encoded_unique_frame_count": unique_count,
                "encoded_frames": encoded_frames,
                "missing_frames": missing,
                "extra_frames": extra,
                "coverage_ratio": (unique_count / len(expected)
                                   if expected else 0.0),
                "technical_complete": technical_complete,
                "strict_draw_order": strict_draw_order,
                "draws_total": self.draws_total,
                "duplicate_draws": self.duplicate_draws,
                "duplicate_frames": list(self.duplicate_frames),
                "out_of_order_draws": self.out_of_order_draws,
                "out_of_order_frames": list(self.out_of_order_frames),
                "out_of_range_draws": self.out_of_range_draws,
                "out_of_range_frames": list(self.out_of_range_frames),
                "queue_drops": self.queue_drops,
                "dropped_frames": list(self.dropped_frames),
                "size_changes": list(self.size_changes),
                "subframe_encoded": subframe_encoded,
                "out_of_order_encoded": out_of_order_encoded,
                "viewport_initial": (list(self.viewport_initial)
                                     if self.viewport_initial else None),
                "viewport_last": (list(self.viewport_last)
                                  if self.viewport_last else None),
                "crop_initial": (list(self.crop_initial)
                                 if self.crop_initial else None),
                "first_draw": (list(self.first_draw_key)
                               if self.first_draw_key else None),
                "last_draw": (list(self.last_draw_key)
                              if self.last_draw_key else None),
                "first_encoded": (encoded_frames[0] if encoded_frames else None),
                "last_encoded": (encoded_frames[-1] if encoded_frames else None),
                "end_seen": self.end_seen,
                "max_seen_frame": self.max_seen_frame,
                "event_lists_capped_at": self.max_event_lists,
                "draw_elapsed_seconds": (
                    (self.draw_last_monotonic - self.draw_started_monotonic)
                    if self.draw_started_monotonic is not None
                    and self.draw_last_monotonic is not None else None
                ),
                "encode_elapsed_seconds": (
                    (self.encoded_last_monotonic - self.encoded_started_monotonic)
                    if self.encoded_started_monotonic is not None
                    and self.encoded_last_monotonic is not None else None
                ),
            }


_SENTINEL = object()


class RawVideoSink:
    """Own exactly one ffmpeg Popen and feed it from a bounded queue.

    Only ``finish`` (main thread) waits for, terminates, or kills the exact
    child.  The writer thread only writes bytes and sample metadata; it never
    imports or calls ``bpy``.
    """

    def __init__(self,
                 job_dir: str | os.PathLike[str],
                 ffmpeg_path: str | os.PathLike[str],
                 output_path: str | os.PathLike[str],
                 stats: RecordingStats,
                 width: int = CAPTURE_WIDTH,
                 height: int = CAPTURE_HEIGHT,
                 fps_num: Any = 24,
                 fps_den: Any = 1,
                 queue_frames: int = DEFAULT_QUEUE_FRAMES,
                 crf: int = DEFAULT_CRF,
                 thread_count: int = DEFAULT_THREADS,
                 popen_factory: Callable[..., Any] | None = None,
                 create_no_window: bool = True):
        self.job_dir = Path(job_dir)
        self.ffmpeg_path = Path(ffmpeg_path)
        self.output_path = Path(output_path)
        self.samples_path = self.job_dir / "samples.jsonl"
        self.log_path = self.job_dir / "ffmpeg.log"
        if self.output_path.parent != self.job_dir:
            raise RecordingError("output_path must be directly inside job_dir")
        if not self.job_dir.is_dir():
            raise RecordingError(f"job_dir does not exist: {self.job_dir}")
        self.stats = stats
        self.width = _as_int("width", width, minimum=2)
        self.height = _as_int("height", height, minimum=2)
        self.fps_num = fps_num
        self.fps_den = fps_den
        self.fps = fps_value(fps_num, fps_den)
        self.queue_frames = _as_int("queue_frames", queue_frames,
                                    minimum=1, maximum=32)
        self.crf = _as_int("crf", crf, minimum=0, maximum=51)
        self.thread_count = _as_int("thread_count", thread_count,
                                    minimum=1, maximum=64)
        self._popen = popen_factory or subprocess.Popen
        self._create_no_window = bool(create_no_window)
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self.queue_frames)
        self._lock = threading.RLock()
        self._proc: Any = None
        self._stdin: Any = None
        self._log_handle: Any = None
        self._thread: threading.Thread | None = None
        self._argv: list[str] = []
        self._started = False
        self._closing = False
        self._closed = False
        self._errors: list[str] = []
        self._metadata_error: str | None = None
        self._write_failed = False
        self._offered = 0
        self._written = 0
        self._next_sample_index = 0
        self._started_monotonic: float | None = None
        self._finished_monotonic: float | None = None
        self._summary: dict[str, Any] | None = None

    @property
    def queue_size(self) -> int:
        return self._queue.maxsize

    def queue_has_capacity(self) -> bool:
        with self._lock:
            if not self._started or self._closing or self._closed:
                return False
            if self._write_failed:
                return False
        return self._queue.qsize() < self._queue.maxsize

    def early_exit_code(self) -> int | None:
        with self._lock:
            if not self._started or self._closed:
                return None
            proc = self._proc
        if proc is None:
            return None
        return proc.poll()

    def errors_snapshot(self) -> list[str]:
        with self._lock:
            return list(self._errors)

    def write_failed(self) -> bool:
        with self._lock:
            return bool(self._write_failed)

    def start(self) -> list[str]:
        with self._lock:
            if self._started:
                raise RecordingError("RawVideoSink already started")
            if self._closed:
                raise RecordingError("RawVideoSink already closed")
            for path in (self.output_path, self.samples_path, self.log_path):
                if path.exists():
                    raise PreflightError(f"refusing to overwrite {path}")
            self._argv = ffmpeg_argv(
                self.ffmpeg_path,
                self.output_path,
                width=self.width,
                height=self.height,
                fps_num=self.fps_num,
                fps_den=self.fps_den,
                crf=self.crf,
                threads=self.thread_count,
            )
            try:
                self._log_handle = open(self.log_path, "xb")
            except BaseException:
                self._closed = True
                raise
            kwargs: dict[str, Any] = {
                "stdin": subprocess.PIPE,
                "stdout": self._log_handle,
                "stderr": subprocess.STDOUT,
                "bufsize": 0,
                "shell": False,
            }
            if os.name == "nt" and self._create_no_window:
                kwargs["creationflags"] = getattr(
                    subprocess, "CREATE_NO_WINDOW", 0)
            elif os.name != "nt":
                kwargs["start_new_session"] = True
            proc = None
            try:
                proc = self._popen(self._argv, **kwargs)
            except BaseException:
                self._close_log()
                self._closed = True
                raise
            if proc is None or getattr(proc, "stdin", None) is None:
                self._terminate_proc(proc, self._errors)
                self._close_log()
                self._closed = True
                raise RecordingError("ffmpeg Popen did not expose a stdin pipe")
            self._proc = proc
            self._stdin = proc.stdin
            self._thread = threading.Thread(
                target=self._writer_main,
                name="native-recording-ffmpeg-writer",
                daemon=True,
            )
            try:
                self._thread.start()
            except BaseException:
                self._terminate_proc(proc, self._errors)
                self._close_log()
                self._closed = True
                raise
            self._started = True
            self._started_monotonic = time.monotonic()
        return list(self._argv)

    def offer(self, scene_frame: int, scene_subframe: float,
              viewport: Sequence[int], crop: Sequence[int],
              rgb: bytes, channels_used: int,
              capture_seconds: float | None = None,
              frame_after: int | None = None) -> bool:
        frame = _as_int("scene_frame", scene_frame)
        subframe = _as_finite("scene_subframe", scene_subframe)
        if not isinstance(rgb, (bytes, bytearray)):
            raise RecordingError("rgb must be bytes")
        expected = self.width * self.height * 3
        if len(rgb) != expected:
            self._errors.append(
                f"rgb size {len(rgb)} != expected {expected}; frame dropped")
            return False
        if len(viewport) != 4 or len(crop) != 4:
            raise RecordingError("viewport and crop must each have four values")
        with self._lock:
            if not self._started or self._closing or self._closed:
                return False
            if self._write_failed:
                return False
            sample_index = self._next_sample_index
        packet = FramePacket(
            sample_index=sample_index,
            scene_frame=frame,
            scene_subframe=subframe,
            viewport=tuple(int(v) for v in viewport),
            crop=tuple(int(v) for v in crop),
            channels_used=_as_int("channels_used", channels_used,
                                  minimum=3, maximum=4),
            rgb=bytes(rgb),
            capture_seconds=(
                _as_finite("capture_seconds", capture_seconds)
                if capture_seconds is not None else None
            ),
            frame_after=(
                _as_int("frame_after", frame_after)
                if frame_after is not None else None
            ),
        )
        try:
            self._queue.put_nowait(packet)
        except queue.Full:
            return False
        with self._lock:
            self._next_sample_index += 1
            self._offered += 1
        return True

    def _writer_main(self) -> None:
        metadata = None
        metadata_error = None
        try:
            metadata = open(self.samples_path, "x", encoding="utf-8",
                            newline="\n")
        except BaseException as exc:
            metadata_error = repr(exc)
        with self._lock:
            self._metadata_error = metadata_error
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.5)
                except queue.Empty:
                    with self._lock:
                        if self._closing:
                            break
                    continue
                if item is _SENTINEL:
                    break
                if self._write_failed:
                    break
                try:
                    written = self._stdin.write(item.rgb)
                    if written is not None and written != len(item.rgb):
                        raise OSError(
                            f"short stdin write {written}/{len(item.rgb)}")
                    self.stats.record_encoded(
                        item.scene_frame,
                        item.scene_subframe,
                        item.sample_index,
                        item.channels_used,
                    )
                    with self._lock:
                        self._written += 1
                    if metadata is not None:
                        row = {
                            "sample_index": item.sample_index,
                            "scene_frame": item.scene_frame,
                            "scene_subframe": item.scene_subframe,
                            "viewport": list(item.viewport),
                            "crop": list(item.crop),
                            "channels_used": item.channels_used,
                            "rgb_bytes": len(item.rgb),
                            "capture_seconds": item.capture_seconds,
                            "frame_after": item.frame_after,
                            "monotonic": time.monotonic(),
                        }
                        metadata.write(json.dumps(
                            row, ensure_ascii=False, allow_nan=False) + "\n")
                        metadata.flush()
                except BaseException as exc:
                    with self._lock:
                        self._write_failed = True
                        self._errors.append("writer: " + repr(exc))
                    break
        finally:
            if metadata is not None:
                try:
                    metadata.close()
                except BaseException as exc:
                    with self._lock:
                        self._errors.append("samples close: " + repr(exc))

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except BaseException as exc:
                self._errors.append("ffmpeg log close: " + repr(exc))
            finally:
                self._log_handle = None

    def _terminate_proc(self, proc: Any, errors: list[str]) -> int | None:
        """Terminate/kill only the exact Popen object passed in."""
        if proc is None:
            return None
        returncode = None
        try:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except BaseException as exc:
                    errors.append("ffmpeg terminate: " + repr(exc))
                try:
                    returncode = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    errors.append("ffmpeg terminate timeout; kill exact child")
                    try:
                        proc.kill()
                    except BaseException as exc:
                        errors.append("ffmpeg kill: " + repr(exc))
                    try:
                        returncode = proc.wait(timeout=5)
                    except BaseException as exc:
                        errors.append("ffmpeg kill wait: " + repr(exc))
                except BaseException as exc:
                    errors.append("ffmpeg terminate wait: " + repr(exc))
            else:
                try:
                    returncode = proc.wait(timeout=5)
                except BaseException as exc:
                    errors.append("ffmpeg final wait: " + repr(exc))
        except BaseException as exc:
            errors.append("ffmpeg cleanup: " + repr(exc))
        return returncode

    def finish(self, reason: str, *, join_timeout: float = 5.0,
               wait_timeout: float = 30.0) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                if self._summary is None:
                    self._summary = self._make_summary(
                        reason, (None, False), False, list(self._errors))
                return dict(self._summary or {})
            self._closing = True
            started = self._started
            proc = self._proc
            thread = self._thread
        errors: list[str] = []
        if not started:
            with self._lock:
                errors.extend(self._errors)
                self._closed = True
                self._finished_monotonic = time.monotonic()
                self._summary = self._make_summary(reason, None, False, errors)
            self._close_log()
            return dict(self._summary)

        try:
            self._queue.put(_SENTINEL, timeout=2.0)
        except queue.Full:
            errors.append("sentinel queue was full")
        if thread is not None:
            thread.join(timeout=join_timeout)
            if thread.is_alive():
                errors.append(
                    "writer thread did not stop; terminating exact ffmpeg child")
                self._terminate_proc(proc, errors)
                thread.join(timeout=2.0)
        try:
            if self._stdin is not None:
                self._stdin.close()
        except BaseException as exc:
            errors.append("stdin close: " + repr(exc))

        returncode = None
        reaped = False
        if proc is not None:
            try:
                returncode = proc.wait(timeout=wait_timeout)
                reaped = True
            except subprocess.TimeoutExpired:
                errors.append(
                    "ffmpeg did not finalize in time; terminating exact child")
                returncode = self._terminate_proc(proc, errors)
                try:
                    proc.wait(timeout=1.0)
                    reaped = True
                except BaseException as exc:
                    errors.append("ffmpeg final reap: " + repr(exc))
            except BaseException as exc:
                errors.append(
                    "ffmpeg wait failed; terminating exact child: " + repr(exc))
                returncode = self._terminate_proc(proc, errors)
                try:
                    proc.wait(timeout=1.0)
                    reaped = True
                except BaseException as exc2:
                    errors.append("ffmpeg final reap: " + repr(exc2))
            if not reaped:
                errors.append("ffmpeg still unreaped; kill exact child")
                try:
                    proc.kill()
                except BaseException as exc:
                    errors.append("ffmpeg final kill: " + repr(exc))
                try:
                    returncode = proc.wait(timeout=5.0)
                    reaped = True
                except BaseException as exc:
                    errors.append("ffmpeg final kill wait: " + repr(exc))
        self._close_log()
        with self._lock:
            errors = list(self._errors) + errors
            self._closed = True
            self._finished_monotonic = time.monotonic()
            self._summary = self._make_summary(
                reason, (returncode, reaped), True, errors)
            summary = dict(self._summary)
        return summary

    def _make_summary(self, reason: str, terminal: Any, started: bool,
                      errors: list[str]) -> dict[str, Any]:
        proc = self._proc
        returncode = terminal[0] if terminal else None
        reaped = bool(terminal[1]) if terminal else False
        thread = self._thread
        video_exists = self.output_path.is_file()
        samples_exists = self.samples_path.is_file()
        log_exists = self.log_path.is_file()
        try:
            video_bytes = self.output_path.stat().st_size if video_exists else None
        except BaseException:
            video_bytes = None
        samples_lines = None
        if samples_exists:
            try:
                with open(self.samples_path, "r", encoding="utf-8") as stream:
                    samples_lines = sum(1 for _ in stream)
            except BaseException:
                samples_lines = None
        elapsed = None
        if self._started_monotonic is not None:
            end = self._finished_monotonic or time.monotonic()
            elapsed = end - self._started_monotonic
        return {
            "started": started,
            "reason": reason,
            "argv": list(self._argv),
            "pid": getattr(proc, "pid", None),
            "returncode": returncode,
            "reaped": reaped,
            "writer_thread_alive": bool(thread and thread.is_alive()),
            "frames_offered_to_queue": self._offered,
            "frames_written": self._written,
            "queue_maxsize": self._queue.maxsize,
            "queue_drops": self.stats.snapshot()["queue_drops"],
            "metadata_error": self._metadata_error,
            "errors": list(errors),
            "video_path": str(self.output_path),
            "video_exists": video_exists,
            "video_bytes": video_bytes,
            "samples_path": str(self.samples_path),
            "samples_exists": samples_exists,
            "samples_lines": samples_lines,
            "log_path": str(self.log_path),
            "log_exists": log_exists,
            "elapsed_seconds": elapsed,
            "popen_ownership": "exact_popen_child_only",
            "argv_fixed_by_module": True,
            "shell": False,
        }


def sha256_file(path: str | os.PathLike[str],
                chunk_size: int = 1024 * 1024) -> str:
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def write_json_unique(job_dir: str | os.PathLike[str], stem: str,
                      payload: Mapping[str, Any]) -> Path:
    """Write a new JSON file without overwriting an earlier report."""
    directory = Path(job_dir)
    if not directory.is_dir():
        raise RecordingError(f"job_dir does not exist: {directory}")
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                      indent=2, sort_keys=True) + "\n"
    for index in range(1, 101):
        name = f"{stem}.json" if index == 1 else f"{stem}-{index}.json"
        path = directory / name
        try:
            with open(path, "x", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            return path
        except FileExistsError:
            continue
    raise RecordingError(f"could not allocate a fresh {stem}.json")


def build_report(*,
                 status: str,
                 reason: str,
                 job_dir: str | os.PathLike[str],
                 preflight: Mapping[str, Any],
                 stats: Mapping[str, Any],
                 encoder: Mapping[str, Any],
                 capture_route: Mapping[str, Any] | None = None,
                 cleanup_errors: Iterable[str] = (),
                 started_at: str | None = None,
                 finished_at: str | None = None,
                 elapsed_seconds: float | None = None,
                 extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the honest, review-oriented report."""
    cleanup = [str(item) for item in cleanup_errors]
    encoder_errors = [str(item) for item in encoder.get("errors", [])]
    all_errors = cleanup + encoder_errors
    missing = list(stats.get("missing_frames", []))
    duplicates = int(stats.get("duplicate_draws", 0))
    out_of_order = int(stats.get("out_of_order_draws", 0))
    queue_drops = int(stats.get("queue_drops", 0))
    encoded_count = int(stats.get("encoded_count", 0))
    expected_count = int(stats.get("expected_count", 0))
    returncode = encoder.get("returncode")
    reaped = bool(encoder.get("reaped", False))
    technical_complete = (
        bool(stats.get("technical_complete", False))
        and returncode == 0
        and reaped
        and not all_errors
    )
    fps = preflight.get("fps_value") or 0.0
    encoded_duration = (encoded_count / fps) if fps else None
    expected_duration = (expected_count / fps) if fps else None
    report: dict[str, Any] = {
        "schema": "animation-native-viewport-recording-v1",
        "status": status,
        "reason": reason,
        "job_dir": str(job_dir),
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_seconds": elapsed_seconds,
        "candidate": True,
        "visual_acceptance": "not_assessed",
        "recording_pass": False,
        "gpu_readback_verified": False,
        "capture_route": (dict(capture_route) if capture_route is not None
                          else {
                              "playback": "bpy.ops.screen.animation_play",
                              "draw": "SpaceView3D POST_PIXEL",
                              "framebuffer": "gpu.state.active_framebuffer_get().read_color",
                              "framebuffer_format": "UBYTE",
                              "encoder": "ffmpeg rawvideo rgb24 via exact Popen",
                              "encoder_fixed_argv_only": True,
                              "arbitrary_script": False,
                              "shell": False,
                              "crop_origin": "viewport_lower_left",
                              "crop_size": [CAPTURE_WIDTH, CAPTURE_HEIGHT],
                              "gpu_offscreen": False,
                              "render_render": False,
                              "render_opengl": False,
                              "playblast": False,
                              "frame_set_screenshot_loop": False,
                          }),
        "preflight": dict(preflight),
        "frames": dict(stats),
        "encoder": dict(encoder),
        "video": {
            "path": encoder.get("video_path"),
            "exists": bool(encoder.get("video_exists", False)),
            "bytes": encoder.get("video_bytes"),
            "encoded_frame_count": encoded_count,
            "expected_frame_count": expected_count,
            "expected_duration_seconds": expected_duration,
            "encoded_duration_seconds": encoded_duration,
            "duration_is_shorter_if_frames_missing": True,
        },
        "honesty": {
            "frames_filled": False,
            "frames_interpolated": False,
            "frames_upscaled": False,
            "native_crop_required": [CAPTURE_WIDTH, CAPTURE_HEIGHT],
            "missing_frames_reported": missing,
            "duplicate_draws": duplicates,
            "out_of_order_draws": out_of_order,
            "queue_drops": queue_drops,
            "missing_frames_are_not_filled": True,
        },
        "technical_complete": technical_complete,
        "cleanup": {
            "errors": all_errors,
            "cleanup_ok": not all_errors,
            "encoder_reaped": reaped,
            "encoder_returncode": returncode,
        },
        "safety": {
            "blend_saved_by_module": False,
            "preferences_written": False,
            "source_file_opened_or_reset": False,
            "other_processes_closed": False,
            "only_exact_ffmpeg_child_owned": True,
        },
        "qualification": {
            "raw_candidate_only": True,
            "requires_main_thread_gui_review": True,
            "requires_visual_review": True,
            "recording_pass": False,
            "gpu_readback_verified": False,
        },
    }
    if extra:
        report.update(dict(extra))
    return report
