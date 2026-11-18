"""Monospace Mobile Unicode Table Renderer.

Renders mobile-optimized Unicode comparison tables formatted strictly to
<= 28 characters per line for zero line wrap across iOS, Android, and Desktop
Telegram clients.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _extract_field(payload: Any, field_name: str, default: Any = None) -> Any:
    """Helper to extract field from object or dict."""
    if hasattr(payload, field_name):
        return getattr(payload, field_name)
    if isinstance(payload, dict):
        return payload.get(field_name, default)
    return default


def _extract_str(val: Any) -> str:
    """Extract string value from enum, object, or string."""
    if val is None:
        return ""
    if hasattr(val, "value"):
        return str(val.value)
    if hasattr(val, "name"):
        return str(val.name)
    return str(val)


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Safely convert value to float without raising exceptions."""
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


EMOJI_SLOT_MAP: Dict[str, str] = {
    "ON": "🟩",
    "OFF": "⬛",
    "MAYBE": "🟨",
    "EMERGENCY": "🚨",
}


def _get_joint_badge(
    joint_str: str,
    alias_a: str,
    alias_b: str,
    single_mode: bool,
    alias_limit: int = 4,
) -> str:
    """Get authoritative compact badge representation for table row and current status."""
    a_short = (alias_a[:alias_limit] if alias_a else "СЛ1")
    b_short = (alias_b[:alias_limit] if alias_b else "СЛ2")

    if single_mode:
        if joint_str == "ON":
            return "🟩Світло є"
        elif joint_str == "OFF":
            return "⬛Відключення"
        elif joint_str == "MAYBE":
            return "🟨Можливе"
        elif joint_str == "EMERGENCY":
            return "🚨Аварія"
        return f"⚪{joint_str[:6]}"

    badge_map: Dict[str, str] = {
        "BOTH_ON": "🟢Всюди",
        "GROUP1_ONLY": f"🔵{a_short}",
        "GROUP2_ONLY": f"🟣{b_short}",
        "BOTH_OFF": "Відключення" if alias_limit <= 4 else "⬛Відключення",
        "GROUP1_ON_GROUP2_MAYBE": "🟢Є/Мож",
        "GROUP1_MAYBE_GROUP2_ON": "🟣Мож/Є",
        "GROUP1_OFF_GROUP2_MAYBE": "⬛Ні/Мож",
        "GROUP1_MAYBE_GROUP2_OFF": "⬛Мож/Ні",
        "BOTH_MAYBE": "🟨Сіра",
        "EMERGENCY": "🚨Аварія",
    }
    return badge_map.get(joint_str, f"⚪{joint_str[:4]}")


def _uk_plural(n: int, form1: str, form2_4: str, form5: str) -> str:
    """Return Ukrainian noun form for integer n."""
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 19:
        return form5
    last = n_abs % 10
    if last == 1:
        return form1
    if 2 <= last <= 4:
        return form2_4
    return form5


def format_duration_uk(minutes: int) -> str:
    """Format duration in minutes as human-readable Ukrainian text (e.g. '1 година 8 хвилин')."""
    total = max(0, int(minutes))
    hours, mins = divmod(total, 60)
    parts: List[str] = []
    if hours > 0:
        h_word = _uk_plural(hours, "година", "години", "годин")
        parts.append(f"{hours} {h_word}")
    if mins > 0 or hours == 0:
        m_word = _uk_plural(mins, "хвилина", "хвилини", "хвилин")
        parts.append(f"{mins} {m_word}")
    return " ".join(parts)


