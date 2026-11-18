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


def _get_joint_badge(joint_str: str, alias_a: str, alias_b: str, single_mode: bool) -> str:
    """Get authoritative compact badge representation for table row and current status."""
    a_short = (alias_a[:4] if alias_a else "СЛ1")
    b_short = (alias_b[:4] if alias_b else "СЛ2")

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
        "BOTH_OFF": "⬛Темно",
        "GROUP1_ON_GROUP2_MAYBE": "🟢Є/Мож",
        "GROUP1_MAYBE_GROUP2_ON": "🟣Мож/Є",
        "GROUP1_OFF_GROUP2_MAYBE": "⬛Ні/Мож",
        "GROUP1_MAYBE_GROUP2_OFF": "⬛Мож/Ні",
        "BOTH_MAYBE": "🟨Сіра",
        "EMERGENCY": "🚨Аварія",
    }
    return badge_map.get(joint_str, f"⚪{joint_str[:4]}")


def render_timeline_text(timeline_data: Any, **kwargs: Any) -> str:
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

    summary = _extract_field(timeline_data, "summary") or {}
    raw_timeline = _extract_field(timeline_data, "timeline") or []

    # Title with variation selector matching test_cross_feature contract
    lines.append(f"⚡️ ДТЕК: {comp_date}")
    lines.append(f"🏠 1: {alias_a[:18]}")
    if not single_mode:
        lines.append(f"🏢 2: {alias_b[:18]}")

    if is_emergency:
        lines.append("🚨 АВАРІЙНІ ВІДКЛЮЧЕННЯ")
    elif stale:
        lines.append("⚠️ Застарілі дані (кеш)")

    lines.append("")

    # 2. Table Column Headers
    if single_mode:
        lines.append("ЧАС   │ СТАН")
        lines.append("──────┼─────")
    else:
        hdr_a = (alias_a[:3] if alias_a else "СЛ1").upper().center(3)
        hdr_b = (alias_b[:3] if alias_b else "СЛ2").upper().center(3)
        lines.append(f"ЧАС   │{hdr_a}│{hdr_b}│СТАН")
        lines.append("──────┼───┼───┼─────")

    # 3. Contiguous Time Window Aggregation
    i = 0
    total_slots = len(raw_timeline)
    while i < total_slots:
        item_i = raw_timeline[i]
        joint_state = _extract_str(_extract_field(item_i, "joint_status", "BOTH_MAYBE"))
        sa = _extract_str(_extract_field(item_i, "status_a", "MAYBE"))
        sb = _extract_str(_extract_field(item_i, "status_b", "MAYBE")) if not single_mode else None

        raw_start = _extract_field(item_i, "start_time")
        start_t_str = str(raw_start) if raw_start else f"{i//2:02d}:{(i%2)*30:02d}"
        chunk_start = start_t_str[:2]

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
            chunk_end = "24"
        elif raw_end:
            chunk_end = str(raw_end)[:2]
        else:
            chunk_end = f"{j//2:02d}"

        if chunk_start == chunk_end:
            if chunk_start.isdigit():
                chunk_end = f"{int(chunk_start) + 1:02d}"
            else:
                chunk_end = f"{min(24, (i//2) + 1):02d}"

        time_range = f"{chunk_start}-{chunk_end}"
        badge = _get_joint_badge(joint_state, alias_a, alias_b, single_mode)

        if single_mode:
            row = f"{time_range:5s} │ {badge}"
        else:
            ea = EMOJI_SLOT_MAP.get(sa, "⬜")
            eb = EMOJI_SLOT_MAP.get(sb, "⬜") if sb else "⬜"
            row = f"{time_range:5s} │ {ea}│ {eb}│{badge}"

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
        lines.append(f"• ⬛ Блекаут: {both_off_h} год")
        if _safe_float(uncertain_h) > 0:
            lines.append(f"• 🟨 Сіра: {uncertain_h} год")
    else:
        lines.append(f"• 🟢 Всюди: {both_on_h} год")
        lines.append(f"• 🔵 Лише 1: {g1_only_h} год")
        lines.append(f"• 🟣 Лише 2: {g2_only_h} год")
        lines.append(f"• ⬛ Блекаут: {both_off_h} год")
        if _safe_float(uncertain_h) > 0:
            lines.append(f"• 🟨 Сіра: {uncertain_h} год")

    # 5. Now Indicator and Countdown
    lines.append("")
    cur_joint = _extract_str(_extract_field(summary, "current_joint_status", "BOTH_MAYBE"))
    cur_badge = _get_joint_badge(cur_joint, alias_a, alias_b, single_mode)
    lines.append(f"📍 Зараз: {cur_badge}")

    next_trans_time = _extract_field(summary, "next_transition_time")
    min_to_trans = _extract_field(summary, "minutes_to_next_transition")

    if min_to_trans is not None and next_trans_time:
        countdown_line = f"⏳ До зміни: {min_to_trans} хв ({next_trans_time})"
        if len(countdown_line) > 28:
            countdown_line = f"⏳ До зміни: {min_to_trans} хв"
        lines.append(countdown_line)
    elif min_to_trans is not None:
        lines.append(f"⏳ До зміни: {min_to_trans} хв")
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
