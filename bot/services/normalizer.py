"""
bot/services/normalizer.py - Authoritative Outage Schedule Normalizer.

Transforms 24-hour raw DTEK hourly status strings into 48 uniform 30-minute
TimelineInterval domain models covering 00:00 to 24:00 Europe/Kyiv.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union

from bot.models.enums import IntervalStatus, SlotStatus
from bot.models.schedule import SubgroupSchedule, TimelineInterval


# Authoritative 7-status DTEK truth table mapping hourly raw code
# to (first_half_hour, second_half_hour)
TRUTH_TABLE: Dict[str, Tuple[IntervalStatus, IntervalStatus]] = {
    "yes": (IntervalStatus.ON, IntervalStatus.ON),
    "no": (IntervalStatus.OFF, IntervalStatus.OFF),
    "first": (IntervalStatus.OFF, IntervalStatus.ON),
    "second": (IntervalStatus.ON, IntervalStatus.OFF),
    "maybe": (IntervalStatus.MAYBE, IntervalStatus.MAYBE),
    "mfirst": (IntervalStatus.MAYBE, IntervalStatus.ON),
    "msecond": (IntervalStatus.ON, IntervalStatus.MAYBE),
}

DEFAULT_PAIR: Tuple[IntervalStatus, IntervalStatus] = (IntervalStatus.MAYBE, IntervalStatus.MAYBE)
EMERGENCY_PAIR: Tuple[IntervalStatus, IntervalStatus] = (IntervalStatus.EMERGENCY, IntervalStatus.EMERGENCY)


class NormalizerInterval(TimelineInterval):
    """30-minute schedule interval supporting both attribute and dict-key access."""

    def __getitem__(self, item: str) -> Any:
        val = getattr(self, item)
        if hasattr(val, "value"):
            return val.value
        return val

    def get(self, item: str, default: Any = None) -> Any:
        try:
            return self[item]
        except (AttributeError, KeyError):
            return default


class Normalizer:
    """Outage schedule normalization engine for Ukrainian electricity grids."""

    @staticmethod
    def format_interval_times(slot_index: int) -> Tuple[str, str]:
        """
        Convert slot index (0..47) to start_time and end_time strings.

        Index 0: ("00:00", "00:30")
        Index 23: ("11:30", "12:00")
        Index 24: ("12:00", "12:30")
        Index 47: ("23:30", "24:00")
        """
        if not (0 <= slot_index <= 47):
            raise ValueError(f"slot_index must be between 0 and 47, got {slot_index}")

        start_min = slot_index * 30
        end_min = (slot_index + 1) * 30

        start_h, start_m = divmod(start_min, 60)
        end_h, end_m = divmod(end_min, 60)

        start_time = f"{start_h:02d}:{start_m:02d}"
        end_time = "24:00" if end_min == 1440 else f"{end_h:02d}:{end_m:02d}"
        return start_time, end_time

    @staticmethod
    def _parse_hour_raw(
        raw_intervals: Dict[Any, Any],
        hour: int,
    ) -> Optional[str]:
        """Extract and clean raw status string for a given hour (1..24)."""
        val = raw_intervals.get(str(hour))
        if val is None:
            val = raw_intervals.get(hour)
        if val is None:
            # Check for whitespace-padded keys or int-as-string keys
            for k, v in raw_intervals.items():
                if str(k).strip() == str(hour):
                    val = v
                    break

        if val is None or not isinstance(val, (str, bytes)):
            return None

        cleaned = str(val).strip().lower()
        return cleaned if cleaned else None

    @classmethod
    def normalize(
        cls,
        raw_intervals: Optional[Dict[Any, Any]],
        is_emergency: bool = False,
    ) -> List[NormalizerInterval]:
        """
        Normalize raw 24-hour DTEK schedule dictionary into 48 30-minute NormalizerIntervals.

        Args:
            raw_intervals: Dictionary mapping hour ("1".."24" or 1..24) to DTEK status string.
            is_emergency: If True, unconditionally overrides all 48 intervals to EMERGENCY.

        Returns:
            Exactly 48 NormalizerInterval objects covering 00:00 to 24:00 Kyiv time.
        """
        safe_raw: Dict[Any, Any] = raw_intervals if isinstance(raw_intervals, dict) else {}
        is_em = bool(is_emergency)

        intervals: List[NormalizerInterval] = []

        for hour in range(1, 25):
            slot_idx_1 = (hour - 1) * 2
            slot_idx_2 = slot_idx_1 + 1

            start_1, end_1 = cls.format_interval_times(slot_idx_1)
            start_2, end_2 = cls.format_interval_times(slot_idx_2)

            if is_em:
                status_1, status_2 = EMERGENCY_PAIR
            else:
                raw_code = cls._parse_hour_raw(safe_raw, hour)
                if raw_code is None:
                    status_1, status_2 = DEFAULT_PAIR
                else:
                    status_1, status_2 = TRUTH_TABLE.get(raw_code, DEFAULT_PAIR)

            intervals.append(
                NormalizerInterval(
                    slot_index=slot_idx_1,
                    start_time=start_1,
                    end_time=end_1,
                    status=status_1,
                )
            )
            intervals.append(
                NormalizerInterval(
                    slot_index=slot_idx_2,
                    start_time=start_2,
                    end_time=end_2,
                    status=status_2,
                )
            )

        return intervals

    @classmethod
    def normalize_statuses(
        cls,
        raw_intervals: Optional[Dict[Any, Any]],
        is_emergency: bool = False,
    ) -> List[IntervalStatus]:
        """Return pure List[IntervalStatus] of length 48."""
        intervals = cls.normalize(raw_intervals, is_emergency=is_emergency)
        return [interval.status for interval in intervals]

    @classmethod
    def normalize_subgroup(
        cls,
        group_id: int,
        subgroup_id: str,
        raw_intervals: Optional[Dict[Any, Any]],
        is_emergency: bool = False,
    ) -> SubgroupSchedule:
        """Construct full SubgroupSchedule model from raw hourly data."""
        slots = cls.normalize(raw_intervals, is_emergency=is_emergency)
        return SubgroupSchedule(
            group_id=group_id,
            subgroup_id=subgroup_id,
            slots=slots,
        )


# Module-level convenience functions and aliases
normalize = Normalizer.normalize
normalize_intervals = Normalizer.normalize
normalize_schedule = Normalizer.normalize
normalize_raw_schedule = Normalizer.normalize
format_interval_times = Normalizer.format_interval_times
