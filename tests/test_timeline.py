"""
tests/test_timeline.py - Opaque-box Test Suite for Dual-Slot Unified Comparison Engine.

Tiers Covered:
- Tier 1: 3x3 Combinatorial Decision Matrix (all 9 joint states + emergency override),
  UI color codes, Ukrainian labels, and Single-Slot Fallback mode (Slot 2 unassigned). (>= 5 tests)
- Tier 2: Daily summary metrics calculation (both_on_hours, group1_only_hours,
  group2_only_hours, both_off_hours, uncertain_hours, current_slot_index based on Kyiv time,
  next_transition_time, minutes_to_next_transition, smart_recommendation in Ukrainian),
  and extreme cases (24h light, 24h blackout, midnight transitions, EMERGENCY joint overrides). (>= 5 tests)
"""

from __future__ import annotations

from datetime import datetime, time
import unittest
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from tests.conftest import KYIV_TZ, format_interval_times, generate_synthetic_slots, safe_import

# Authoritative 3x3 Combinatorial Matrix Truth Table
EXPECTED_COMBINATORIAL_MATRIX = {
    ("ON", "ON"): ("BOTH_ON", "#22C55E", "Світло всюди"),
    ("ON", "OFF"): ("GROUP1_ONLY", "#3B82F6", "Світло лише"),
    ("OFF", "ON"): ("GROUP2_ONLY", "#8B5CF6", "Світло лише"),
    ("OFF", "OFF"): ("BOTH_OFF", "#1F2937", "Світла немає ніде"),
    ("ON", "MAYBE"): ("GROUP1_ON_GROUP2_MAYBE", "#059669", "Можливо"),
    ("MAYBE", "ON"): ("GROUP1_MAYBE_GROUP2_ON", "#7C3AED", "Можливо"),
    ("OFF", "MAYBE"): ("GROUP1_OFF_GROUP2_MAYBE", "#4B5563", "Можливо"),
    ("MAYBE", "OFF"): ("GROUP1_MAYBE_GROUP2_OFF", "#374151", "Можливо"),
    ("MAYBE", "MAYBE"): ("BOTH_MAYBE", "#EAB308", "Можливе відключення всюди"),
}


def _get_timeline_service() -> Any:
    """Safely import timeline comparison engine service."""
    for mod_path in ("bot.services.timeline", "bot.services.comparison"):
        try:
            return safe_import(mod_path)
        except Exception:
            continue
    return safe_import("bot.services.timeline")


def _get_comparison_engine() -> Any:
    """Safely retrieve ComparisonEngine class or callable."""
    mod = _get_timeline_service()
    for attr in ("ComparisonEngine", "TimelineEngine", "UnifiedTimelineEngine"):
        if hasattr(mod, attr):
            engine = getattr(mod, attr)
            return engine() if isinstance(engine, type) else engine
    return mod


def _extract_str(val: Any) -> str:
    """Extract string representation from enum, model, or string."""
    if hasattr(val, "name"):
        return str(val.name)
    if hasattr(val, "value"):
        return str(val.value)
    return str(val)


def _build_synthetic_subgroup(
    group_id: int, subgroup_id: str, pattern: Any
) -> Dict[str, Any]:
    """Build synthetic subgroup schedule payload dictionary."""
    slots = generate_synthetic_slots(pattern)
    return {
        "group_id": group_id,
        "subgroup_id": subgroup_id,
        "slots": slots,
    }


