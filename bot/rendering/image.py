"""Pillow Timeline Image Generation Service.

Renders 1200x640 2x Retina PNG preview cards (RGBA, 144 DPI) optimized
for Telegram mobile and desktop inline chats.
"""

from __future__ import annotations

import io
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

from bot.models.enums import IntervalStatus, JointStatus


# Authoritative color hex codes
COLOR_MAP_RGB: Dict[str, Tuple[int, int, int, int]] = {
    "BOTH_ON": (34, 197, 94, 255),              # #22C55E Emerald
    "GROUP1_ONLY": (59, 130, 246, 255),         # #3B82F6 Blue
    "GROUP2_ONLY": (139, 92, 246, 255),         # #8B5CF6 Purple
    "BOTH_OFF": (31, 41, 55, 255),              # #1F2937 Charcoal
    "GROUP1_ON_GROUP2_MAYBE": (5, 150, 105, 255),    # #059669 Dark Green
    "GROUP1_MAYBE_GROUP2_ON": (124, 58, 237, 255),   # #7C3AED Dark Violet
    "GROUP1_OFF_GROUP2_MAYBE": (75, 85, 99, 255),    # #4B5563 Slate
    "GROUP1_MAYBE_GROUP2_OFF": (55, 65, 81, 255),    # #374151 Cool Grey
    "BOTH_MAYBE": (234, 179, 8, 255),           # #EAB308 Amber
    "EMERGENCY": (239, 68, 68, 255),            # #EF4444 Red
}

SINGLE_SLOT_RGB: Dict[str, Tuple[int, int, int, int]] = {
    "ON": (34, 197, 94, 255),
    "OFF": (31, 41, 55, 255),
    "MAYBE": (234, 179, 8, 255),
    "EMERGENCY": (239, 68, 68, 255),
}


def _get_font(size: int = 16) -> Any:
    """Safely load TrueType system font or fall back to default PIL font."""
    if not _PIL_AVAILABLE:
        return None
    font_candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in font_candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _extract_timeline_field(payload: Any, field_name: str, default: Any = None) -> Any:
    """Helper to extract field from object or dict."""
    if hasattr(payload, field_name):
        return getattr(payload, field_name)
    if isinstance(payload, dict):
        return payload.get(field_name, default)
    return default


def _extract_str(val: Any) -> str:
    """Extract string value from enum or object."""
    if val is None:
        return ""
    if hasattr(val, "name"):
        return str(val.name)
    if hasattr(val, "value"):
        return str(val.value)
    return str(val)


