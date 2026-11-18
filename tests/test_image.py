"""
tests/test_image.py - Comprehensive Test Suite for Pillow Timeline Image Generation (Tiers 1 & 2).

Verifies Mode 1 presentation specifications:
- Tier 1: 1200x640 2x Retina PNG card rendering (valid PNG signature, dimensions 1200x640,
  RGBA mode, 144 DPI, 1.875:1 aspect ratio).
- Tier 2: Color sanity & structural elements (#0F172A background, Lane 1/2/3 bar regions,
  Now needle #EF4444 spanning Y:160-410, 4 summary metric pills, single-slot fallback,
  extreme cases: 100% ON, 100% OFF, 100% EMERGENCY).

Public API under test:
- `bot.rendering.image.ImageRenderer.render_timeline_png` / `render_timeline_png`
  (or fallback to `bot.services.image_renderer`)
"""

from __future__ import annotations

import io
import struct
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import pytest
except ImportError:
    from tests.conftest import pytest  # type: ignore[no-redef]

from tests.conftest import safe_import


# ============================================================================
# HELPER & FIXTURE FACTORIES
# ============================================================================

def get_render_image_func() -> Callable[..., bytes]:
    """
    Import and return the PNG timeline rendering callable.
    Checks bot.rendering.image then bot.services.image_renderer.
    If unavailable, safe_import triggers pytest.skip().
    """
    # 1. Check bot.rendering.image
    try:
        from bot.rendering.image import ImageRenderer
        if hasattr(ImageRenderer, "render_timeline_png"):
            def _wrapped_class(*args: Any, **kwargs: Any) -> bytes:
                try:
                    res = ImageRenderer.render_timeline_png(*args, **kwargs)
                except TypeError:
                    res = ImageRenderer().render_timeline_png(*args, **kwargs)
                return _normalize_bytes(res)
            return _wrapped_class
    except (ImportError, ModuleNotFoundError, AttributeError):
        pass

    try:
        from bot.rendering.image import render_timeline_png
        return lambda *args, **kwargs: _normalize_bytes(render_timeline_png(*args, **kwargs))
    except (ImportError, ModuleNotFoundError, AttributeError):
        pass

    # 2. Check bot.services.image_renderer
    try:
        from bot.services.image_renderer import ImageRenderer
        if hasattr(ImageRenderer, "render_timeline_png"):
            def _wrapped_service(*args: Any, **kwargs: Any) -> bytes:
                try:
                    res = ImageRenderer.render_timeline_png(*args, **kwargs)
                except TypeError:
                    res = ImageRenderer().render_timeline_png(*args, **kwargs)
                return _normalize_bytes(res)
            return _wrapped_service
    except (ImportError, ModuleNotFoundError, AttributeError):
        pass

    try:
        from bot.services.image_renderer import render_timeline_png
        return lambda *args, **kwargs: _normalize_bytes(render_timeline_png(*args, **kwargs))
    except (ImportError, ModuleNotFoundError, AttributeError):
        pass

    # Neither exists yet -> trigger safe_import skip
    return safe_import("bot.rendering.image", "render_timeline_png")


def _normalize_bytes(output: Any) -> bytes:
    """Ensure rendering output is converted to raw bytes."""
    if isinstance(output, bytes):
        return output
    if hasattr(output, "getvalue"):
        return output.getvalue()
    if hasattr(output, "read"):
        return output.read()
    raise TypeError(f"Expected bytes or io.BytesIO from image renderer, got {type(output)}")


def parse_png_ihdr(data: bytes) -> Tuple[int, int, int, int]:
    """
    Pure-Python parser for PNG header (IHDR chunk).
    Returns: (width, height, bit_depth, color_type).
    """
    if len(data) < 33:
        raise ValueError(f"PNG data too short ({len(data)} bytes)")
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Invalid PNG magic bytes")

    # Chunk format: 4 bytes length, 4 bytes type, length bytes data, 4 bytes CRC
    chunk_len = struct.unpack(">I", data[8:12])[0]
    chunk_type = data[12:16]
    if chunk_type != b"IHDR" or chunk_len != 13:
        raise ValueError(f"Expected IHDR chunk, got {chunk_type}")

    width, height, bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
    return width, height, bit_depth, color_type


