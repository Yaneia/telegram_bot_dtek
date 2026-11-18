"""
bot/services - Core business logic services.

Provides schedule normalization, 5-tier scraper cascade, circuit breaker,
token-bucket rate limiter, and background outage notifier.
"""

from bot.services.normalizer import (
    DEFAULT_PAIR,
    EMERGENCY_PAIR,
    Normalizer,
    NormalizerInterval,
    TRUTH_TABLE,
    format_interval_times,
    normalize,
    normalize_intervals,
    normalize_raw_schedule,
    normalize_schedule,
)
from bot.services.notifier import (
    NotifierService,
    compute_schedule_sha256,
    detect_advance_outage_transition,
    detect_schedule_revision_diffs,
    format_advance_warning_message,
    format_schedule_diff_message,
    get_slot_index_from_datetime,
    get_slot_time_bounds,
    is_in_silent_hours,
)
from bot.services.rate_limiter import TokenBucketRateLimiter
from bot.services.scraper import (
    CircuitBreaker,
    DTEKScraper,
    DTEKScraperCascade,
    EMERGENCY_BANNER_REGEX,
    FACT_REGEX,
    PRESET_REGEX,
    ScraperCascade,
    ScraperService,
)

__all__ = [
    "Normalizer",
    "NormalizerInterval",
    "TRUTH_TABLE",
    "DEFAULT_PAIR",
    "EMERGENCY_PAIR",
    "normalize",
    "normalize_intervals",
    "normalize_schedule",
    "normalize_raw_schedule",
    "format_interval_times",
    "CircuitBreaker",
    "ScraperCascade",
    "DTEKScraperCascade",
    "DTEKScraper",
    "ScraperService",
    "FACT_REGEX",
    "PRESET_REGEX",
    "EMERGENCY_BANNER_REGEX",
    "TokenBucketRateLimiter",
    "NotifierService",
    "get_slot_index_from_datetime",
    "get_slot_time_bounds",
    "detect_advance_outage_transition",
    "format_advance_warning_message",
    "format_schedule_diff_message",
    "compute_schedule_sha256",
    "detect_schedule_revision_diffs",
    "is_in_silent_hours",
]