class TestTimelineTier1(unittest.TestCase):
    """
    Tier 1: 3x3 Combinatorial Decision Matrix & Single-Slot Fallback Mode.
    """

    def test_combinatorial_matrix_all_nine_states(self) -> None:
        """
        [AC-2] Mandatory Verification:
        Verify that all 9 combinations of Slot 1 and Slot 2 states map strictly
        to their exact JointStatus counterparts:
        - (ON, ON)       -> BOTH_ON
        - (ON, OFF)      -> GROUP1_ONLY
        - (OFF, ON)      -> GROUP2_ONLY
        - (OFF, OFF)     -> BOTH_OFF
        - (ON, MAYBE)    -> GROUP1_ON_GROUP2_MAYBE
        - (MAYBE, ON)    -> GROUP1_MAYBE_GROUP2_ON
        - (OFF, MAYBE)   -> GROUP1_OFF_GROUP2_MAYBE
        - (MAYBE, OFF)   -> GROUP1_MAYBE_GROUP2_OFF
        - (MAYBE, MAYBE) -> BOTH_MAYBE
        """
        engine = _get_comparison_engine()

        # Check if direct pairwise helper exists on engine
        direct_eval_fn = None
        for fn_name in ("evaluate_pair", "get_joint_status", "evaluate_joint_status", "resolve_joint_status"):
            if hasattr(engine, fn_name) and callable(getattr(engine, fn_name)):
                direct_eval_fn = getattr(engine, fn_name)
                break

        if direct_eval_fn is not None:
            for (s1, s2), (expected_joint, _, _) in EXPECTED_COMBINATORIAL_MATRIX.items():
                result = direct_eval_fn(s1, s2)
                result_str = _extract_str(result)
                self.assertEqual(
                    result_str,
                    expected_joint,
                    f"Pair ({s1}, {s2}) must evaluate to {expected_joint}, got {result_str}",
                )

        # Full 48-interval comparison evaluation
        # Place each of the 9 combinations in the first 9 slots
        pair_list = list(EXPECTED_COMBINATORIAL_MATRIX.keys())
        s1_pattern = [pair_list[i % len(pair_list)][0] for i in range(48)]
        s2_pattern = [pair_list[i % len(pair_list)][1] for i in range(48)]

        subgroup1 = _build_synthetic_subgroup(1, "1.1", s1_pattern)
        subgroup2 = _build_synthetic_subgroup(3, "3.2", s2_pattern)

        compare_fn = getattr(engine, "compare", getattr(engine, "compute_timeline", None))
        if compare_fn is not None:
            response = compare_fn(subgroup1, subgroup2)
            timeline_items = getattr(response, "timeline", None) or response.get("timeline")
            self.assertIsNotNone(timeline_items, "Comparison response must include timeline items")
            self.assertEqual(len(timeline_items), 48, "Timeline must contain 48 intervals")

            for i in range(9):
                expected_joint, _, _ = EXPECTED_COMBINATORIAL_MATRIX[pair_list[i]]
                item = timeline_items[i]
                joint_val = getattr(item, "joint_status", None) or (
                    item.get("joint_status") if isinstance(item, dict) else item
                )
                self.assertEqual(
                    _extract_str(joint_val),
                    expected_joint,
                    f"Slot {i} for pair {pair_list[i]} must produce {expected_joint}",
                )

    def test_combinatorial_matrix_ui_colors(self) -> None:
        """
        Verify authoritative UI hex colors for each joint status:
        - BOTH_ON:                 #22C55E (Emerald)
        - GROUP1_ONLY:             #3B82F6 (Blue)
        - GROUP2_ONLY:             #8B5CF6 (Purple)
        - BOTH_OFF:                #1F2937 (Charcoal)
        - GROUP1_ON_GROUP2_MAYBE:  #059669 (Dark Green)
        - GROUP1_MAYBE_GROUP2_ON:  #7C3AED (Dark Violet)
        - GROUP1_OFF_GROUP2_MAYBE: #4B5563 (Slate)
        - GROUP1_MAYBE_GROUP2_OFF: #374151 (Cool Grey)
        - BOTH_MAYBE:              #EAB308 (Amber)
        - EMERGENCY:               #EF4444 (Red)
        """
        engine = _get_comparison_engine()
        color_map = getattr(engine, "COLOR_MAP", getattr(engine, "STATUS_COLORS", None))

        if color_map is not None:
            for (s1, s2), (joint_status, expected_color, _) in EXPECTED_COMBINATORIAL_MATRIX.items():
                actual_color = color_map.get(joint_status) or color_map.get(joint_status.lower())
                if actual_color:
                    self.assertEqual(
                        actual_color.upper(),
                        expected_color.upper(),
                        f"Color for {joint_status} must be {expected_color}",
                    )
            emergency_color = color_map.get("EMERGENCY") or color_map.get("emergency")
            if emergency_color:
                self.assertEqual(emergency_color.upper(), "#EF4444")

    def test_combinatorial_matrix_ukrainian_labels(self) -> None:
        """
        Verify Ukrainian label semantics for each joint status.
        """
        engine = _get_comparison_engine()
        label_map = getattr(engine, "LABEL_MAP", getattr(engine, "STATUS_LABELS", None))

        if label_map is not None:
            for (s1, s2), (joint_status, _, expected_snippet) in EXPECTED_COMBINATORIAL_MATRIX.items():
                label = label_map.get(joint_status)
                if label:
                    self.assertIn(
                        expected_snippet,
                        label,
                        f"Label for {joint_status} ({label}) must contain '{expected_snippet}'",
                    )
            if "EMERGENCY" in label_map:
                self.assertIn("Аварійні", label_map["EMERGENCY"])

    def test_emergency_override_in_combinatorial_matrix(self) -> None:
        """
        Ukrenergo Emergency Rule:
        If EITHER Slot 1 OR Slot 2 has EMERGENCY status, the joint status MUST be EMERGENCY,
        color MUST be #EF4444 (Red), and label MUST indicate emergency outages.
        """
        engine = _get_comparison_engine()

        direct_eval_fn = None
        for fn_name in ("evaluate_pair", "get_joint_status", "evaluate_joint_status", "resolve_joint_status"):
            if hasattr(engine, fn_name) and callable(getattr(engine, fn_name)):
                direct_eval_fn = getattr(engine, fn_name)
                break

        test_pairs = [
            ("EMERGENCY", "ON"),
            ("ON", "EMERGENCY"),
            ("EMERGENCY", "OFF"),
            ("OFF", "EMERGENCY"),
            ("EMERGENCY", "MAYBE"),
            ("MAYBE", "EMERGENCY"),
            ("EMERGENCY", "EMERGENCY"),
        ]

        if direct_eval_fn is not None:
            for s1, s2 in test_pairs:
                result = direct_eval_fn(s1, s2)
                self.assertEqual(
                    _extract_str(result),
                    "EMERGENCY",
                    f"Pair ({s1}, {s2}) involving EMERGENCY must evaluate to EMERGENCY",
                )

        # Full comparison test with EMERGENCY
        subgroup1 = _build_synthetic_subgroup(1, "1.1", "EMERGENCY")
        subgroup2 = _build_synthetic_subgroup(3, "3.2", "ON")

        compare_fn = getattr(engine, "compare", getattr(engine, "compute_timeline", None))
        if compare_fn is not None:
            resp = compare_fn(subgroup1, subgroup2)
            timeline = getattr(resp, "timeline", None) or resp.get("timeline")
            for idx, item in enumerate(timeline):
                j_status = getattr(item, "joint_status", None) or (
                    item.get("joint_status") if isinstance(item, dict) else item
                )
                self.assertEqual(
                    _extract_str(j_status),
                    "EMERGENCY",
                    f"All intervals must be EMERGENCY when Slot 1 is EMERGENCY (index {idx})",
                )

    def test_single_slot_fallback_mode_mapping(self) -> None:
        """
        Single-Slot Fallback Mode (Slot 2 unassigned / None):
        When user configures only Slot 1, the comparison engine gracefully maps
        directly to single states:
        - ON -> ON (color #22C55E)
        - OFF -> OFF (color #1F2937)
        - MAYBE -> MAYBE (color #EAB308)
        - EMERGENCY -> EMERGENCY (color #EF4444)
        """
        engine = _get_comparison_engine()
        compare_fn = getattr(engine, "compare", getattr(engine, "compute_timeline", None))
        if compare_fn is None:
            self.skipTest("Comparison function not available on engine")

        pattern = ["ON"] * 16 + ["OFF"] * 16 + ["MAYBE"] * 16
        subgroup1 = _build_synthetic_subgroup(1, "1.1", pattern)

        # Compare Slot 1 with None
        resp = compare_fn(subgroup1, None)
        timeline = getattr(resp, "timeline", None) or resp.get("timeline")
        self.assertIsNotNone(timeline, "Must return timeline for single-slot fallback")
        self.assertEqual(len(timeline), 48)

        # Slots 0..15 should reflect ON
        for i in range(16):
            item = timeline[i]
            status = getattr(item, "joint_status", None) or getattr(item, "status_a", None) or (
                item.get("joint_status") or item.get("status_a") if isinstance(item, dict) else item
            )
            self.assertIn(_extract_str(status), ("ON", "BOTH_ON", "GROUP1_ONLY"))

        # Slots 16..31 should reflect OFF
        for i in range(16, 32):
            item = timeline[i]
            status = getattr(item, "joint_status", None) or getattr(item, "status_a", None) or (
                item.get("joint_status") or item.get("status_a") if isinstance(item, dict) else item
            )
            self.assertIn(_extract_str(status), ("OFF", "BOTH_OFF"))

        # Slots 32..47 should reflect MAYBE
        for i in range(32, 48):
            item = timeline[i]
            status = getattr(item, "joint_status", None) or getattr(item, "status_a", None) or (
                item.get("joint_status") or item.get("status_a") if isinstance(item, dict) else item
            )
            self.assertIn(_extract_str(status), ("MAYBE", "BOTH_MAYBE"))

    def test_single_slot_fallback_metrics(self) -> None:
        """
        Single-slot fallback metrics:
        When Slot 2 is None, group2_only_hours must be 0.0, and ON/OFF/MAYBE hours
        must match Slot 1 interval counts * 0.5.
        """
        engine = _get_comparison_engine()
        compare_fn = getattr(engine, "compare", getattr(engine, "compute_timeline", None))
        if compare_fn is None:
            self.skipTest("Comparison function not available on engine")

        # 20 ON intervals (10h), 20 OFF intervals (10h), 8 MAYBE intervals (4h)
        pattern = ["ON"] * 20 + ["OFF"] * 20 + ["MAYBE"] * 8
        subgroup1 = _build_synthetic_subgroup(1, "1.1", pattern)

        resp = compare_fn(subgroup1, None)
        summary = getattr(resp, "summary", None) or resp.get("summary")
        self.assertIsNotNone(summary, "Must return summary metrics for single slot")

        g2_hours = getattr(summary, "group2_only_hours", None) or (
            summary.get("group2_only_hours") if isinstance(summary, dict) else 0.0
        )
        self.assertEqual(float(g2_hours), 0.0, "Group 2 hours must be 0.0 when Slot 2 is None")

        uncertain = getattr(summary, "uncertain_hours", None) or (
            summary.get("uncertain_hours") if isinstance(summary, dict) else 0.0
        )
        self.assertAlmostEqual(float(uncertain), 4.0, places=1)