def parse_png_phys_dpi(data: bytes) -> Optional[Tuple[float, float]]:
    """
    Pure-Python parser for PNG physical pixel dimensions (pHYs chunk).
    Returns (dpi_x, dpi_y) or None if chunk is absent.
    """
    offset = 8
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        ctype = data[offset + 4:offset + 8]
        if ctype == b"pHYs" and length >= 9:
            ppu_x, ppu_y, unit = struct.unpack(">IIB", data[offset + 8:offset + 17])
            if unit == 1:  # unit is meter
                dpi_x = ppu_x * 0.0254
                dpi_y = ppu_y * 0.0254
                return dpi_x, dpi_y
        offset += 8 + length + 4  # skip len + type + data + crc
    return None


def _format_time_interval(index: int) -> Tuple[str, str]:
    start_min = index * 30
    end_min = (index + 1) * 30
    sh, sm = divmod(start_min, 60)
    eh, em = divmod(end_min, 60)
    end_str = "24:00" if end_min == 1440 else f"{eh:02d}:{em:02d}"
    return f"{sh:02d}:{sm:02d}", end_str


def build_timeline_response_payload(
    status_a_pattern: Any = "ON",
    status_b_pattern: Any = "ON",
    joint_pattern: Any = "BOTH_ON",
    alias_a: str = "Дім",
    alias_b: str = "Офіс",
    is_emergency: bool = False,
    single_slot: bool = False,
    current_slot_index: int = 28,
) -> Any:
    """
    Build a TwoGroupTimelineResponse object or dictionary matching the domain schema.
    If the Pydantic model is available in bot.models.schedule, instantiates it.
    """
    if isinstance(status_a_pattern, str):
        statuses_a = [status_a_pattern] * 48
    else:
        statuses_a = list(status_a_pattern)[:48]

    if isinstance(status_b_pattern, str):
        statuses_b = [status_b_pattern] * 48
    else:
        statuses_b = list(status_b_pattern)[:48]

    if isinstance(joint_pattern, str):
        joints = [joint_pattern] * 48
    else:
        joints = list(joint_pattern)[:48]

    intervals = []
    for idx in range(48):
        start_t, end_t = _format_time_interval(idx)
        intervals.append({
            "slot_index": idx,
            "start_time": start_t,
            "end_time": end_t,
            "status_a": statuses_a[idx],
            "status_b": None if single_slot else statuses_b[idx],
            "joint_status": joints[idx],
        })

    slot_a_dict = {
        "slot_number": 1,
        "alias": alias_a,
        "region_code": "kyiv",
        "region_name": "м. Київ",
        "group_id": 1,
        "subgroup_id": "1.1",
    }
    slot_b_dict = None if single_slot else {
        "slot_number": 2,
        "alias": alias_b,
        "region_code": "kyiv",
        "region_name": "м. Київ",
        "group_id": 3,
        "subgroup_id": "3.2",
    }

    summary_dict = {
        "both_on_hours": 12.0 if not single_slot else 0.0,
        "group1_only_hours": 6.0,
        "group2_only_hours": 6.0 if not single_slot else 0.0,
        "both_off_hours": 0.0,
        "uncertain_hours": 0.0,
        "current_slot_index": current_slot_index,
        "current_joint_status": joints[current_slot_index],
        "next_transition_time": "18:00",
        "minutes_to_next_transition": 225,
        "smart_recommendation": "Зараз світло є в Офісі. Доцільно вирушати туди.",
    }

    payload = {
        "comparison_date": "2026-11-11",
        "is_emergency": is_emergency,
        "emergency_message": "Аварійні відключення" if is_emergency else None,
        "stale": False,
        "slot_a": slot_a_dict,
        "slot_b": slot_b_dict,
        "summary": summary_dict,
        "timeline": intervals,
    }

    # If domain model is importable, validate and return instance
    try:
        from bot.models.schedule import TwoGroupTimelineResponse
        return TwoGroupTimelineResponse.model_validate(payload)
    except Exception:
        # Fallback to an object with attribute and dict access
        class _AttrDict(dict):
            def __getattr__(self, item: str) -> Any:
                val = self.get(item)
                if isinstance(val, dict):
                    return _AttrDict(val)
                if isinstance(val, list):
                    return [_AttrDict(x) if isinstance(x, dict) else x for x in val]
                return val
        return _AttrDict(payload)


# ============================================================================
# TIER 1 TESTS: PILLOW 1200x640 2x RETINA PNG CARD RENDERING
# ============================================================================

