"""
tests/test_normalizer.py - Opaque-box Test Suite for DTEK Outage Schedule Normalization.

Tiers Covered:
- Tier 1: 7 raw DTEK status mappings (yes -> ON/ON, no -> OFF/OFF, first -> OFF/ON,
  second -> ON/OFF, maybe -> MAYBE/MAYBE, mfirst -> MAYBE/ON, msecond -> ON/MAYBE).
  Partitioning of 24-hour daily schedules into exactly 48 uniform 30-minute intervals
  covering 00:00 to 24:00 Kyiv time. (>= 5 tests)
- Tier 2: Boundary intervals (index 0: 00:00-00:30, index 47: 23:30-24:00,
  index 23: 11:30-12:00, index 24: 12:00-12:30). Unknown/missing status mapping to MAYBE.
  Emergency override mode (when is_emergency=True or emergency notice detected,
  all 48 intervals assigned EMERGENCY). Malformed input handling. (>= 5 tests)
"""

from __future__ import annotations

import unittest
from typing import Any, Callable, Dict, List

from tests.conftest import safe_import


def _get_normalizer_callable() -> Callable[..., Any]:
    """
    Safely import the normalizer entrypoint from bot.services.normalizer.
    Supports Normalizer.normalize (@staticmethod/@classmethod), Normalizer().normalize,
    normalize_intervals, or normalize callable.
    """
    mod = safe_import("bot.services.normalizer")
    if hasattr(mod, "Normalizer"):
        candidate = getattr(mod, "Normalizer")
        if hasattr(candidate, "normalize") and callable(getattr(candidate, "normalize")):
            return getattr(candidate, "normalize")
        elif callable(candidate):
            try:
                inst = candidate()
                if hasattr(inst, "normalize") and callable(getattr(inst, "normalize")):
                    return getattr(inst, "normalize")
            except Exception:
                pass
            return candidate
    for attr in ("normalize_intervals", "normalize", "normalize_schedule"):
        if hasattr(mod, attr) and callable(getattr(mod, attr)):
            return getattr(mod, attr)
    raise AttributeError("No suitable normalize callable found in 'bot.services.normalizer'")


def _extract_status_str(item: Any) -> str:
    """Extract string status value ('ON', 'OFF', 'MAYBE', 'EMERGENCY') from an item."""
    if hasattr(item, "status"):
        val = item.status
    else:
        val = item
    if hasattr(val, "value"):
        return str(val.value)
    return str(val)


def _make_24h_raw_dict(status: str) -> Dict[str, str]:
    """Generate raw DTEK dictionary with all 24 hour keys set to given status."""
    return {str(h): status for h in range(1, 25)}


