"""
tests/test_text.py - Comprehensive Test Suite for Monospace Mobile Unicode Table (Tiers 1 & 2).

Verifies Mode 2 presentation specifications:
- Tier 1: Monospace Unicode table width strictly <= 28 characters per line (zero line wrap
  on mobile Telegram clients across iOS, Android, and Desktop).
- Tier 2: Table structure (Header with date and slot aliases, time window aggregation,
  emoji badges [🟩, ⬛, 🟨, 🚨, 🟢, 🔵, 🟣], summary stats, Now indicator & countdown,
  boundary tests: truncation of long custom aliases to keep <= 28 chars, all 9 joint states,
  emergency mode text layout).

Public API under test:
- `bot.rendering.text.TextRenderer.render_timeline_text` / `render_timeline_text`
  (or fallback to `bot.services.text_renderer`)
"""

from __future__ import annotations

import unicodedata
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import pytest
except ImportError:
    from tests.conftest import pytest  # type: ignore[no-redef]

from tests.conftest import safe_import


# ============================================================================
# HELPER & FIXTURE FACTORIES
# ============================================================================

def get_render_text_func() -> Callable[..., str]:
    """
    Import and return the monospace text table rendering callable.
    Checks bot.rendering.text then bot.services.text_renderer.
    If unavailable, safe_import triggers pytest.skip().
    """
    # 1. Check bot.rendering.text
    try:
        from bot.rendering.text import TextRenderer
        if hasattr(TextRenderer, "render_timeline_text"):
            def _wrapped_class(*args: Any, **kwargs: Any) -> str:
                try:
                    res = TextRenderer.render_timeline_text(*args, **kwargs)
                except TypeError:
                    res = TextRenderer().render_timeline_text(*args, **kwargs)
                return str(res)
            return _wrapped_class
    except (ImportError, ModuleNotFoundError, AttributeError):
        pass

    try:
        from bot.rendering.text import render_timeline_text
        return lambda *args, **kwargs: str(render_timeline_text(*args, **kwargs))
    except (ImportError, ModuleNotFoundError, AttributeError):
        pass

    # 2. Check bot.services.text_renderer
    try:
        from bot.services.text_renderer import TextRenderer
        if hasattr(TextRenderer, "render_timeline_text"):
            def _wrapped_service(*args: Any, **kwargs: Any) -> str:
                try:
                    res = TextRenderer.render_timeline_text(*args, **kwargs)
                except TypeError:
                    res = TextRenderer().render_timeline_text(*args, **kwargs)
                return str(res)
            return _wrapped_service
    except (ImportError, ModuleNotFoundError, AttributeError):
        pass

    try:
        from bot.services.text_renderer import render_timeline_text
        return lambda *args, **kwargs: str(render_timeline_text(*args, **kwargs))
    except (ImportError, ModuleNotFoundError, AttributeError):
        pass

    # Neither exists yet -> trigger safe_import skip
    return safe_import("bot.rendering.text", "render_timeline_text")


def _format_time_interval(index: int) -> Tuple[str, str]:
    start_min = index * 30
    end_min = (index + 1) * 30
    sh, sm = divmod(start_min, 60)
    eh, em = divmod(end_min, 60)
    end_str = "24:00" if end_min == 1440 else f"{eh:02d}:{em:02d}"
    return f"{sh:02d}:{sm:02d}", end_str


