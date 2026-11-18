"""Pure host-testable helpers for final-window screenshot capture.

The Blender 5.2 ``Window.screenshot(region=..., use_alpha=False)`` API returns
an RGBA memoryview of the window's pixel data, rows bottom-to-top.  This module
contains only geometry, pixel conversion, metrics, and BMP writing so those
rules can be tested without a GUI or GPU context.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Sequence


class CaptureGeometryError(ValueError):
    """The requested window region/crop is outside the real window."""


def _as_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CaptureGeometryError(f"{name} must be an integer")
    return value


def window_region(area_x: int, area_y: int,
                  region_x: int, region_y: int,
                  region_width: int, region_height: int,
                  crop_x: int, crop_y: int,
                  crop_width: int, crop_height: int,
                  window_width: int, window_height: int
                  ) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return ((x0,y0),(x1,y1)) for Window.screenshot in window pixels.

    Coordinates are window-local with the lower-left origin, matching Blender
    area/region coordinates and the documented Window.screenshot region.
    The end coordinate is exclusive.
    """
    area_x = _as_int("area_x", area_x)
    area_y = _as_int("area_y", area_y)
    region_x = _as_int("region_x", region_x)
    region_y = _as_int("region_y", region_y)
    region_width = _as_int("region_width", region_width)
    region_height = _as_int("region_height", region_height)
    crop_x = _as_int("crop_x", crop_x)
    crop_y = _as_int("crop_y", crop_y)
    crop_width = _as_int("crop_width", crop_width)
    crop_height = _as_int("crop_height", crop_height)
    window_width = _as_int("window_width", window_width)
    window_height = _as_int("window_height", window_height)
    if min(region_width, region_height, crop_width, crop_height,
           window_width, window_height) <= 0:
        raise CaptureGeometryError("region/crop/window dimensions must be positive")
    if crop_x < 0 or crop_y < 0:
        raise CaptureGeometryError("crop origin must be non-negative")
    if crop_x + crop_width > region_width:
        raise CaptureGeometryError("crop exceeds region width")
    if crop_y + crop_height > region_height:
        raise CaptureGeometryError("crop exceeds region height")
    x0 = area_x + region_x + crop_x
    y0 = area_y + region_y + crop_y
    x1 = x0 + crop_width
    y1 = y0 + crop_height
    if x0 < 0 or y0 < 0 or x1 > window_width or y1 > window_height:
        raise CaptureGeometryError(
            "Window.screenshot crop is outside the real window: "
            f"({x0},{y0})-({x1},{y1}) not inside "
            f"{window_width}x{window_height}"
        )
    return (x0, y0), (x1, y1)


def rgba_to_rgb24(buf: object, width: int, height: int,
                  channels: int = 4, flip_vertical: bool = True) -> bytes:
    """Convert RGBA/UBYTE rows (bottom-to-top) to top-down RGB24 bytes."""
    import numpy as np

    width = _as_int("width", width)
    height = _as_int("height", height)
    channels = _as_int("channels", channels)
    if channels not in (3, 4):
        raise ValueError("channels must be 3 or 4")
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
        values = buf.to_list()  # type: ignore[attr-defined]
        array = np.asarray(values, dtype=np.uint8).reshape(-1)
    if array.size != expected:
        raise ValueError(
            f"pixel buffer has {array.size} bytes; expected {expected}")
    if channels == 4:
        array = array.reshape(height, width, 4)[:, :, :3]
    else:
        array = array.reshape(height, width, 3)
    if flip_vertical:
        array = array[::-1]
    return array.tobytes()


def window_screenshot_rgba_to_rgb24(buf: object, width: int, height: int,
                                   flip_vertical: bool = True
                                   ) -> tuple[bytes, str]:
    """Convert Window.screenshot RGBA data without assuming numeric format.

    ``Window.screenshot`` documents a memoryview of format ``'B'``, but this
    helper also accepts float32/float64 memoryviews and reports which path was
    used.  This prevents 0..1 float pixels being silently truncated to uint8.
    """
    import numpy as np

    width = _as_int("width", width)
    height = _as_int("height", height)
    expected_bytes = width * height * 4
    fmt = getattr(buf, "format", None)
    nbytes = getattr(buf, "nbytes", None)
    if fmt == "B" and (nbytes is None or nbytes == expected_bytes):
        return rgba_to_rgb24(buf, width, height, 4, flip_vertical), "ubyte"
    if fmt in ("f", "d") or nbytes == expected_bytes * 4:
        values = np.asarray(buf, dtype=np.float32).reshape(
            height, width, 4)[:, :, :3]
        if flip_vertical:
            values = values[::-1]
        if values.size and float(np.nanmax(values)) <= 1.0 + 1e-6:
            values = values * 255.0
        values = np.clip(values, 0.0, 255.0).astype(np.uint8)
        return values.tobytes(), "float32"
    try:
        raw = np.frombuffer(buf, dtype=np.uint8).reshape(-1)
        if raw.size == expected_bytes:
            return rgba_to_rgb24(buf, width, height, 4, flip_vertical), "ubyte"
    except BaseException:
        pass
    try:
        values = np.asarray(buf, dtype=np.float32).reshape(-1)
        if values.size == width * height * 4:
            values = values.reshape(height, width, 4)[:, :, :3]
            if flip_vertical:
                values = values[::-1]
            if values.size and float(np.nanmax(values)) <= 1.0 + 1e-6:
                values = values * 255.0
            values = np.clip(values, 0.0, 255.0).astype(np.uint8)
            return values.tobytes(), "float32_fallback"
    except BaseException:
        pass
    raise ValueError(
        "Window.screenshot buffer did not look like RGBA UBYTE or float: "
        f"format={fmt!r} nbytes={nbytes!r} expected={expected_bytes}")