class TestNormalizerTier1(unittest.TestCase):
    """
    Tier 1: Unit & Value Verification of 7 Raw DTEK Status Codes to 48 Intervals.
    """

    def test_raw_status_yes_maps_to_on_on(self) -> None:
        """
        'yes' -> Guaranteed Power Available for entire hour.
        Must map to First Half (:00-:30) = ON, Second Half (:30-:00) = ON.
        Full 24-hour day must yield exactly 48 ON intervals.
        """
        normalize = _get_normalizer_callable()
        raw = _make_24h_raw_dict("yes")
        intervals = normalize(raw, is_emergency=False)

        self.assertEqual(len(intervals), 48, "Must produce exactly 48 intervals")
        for idx, item in enumerate(intervals):
            status = _extract_status_str(item)
            self.assertEqual(status, "ON", f"Interval {idx} must be ON for 'yes'")

    def test_raw_status_no_maps_to_off_off(self) -> None:
        """
        'no' -> Definite Scheduled Outage for entire hour.
        Must map to First Half (:00-:30) = OFF, Second Half (:30-:00) = OFF.
        Full 24-hour day must yield exactly 48 OFF intervals.
        """
        normalize = _get_normalizer_callable()
        raw = _make_24h_raw_dict("no")
        intervals = normalize(raw, is_emergency=False)

        self.assertEqual(len(intervals), 48, "Must produce exactly 48 intervals")
        for idx, item in enumerate(intervals):
            status = _extract_status_str(item)
            self.assertEqual(status, "OFF", f"Interval {idx} must be OFF for 'no'")

    def test_raw_status_first_maps_to_off_on(self) -> None:
        """
        'first' -> Outage First Half-Hour, Restored Second Half-Hour.
        Must map to First Half (:00-:30) = OFF, Second Half (:30-:00) = ON.
        """
        normalize = _get_normalizer_callable()
        raw = _make_24h_raw_dict("first")
        intervals = normalize(raw, is_emergency=False)

        self.assertEqual(len(intervals), 48)
        for h in range(1, 25):
            idx_first = (h - 1) * 2
            idx_second = idx_first + 1
            status_first = _extract_status_str(intervals[idx_first])
            status_second = _extract_status_str(intervals[idx_second])
            self.assertEqual(status_first, "OFF", f"Hour {h} first half (:00-:30) must be OFF")
            self.assertEqual(status_second, "ON", f"Hour {h} second half (:30-:00) must be ON")

    def test_raw_status_second_maps_to_on_off(self) -> None:
        """
        'second' -> Power First Half-Hour, Outage Second Half-Hour.
        Must map to First Half (:00-:30) = ON, Second Half (:30-:00) = OFF.
        """
        normalize = _get_normalizer_callable()
        raw = _make_24h_raw_dict("second")
        intervals = normalize(raw, is_emergency=False)

        self.assertEqual(len(intervals), 48)
        for h in range(1, 25):
            idx_first = (h - 1) * 2
            idx_second = idx_first + 1
            status_first = _extract_status_str(intervals[idx_first])
            status_second = _extract_status_str(intervals[idx_second])
            self.assertEqual(status_first, "ON", f"Hour {h} first half (:00-:30) must be ON")
            self.assertEqual(status_second, "OFF", f"Hour {h} second half (:30-:00) must be OFF")

    def test_raw_status_maybe_maps_to_maybe_maybe(self) -> None:
        """
        'maybe' -> Discretionary Grey Zone (Possible Outage) for entire hour.
        Must map to First Half (:00-:30) = MAYBE, Second Half (:30-:00) = MAYBE.
        """
        normalize = _get_normalizer_callable()
        raw = _make_24h_raw_dict("maybe")
        intervals = normalize(raw, is_emergency=False)

        self.assertEqual(len(intervals), 48)
        for idx, item in enumerate(intervals):
            status = _extract_status_str(item)
            self.assertEqual(status, "MAYBE", f"Interval {idx} must be MAYBE for 'maybe'")

    def test_raw_status_mfirst_maps_to_maybe_on(self) -> None:
        """
        'mfirst' -> Possible Outage First Half-Hour, Power Second Half-Hour.
        Must map to First Half (:00-:30) = MAYBE, Second Half (:30-:00) = ON.
        """
        normalize = _get_normalizer_callable()
        raw = _make_24h_raw_dict("mfirst")
        intervals = normalize(raw, is_emergency=False)

        self.assertEqual(len(intervals), 48)
        for h in range(1, 25):
            idx_first = (h - 1) * 2
            idx_second = idx_first + 1
            status_first = _extract_status_str(intervals[idx_first])
            status_second = _extract_status_str(intervals[idx_second])
            self.assertEqual(status_first, "MAYBE", f"Hour {h} first half (:00-:30) must be MAYBE")
            self.assertEqual(status_second, "ON", f"Hour {h} second half (:30-:00) must be ON")

    def test_raw_status_msecond_maps_to_on_maybe(self) -> None:
        """
        'msecond' -> Power First Half-Hour, Possible Outage Second Half-Hour.
        Must map to First Half (:00-:30) = ON, Second Half (:30-:00) = MAYBE.
        """
        normalize = _get_normalizer_callable()
        raw = _make_24h_raw_dict("msecond")
        intervals = normalize(raw, is_emergency=False)

        self.assertEqual(len(intervals), 48)
        for h in range(1, 25):
            idx_first = (h - 1) * 2
            idx_second = idx_first + 1
            status_first = _extract_status_str(intervals[idx_first])
            status_second = _extract_status_str(intervals[idx_second])
            self.assertEqual(status_first, "ON", f"Hour {h} first half (:00-:30) must be ON")
            self.assertEqual(status_second, "MAYBE", f"Hour {h} second half (:30-:00) must be MAYBE")

    def test_composite_schedule_all_seven_statuses_order_and_count(self) -> None:
        """
        A composite 24-hour schedule cycling across all 7 statuses:
        Verifies exact 48 intervals, sequential index progression 0..47,
        and accurate half-hour status resolutions.
        """
        normalize = _get_normalizer_callable()
        codes = ["yes", "no", "first", "second", "maybe", "mfirst", "msecond"]
        raw = {str(h): codes[(h - 1) % len(codes)] for h in range(1, 25)}

        intervals = normalize(raw, is_emergency=False)
        self.assertEqual(len(intervals), 48, "Must produce exactly 48 intervals")

        expected_pairs = {
            "yes": ("ON", "ON"),
            "no": ("OFF", "OFF"),
            "first": ("OFF", "ON"),
            "second": ("ON", "OFF"),
            "maybe": ("MAYBE", "MAYBE"),
            "mfirst": ("MAYBE", "ON"),
            "msecond": ("ON", "MAYBE"),
        }

        for h in range(1, 25):
            code = raw[str(h)]
            exp_first, exp_second = expected_pairs[code]
            idx_first = (h - 1) * 2
            idx_second = idx_first + 1

            self.assertEqual(
                _extract_status_str(intervals[idx_first]),
                exp_first,
                f"Hour {h} (:00-:30) expected {exp_first}",
            )
            self.assertEqual(
                _extract_status_str(intervals[idx_second]),
                exp_second,
                f"Hour {h} (:30-:00) expected {exp_second}",
            )