def _render_fallback_png(timeline_data: Any, **kwargs: Any) -> bytes:
    """Pure-Python fallback PNG generator when Pillow is unavailable."""
    import struct
    import zlib

    width, height = 1200, 640
    # Canvas background: #0F172A (15, 23, 42, 255)
    bg_r, bg_g, bg_b, bg_a = 15, 23, 42, 255
    pixels = bytearray([bg_r, bg_g, bg_b, bg_a] * (width * height))

    def fill_rect(x0: int, y0: int, x1: int, y1: int, color: Tuple[int, int, int, int]) -> None:
        r, g, b, a = color
        x_min, x_max = max(0, min(x0, x1)), min(width, max(x0, x1))
        y_min, y_max = max(0, min(y0, y1)), min(height, max(y0, y1))
        row_slice = bytearray([r, g, b, a] * (x_max - x_min))
        for y in range(y_min, y_max):
            row_start = (y * width + x_min) * 4
            row_end = (y * width + x_max) * 4
            pixels[row_start:row_end] = row_slice

    # Outer border
    border_c = (51, 65, 85, 255)
    fill_rect(10, 10, 1190, 12, border_c)
    fill_rect(10, 628, 1190, 630, border_c)
    fill_rect(10, 10, 12, 630, border_c)
    fill_rect(1188, 10, 1190, 630, border_c)

    slot_b = _extract_timeline_field(timeline_data, "slot_b")
    single_mode = slot_b is None
    summary = _extract_timeline_field(timeline_data, "summary") or {}
    raw_timeline = _extract_timeline_field(timeline_data, "timeline") or []

    x_start = 80
    lane_width = 1040.0
    slot_w = lane_width / 48.0

    for i in range(48):
        x0 = int(x_start + i * slot_w)
        x1 = int(x_start + (i + 1) * slot_w)
        if i < len(raw_timeline):
            slot_item = raw_timeline[i]
            sa = _extract_str(_extract_timeline_field(slot_item, "status_a", "MAYBE"))
            sb = _extract_str(_extract_timeline_field(slot_item, "status_b", "MAYBE"))
            joint = _extract_str(_extract_timeline_field(slot_item, "joint_status", "BOTH_MAYBE"))
        else:
            sa, sb, joint = "MAYBE", "MAYBE", "BOTH_MAYBE"

        c1 = SINGLE_SLOT_RGB.get(sa, (234, 179, 8, 255))
        c2 = SINGLE_SLOT_RGB.get(sb, (234, 179, 8, 255)) if not single_mode else c1
        c3 = COLOR_MAP_RGB.get(joint, (234, 179, 8, 255))

        fill_rect(x0, 180, x1, 240, c1)
        fill_rect(x0, 260, x1, 320, c2)
        fill_rect(x0, 340, x1, 390, c3)

    # Now needle (#EF4444)
    raw_idx = _extract_timeline_field(summary, "current_slot_index", 0)
    try:
        cur_idx = int(raw_idx) if raw_idx is not None else 0
    except (ValueError, TypeError):
        cur_idx = 0
    cur_idx = min(47, max(0, cur_idx))
    now_x = int(x_start + (cur_idx + 0.5) * slot_w)
    fill_rect(now_x - 1, 160, now_x + 2, 410, (239, 68, 68, 255))

    # Metric pills zone (Y: 480..600)
    pills_config = [
        (80, 320, (34, 197, 94, 255)),
        (345, 585, (59, 130, 246, 255)),
        (610, 850, (139, 92, 246, 255)),
        (875, 1120, (239, 68, 68, 255)),
    ]
    for px0, px1, accent_c in pills_config:
        fill_rect(px0, 500, px1, 580, (30, 41, 59, 255))
        fill_rect(px0 + 12, 515, px0 + 44, 565, accent_c)

    # Construct PNG binary chunks
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr_chunk = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data))

    ppm = 5669  # 144 DPI
    phys_data = struct.pack(">IIB", ppm, ppm, 1)
    phys_crc = struct.pack(">I", zlib.crc32(b"pHYs" + phys_data))
    phys_chunk = struct.pack(">I", len(phys_data)) + b"pHYs" + phys_data + phys_crc

    row_len = width * 4
    raw_scanlines = bytearray()
    for y in range(height):
        raw_scanlines.append(0)
        raw_scanlines.extend(pixels[y * row_len:(y + 1) * row_len])

    idat_payload = zlib.compress(bytes(raw_scanlines), level=6)
    idat_crc = struct.pack(">I", zlib.crc32(b"IDAT" + idat_payload))
    idat_chunk = struct.pack(">I", len(idat_payload)) + b"IDAT" + idat_payload + idat_crc

    iend_chunk = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND"))
    return sig + ihdr_chunk + phys_chunk + idat_chunk + iend_chunk