def render_timeline_text(timeline_data: Any, show_joint: bool = True, **kwargs: Any) -> str:
    """
    Render mobile monospace Unicode table strictly <= 28 characters wide.
    Supports both dual-slot schedule comparison and single-slot fallback.
    """
    lines: List[str] = []

    # 1. Header extraction
    comp_date = _extract_str(_extract_field(timeline_data, "comparison_date", "11.11.2026")) or "11.11.2026"
    is_emergency = bool(_extract_field(timeline_data, "is_emergency", False))
    stale = bool(_extract_field(timeline_data, "stale", False))

    slot_a = _extract_field(timeline_data, "slot_a") or {}
    slot_b = _extract_field(timeline_data, "slot_b")
    single_mode = slot_b is None

    alias_a = str(_extract_field(slot_a, "alias", "Дім") or "Дім")
    alias_b = str(_extract_field(slot_b, "alias", "Офіс") or "Офіс") if not single_mode else ""
    subgrp_a = str(_extract_field(slot_a, "subgroup_id", "") or "")
    subgrp_b = str(_extract_field(slot_b, "subgroup_id", "") or "") if not single_mode else ""

    summary = _extract_field(timeline_data, "summary") or {}
    raw_timeline = _extract_field(timeline_data, "timeline") or []

    # Title with variation selector matching test_cross_feature contract
    updated_at = _extract_field(timeline_data, "updated_at")
    lines.append(f"⚡️ ДТЕК: {comp_date}")
    if updated_at:
        lines.append(f"🕒 Оновл.: {str(updated_at)[:17]}")
    lines.append(f"🏠 {subgrp_a or '1'}: {alias_a[:18]}")
    if not single_mode:
        lines.append(f"🏢 {subgrp_b or '2'}: {alias_b[:18]}")

    if is_emergency:
        lines.append("🚨 АВАРІЙНІ ВІДКЛЮЧЕННЯ")
    elif stale:
        lines.append("⚠️ Застарілі дані (кеш)")

    lines.append("")

    # 2. Table Column Headers
    if single_mode:
        lines.append("ЧАС         │ СТАН")
        lines.append("────────────┼─────")
    else:
        hdr_a = (subgrp_a[:3] if subgrp_a else alias_a[:3] or "СЛ1").center(3)
        hdr_b = (subgrp_b[:3] if subgrp_b else alias_b[:3] or "СЛ2").center(3)
        lines.append(f"ЧАС        │{hdr_a}│{hdr_b}│СТАН")
        lines.append("───────────┼───┼───┼─────")

    # 3. Contiguous Time Window Aggregation (30-min precision HH:MM-HH:MM)
    i = 0
    total_slots = len(raw_timeline)
    while i < total_slots:
        item_i = raw_timeline[i]
        joint_state = _extract_str(_extract_field(item_i, "joint_status", "BOTH_MAYBE"))
        sa = _extract_str(_extract_field(item_i, "status_a", "MAYBE"))
        sb = _extract_str(_extract_field(item_i, "status_b", "MAYBE")) if not single_mode else None

        raw_start = _extract_field(item_i, "start_time")
        chunk_start = str(raw_start)[:5] if raw_start else f"{i//2:02d}:{(i%2)*30:02d}"

        j = i
        while j < total_slots:
            item_j = raw_timeline[j]
            curr_joint = _extract_str(_extract_field(item_j, "joint_status", "BOTH_MAYBE"))
            curr_sa = _extract_str(_extract_field(item_j, "status_a", "MAYBE"))
            curr_sb = _extract_str(_extract_field(item_j, "status_b", "MAYBE")) if not single_mode else None

            # Group identical consecutive interval statuses
            if curr_joint != joint_state or curr_sa != sa or curr_sb != sb:
                break
            j += 1

        last_item = raw_timeline[j - 1]
        raw_end = _extract_field(last_item, "end_time")
        if j >= total_slots:
            chunk_end = "24:00"
        elif raw_end:
            chunk_end = str(raw_end)[:5]
        else:
            chunk_end = f"{j//2:02d}:{(j%2)*30:02d}"

        time_range = f"{chunk_start}-{chunk_end}"
        badge = _get_joint_badge(joint_state, alias_a, alias_b, single_mode)

        if single_mode:
            row = f"{time_range:11s} │ {badge}"
        else:
            ea = EMOJI_SLOT_MAP.get(sa, "⬜")
            eb = EMOJI_SLOT_MAP.get(sb, "⬜") if sb else "⬜"
            row = f"{time_range:11s}│{ea}│{eb}│{badge}"

        lines.append(row)
        i = j

    # 4. Summary Statistics Section
    lines.append("")
    lines.append("📊 Підсумок доби:")

    both_on_h = _extract_field(summary, "both_on_hours", 0.0)
    g1_only_h = _extract_field(summary, "group1_only_hours", 0.0)
    g2_only_h = _extract_field(summary, "group2_only_hours", 0.0)
    both_off_h = _extract_field(summary, "both_off_hours", 0.0)
    uncertain_h = _extract_field(summary, "uncertain_hours", 0.0)

    if single_mode:
        lines.append(f"• 🟢 Світло є: {g1_only_h} год")
        lines.append(f"• ⬛ Відключення: {both_off_h} год")
        if _safe_float(uncertain_h) > 0:
            lines.append(f"• 🟨 Сіра: {uncertain_h} год")
    else:
        if show_joint:
            lines.append(f"• 🟢 Всюди: {both_on_h} год")
            lines.append(f"• 🔵 {alias_a[:11]}: {g1_only_h} год")
            lines.append(f"• 🟣 {alias_b[:11]}: {g2_only_h} год")
        lines.append(f"• ⬛ Відключення: {both_off_h} год")
        if _safe_float(uncertain_h) > 0:
            lines.append(f"• 🟨 Сіра: {uncertain_h} год")

    # 5. Now Indicator and Countdown
    lines.append("")
    cur_joint = _extract_str(_extract_field(summary, "current_joint_status", "BOTH_MAYBE"))
    cur_badge = _get_joint_badge(cur_joint, alias_a, alias_b, single_mode, alias_limit=16)
    lines.append(f"📍 Зараз: {cur_badge}")

    next_trans_time = _extract_field(summary, "next_transition_time")
    min_to_trans = _extract_field(summary, "minutes_to_next_transition")

    if min_to_trans is not None and next_trans_time:
        dur_text = format_duration_uk(int(min_to_trans))
        header_line = f"⏳ До зміни ({next_trans_time}):"
        if len(header_line) <= 28 and len(f"  {dur_text}") <= 28:
            lines.append(header_line)
            lines.append(f"  {dur_text}")
        else:
            lines.append(f"⏳ До зміни ({next_trans_time}):")
            lines.append(dur_text)
    elif min_to_trans is not None:
        dur_text = format_duration_uk(int(min_to_trans))
        lines.append("⏳ До зміни:")
        lines.append(f"  {dur_text}")
    elif next_trans_time:
        lines.append(f"⏳ До зміни: {next_trans_time}")
    else:
        lines.append("⏳ До зміни: —")

    # Final Invariant Enforcement: Every single line must be <= 28 characters
    clamped_lines = [line[:28] for line in lines]
    return "\n".join(clamped_lines)


class TextRenderer:
    """Monospace table renderer supporting class-level and instance-level invocation."""

    @classmethod
    def render_timeline_text(cls, timeline_data: Any, **kwargs: Any) -> str:
        return render_timeline_text(timeline_data, **kwargs)

    render = render_timeline_text