class TestNormalizerTier2(unittest.TestCase):
    """
    Tier 2: Boundary Intervals, Unknown/Missing Statuses, Emergency Overrides, Malformed Inputs.
    """

    def test_boundary_interval_start_of_day_index_0(self) -> None:
        """
        Index 0 represents 00:00 - 00:30 (first half of hour 1).
        Verify start and end times if structured object, and status mapping.
        """
        normalize = _get_normalizer_callable()
        raw = _make_24h_raw_dict("yes")
        raw["1"] = "first"  # Hour 1: OFF (:00-:30), ON (:30-:00)
        intervals = normalize(raw, is_emergency=False)

        first_slot = intervals[0]
        self.assertEqual(_extract_status_str(first_slot), "OFF")

        if hasattr(first_slot, "slot_index"):
            self.assertEqual(first_slot.slot_index, 0)
        if hasattr(first_slot, "start_time"):
            self.assertEqual(first_slot.start_time, "00:00")
        if hasattr(first_slot, "end_time"):
            self.assertEqual(first_slot.end_time, "00:30")

    def test_boundary_interval_end_of_day_index_47(self) -> None:
        """
        Index 47 represents 23:30 - 24:00 (second half of hour 24).
        Verify start and end times if structured object, and status mapping.
        """
        normalize = _get_normalizer_callable()
        raw = _make_24h_raw_dict("yes")
        raw["24"] = "second"  # Hour 24: ON (:00-:30), OFF (:30-:00)
        intervals = normalize(raw, is_emergency=False)

        last_slot = intervals[47]
        self.assertEqual(_extract_status_str(last_slot), "OFF")

        if hasattr(last_slot, "slot_index"):
            self.assertEqual(last_slot.slot_index, 47)
        if hasattr(last_slot, "start_time"):
            self.assertEqual(last_slot.start_time, "23:30")
        if hasattr(last_slot, "end_time"):
            self.assertIn(last_slot.end_time, ("24:00", "00:00"))

    def test_boundary_intervals_noon_split_indices_23_and_24(self) -> None:
        """
        Index 23 represents 11:30 - 12:00 (hour 12 second half).
        Index 24 represents 12:00 - 12:30 (hour 13 first half).
        Mid-day boundary transition test.
        """
        normalize = _get_normalizer_callable()
        raw = _make_24h_raw_dict("yes")
        raw["12"] = "second"  # 11:00-11:30 ON, 11:30-12:00 OFF -> idx 22=ON, idx 23=OFF
        raw["13"] = "first"   # 12:00-12:30 OFF, 12:30-13:00 ON -> idx 24=OFF, idx 25=ON

        intervals = normalize(raw, is_emergency=False)
        self.assertEqual(_extract_status_str(intervals[23]), "OFF", "Index 23 (11:30-12:00) must be OFF")
        self.assertEqual(_extract_status_str(intervals[24]), "OFF", "Index 24 (12:00-12:30) must be OFF")

        if hasattr(intervals[23], "start_time"):
            self.assertEqual(intervals[23].start_time, "11:30")
            self.assertEqual(intervals[23].end_time, "12:00")
        if hasattr(intervals[24], "start_time"):
            self.assertEqual(intervals[24].start_time, "12:00")
            self.assertEqual(intervals[24].end_time, "12:30")

    def test_unknown_and_unrecognized_statuses_default_to_maybe(self) -> None:
        """
        RFC 2119 / SPEC constraint:
        Any unknown, unrecognized, or corrupted status code (e.g. 'unknown', 'invalid',
        'foo', '', None) MUST default both half-hours to MAYBE.
        """
        normalize = _get_normalizer_callable()
        raw = _make_24h_raw_dict("yes")
        raw["5"] = "unknown"
        raw["6"] = "invalid_status"
        raw["7"] = ""
        raw["8"] = None  # type: ignore[assignment]

        intervals = normalize(raw, is_emergency=False)

        for h in (5, 6, 7, 8):
            idx_1 = (h - 1) * 2
            idx_2 = idx_1 + 1
            self.assertEqual(
                _extract_status_str(intervals[idx_1]),
                "MAYBE",
                f"Hour {h} first half should default to MAYBE for unknown status",
            )
            self.assertEqual(
                _extract_status_str(intervals[idx_2]),
                "MAYBE",
                f"Hour {h} second half should default to MAYBE for unknown status",
            )

    def test_missing_hour_keys_default_to_maybe(self) -> None:
        """
        If raw dictionary omits certain hour keys (e.g. only hours 1..12 provided,
        hours 13..24 omitted), the resulting intervals for the omitted hours must
        gracefully default to MAYBE, maintaining exactly 48 total intervals.
        """
        normalize = _get_normalizer_callable()
        partial_raw = {str(h): "yes" for h in range(1, 13)}  # only first 12 hours

        intervals = normalize(partial_raw, is_emergency=False)
        self.assertEqual(len(intervals), 48, "Must always produce exactly 48 intervals")

        # First 24 intervals should be ON
        for i in range(24):
            self.assertEqual(_extract_status_str(intervals[i]), "ON", f"Interval {i} must be ON")
        # Remaining 24 intervals (hours 13..24) must default to MAYBE
        for i in range(24, 48):
            self.assertEqual(
                _extract_status_str(intervals[i]),
                "MAYBE",
                f"Interval {i} (omitted hour) must default to MAYBE",
            )

    def test_emergency_override_mode_all_48_intervals_emergency(self) -> None:
        """
        Emergency Shutdown Rule:
        When is_emergency=True, ALL 48 intervals across all hours MUST be assigned
        status EMERGENCY, regardless of what the scheduled raw values were.
        """
        normalize = _get_normalizer_callable()
        raw = _make_24h_raw_dict("yes")  # All normally ON

        intervals = normalize(raw, is_emergency=True)
        self.assertEqual(len(intervals), 48)

        for idx, item in enumerate(intervals):
            status = _extract_status_str(item)
            self.assertEqual(
                status,
                "EMERGENCY",
                f"Interval {idx} must be EMERGENCY when is_emergency=True",
            )

    def test_emergency_override_precedence_over_all_guaranteed_power(self) -> None:
        """
        Ukrenergo Emergency Order Precedence:
        Verify that an emergency flag strictly overrides a schedule containing mixed
        guaranteed power ('yes'), half-hour power ('first', 'second'), and blackouts ('no').
        """
        normalize = _get_normalizer_callable()
        mixed = {
            "1": "yes", "2": "first", "3": "second", "4": "no",
            "5": "maybe", "6": "mfirst", "7": "msecond",
        }
        for h in range(8, 25):
            mixed[str(h)] = "yes"

        intervals = normalize(mixed, is_emergency=True)
        self.assertEqual(len(intervals), 48)

        for idx in range(48):
            self.assertEqual(
                _extract_status_str(intervals[idx]),
                "EMERGENCY",
                f"Emergency status must override scheduled status at index {idx}",
            )

    def test_malformed_input_empty_dict(self) -> None:
        """
        An empty dictionary {} passed to the normalizer must not raise an unhandled
        exception. It should produce 48 intervals all defaulting to MAYBE.
        """
        normalize = _get_normalizer_callable()
        intervals = normalize({}, is_emergency=False)
        self.assertEqual(len(intervals), 48)
        for idx, item in enumerate(intervals):
            self.assertEqual(_extract_status_str(item), "MAYBE")

    def test_malformed_input_integer_keys_normalization(self) -> None:
        """
        Upstream DTEK JSON or mirrors sometimes return integer keys {1: 'yes', 2: 'no'}
        instead of string keys {'1': 'yes', '2': 'no'}.
        Normalizer must handle or coerce integer keys robustly.
        """
        normalize = _get_normalizer_callable()
        int_keys_raw = {h: "yes" if h % 2 == 1 else "no" for h in range(1, 25)}

        intervals = normalize(int_keys_raw, is_emergency=False)
        self.assertEqual(len(intervals), 48)

        for h in range(1, 25):
            exp = "ON" if h % 2 == 1 else "OFF"
            idx_1 = (h - 1) * 2
            idx_2 = idx_1 + 1
            self.assertEqual(_extract_status_str(intervals[idx_1]), exp)
            self.assertEqual(_extract_status_str(intervals[idx_2]), exp)

    def test_whitespace_and_case_insensitivity(self) -> None:
        """
        Status strings with accidental whitespace or unexpected casing
        (' YES ', 'No\n', 'First', '  mFirst  ') must be parsed correctly.
        """
        normalize = _get_normalizer_callable()
        raw = _make_24h_raw_dict("yes")
        raw["1"] = "  YES  "
        raw["2"] = "No\n"
        raw["3"] = "First"
        raw["4"] = "mFirst"

        intervals = normalize(raw, is_emergency=False)

        # Hour 1: YES -> ON, ON
        self.assertEqual(_extract_status_str(intervals[0]), "ON")
        self.assertEqual(_extract_status_str(intervals[1]), "ON")
        # Hour 2: No -> OFF, OFF
        self.assertEqual(_extract_status_str(intervals[2]), "OFF")
        self.assertEqual(_extract_status_str(intervals[3]), "OFF")
        # Hour 3: First -> OFF, ON
        self.assertEqual(_extract_status_str(intervals[4]), "OFF")
        self.assertEqual(_extract_status_str(intervals[5]), "ON")
        # Hour 4: mFirst -> MAYBE, ON
        self.assertEqual(_extract_status_str(intervals[6]), "MAYBE")
        self.assertEqual(_extract_status_str(intervals[7]), "ON")


if __name__ == "__main__":
    unittest.main()
