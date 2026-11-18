"""Timeline Service Re-export Proxy.

Re-exports all comparison and timeline symbols from bot.services.comparison.
"""

from __future__ import annotations

from bot.services.comparison import (
    COLOR_MAP,
    COMBINATORIAL_MATRIX,
    ComparisonEngine,
    ComparisonJointInterval,
    ComparisonSlotConfig,
    ComparisonTimelineSummary,
    EXPECTED_COMBINATORIAL_MATRIX,
    KYIV_TZ,
    LABEL_MAP,
    STATUS_COLORS,
    STATUS_LABELS,
    TimelineEngine,
    TimelineResponse,
    UnifiedTimelineEngine,
    calculate_next_transition,
    evaluate_joint_status,
    evaluate_pair,
    find_next_transition,
    get_current_slot_index,
    get_joint_status,
    resolve_joint_status,
    slot_index_for_time,
)

__all__ = [
    "COLOR_MAP",
    "COMBINATORIAL_MATRIX",
    "ComparisonEngine",
    "ComparisonJointInterval",
    "ComparisonSlotConfig",
    "ComparisonTimelineSummary",
    "EXPECTED_COMBINATORIAL_MATRIX",
    "KYIV_TZ",
    "LABEL_MAP",
    "STATUS_COLORS",
    "STATUS_LABELS",
    "TimelineEngine",
    "TimelineResponse",
    "UnifiedTimelineEngine",
    "calculate_next_transition",
    "evaluate_joint_status",
    "evaluate_pair",
    "find_next_transition",
    "get_current_slot_index",
    "get_joint_status",
    "resolve_joint_status",
    "slot_index_for_time",
]