def crop_rgb24(rgb: bytes, width: int, height: int,
               x: int, y: int, crop_width: int, crop_height: int) -> bytes:
    """Crop a top-down RGB24 image; coordinates are top-left based."""
    import numpy as np

    width = _as_int("width", width)
    height = _as_int("height", height)
    x = _as_int("x", x)
    y = _as_int("y", y)
    crop_width = _as_int("crop_width", crop_width)
    crop_height = _as_int("crop_height", crop_height)
    if x < 0 or y < 0 or x + crop_width > width or y + crop_height > height:
        raise CaptureGeometryError("RGB crop exceeds image")
    array = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3)
    return array[y:y + crop_height, x:x + crop_width].tobytes()


def rgb_metrics(rgb: bytes, width: int, height: int,
                black_threshold: int = 8,
                color_threshold: int = 8) -> dict[str, object]:
    """Report simple content metrics; never a visual-acceptance claim."""
    import numpy as np

    width = _as_int("width", width)
    height = _as_int("height", height)
    array = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3)
    wide = array.astype(np.int16)
    maximum = wide.max(axis=2)
    minimum = wide.min(axis=2)
    nonblack = maximum > black_threshold
    colored = (maximum - minimum) > color_threshold
    total = width * height
    return {
        "width": width,
        "height": height,
        "nonblack_fraction": float(nonblack.sum()) / total,
        "colored_fraction": float(colored.sum()) / total,
        "mean_rgb": [float(value) for value in wide.mean(axis=(0, 1))],
        "max_rgb": [int(value) for value in array.max(axis=(0, 1))],
        "black_threshold": black_threshold,
        "color_threshold": color_threshold,
        "visual_acceptance": "not_assessed",
    }


def classify_pixel_values(values: object, expected_count: int) -> dict[str, object]:
    """Classify a raw pixel buffer's numeric range without assuming a format.

    This directly guards against reading UBYTE as float 0..1 and then
    truncating it to uint8 (which would make most pixels black).
    """
    import numpy as np

    expected = _as_int("expected_count", expected_count)
    if (isinstance(values, (bytes, bytearray))
            or (isinstance(values, memoryview)
                and getattr(values, "format", "B") == "B")):
        array = np.frombuffer(values, dtype=np.uint8).reshape(-1)
    else:
        array = np.asarray(values).reshape(-1)
    if array.size != expected:
        return {
            "count": int(array.size),
            "expected_count": expected,
            "size_mismatch": True,
            "visual_acceptance": "not_assessed",
        }
    numeric = array.astype(np.float64, copy=False)
    finite = np.isfinite(numeric)
    if not finite.all():
        return {
            "count": int(array.size),
            "expected_count": expected,
            "size_mismatch": False,
            "nonfinite_count": int((~finite).sum()),
            "visual_acceptance": "not_assessed",
        }
    minimum = float(numeric.min())
    maximum = float(numeric.max())
    mean = float(numeric.mean())
    integral = bool(np.allclose(numeric, np.round(numeric),
                                rtol=0.0, atol=1e-6))
    return {
        "count": int(array.size),
        "expected_count": expected,
        "size_mismatch": False,
        "min": minimum,
        "max": maximum,
        "mean": mean,
        "integral": integral,
        "unit_float_candidate": bool(minimum >= 0.0
                                     and maximum <= 1.0 + 1e-6),
        "ubyte_candidate": bool(minimum >= 0.0
                               and maximum <= 255.0
                               and integral),
        "float_0_255_candidate": bool(minimum >= 0.0
                                      and maximum <= 255.0
                                      and not integral),
        "visual_acceptance": "not_assessed",
    }


def write_bmp_rgb24(path: str | Path, rgb: bytes, width: int, height: int) -> Path:
    """Write a top-down 24-bit BMP with correct 4-byte row padding."""
    import numpy as np

    width = _as_int("width", width)
    height = _as_int("height", height)
    array = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3)
    row_bytes = width * 3
    padding = (-row_bytes) % 4
    pixel_bytes = (row_bytes + padding) * height
    file_size = 14 + 40 + pixel_bytes
    header = struct.pack(
        "<2sIHHI",
        b"BM", file_size, 0, 0, 54,
    )
    dib = struct.pack(
        "<IiiHHIIiiII",
        40, width, -height, 1, 24, 0, pixel_bytes, 0, 0, 0, 0,
    )
    bgr = array[:, :, ::-1]
    pad = b"\x00" * padding
    target = Path(path)
    with open(target, "xb") as stream:
        stream.write(header)
        stream.write(dib)
        for row in bgr:
            stream.write(row.tobytes())
            if padding:
                stream.write(pad)
    return target


def probe_targets(start_frame: int, end_frame: int) -> list[int]:
    """A small probe schedule; captures are triggered by real playback."""
    start = _as_int("start_frame", start_frame)
    end = _as_int("end_frame", end_frame)
    if start > end:
        raise ValueError("start_frame must be <= end_frame")
    return sorted({start, min(start + 2, end), min(start + 30, end)})


def playback_targets(start_frame: int, end_frame: int) -> list[int]:
    """Targets for the playback probe, excluding the static start frame."""
    start = _as_int("start_frame", start_frame)
    end = _as_int("end_frame", end_frame)
    if start > end:
        raise ValueError("start_frame must be <= end_frame")
    return sorted({min(start + 3, end),
                   min(start + 30, end),
                   min(start + 60, end)})


def timing_summary(values: Sequence[float]) -> dict[str, object]:
    finite = [float(value) for value in values if value is not None]
    if not finite:
        return {"count": 0, "min": None, "mean": None, "max": None}
    return {
        "count": len(finite),
        "min": min(finite),
        "mean": sum(finite) / len(finite),
        "max": max(finite),
    }