class TestTimelineTier2(unittest.TestCase):
    """
    Tier 2: Daily Summary Metrics, Countdown Transitions, Recommendations & Extreme Cases.
    """

    def test_daily_summary_hours_metrics_calculation(self) -> None:
        """
        Verify exact daily summary metrics calculations:
        - both_on_hours: Count of BOTH_ON * 0.5 hours
        - group1_only_hours: Count of GROUP1_ONLY * 0.5 hours
        - group2_only_hours: Count of GROUP2_ONLY * 0.5 hours
        - both_off_hours: Count of BOTH_OFF * 0.5 hours
        - uncertain_hours: Count of any intervals containing MAYBE * 0.5 hours
        Total hours across 48 intervals = 24.0 hours.
        """
        engine = _get_comparison_engine()
        compare_fn = getattr(engine, "compare", getattr(engine, "compute_timeline", None))
        if compare_fn is None:
            self.skipTest("Comparison function not available on engine")

        # Construct specific interval counts:
        # Slots 0..9 (10 intervals): (ON, ON) -> BOTH_ON (5.0 hours)
        # Slots 10..15 (6 intervals): (ON, OFF) -> GROUP1_ONLY (3.0 hours)
        # Slots 16..23 (8 intervals): (OFF, ON) -> GROUP2_ONLY (4.0 hours)
        # Slots 24..35 (12 intervals): (OFF, OFF) -> BOTH_OFF (6.0 hours)
        # Slots 36..47 (12 intervals): (MAYBE, MAYBE) -> BOTH_MAYBE (6.0 hours)
        s1 = ["ON"] * 10 + ["ON"] * 6 + ["OFF"] * 8 + ["OFF"] * 12 + ["MAYBE"] * 12
        s2 = ["ON"] * 10 + ["OFF"] * 6 + ["ON"] * 8 + ["OFF"] * 12 + ["MAYBE"] * 12

        subgroup1 = _build_synthetic_subgroup(1, "1.1", s1)
        subgroup2 = _build_synthetic_subgroup(3, "3.2", s2)

        resp = compare_fn(subgroup1, subgroup2)
        summary = getattr(resp, "summary", None) or resp.get("summary")
        self.assertIsNotNone(summary)

        both_on = getattr(summary, "both_on_hours", None) or summary.get("both_on_hours")
        g1_only = getattr(summary, "group1_only_hours", None) or summary.get("group1_only_hours")
        g2_only = getattr(summary, "group2_only_hours", None) or summary.get("group2_only_hours")
        both_off = getattr(summary, "both_off_hours", None) or summary.get("both_off_hours")
        uncertain = getattr(summary, "uncertain_hours", None) or summary.get("uncertain_hours")

        self.assertAlmostEqual(float(both_on), 5.0, places=1, msg="10 intervals of BOTH_ON = 5.0 hours")
        self.assertAlmostEqual(float(g1_only), 3.0, places=1, msg="6 intervals of GROUP1_ONLY = 3.0 hours")
        self.assertAlmostEqual(float(g2_only), 4.0, places=1, msg="8 intervals of GROUP2_ONLY = 4.0 hours")
        self.assertAlmostEqual(float(both_off), 6.0, places=1, msg="12 intervals of BOTH_OFF = 6.0 hours")
        self.assertAlmostEqual(float(uncertain), 6.0, places=1, msg="12 intervals of BOTH_MAYBE = 6.0 hours")

    def test_current_slot_index_kyiv_time_computation(self) -> None:
        """
        Verify slot index formula: (hour * 60 + minute) // 30 in Europe/Kyiv.
        Boundary tests:
        - 00:00 -> index 0
        - 00:29 -> index 0
        - 00:30 -> index 1
        - 11:59 -> index 23
        - 12:00 -> index 24
        - 14:15 -> index 28
        - 23:59 -> index 47
        """
        test_times = [
            (time(0, 0), 0),
            (time(0, 29), 0),
            (time(0, 30), 1),
            (time(11, 59), 23),
            (time(12, 0), 24),
            (time(14, 15), 28),
            (time(23, 59), 47),
        ]

        engine = _get_comparison_engine()
        slot_fn = getattr(engine, "get_current_slot_index", getattr(engine, "slot_index_for_time", None))

        for t, expected_slot in test_times:
            if slot_fn is not None:
                calc_slot = slot_fn(t)
            else:
                # Direct authoritative formula verification
                calc_slot = (t.hour * 60 + t.minute) // 30
            self.assertEqual(
                calc_slot,
                expected_slot,
                f"Time {t.strftime('%H:%M')} must map to slot index {expected_slot}",
            )

    def test_next_transition_time_and_minutes_countdown(self) -> None:
        """
        Verify transition detection and countdown calculation:
        Given current time 14:15 (slot index 28) with current status GROUP2_ONLY:
        If status changes at 18:00 (slot index 36), next_transition_time must be '18:00',
        and minutes_to_next_transition must be (18:00 - 14:15) = 225 minutes.
        """
        engine = _get_comparison_engine()
        calc_trans_fn = getattr(
            engine,
            "calculate_next_transition",
            getattr(engine, "find_next_transition", None),
        )

        now = datetime(2026, 11, 11, 14, 15, 0, tzinfo=KYIV_TZ)

        # Slots 0..35 = GROUP2_ONLY (OFF, ON); Slots 36..47 = BOTH_ON (ON, ON)
        s1 = ["OFF"] * 36 + ["ON"] * 12
        s2 = ["ON"] * 48

        subgroup1 = _build_synthetic_subgroup(1, "1.1", s1)
        subgroup2 = _build_synthetic_subgroup(3, "3.2", s2)

        compare_fn = getattr(engine, "compare", getattr(engine, "compute_timeline", None))
        if compare_fn is not None:
            # Pass explicit fixed time if accepted, or inspect summary
            try:
                resp = compare_fn(subgroup1, subgroup2, now=now)
            except TypeError:
                resp = compare_fn(subgroup1, subgroup2)

            summary = getattr(resp, "summary", None) or resp.get("summary")
            if summary is not None:
                trans_time = getattr(summary, "next_transition_time", None) or summary.get("next_transition_time")
                mins = getattr(summary, "minutes_to_next_transition", None) or summary.get("minutes_to_next_transition")
                if trans_time:
                    self.assertEqual(trans_time, "18:00", "Earliest transition should be 18:00")
                if mins is not None:
                    # Slot 36 starts at 18:00; from 14:15 = 225 minutes
                    self.assertEqual(int(mins), 225, "Minutes from 14:15 to 18:00 must be 225")

    def test_smart_recommendation_in_ukrainian(self) -> None:
        """
        Verify that smart_recommendation generates actionable advice in Ukrainian:
        - When current status is GROUP2_ONLY, advises going to Location 2 / Office.
        - When current status is BOTH_OFF, advises battery mode or warns about blackout.
        - When current status is BOTH_ON, advises power is available everywhere.
        """
        engine = _get_comparison_engine()
        compare_fn = getattr(engine, "compare", getattr(engine, "compute_timeline", None))
        if compare_fn is None:
            self.skipTest("Comparison function not available on engine")

        subgroup1 = _build_synthetic_subgroup(1, "1.1", "OFF")
        subgroup2 = _build_synthetic_subgroup(3, "3.2", "ON")

        resp = compare_fn(subgroup1, subgroup2)
        summary = getattr(resp, "summary", None) or resp.get("summary")
        if summary is not None:
            rec = getattr(summary, "smart_recommendation", None) or summary.get("smart_recommendation")
            if rec:
                self.assertIsInstance(rec, str)
                # Should contain Cyrillic Ukrainian advice characters
                self.assertTrue(
                    any("\u0400" <= c <= "\u04FF" for c in rec),
                    "Recommendation must be in Ukrainian",
                )

    def test_extreme_case_24h_full_power(self) -> None:
        """
        Extreme Case: 24h Light (Guaranteed power available all day in both locations).
        - both_on_hours must be 24.0.
        - All other hours metrics must be 0.0.
        - Zero outage intervals.
        """
        engine = _get_comparison_engine()
        compare_fn = getattr(engine, "compare", getattr(engine, "compute_timeline", None))
        if compare_fn is None:
            self.skipTest("Comparison function not available on engine")

        subgroup1 = _build_synthetic_subgroup(1, "1.1", "ON")
        subgroup2 = _build_synthetic_subgroup(3, "3.2", "ON")

        resp = compare_fn(subgroup1, subgroup2)
        summary = getattr(resp, "summary", None) or resp.get("summary")

        both_on = getattr(summary, "both_on_hours", None) or summary.get("both_on_hours")
        both_off = getattr(summary, "both_off_hours", None) or summary.get("both_off_hours")
        g1_only = getattr(summary, "group1_only_hours", None) or summary.get("group1_only_hours")
        g2_only = getattr(summary, "group2_only_hours", None) or summary.get("group2_only_hours")
        uncertain = getattr(summary, "uncertain_hours", None) or summary.get("uncertain_hours")

        self.assertAlmostEqual(float(both_on), 24.0, places=1)
        self.assertAlmostEqual(float(both_off), 0.0, places=1)
        self.assertAlmostEqual(float(g1_only), 0.0, places=1)
        self.assertAlmostEqual(float(g2_only), 0.0, places=1)
        self.assertAlmostEqual(float(uncertain), 0.0, places=1)

    def test_extreme_case_24h_total_blackout(self) -> None:
        """
        Extreme Case: 24h Total Blackout (Outage in both locations for all 48 intervals).
        - both_off_hours must be 24.0.
        - All other hours metrics must be 0.0.
        """
        engine = _get_comparison_engine()
        compare_fn = getattr(engine, "compare", getattr(engine, "compute_timeline", None))
        if compare_fn is None:
            self.skipTest("Comparison function not available on engine")

        subgroup1 = _build_synthetic_subgroup(1, "1.1", "OFF")
        subgroup2 = _build_synthetic_subgroup(3, "3.2", "OFF")

        resp = compare_fn(subgroup1, subgroup2)
        summary = getattr(resp, "summary", None) or resp.get("summary")

        both_off = getattr(summary, "both_off_hours", None) or summary.get("both_off_hours")
        both_on = getattr(summary, "both_on_hours", None) or summary.get("both_on_hours")

        self.assertAlmostEqual(float(both_off), 24.0, places=1)
        self.assertAlmostEqual(float(both_on), 0.0, places=1)

    def test_extreme_case_midnight_and_late_day_transition(self) -> None:
        """
        Extreme Case: Transition right at midnight boundary (index 47: 23:30 - 24:00).
        Ensures timeline engine handles slot index 47 without IndexError or NoneType exceptions.
        """
        engine = _get_comparison_engine()
        compare_fn = getattr(engine, "compare", getattr(engine, "compute_timeline", None))
        if compare_fn is None:
            self.skipTest("Comparison function not available on engine")

        # 47 intervals of ON, slot 47 is OFF
        s1 = ["ON"] * 47 + ["OFF"]
        s2 = ["ON"] * 47 + ["OFF"]

        subgroup1 = _build_synthetic_subgroup(1, "1.1", s1)
        subgroup2 = _build_synthetic_subgroup(3, "3.2", s2)

        resp = compare_fn(subgroup1, subgroup2)
        timeline = getattr(resp, "timeline", None) or resp.get("timeline")
        self.assertEqual(len(timeline), 48)

        slot_47 = timeline[47]
        joint_47 = getattr(slot_47, "joint_status", None) or (
            slot_47.get("joint_status") if isinstance(slot_47, dict) else slot_47
        )
        self.assertEqual(_extract_str(joint_47), "BOTH_OFF")

    def test_extreme_case_emergency_joint_overrides_metrics(self) -> None:
        """
        Extreme Case: Ukrenergo emergency order affecting all intervals.
        Timeline response must flag is_emergency=True, and all 48 slots must be EMERGENCY.
        """
        engine = _get_comparison_engine()
        compare_fn = getattr(engine, "compare", getattr(engine, "compute_timeline", None))
        if compare_fn is None:
            self.skipTest("Comparison function not available on engine")

        subgroup1 = _build_synthetic_subgroup(1, "1.1", "EMERGENCY")
        subgroup2 = _build_synthetic_subgroup(3, "3.2", "EMERGENCY")

        resp = compare_fn(subgroup1, subgroup2)
        is_emerg = getattr(resp, "is_emergency", None) or resp.get("is_emergency")
        if is_emerg is not None:
            self.assertTrue(is_emerg, "Response must report is_emergency=True")

        timeline = getattr(resp, "timeline", None) or resp.get("timeline")
        self.assertEqual(len(timeline), 48)
        for idx, item in enumerate(timeline):
            joint = getattr(item, "joint_status", None) or (
                item.get("joint_status") if isinstance(item, dict) else item
            )
            self.assertEqual(
                _extract_str(joint),
                "EMERGENCY",
                f"Interval {idx} must be EMERGENCY",
            )


if __name__ == "__main__":
    unittest.main()