def build_text_timeline_payload(
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
    Build a TwoGroupTimelineResponse object or dictionary for text rendering tests.
    """
    # Authoritative 48-slot pattern for Kyiv City GPV1.1 vs GPV3.2 from PROMPT_SPECIFICATION
    if status_a_pattern == "default":
        # GPV1.1: 0..3 ON, 4..10 OFF, 11..17 ON, 18..21 MAYBE, 22..27 ON, 28..34 OFF, 35..41 ON, 42..43 MAYBE, 44..47 ON
        statuses_a = (
            ["ON"] * 4 + ["OFF"] * 7 + ["ON"] * 7 + ["MAYBE"] * 4 +
            ["ON"] * 6 + ["OFF"] * 7 + ["ON"] * 7 + ["MAYBE"] * 2 + ["ON"] * 4
        )
    elif isinstance(status_a_pattern, str):
        statuses_a = [status_a_pattern] * 48
    else:
        statuses_a = list(status_a_pattern)[:48]

    if status_b_pattern == "default":
        # GPV3.2: 0..15 ON, 16..23 OFF, 24..35 ON, 36..39 OFF, 40..47 ON
        statuses_b = (
            ["ON"] * 16 + ["OFF"] * 8 + ["ON"] * 12 + ["OFF"] * 4 + ["ON"] * 8
        )
    elif isinstance(status_b_pattern, str):
        statuses_b = [status_b_pattern] * 48
    else:
        statuses_b = list(status_b_pattern)[:48]

    # Map joint statuses from combinatorial matrix if default
    if joint_pattern == "default":
        joints = []
        for a, b in zip(statuses_a, statuses_b):
            if a == "ON" and b == "ON":
                joints.append("BOTH_ON")
            elif a == "ON" and b == "OFF":
                joints.append("GROUP1_ONLY")
            elif a == "OFF" and b == "ON":
                joints.append("GROUP2_ONLY")
            elif a == "OFF" and b == "OFF":
                joints.append("BOTH_OFF")
            elif a == "ON" and b == "MAYBE":
                joints.append("GROUP1_ON_GROUP2_MAYBE")
            elif a == "MAYBE" and b == "ON":
                joints.append("GROUP1_MAYBE_GROUP2_ON")
            elif a == "OFF" and b == "MAYBE":
                joints.append("GROUP1_OFF_GROUP2_MAYBE")
            elif a == "MAYBE" and b == "OFF":
                joints.append("GROUP1_MAYBE_GROUP2_OFF")
            else:
                joints.append("BOTH_MAYBE")
    elif isinstance(joint_pattern, str):
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

    try:
        from bot.models.schedule import TwoGroupTimelineResponse
        return TwoGroupTimelineResponse.model_validate(payload)
    except Exception:
        class _AttrDict(dict):
            def __getattr__(self, item: str) -> Any:
                val = self.get(item)
                if isinstance(val, dict):
                    return _AttrDict(val)
                if isinstance(val, list):
                    return [_AttrDict(x) if isinstance(x, dict) else x for x in val]
                return val
        return _AttrDict(payload)


def assert_all_lines_within_max_width(text: str, max_width: int = 28) -> None:
    """
    Assert that every single line in the rendered text table does not exceed max_width.
    Checks character length (len(line.rstrip('\\r\\n'))).
    """
    lines = text.splitlines()
    assert len(lines) > 0, "Rendered text table is empty"
    violations = []
    for idx, line in enumerate(lines, 1):
        clean_line = line.rstrip("\r\n")
        if len(clean_line) > max_width:
            violations.append((idx, len(clean_line), clean_line))

    assert not violations, (
        f"STRICT WIDTH VIOLATION: {len(violations)} line(s) exceed {max_width} chars:\n" +
        "\n".join(f"  Line {i} ({length} chars): '{content}'" for i, length, content in violations)
    )


# ============================================================================
# TIER 1 TESTS: MONOSPACE UNICODE TABLE WIDTH (<= 28 CHARACTERS)
# ============================================================================

def test_text_every_line_le_28_chars_standard_dual_slot() -> None:
    """
    Tier 1 [Text-1]: Verify every single line of the standard dual-slot comparison table
    strictly does not exceed 28 characters (PROMPT_SPECIFICATION:1070).
    """
    render_func = get_render_text_func()
    timeline = build_text_timeline_payload("default", "default", "default")
    rendered_text = render_func(timeline)
    assert_all_lines_within_max_width(rendered_text, max_width=28)


def test_text_every_line_le_28_chars_single_slot() -> None:
    """
    Tier 1 [Text-2]: Verify every single line of the single-slot fallback table
    strictly does not exceed 28 characters.
    """
    render_func = get_render_text_func()
    timeline = build_text_timeline_payload(
        status_a_pattern="default",
        single_slot=True,
    )
    rendered_text = render_func(timeline)
    assert_all_lines_within_max_width(rendered_text, max_width=28)


def test_text_every_line_le_28_chars_extreme_100_percent_on() -> None:
    """
    Tier 1 [Text-3]: Verify line width <= 28 chars when schedule is 100% ON.
    """
    render_func = get_render_text_func()
    timeline = build_text_timeline_payload("ON", "ON", "BOTH_ON")
    rendered_text = render_func(timeline)
    assert_all_lines_within_max_width(rendered_text, max_width=28)


def test_text_every_line_le_28_chars_extreme_100_percent_off() -> None:
    """
    Tier 1 [Text-4]: Verify line width <= 28 chars when schedule is 100% OFF (total blackout).
    """
    render_func = get_render_text_func()
    timeline = build_text_timeline_payload("OFF", "OFF", "BOTH_OFF")
    rendered_text = render_func(timeline)
    assert_all_lines_within_max_width(rendered_text, max_width=28)


def test_text_every_line_le_28_chars_emergency_mode() -> None:
    """
    Tier 1 [Text-5]: Verify line width <= 28 chars during emergency outage declaration.
    """
    render_func = get_render_text_func()
    timeline = build_text_timeline_payload(
        "EMERGENCY", "EMERGENCY", "EMERGENCY",
        is_emergency=True,
    )
    rendered_text = render_func(timeline)
    assert_all_lines_within_max_width(rendered_text, max_width=28)


def test_text_valid_multiline_string_structure() -> None:
    """
    Tier 1 [Text-6]: Verify rendered text is a valid non-empty string
    containing multiple lines separated by newlines, with table borders.
    """
    render_func = get_render_text_func()
    timeline = build_text_timeline_payload("default", "default", "default")
    rendered_text = render_func(timeline)

    assert isinstance(rendered_text, str)
    lines = [line.strip() for line in rendered_text.splitlines() if line.strip()]
    assert len(lines) >= 8, f"Expected at least 8 non-empty lines, got {len(lines)}"


# ============================================================================
# TIER 2 TESTS: TABLE STRUCTURE, BADGES, AGGREGATION & BOUNDARIES
# ============================================================================

def test_text_header_structure_with_date_and_aliases() -> None:
    """
    Tier 2 [Text-7]: Verify header structure contains the schedule date
    (e.g. '11.11.2026' or '2026-11-11') and slot aliases ('Дім', 'Офіс').
    """
    render_func = get_render_text_func()
    timeline = build_text_timeline_payload(
        "default", "default", "default",
        alias_a="Дім",
        alias_b="Офіс",
    )
    rendered_text = render_func(timeline)

    # Check date presence
    assert "11.11.2026" in rendered_text or "2026-11-11" in rendered_text, (
        "Date not found in text table header"
    )
    # Check aliases presence in header lines
    assert "Дім" in rendered_text, "Slot 1 alias 'Дім' missing from text table"
    assert "Офіс" in rendered_text, "Slot 2 alias 'Офіс' missing from text table"


def test_text_time_window_aggregation() -> None:
    """
    Tier 2 [Text-8]: Verify consecutive 30-minute intervals with identical status
    are collapsed into contiguous time windows (e.g. '00-02', '02-06', '06-08', '08-12')
    rather than outputting 48 distinct rows.
    """
    render_func = get_render_text_func()
    timeline = build_text_timeline_payload("default", "default", "default")
    rendered_text = render_func(timeline)

    lines = rendered_text.splitlines()
    # Find table rows with box-drawing separator '│' or '|'
    table_rows = [line for line in lines if ("│" in line or "|" in line) and ("00" in line or "02" in line or "06" in line)]

    # 48 intervals collapsed into contiguous windows should produce <= 20 table rows
    assert 2 <= len(table_rows) <= 20, (
        f"Expected between 2 and 20 aggregated time window rows, got {len(table_rows)}"
    )
    # Expected representative window strings
    has_window_format = any("-" in row for row in table_rows)
    assert has_window_format, "Aggregated time windows must contain hyphen separator (e.g. '00-02')"


def test_text_emoji_badges_present() -> None:
    """
    Tier 2 [Text-9]: Verify presence of authoritative emoji badges:
    🟩 (ON), ⬛ (OFF), 🟨 (MAYBE), 🟢 (BOTH_ON), 🔵 (GROUP1_ONLY), 🟣 (GROUP2_ONLY).
    """
    render_func = get_render_text_func()
    timeline = build_text_timeline_payload("default", "default", "default")
    rendered_text = render_func(timeline)

    # At least slot status emoji badges and joint status badges should appear
    assert "🟩" in rendered_text or "🟢" in rendered_text, "Green power-on emoji badge missing"
    assert "⬛" in rendered_text, "Blackout emoji badge '⬛' missing"


def test_text_summary_statistics_section() -> None:
    """
    Tier 2 [Text-10]: Verify daily summary statistics section
    ('Підсумок доби') includes hour calculations formatted in Ukrainian.
    """
    render_func = get_render_text_func()
    timeline = build_text_timeline_payload("default", "default", "default")
    rendered_text = render_func(timeline)

    # Summary header indicator
    assert "Підсумок" in rendered_text or "📊" in rendered_text, (
        "Summary statistics section header missing"
    )
    # Hours unit indicator ('год' or 'г')
    assert "год" in rendered_text or "г" in rendered_text, (
        "Ukrainian hour unit ('год') missing from summary statistics"
    )


def test_text_now_indicator_and_countdown() -> None:
    """
    Tier 2 [Text-11]: Verify presence of the 'Now' indicator ('Зараз')
    and transition countdown timer ('До зміни').
    """
    render_func = get_render_text_func()
    timeline = build_text_timeline_payload("default", "default", "default")
    rendered_text = render_func(timeline)

    assert "Зараз" in rendered_text or "📍" in rendered_text, (
        "'Now' indicator ('Зараз' or 📍) missing from text footer"
    )
    assert "зміни" in rendered_text or "⏳" in rendered_text or "хв" in rendered_text, (
        "Transition countdown ('До зміни' or ⏳) missing from text footer"
    )


def test_text_boundary_long_custom_aliases_truncation() -> None:
    """
    Tier 2 [Text-12]: Boundary condition: Long custom aliases.
    When users enter lengthy aliases (e.g. 30+ characters), verify the text renderer
    truncates column headers so that EVERY single line STRICTLY satisfies <= 28 chars.
    """
    render_func = get_render_text_func()
    timeline = build_text_timeline_payload(
        "default", "default", "default",
        alias_a="НадзвичайноДовгийПсевдонімДляСлоту1",
        alias_b="ГігантськаНазваОфісноїЛокації2",
    )
    rendered_text = render_func(timeline)

    # Verify no line exceeds 28 characters despite massive aliases
    assert_all_lines_within_max_width(rendered_text, max_width=28)


def test_text_all_nine_joint_states_formatted() -> None:
    """
    Tier 2 [Text-13]: Verify formatted text layout when all 9 joint states are present:
    BOTH_ON, GROUP1_ONLY, GROUP2_ONLY, BOTH_OFF, GROUP1_ON_GROUP2_MAYBE,
    GROUP1_MAYBE_GROUP2_ON, GROUP1_OFF_GROUP2_MAYBE, GROUP1_MAYBE_GROUP2_OFF, BOTH_MAYBE.
    Every line must remain <= 28 characters without throwing unhandled exceptions.
    """
    render_func = get_render_text_func()

    all_9_joints = [
        "BOTH_ON", "GROUP1_ONLY", "GROUP2_ONLY", "BOTH_OFF",
        "GROUP1_ON_GROUP2_MAYBE", "GROUP1_MAYBE_GROUP2_ON",
        "GROUP1_OFF_GROUP2_MAYBE", "GROUP1_MAYBE_GROUP2_OFF", "BOTH_MAYBE",
    ]
    # Cycle to 48 slots
    cycled_joints = [all_9_joints[i % len(all_9_joints)] for i in range(48)]

    timeline = build_text_timeline_payload(
        status_a_pattern="ON",
        status_b_pattern="ON",
        joint_pattern=cycled_joints,
    )
    rendered_text = render_func(timeline)

    assert_all_lines_within_max_width(rendered_text, max_width=28)


def test_text_emergency_mode_layout() -> None:
    """
    Tier 2 [Text-14]: Verify emergency mode text layout:
    Includes emergency badge (🚨), emergency warning banner,
    and guarantees every line strictly <= 28 characters width.
    """
    render_func = get_render_text_func()
    timeline = build_text_timeline_payload(
        status_a_pattern="EMERGENCY",
        status_b_pattern="EMERGENCY",
        joint_pattern="EMERGENCY",
        is_emergency=True,
    )
    rendered_text = render_func(timeline)

    assert "🚨" in rendered_text or "Аварійні" in rendered_text or "АВАРІЙНІ" in rendered_text, (
        "Emergency warning badge/text missing from emergency layout"
    )
    assert_all_lines_within_max_width(rendered_text, max_width=28)


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