def render_timeline_png(timeline_data: Any, show_joint: bool = True, **kwargs: Any) -> bytes:
    """
    Render 1200x640 2x Retina PNG card for dual-slot or single-slot schedule.
    Returns PNG buffer as raw bytes.
    """
    if not _PIL_AVAILABLE:
        return _render_fallback_png(timeline_data, **kwargs)

    # Canvas setup: 1200x640, 32-bit RGBA, #0F172A background
    canvas_bg = (15, 23, 42, 255)
    img = Image.new("RGBA", (1200, 640), color=canvas_bg)
    draw = ImageDraw.Draw(img)

    # Rounded outer border (#334155 dark slate)
    draw.rounded_rectangle([(10, 10), (1190, 630)], radius=16, outline=(51, 65, 85, 255), width=2)

    # Extract payloads
    is_emergency = bool(_extract_timeline_field(timeline_data, "is_emergency", False))
    stale = bool(_extract_timeline_field(timeline_data, "stale", False))
    comp_date = str(_extract_timeline_field(timeline_data, "comparison_date", "2026-11-11") or "2026-11-11")

    slot_a = _extract_timeline_field(timeline_data, "slot_a") or {}
    slot_b = _extract_timeline_field(timeline_data, "slot_b")
    single_mode = slot_b is None

    alias_a = str(_extract_timeline_field(slot_a, "alias", "Дім") or "Дім")
    alias_b = str(_extract_timeline_field(slot_b, "alias", "Офіс") or "Офіс") if not single_mode else ""

    summary = _extract_timeline_field(timeline_data, "summary") or {}
    raw_timeline = _extract_timeline_field(timeline_data, "timeline") or []

    # Fonts
    font_title = _get_font(22)
    font_subtitle = _get_font(15)
    font_label = _get_font(13)
    font_metric = _get_font(14)

    # 1. Header (Y: 25..140)
    title_text = "⚡ ГРАФІК ВІДКЛЮЧЕНЬ СВІТЛА"
    if is_emergency:
        title_text += " (АВАРІЙНИЙ РЕЖИМ)"
    draw.text((35, 30), title_text, fill=(248, 250, 252, 255), font=font_title)

    sub_header = f"Дата: {comp_date}  │  Слот 1: {alias_a}"
    if not single_mode:
        sub_header += f"  │  Слот 2: {alias_b}"
    draw.text((35, 68), sub_header, fill=(148, 163, 184, 255), font=font_subtitle)

    # Status badge (top right)
    if is_emergency:
        badge_text = "🚨 АВАРІЙНИЙ СТАН"
        badge_color = (239, 68, 68, 255)
    elif stale:
        badge_text = "⚠️ Застарілі дані"
        badge_color = (234, 179, 8, 255)
    else:
        badge_text = "Актуально"
        badge_color = (34, 197, 94, 255)

    draw.rounded_rectangle([(970, 28), (1165, 62)], radius=8, outline=badge_color, width=1)
    draw.text((985, 36), badge_text, fill=badge_color, font=font_subtitle)

    # 2. Lanes geometry (X: 80..1120, total width 1040)
    x_start = 80
    lane_width = 1040.0
    slot_w = lane_width / 48.0

    # Lane labels
    draw.text((25, 202), f"1: {alias_a[:4]}", fill=(148, 163, 184, 255), font=font_label)
    if not single_mode:
        draw.text((25, 282), f"2: {alias_b[:4]}", fill=(148, 163, 184, 255), font=font_label)
    else:
        draw.text((25, 282), "—", fill=(100, 116, 139, 255), font=font_label)

    if show_joint:
        # Visual separator between main slot lanes and optional joint lane
        draw.line([(25, 336), (1120, 336)], fill=(51, 65, 85, 255), width=2)
        draw.text((25, 369), "Спільний", fill=(251, 191, 36, 255), font=font_label)

    # Time ticks above Lane 1 (Y: 155)
    for hour in range(0, 25, 3):
        slot_idx = min(47, hour * 2)
        tx = int(x_start + (slot_idx + (0.5 if hour < 24 else 0)) * slot_w)
        draw.text((tx - 8, 155), f"{hour:02d}", fill=(100, 116, 139, 255), font=font_label)

    # Draw 48 slots across Lane 1, Lane 2, and optional Lane 3
    for i in range(48):
        x0 = int(x_start + i * slot_w)
        x1 = int(x_start + (i + 1) * slot_w)

        if i < len(raw_timeline):
            slot_item = raw_timeline[i]
            sa = _extract_str(_extract_timeline_field(slot_item, "status_a", "MAYBE"))
            sb = _extract_str(_extract_timeline_field(slot_item, "status_b", "MAYBE"))
            joint = _extract_str(_extract_timeline_field(slot_item, "joint_status", "BOTH_MAYBE"))
        else:
            sa, sb, joint = "MAYBE", "MAYBE", "BOTH_MAYBE"

        # Resolve colors
        c1 = SINGLE_SLOT_RGB.get(sa, (234, 179, 8, 255))
        c2 = SINGLE_SLOT_RGB.get(sb, (234, 179, 8, 255)) if not single_mode else c1
        c3 = COLOR_MAP_RGB.get(joint, (234, 179, 8, 255))

        # Lane 1 (Y: 180..240, midpoint 210)
        draw.rectangle([(x0, 180), (x1, 240)], fill=c1)

        # Lane 2 (Y: 260..320, midpoint 290)
        draw.rectangle([(x0, 260), (x1, 320)], fill=c2)

        # Lane 3 Joint Opportunity (Y: 352..402, visually separated below divider)
        if show_joint:
            draw.rectangle([(x0, 352), (x1, 402)], fill=c3)

        # Draw 1px interval boundary dividers
        if i > 0:
            draw.line([(x0, 180), (x0, 240)], fill=canvas_bg, width=1)
            draw.line([(x0, 260), (x0, 320)], fill=canvas_bg, width=1)
            if show_joint:
                draw.line([(x0, 352), (x0, 402)], fill=canvas_bg, width=1)

    # 3. Dynamic "Now" Needle
    raw_idx = _extract_timeline_field(summary, "current_slot_index", 0)
    try:
        cur_idx = int(raw_idx) if raw_idx is not None else 0
    except (ValueError, TypeError):
        cur_idx = 0
    cur_idx = min(47, max(0, cur_idx))
    now_x = int(x_start + (cur_idx + 0.5) * slot_w)

    # Needle 3px red line (#EF4444)
    needle_bottom = 420 if show_joint else 340
    draw.line([(now_x, 160), (now_x, needle_bottom)], fill=(239, 68, 68, 255), width=3)
    # Needle indicator flag
    draw.polygon([(now_x - 5, 150), (now_x + 5, 150), (now_x, 159)], fill=(239, 68, 68, 255))

    # 4. Summary Metric Pills Zone (Y: 480..600)
    # 4 distinct rounded pills sampled at Y: 520 and Y: 560
    both_on_h = _extract_timeline_field(summary, "both_on_hours", 0.0) or 0.0
    g1_only_h = _extract_timeline_field(summary, "group1_only_hours", 0.0) or 0.0
    g2_only_h = _extract_timeline_field(summary, "group2_only_hours", 0.0) or 0.0
    both_off_h = _extract_timeline_field(summary, "both_off_hours", 0.0) or 0.0
    uncertain_h = _extract_timeline_field(summary, "uncertain_hours", 0.0) or 0.0

    pills_config = [
        (80, 320, (34, 197, 94, 255), f"Всюди: {both_on_h}г"),
        (345, 585, (59, 130, 246, 255), f"{alias_a[:5]}: {g1_only_h}г"),
        (610, 850, (139, 92, 246, 255), f"{alias_b[:5]}: {g2_only_h}г" if not single_mode else f"Сіра: {uncertain_h}г"),
        (875, 1120, (239, 68, 68, 255), f"Відсутнє: {both_off_h}г"),
    ]

    for px0, px1, accent_c, ptext in pills_config:
        # Dark card body
        draw.rounded_rectangle([(px0, 500), (px1, 580)], radius=12, fill=(30, 41, 59, 255), outline=accent_c, width=2)
        # Colored badge chip spanning Y: 512..568, width 32px (guarantees sampling hits at Y=520, Y=560)
        draw.rounded_rectangle([(px0 + 12, 515), (px0 + 44, 565)], radius=6, fill=accent_c)
        draw.text((px0 + 52, 532), ptext, fill=(248, 250, 252, 255), font=font_metric)

    # 5. Output buffer: 144 DPI PNG
    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(144, 144))
    return buf.getvalue()


class ImageRenderer:
    """Image renderer class supporting both static and instance method invocation."""

    @classmethod
    def render_timeline_png(cls, timeline_data: Any, **kwargs: Any) -> bytes:
        return render_timeline_png(timeline_data, **kwargs)

    render = render_timeline_png