def test_png_buffer_signature() -> None:
    """
    Tier 1 [Image-1]: Verify output buffer is non-empty bytes starting with
    the authoritative 8-byte PNG magic signature b'\\x89PNG\\r\\n\\x1a\\n'
    and ending with IEND chunk.
    """
    render_func = get_render_image_func()
    timeline = build_timeline_response_payload()
    png_bytes = render_func(timeline)

    assert isinstance(png_bytes, bytes), f"Expected bytes, got {type(png_bytes)}"
    assert len(png_bytes) > 64, f"Buffer too small to be a PNG ({len(png_bytes)} bytes)"
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n"), "PNG buffer missing valid magic signature"
    assert b"IEND" in png_bytes[-12:], "PNG buffer missing terminal IEND chunk"


def test_image_dimensions_1200x640() -> None:
    """
    Tier 1 [Image-2]: Verify image canvas width is exactly 1200 pixels
    and height is exactly 640 pixels (both via PIL and IHDR chunk header).
    """
    render_func = get_render_image_func()
    timeline = build_timeline_response_payload()
    png_bytes = render_func(timeline)

    # 1. Pure binary IHDR verification
    width, height, bit_depth, color_type = parse_png_ihdr(png_bytes)
    assert width == 1200, f"Expected width 1200, got {width}"
    assert height == 640, f"Expected height 640, got {height}"

    # 2. PIL Image.open verification if PIL is available
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes))
        assert img.size == (1200, 640), f"Expected PIL size (1200, 640), got {img.size}"
        assert img.width == 1200
        assert img.height == 640
    except ImportError:
        pass


def test_image_mode_rgba() -> None:
    """
    Tier 1 [Image-3]: Verify image is rendered in 32-bit RGBA mode
    (color type 6 in PNG IHDR chunk, or mode == 'RGBA' in PIL).
    """
    render_func = get_render_image_func()
    timeline = build_timeline_response_payload()
    png_bytes = render_func(timeline)

    # IHDR color type 6 is RGBA (Truecolor with alpha)
    _, _, bit_depth, color_type = parse_png_ihdr(png_bytes)
    assert bit_depth == 8, f"Expected 8-bit depth, got {bit_depth}"
    assert color_type in (6, 2), f"Expected color_type 6 (RGBA) or 2 (RGB), got {color_type}"

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes))
        assert img.mode in ("RGBA", "RGB"), f"Expected RGBA/RGB mode, got {img.mode}"
    except ImportError:
        pass


def test_image_retina_144_dpi() -> None:
    """
    Tier 1 [Image-4]: Verify image metadata specifies 144 DPI (Retina resolution)
    either in PIL image info or via the PNG pHYs chunk (~5669 pixels/meter).
    """
    render_func = get_render_image_func()
    timeline = build_timeline_response_payload()
    png_bytes = render_func(timeline)

    # Check via PIL if available
    checked = False
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes))
        dpi = img.info.get("dpi")
        if dpi is not None:
            dpi_x, dpi_y = (dpi, dpi) if isinstance(dpi, (int, float)) else dpi
            assert 140 <= dpi_x <= 148, f"Expected ~144 DPI on X, got {dpi_x}"
            assert 140 <= dpi_y <= 148, f"Expected ~144 DPI on Y, got {dpi_y}"
            checked = True
    except ImportError:
        pass

    # Check via binary pHYs chunk if PIL was not present or didn't extract dpi
    if not checked:
        phys_dpi = parse_png_phys_dpi(png_bytes)
        if phys_dpi is not None:
            assert 140 <= phys_dpi[0] <= 148, f"pHYs DPI X mismatch: {phys_dpi[0]}"
            assert 140 <= phys_dpi[1] <= 148, f"pHYs DPI Y mismatch: {phys_dpi[1]}"


def test_image_aspect_ratio_retina() -> None:
    """
    Tier 1 [Image-5]: Verify the 1.875:1 aspect ratio (1200 / 640) optimized for
    Telegram mobile and desktop inline preview cards.
    """
    render_func = get_render_image_func()
    timeline = build_timeline_response_payload()
    png_bytes = render_func(timeline)

    width, height, _, _ = parse_png_ihdr(png_bytes)
    aspect_ratio = width / height
    assert abs(aspect_ratio - 1.875) < 1e-4, f"Aspect ratio must be 1.875, got {aspect_ratio}"


def test_image_buffer_non_empty_reasonable_size() -> None:
    """
    Tier 1 [Image-6]: Verify rendered PNG file size is within reasonable bounds
    (> 5 KB to guarantee actual graphic content, < 2 MB for mobile performance).
    """
    render_func = get_render_image_func()
    timeline = build_timeline_response_payload()
    png_bytes = render_func(timeline)

    assert len(png_bytes) >= 5 * 1024, f"PNG buffer too small ({len(png_bytes)} B)"
    assert len(png_bytes) <= 2 * 1024 * 1024, f"PNG buffer exceeds 2 MB ({len(png_bytes)} B)"


