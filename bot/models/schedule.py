"""Domain models for DTEK electricity outage schedules and comparison timelines."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Union

try:
    from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
    _PYDANTIC_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PYDANTIC_AVAILABLE = False
    BaseModel = object  # type: ignore
    ConfigDict = dict  # type: ignore
    ValidationError = ValueError  # type: ignore

    def Field(*args: Any, **kwargs: Any) -> Any:  # type: ignore
        return kwargs.get("default", None)

    def field_validator(*args: Any, **kwargs: Any) -> Any:  # type: ignore
        def decorator(fn: Any) -> Any:
            return fn
        return decorator

from bot.models.enums import DSORegion, IntervalStatus, JointStatus, SlotStatus


if not _PYDANTIC_AVAILABLE:  # pragma: no cover
    class _BaseModelCompat:
        """Pydantic v2 BaseModel compatibility implementation for offline execution."""

        def __init__(self, **data: Any) -> None:
            annotations = getattr(self.__class__, "__annotations__", {})
            for key, val in data.items():
                setattr(self, key, val)
            for key in annotations:
                if key not in data and hasattr(self.__class__, key):
                    setattr(self, key, getattr(self.__class__, key))
            self._validate()

        def _validate(self) -> None:
            pass

        def model_dump(self, mode: str = "python") -> Dict[str, Any]:
            res: Dict[str, Any] = {}
            for k, v in self.__dict__.items():
                if k.startswith("_"):
                    continue
                if hasattr(v, "model_dump"):
                    res[k] = v.model_dump(mode=mode)
                elif isinstance(v, list):
                    res[k] = [item.model_dump(mode=mode) if hasattr(item, "model_dump") else (item.value if hasattr(item, "value") else item) for item in v]
                elif isinstance(v, dict):
                    res[k] = {dk: (dv.model_dump(mode=mode) if hasattr(dv, "model_dump") else (dv.value if hasattr(dv, "value") else dv)) for dk, dv in v.items()}
                elif hasattr(v, "value"):
                    res[k] = v.value
                else:
                    res[k] = v
            return res

        def model_dump_json(self) -> str:
            return json.dumps(self.model_dump(mode="json"))

        @classmethod
        def model_validate(cls, obj: Any) -> Any:
            if isinstance(obj, cls):
                return obj
            if isinstance(obj, dict):
                return cls(**obj)
            raise ValueError(f"Cannot validate {type(obj)} as {cls.__name__}")

        def __repr__(self) -> str:
            fields = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items() if not k.startswith("_"))
            return f"{self.__class__.__name__}({fields})"

        def __eq__(self, other: Any) -> bool:
            if isinstance(other, self.__class__):
                return self.__dict__ == other.__dict__
            return False

    _BaseClass = _BaseModelCompat
else:
    _BaseClass = BaseModel  # type: ignore


class SlotConfig(_BaseClass):
    """User configuration for a monitored location slot (Slot 1 or Slot 2)."""

    if _PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)

    slot_number: int = Field(..., ge=1, le=2, description="Slot index (1 or 2)")
    alias: str = Field(default="Моя черга", max_length=64, description="User label (Дім, Офіс)")
    region_code: Union[DSORegion, str] = Field(..., description="DSO region identifier")
    region_name: Optional[str] = Field(default=None, description="Human readable region name")
    group_id: int = Field(..., ge=1, le=6, description="DTEK group 1 to 6")
    subgroup_id: str = Field(..., description="Subgroup ID (e.g. '1.1', '3.2')")

    def _validate(self) -> None:
        if not (1 <= self.slot_number <= 2):
            raise ValueError(f"slot_number must be 1 or 2, got {self.slot_number}")
        if not (1 <= self.group_id <= 6):
            raise ValueError(f"group_id must be between 1 and 6, got {self.group_id}")
        if not re.match(r"^[1-6]\.[1-2]$", self.subgroup_id):
            raise ValueError(f"subgroup_id must match 'X.Y' (1-6, 1-2), got {self.subgroup_id}")


class TimelineInterval(_BaseClass):
    """Single 30-minute schedule interval representation (0 to 47)."""

    if _PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)

    slot_index: int = Field(..., ge=0, le=47, description="0 to 47 interval index")
    start_time: str = Field(..., description="HH:MM start time, e.g. '14:00'")
    end_time: str = Field(..., description="HH:MM end time, e.g. '14:30' or '24:00'")
    status: IntervalStatus = Field(..., description="Interval power status")

    def __init__(self, **data: Any) -> None:
        if "status" in data and isinstance(data["status"], str):
            data["status"] = IntervalStatus(data["status"])
        super().__init__(**data)

    def _validate(self) -> None:
        if not (0 <= self.slot_index <= 47):
            raise ValueError(f"slot_index must be between 0 and 47, got {self.slot_index}")
        if not isinstance(self.status, IntervalStatus):
            self.status = IntervalStatus(self.status)


# Specification compatibility alias
RawScheduleInterval = TimelineInterval


class SubgroupSchedule(_BaseClass):
    """48-slot outage schedule for a specific DTEK group/subgroup."""

    group_id: int = Field(..., ge=1, le=6, description="DTEK group 1 to 6")
    subgroup_id: str = Field(..., description="e.g. '1.1', '3.2'")
    slots: List[TimelineInterval] = Field(..., min_length=48, max_length=48, description="Exactly 48 intervals")

    def __init__(self, **data: Any) -> None:
        if "slots" in data and isinstance(data["slots"], list):
            data["slots"] = [
                item if isinstance(item, TimelineInterval) else TimelineInterval(**item)
                for item in data["slots"]
            ]
        super().__init__(**data)

    def _validate(self) -> None:
        if not (1 <= self.group_id <= 6):
            raise ValueError(f"group_id must be between 1 and 6, got {self.group_id}")
        if len(self.slots) != 48:
            raise ValueError(f"slots must contain exactly 48 items, got {len(self.slots)}")


class DailySchedule(_BaseClass):
    """Normalized schedule data carrier between Scraper Cascade and Comparison Engine."""

    region: DSORegion = Field(..., description="DSO region")
    group: str = Field(..., description="Subgroup identifier, e.g. 'GPV1.1' or '1.1'")
    date: Optional[str] = Field(default=None, description="Schedule date YYYY-MM-DD")
    intervals: Union[List[IntervalStatus], List[TimelineInterval]] = Field(
        ..., min_length=48, max_length=48, description="48 normalized intervals"
    )
    tier_used: str = Field(default="direct_scrape", description="direct_scrape, github_mirror, yasno_api, cache, mock")
    is_stale: bool = Field(default=False)
    schedule_hash: Optional[str] = Field(default=None)
    hash: Optional[str] = Field(default=None)

    def __init__(self, **data: Any) -> None:
        if "region" in data and isinstance(data["region"], str):
            data["region"] = DSORegion(data["region"])
        # Support both 'hash' and 'schedule_hash' interchangeably
        h = data.get("hash") or data.get("schedule_hash")
        data["hash"] = h
        data["schedule_hash"] = h
        super().__init__(**data)

    def _validate(self) -> None:
        if len(self.intervals) != 48:
            raise ValueError(f"intervals must contain exactly 48 items, got {len(self.intervals)}")


class JointInterval(_BaseClass):
    """Point-in-time comparison interval displaying Slot A, Slot B, and Joint status."""

    if _PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)

    slot_index: int = Field(..., ge=0, le=47)
    start_time: str = Field(...)
    end_time: str = Field(...)
    status_a: IntervalStatus = Field(...)
    status_b: Optional[IntervalStatus] = Field(default=None)
    joint_status: JointStatus = Field(...)

    def __init__(self, **data: Any) -> None:
        if "status_a" in data and isinstance(data["status_a"], str):
            data["status_a"] = IntervalStatus(data["status_a"])
        if "status_b" in data and isinstance(data["status_b"], str):
            data["status_b"] = IntervalStatus(data["status_b"])
        if "joint_status" in data and isinstance(data["joint_status"], str):
            data["joint_status"] = JointStatus(data["joint_status"])
        super().__init__(**data)

    def _validate(self) -> None:
        if not (0 <= self.slot_index <= 47):
            raise ValueError(f"slot_index must be 0..47, got {self.slot_index}")


class TimelineSummary(_BaseClass):
    """Daily aggregate summary metrics and current state recommendation."""

    both_on_hours: float = Field(default=0.0)
    group1_only_hours: float = Field(default=0.0)
    group2_only_hours: float = Field(default=0.0)
    both_off_hours: float = Field(default=0.0)
    uncertain_hours: float = Field(default=0.0)
    current_slot_index: int = Field(default=0, ge=0, le=47)
    current_joint_status: JointStatus = Field(...)
    next_transition_time: Optional[str] = Field(default=None)
    minutes_to_next_transition: Optional[int] = Field(default=None)
    smart_recommendation: str = Field(default="")

    def __init__(self, **data: Any) -> None:
        if "current_joint_status" in data and isinstance(data["current_joint_status"], str):
            data["current_joint_status"] = JointStatus(data["current_joint_status"])
        super().__init__(**data)


class TwoGroupTimelineResponse(_BaseClass):
    """Full unified timeline comparison response for chat renderers and TMA payload bridge."""

    comparison_date: str = Field(..., description="YYYY-MM-DD")
    is_emergency: bool = Field(default=False)
    emergency_message: Optional[str] = Field(default=None)
    stale: bool = Field(default=False)
    slot_a: SlotConfig = Field(...)
    slot_b: Optional[SlotConfig] = Field(default=None)
    summary: TimelineSummary = Field(...)
    timeline: List[JointInterval] = Field(..., min_length=48, max_length=48)

    def __init__(self, **data: Any) -> None:
        if "slot_a" in data and isinstance(data["slot_a"], dict):
            data["slot_a"] = SlotConfig(**data["slot_a"])
        if "slot_b" in data and isinstance(data["slot_b"], dict):
            data["slot_b"] = SlotConfig(**data["slot_b"])
        if "summary" in data and isinstance(data["summary"], dict):
            data["summary"] = TimelineSummary(**data["summary"])
        if "timeline" in data and isinstance(data["timeline"], list):
            data["timeline"] = [
                item if isinstance(item, JointInterval) else JointInterval(**item)
                for item in data["timeline"]
            ]
        super().__init__(**data)

    def _validate(self) -> None:
        if len(self.timeline) != 48:
            raise ValueError(f"timeline must contain exactly 48 intervals, got {len(self.timeline)}")


class RegionalScheduleResponse(_BaseClass):
    """Complete daily schedule payload for a DSO region across all its subgroups."""

    region_id: str = Field(..., description="'kyiv', 'kyiv-region', 'dnipro', 'odesa'")
    region_name: str = Field(...)
    date: str = Field(..., description="YYYY-MM-DD")
    updated_at: str = Field(..., description="ISO 8601 string")
    is_emergency: bool = Field(default=False)
    emergency_message: Optional[str] = Field(default=None)
    data_source: str = Field(default="mock", description="direct_scrape, github_mirror, yasno_api, cache, mock")
    stale: bool = Field(default=False)
    groups: Dict[str, SubgroupSchedule] = Field(...)

    def __init__(self, **data: Any) -> None:
        if "groups" in data and isinstance(data["groups"], dict):
            parsed_groups: Dict[str, SubgroupSchedule] = {}
            for k, v in data["groups"].items():
                if isinstance(v, SubgroupSchedule):
                    parsed_groups[k] = v
                elif isinstance(v, dict):
                    parsed_groups[k] = SubgroupSchedule(**v)
            data["groups"] = parsed_groups
        super().__init__(**data)
