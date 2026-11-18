"""Domain models and enumerations for Telegram Bot DTEK."""

from bot.models.enums import (
    DSO_PORTAL_URLS,
    GITHUB_MIRROR_FILENAMES,
    JOINT_STATUS_COLORS,
    JOINT_STATUS_LABELS,
    REGION_NAMES,
    REGION_SHORT_NAMES,
    SINGLE_SLOT_COLORS,
    YASNO_API_URLS,
    DSORegion,
    IntervalStatus,
    JointStatus,
    JointTimelineStatus,
    SlotStatus,
)
from bot.models.schedule import (
    DailySchedule,
    JointInterval,
    RawScheduleInterval,
    RegionalScheduleResponse,
    SlotConfig,
    SubgroupSchedule,
    TimelineInterval,
    TimelineSummary,
    TwoGroupTimelineResponse,
)

__all__ = [
    # Enums & Aliases
    "IntervalStatus",
    "SlotStatus",
    "JointStatus",
    "JointTimelineStatus",
    "DSORegion",
    # Metadata Dictionaries
    "JOINT_STATUS_COLORS",
    "JOINT_STATUS_LABELS",
    "SINGLE_SLOT_COLORS",
    "REGION_NAMES",
    "REGION_SHORT_NAMES",
    "DSO_PORTAL_URLS",
    "YASNO_API_URLS",
    "GITHUB_MIRROR_FILENAMES",
    # Domain Models
    "SlotConfig",
    "TimelineInterval",
    "RawScheduleInterval",
    "SubgroupSchedule",
    "DailySchedule",
    "JointInterval",
    "TimelineSummary",
    "TwoGroupTimelineResponse",
    "RegionalScheduleResponse",
]