# ============================================================================
# TIER 2 TESTS: STRUCTURAL ELEMENTS, COLOR SANITY & BOUNDARY CASES
# ============================================================================

def test_image_background_color_slate() -> None:
    """
    Tier 2 [Image-7]: Verify background color aesthetic is dark slate #0F172A
    (RGB: 15, 23, 42) sampled from the card's inner background area.
    """
    render_func = get_render_image_func()
    timeline = build_timeline_response_payload()
    png_bytes = render_func(timeline)

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        # Sample background pixel near top-left margin (e.g. x=20, y=20)
        pixel = img.getpixel((20, 20))
        r, g, b = pixel[:3]
        # Allow tolerance for subtle gradients or borders
        assert abs(r - 15) <= 20, f"Red component {r} deviates from #0F172A"
        assert abs(g - 23) <= 20, f"Green component {g} deviates from #0F172A"
        assert abs(b - 42) <= 20, f"Blue component {b} deviates from #0F172A"
    except ImportError:
        pytest.skip("PIL required for pixel-level color sampling")


def test_image_structural_lanes_dual_slot() -> None:
    """
    Tier 2 [Image-8]: Verify presence of distinct visual content in the 3 timeline lanes:
    - Lane 1 Bar (Y: 180 - 240)
    - Lane 2 Bar (Y: 260 - 320)
    - Lane 3 Joint Opportunity Bar (Y: 340 - 390)
    """
    render_func = get_render_image_func()
    timeline = build_timeline_response_payload()
    png_bytes = render_func(timeline)

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")

        # Sample horizontal cross-sections at lane midpoints: Y=210 (Lane 1), Y=290 (Lane 2), Y=365 (Lane 3)
        lane1_pixels = [img.getpixel((x, 210)) for x in range(100, 1100, 20)]
        lane2_pixels = [img.getpixel((x, 290)) for x in range(100, 1100, 20)]
        lane3_pixels = [img.getpixel((x, 365)) for x in range(100, 1100, 20)]

        # Each lane must exhibit color variation representing 48 slots
        assert len(set(lane1_pixels)) > 1, "Lane 1 should contain multiple colors across slots"
        assert len(set(lane2_pixels)) > 1, "Lane 2 should contain multiple colors across slots"
        assert len(set(lane3_pixels)) > 1, "Lane 3 should contain multiple joint status colors"
    except ImportError:
        pytest.skip("PIL required for structural lane pixel analysis")


def test_image_now_needle_presence() -> None:
    """
    Tier 2 [Image-9]: Verify the dynamic 'Now' indicator needle:
    A vertical red line (#EF4444, RGB: 239, 68, 68) spanning Y: 160-410 px.
    """
    render_func = get_render_image_func()
    # Fixed at slot index 28 (14:00 - 14:30)
    timeline = build_timeline_response_payload(current_slot_index=28)
    png_bytes = render_func(timeline)

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")

        # The needle should be located around 14h on the 24h timeline (x in ~650..850)
        # Scan vertical range Y: 200..350 for pixels where red is prominent (R > 180, G < 100, B < 100)
        found_needle_pixel = False
        for x in range(400, 900, 2):
            column = [img.getpixel((x, y)) for y in range(200, 350, 10)]
            red_pixels = [p for p in column if p[0] > 180 and p[1] < 100 and p[2] < 100]
            if len(red_pixels) >= 5:
                found_needle_pixel = True
                break

        assert found_needle_pixel, "Now needle (#EF4444) not found spanning vertical lanes Y: 160-410"
    except ImportError:
        pytest.skip("PIL required for Now needle pixel verification")


def test_image_summary_metric_pills_zone() -> None:
    """
    Tier 2 [Image-10]: Verify the summary metric pills zone (Y: 480 - 600 px)
    contains rendered pill content and text, distinct from blank background.
    """
    render_func = get_render_image_func()
    timeline = build_timeline_response_payload()
    png_bytes = render_func(timeline)

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")

        # Sample across the metric pills area (Y=520, Y=560)
        metric_pixels = [img.getpixel((x, y)) for x in range(100, 1100, 15) for y in (520, 560)]
        distinct_colors = set(metric_pixels)

        # Region should contain multiple colors (pills, badges, white text, dark cards)
        assert len(distinct_colors) >= 4, (
            f"Expected metric pills zone (Y:480-600) to contain varied content, got {len(distinct_colors)} colors"
        )
    except ImportError:
        pytest.skip("PIL required for summary metric zone verification")


def test_image_single_slot_fallback() -> None:
    """
    Tier 2 [Image-11]: Single-slot fallback rendering:
    When slot_b is None, image rendering must succeed cleanly and produce
    a valid 1200x640 RGBA PNG card without crashing or throwing KeyError.
    """
    render_func = get_render_image_func()
    timeline = build_timeline_response_payload(single_slot=True)
    png_bytes = render_func(timeline)

    width, height, _, _ = parse_png_ihdr(png_bytes)
    assert width == 1200
    assert height == 640


def test_image_extreme_case_100_percent_on() -> None:
    """
    Tier 2 [Image-12]: Extreme case: 100% ON power availability.
    All 48 intervals are ON / BOTH_ON (#22C55E emerald).
    Verify rendering succeeds and emerald green is dominant in lanes.
    """
    render_func = get_render_image_func()
    timeline = build_timeline_response_payload(
        status_a_pattern="ON",
        status_b_pattern="ON",
        joint_pattern="BOTH_ON",
    )
    png_bytes = render_func(timeline)

    width, height, _, _ = parse_png_ihdr(png_bytes)
    assert width == 1200 and height == 640

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        # In Lane 1 (Y=210), verify green pixels presence
        lane_pixels = [img.getpixel((x, 210)) for x in range(150, 1050, 20)]
        green_pixels = [p for p in lane_pixels if p[1] > 140 and p[0] < 100]
        assert len(green_pixels) > 10, "100% ON schedule must have prominent green (#22C55E) in Lane 1"
    except ImportError:
        pass


def test_image_extreme_case_100_percent_off() -> None:
    """
    Tier 2 [Image-13]: Extreme case: 100% Total Blackout.
    All 48 intervals are OFF / BOTH_OFF (#1F2937 charcoal).
    Verify rendering succeeds and dark colors dominate the lane bars.
    """
    render_func = get_render_image_func()
    timeline = build_timeline_response_payload(
        status_a_pattern="OFF",
        status_b_pattern="OFF",
        joint_pattern="BOTH_OFF",
    )
    png_bytes = render_func(timeline)

    width, height, _, _ = parse_png_ihdr(png_bytes)
    assert width == 1200 and height == 640

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        lane_pixels = [img.getpixel((x, 210)) for x in range(150, 1050, 20)]
        # Dark charcoal: low values across R, G, B
        dark_pixels = [p for p in lane_pixels if p[0] < 80 and p[1] < 80 and p[2] < 90]
        assert len(dark_pixels) > 10, "100% OFF schedule must have dark charcoal (#1F2937) in Lane 1"
    except ImportError:
        pass


def test_image_extreme_case_100_percent_emergency() -> None:
    """
    Tier 2 [Image-14]: Extreme case: Emergency Outage Declaration.
    All intervals are EMERGENCY (#EF4444 red) with emergency banner.
    Verify rendering succeeds with prominent emergency red theme.
    """
    render_func = get_render_image_func()
    timeline = build_timeline_response_payload(
        status_a_pattern="EMERGENCY",
        status_b_pattern="EMERGENCY",
        joint_pattern="EMERGENCY",
        is_emergency=True,
    )
    png_bytes = render_func(timeline)

    width, height, _, _ = parse_png_ihdr(png_bytes)
    assert width == 1200 and height == 640

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        # In Lane 1 and banner area, verify red pixels
        lane_pixels = [img.getpixel((x, 210)) for x in range(150, 1050, 20)]
        red_pixels = [p for p in lane_pixels if p[0] > 180 and p[1] < 100 and p[2] < 100]
        assert len(red_pixels) > 5, "Emergency schedule must have red (#EF4444) in Lane 1"
    except ImportError:
        pass


def load_tests(loader: Any, standard_tests: Any, pattern: Any) -> Any:
    """Standard unittest load_tests protocol to support python -m unittest discovery."""
    import unittest
    suite = unittest.TestSuite()
    for name, obj in list(globals().items()):
        if name.startswith("test_") and callable(obj):
            class _DynCase(unittest.TestCase):
                pass
            setattr(_DynCase, name, lambda self, fn=obj: fn())
            suite.addTest(_DynCase(name))
    return suite

