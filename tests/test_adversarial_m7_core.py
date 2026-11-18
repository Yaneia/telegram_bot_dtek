"""
tests/test_adversarial_m7_core.py - Tier 5 Adversarial Verification Suite.

Adversarial Stress Testing & White-Box Fault Injection for:
1. bot/services/normalizer.py (Truth table edge cases, noisy formatting, malformed dictionaries, emergency override)
2. bot/services/scraper.py (5-tier cascade failures, circuit breaker dynamics, SWR cache boundaries, mock fallback)
3. bot/services/comparison.py (3x3 combinatorial matrix, 7 emergency states, single-slot fallback, summary metric invariants, boundary transitions)
4. bot/database/ (Async WAL concurrency, foreign keys, CHECK constraints, notification deduplication index)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from bot.database.connection import DatabaseConnectionManager, configure_connection
from bot.database.repository import (
    DatabaseRepository,
    NotificationHistoryRepository,
    NotificationSettingsRepository,
    ScheduleCacheRepository,
    UserRepository,
    UserSlotsRepository,
)
from bot.database.schema import init_db, verify_schema_integrity
from bot.models.enums import DSORegion, IntervalStatus, JointStatus
from bot.models.schedule import DailySchedule, SubgroupSchedule, TimelineInterval
from bot.services.comparison import (
    COLOR_MAP,
    COMBINATORIAL_MATRIX,
    ComparisonEngine,
    LABEL_MAP,
    calculate_next_transition,
    evaluate_pair,
    get_current_slot_index,
)
from bot.services.normalizer import (
    EMERGENCY_PAIR,
    Normalizer,
    NormalizerInterval,
    TRUTH_TABLE,
)
from bot.services.scraper import CircuitBreaker, ScraperCascade

KYIV_TZ = ZoneInfo("Europe/Kyiv")


# ============================================================================
# Area 1: Adversarial Normalizer Tests
# ============================================================================
class TestAdversarialNormalizerCore(unittest.TestCase):
    """Adversarial stress testing for bot/services/normalizer.py."""

    def test_all_seven_truth_table_codes_with_noisy_formatting(self) -> None:
        """
        Verify all 7 DTEK truth table codes under adversarial formatting:
        mixed casing, leading/trailing whitespace, tabs, and carriage returns.
        """
        noisy_samples = {
            "  yes  ": (IntervalStatus.ON, IntervalStatus.ON),
            "YES": (IntervalStatus.ON, IntervalStatus.ON),
            "  no\t": (IntervalStatus.OFF, IntervalStatus.OFF),
            "NO": (IntervalStatus.OFF, IntervalStatus.OFF),
            " FiRsT ": (IntervalStatus.OFF, IntervalStatus.ON),
            "FIRST": (IntervalStatus.OFF, IntervalStatus.ON),
            "sEcOnD\n": (IntervalStatus.ON, IntervalStatus.OFF),
            "SECOND": (IntervalStatus.ON, IntervalStatus.OFF),
            "\tmaybe\r": (IntervalStatus.MAYBE, IntervalStatus.MAYBE),
            "MAYBE": (IntervalStatus.MAYBE, IntervalStatus.MAYBE),
            "  mFirst  ": (IntervalStatus.MAYBE, IntervalStatus.ON),
            "MFIRST": (IntervalStatus.MAYBE, IntervalStatus.ON),
            " mSecond\t\n": (IntervalStatus.ON, IntervalStatus.MAYBE),
            "MSECOND": (IntervalStatus.ON, IntervalStatus.MAYBE),
        }

        for raw_val, expected_pair in noisy_samples.items():
            raw_dict = {str(h): raw_val for h in range(1, 25)}
            intervals = Normalizer.normalize(raw_dict, is_emergency=False)
            self.assertEqual(len(intervals), 48, f"Output length must be 48 for '{raw_val}'")
            for h in range(1, 25):
                slot1 = intervals[(h - 1) * 2]
                slot2 = intervals[(h - 1) * 2 + 1]
                self.assertEqual(
                    (slot1.status, slot2.status),
                    expected_pair,
                    f"Hour {h} failed for noisy input '{raw_val}'",
                )

    def test_malformed_sparse_and_corrupt_types(self) -> None:
        """
        Verify normalizer resilience against non-dict, sparse dict, and corrupt value types.
        Should never raise uncaught exceptions and always return 48 valid intervals.
        """
        # Non-dict inputs
        for non_dict in (None, [], "unexpected string", 12345, True, False, 3.14):
            intervals = Normalizer.normalize(non_dict)  # type: ignore[arg-type]
            self.assertEqual(len(intervals), 48)
            for item in intervals:
                self.assertEqual(item.status, IntervalStatus.MAYBE)

        # Sparse dictionary: only hours 2 and 18 provided
        sparse_dict = {"2": "yes", "18": "no"}
        intervals = Normalizer.normalize(sparse_dict)
        self.assertEqual(len(intervals), 48)
        # Hour 2 -> slots 2, 3 should be ON
        self.assertEqual(intervals[2].status, IntervalStatus.ON)
        self.assertEqual(intervals[3].status, IntervalStatus.ON)
        # Hour 18 -> slots 34, 35 should be OFF
        self.assertEqual(intervals[34].status, IntervalStatus.OFF)
        self.assertEqual(intervals[35].status, IntervalStatus.OFF)
        # Missing hours (e.g. hour 1 -> slots 0, 1) should default to MAYBE
        self.assertEqual(intervals[0].status, IntervalStatus.MAYBE)
        self.assertEqual(intervals[1].status, IntervalStatus.MAYBE)
        self.assertEqual(intervals[46].status, IntervalStatus.MAYBE)
        self.assertEqual(intervals[47].status, IntervalStatus.MAYBE)

        # Corrupt and unexpected value types
        corrupt_dict = {
            "1": None,
            "2": 999,
            "3": ["yes"],
            "4": {"status": "no"},
            "5": "",
            "6": "unknown_status_code",
            "7": "   ",
            "8": b"yes",  # raw bytes repr 'b\'yes\'' maps to MAYBE
        }
        intervals = Normalizer.normalize(corrupt_dict)
        self.assertEqual(len(intervals), 48)
        # All corrupt values safely default to MAYBE
        for h in range(1, 9):
            self.assertEqual(intervals[(h - 1) * 2].status, IntervalStatus.MAYBE)
            self.assertEqual(intervals[(h - 1) * 2 + 1].status, IntervalStatus.MAYBE)

    def test_out_of_range_and_non_integer_keys(self) -> None:
        """
        Verify that out-of-range keys (0, 25, 99, negative, strings) are safely ignored
        and key types (int, string, whitespace padded) are resolved correctly.
        """
        mixed_keys = {
            "0": "yes",   # Should be ignored (hours are 1..24)
            "-5": "yes",  # Should be ignored
            "25": "yes",  # Should be ignored
            "100": "yes", # Should be ignored
            "hour_1": "yes", # Should be ignored
            1: "yes",     # Integer key 1 -> slots 0, 1
            " 2 ": "no",  # Whitespace-padded string key 2 -> slots 2, 3
            "\t3\n": "first", # Tab-padded key 3 -> slots 4, 5
        }
        intervals = Normalizer.normalize(mixed_keys)
        self.assertEqual(len(intervals), 48)
        # Hour 1 (int key)
        self.assertEqual(intervals[0].status, IntervalStatus.ON)
        self.assertEqual(intervals[1].status, IntervalStatus.ON)
        # Hour 2 (padded key)
        self.assertEqual(intervals[2].status, IntervalStatus.OFF)
        self.assertEqual(intervals[3].status, IntervalStatus.OFF)
        # Hour 3 (tab-padded key)
        self.assertEqual(intervals[4].status, IntervalStatus.OFF)
        self.assertEqual(intervals[5].status, IntervalStatus.ON)
        # Hours 4..24 default to MAYBE
        for h in range(4, 25):
            self.assertEqual(intervals[(h - 1) * 2].status, IntervalStatus.MAYBE)

    def test_emergency_override_invariance(self) -> None:
        """
        Verify emergency alert override invariant:
        When is_emergency is True (or truthy), all 48 intervals MUST be EMERGENCY,
        even if raw input states 100% power ('yes') or invalid data.
        """
        all_yes = {str(h): "yes" for h in range(1, 25)}

        # Boolean True
        intervals_true = Normalizer.normalize(all_yes, is_emergency=True)
        self.assertEqual(len(intervals_true), 48)
        for idx, item in enumerate(intervals_true):
            self.assertEqual(item.status, IntervalStatus.EMERGENCY, f"Slot {idx} must be EMERGENCY")

        # Truthy int (1) and string ("true")
        for truthy in (1, "true", [True]):
            intervals = Normalizer.normalize(all_yes, is_emergency=truthy)  # type: ignore[arg-type]
            self.assertTrue(all(item.status == IntervalStatus.EMERGENCY for item in intervals))

        # Falsy int (0), empty string (""), and None should not trigger emergency
        for falsy in (0, "", None, False):
            intervals = Normalizer.normalize(all_yes, is_emergency=falsy)  # type: ignore[arg-type]
            self.assertTrue(all(item.status == IntervalStatus.ON for item in intervals))

    def test_interval_indexing_and_time_formatting_monotonicity(self) -> None:
        """
        Verify interval indexing continuity, monotonicity, boundary values,
        and strict ValueError on out-of-bounds indices.
        """
        for i in range(48):
            st, et = Normalizer.format_interval_times(i)
            # Check format HH:MM
            self.assertEqual(len(st), 5)
            self.assertEqual(len(et), 5)
            self.assertEqual(st[2], ":")
            self.assertEqual(et[2], ":")
            if i > 0:
                prev_st, prev_et = Normalizer.format_interval_times(i - 1)
                self.assertEqual(st, prev_et, f"Slot {i} start {st} must match slot {i-1} end {prev_et}")

        # Exact boundary values
        self.assertEqual(Normalizer.format_interval_times(0), ("00:00", "00:30"))
        self.assertEqual(Normalizer.format_interval_times(23), ("11:30", "12:00"))
        self.assertEqual(Normalizer.format_interval_times(24), ("12:00", "12:30"))
        self.assertEqual(Normalizer.format_interval_times(47), ("23:30", "24:00"))

        # Out-of-bounds slot index raises ValueError
        with self.assertRaises(ValueError):
            Normalizer.format_interval_times(-1)
        with self.assertRaises(ValueError):
            Normalizer.format_interval_times(48)
        with self.assertRaises(ValueError):
            Normalizer.format_interval_times(100)

    def test_normalizer_convenience_methods_and_subgroup_model(self) -> None:
        """
        Verify normalize_statuses and normalize_subgroup contracts.
        """
        statuses = Normalizer.normalize_statuses({"1": "yes", "2": "no"})
        self.assertEqual(len(statuses), 48)
        self.assertIsInstance(statuses[0], IntervalStatus)
        self.assertEqual(statuses[0], IntervalStatus.ON)
        self.assertEqual(statuses[2], IntervalStatus.OFF)

        subgroup = Normalizer.normalize_subgroup(
            group_id=2,
            subgroup_id="2.1",
            raw_intervals={"1": "yes"},
            is_emergency=False,
        )
        self.assertIsInstance(subgroup, SubgroupSchedule)
        self.assertEqual(subgroup.group_id, 2)
        self.assertEqual(subgroup.subgroup_id, "2.1")
        self.assertEqual(len(subgroup.slots), 48)

        # NormalizerInterval dictionary subscripting
        item = subgroup.slots[0]
        self.assertEqual(item["slot_index"], 0)
        self.assertEqual(item["status"], "ON")
        self.assertEqual(item.get("start_time"), "00:00")
        self.assertEqual(item.get("non_existent_key", "default"), "default")


# ============================================================================
# Area 2: Adversarial Scraper Cascade & Circuit Breaker Tests
# ============================================================================
class TestAdversarialScraperCascadeCore(unittest.IsolatedAsyncioTestCase):
    """Adversarial stress testing for bot/services/scraper.py."""

    async def test_cascade_simultaneous_tiers_1_to_4_failure_clean_tier5_fallback(self) -> None:
        """
        Adversarial Test: Force Tiers 1-4 to simultaneously fail across all 4 DSO regions:
        Tier 1: WAF challenge / network timeout
        Tier 2: GitHub 404 / connection reset
        Tier 3: YASNO 500 error / unsupported region
        Tier 4: SQLite cache corrupted or missing
        Verification: Must cleanly fall back to Tier 5 mock fixtures with zero unhandled exceptions.
        """
        scraper = ScraperCascade()

        regions = [
            DSORegion.KYIV,
            DSORegion.KYIV_REGION,
            DSORegion.DNIPRO,
            DSORegion.ODESA,
        ]

        with (
            patch.object(scraper, "_fetch_tier1_direct", side_effect=ConnectionError("WAF challenge HTTP 403")),
            patch.object(scraper, "_fetch_tier2_mirror", side_effect=RuntimeError("GitHub raw 404 Not Found")),
            patch.object(scraper, "_fetch_tier3_yasno", side_effect=ValueError("YASNO Retail API HTTP 500")),
            patch.object(scraper, "_fetch_tier4_cache", side_effect=sqlite3.DatabaseError("Corrupted SQLite cache")),
        ):
            for region in regions:
                resp = await scraper.get_schedule(region)
                self.assertIsNotNone(resp)
                self.assertEqual(resp.data_source, "mock")
                self.assertTrue(resp.stale)
                self.assertTrue(len(resp.groups) > 0, f"Region {region} must have subgroups loaded")

                # Test group extraction under complete upstream failure
                daily = await scraper.get_schedule(region, group="1.1")
                self.assertIsInstance(daily, DailySchedule)
                self.assertEqual(daily.tier_used, "mock")
                self.assertTrue(daily.is_stale)
                self.assertEqual(len(daily.intervals), 48)
                self.assertTrue(len(daily.schedule_hash) == 64, "Must compute valid SHA-256 hash")

    def test_circuit_breaker_complete_state_transitions(self) -> None:
        """
        Verify Circuit Breaker transition dynamics:
        CLOSED -> trip() -> OPEN -> elapsed cooldown -> auto-CLOSED -> manual reset.
        """
        cb = CircuitBreaker(cooldown_seconds=900.0)

        # Initial state is CLOSED
        self.assertFalse(cb.is_open())
        self.assertIsNone(cb.last_trip_time)

        # Trip breaker into OPEN
        cb.trip()
        self.assertTrue(cb.is_open())
        self.assertIsNotNone(cb.last_trip_time)

        # Elapsed cooldown: simulate 899 seconds (still OPEN)
        cb._tripped_at = datetime.now() - timedelta(seconds=899)
        self.assertTrue(cb.is_open())

        # Elapsed cooldown: simulate 901 seconds (auto-CLOSED)
        cb._tripped_at = datetime.now() - timedelta(seconds=901)
        self.assertFalse(cb.is_open())
        self.assertIsNone(cb.last_trip_time)

        # Trip again and test manual reset
        cb.trip()
        self.assertTrue(cb.is_open())
        cb.reset()
        self.assertFalse(cb.is_open())
        self.assertIsNone(cb.last_trip_time)

        # Custom cooldown trip
        cb.trip(cooldown_seconds=30.0)
        self.assertEqual(cb.cooldown_seconds, 30.0)
        cb._tripped_at = datetime.now() - timedelta(seconds=31)
        self.assertFalse(cb.is_open())

    def test_circuit_breaker_timestamp_drift_resilience(self) -> None:
        """
        Verify Circuit Breaker resilience against negative timestamp drift (system clock jumped backward).
        Should remain OPEN and not prematurely reset.
        """
        cb = CircuitBreaker(cooldown_seconds=900.0)
        cb.trip()
        # Simulated backward clock jump: tripped_at appears to be 100 seconds in the future
        cb._tripped_at = datetime.now() + timedelta(seconds=100)
        self.assertTrue(cb.is_open(), "Breaker must remain OPEN when elapsed time is negative")

    def test_swr_cache_freshness_window_boundaries(self) -> None:
        """
        Verify Stale-While-Revalidate cache validity window (<= 48 hours):
        Exact boundary tests: 0s, 24h, 47h 59m, 48h 01m, 72h, future dates, invalid strings.
        """
        scraper = ScraperCascade()
        now = datetime.now()

        # Valid boundaries (<= 48h)
        self.assertTrue(scraper._is_cache_valid(now.isoformat()))
        self.assertTrue(scraper._is_cache_valid((now - timedelta(hours=24)).isoformat()))
        self.assertTrue(scraper._is_cache_valid((now - timedelta(hours=47, minutes=59)).isoformat()))
        self.assertTrue(scraper._is_cache_valid((now - timedelta(seconds=48 * 3600 - 10)).isoformat()))

        # Expired boundaries (> 48h)
        self.assertFalse(scraper._is_cache_valid((now - timedelta(seconds=48 * 3600 + 10)).isoformat()))
        self.assertFalse(scraper._is_cache_valid((now - timedelta(hours=72)).isoformat()))
        self.assertFalse(scraper._is_cache_valid((now - timedelta(days=7)).isoformat()))

        # Future timestamp (clock desync / negative diff) -> False
        self.assertFalse(scraper._is_cache_valid((now + timedelta(hours=1)).isoformat()))

        # Datetime object inputs (both aware and naive)
        self.assertTrue(scraper._is_cache_valid(now - timedelta(hours=12)))
        self.assertFalse(scraper._is_cache_valid(now - timedelta(hours=50)))

        aware_now = datetime.now(KYIV_TZ)
        self.assertTrue(scraper._is_cache_valid(aware_now - timedelta(hours=10)))
        self.assertFalse(scraper._is_cache_valid(aware_now - timedelta(hours=49)))

        # Invalid formats
        self.assertFalse(scraper._is_cache_valid("corrupted-date-string"))
        self.assertFalse(scraper._is_cache_valid(None))  # type: ignore[arg-type]
        self.assertFalse(scraper._is_cache_valid(123456789))  # type: ignore[arg-type]

    async def test_tier4_cache_hit_and_expired_fallthrough(self) -> None:
        """
        Verify Tier 4 SQLite cache behavior:
        When Tier 1-3 fail:
        - Fresh cache (2 hours old) is returned with data_source='cache' and stale=True.
        - Expired cache (50 hours old) falls through to Tier 5 mock fixtures.
        """
        fd, temp_db = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        try:
            cm = DatabaseConnectionManager(temp_db)
            repo = DatabaseRepository(cm)
            await repo.init_schema()

            scraper = ScraperCascade(cache_repo=repo.cache)

            # 1. Populate cache with fresh data (2 hours ago)
            fresh_time = (datetime.now() - timedelta(hours=2)).isoformat()
            slot_dicts = [
                {
                    "slot_index": i,
                    "start_time": f"{i//2:02d}:{(i%2)*30:02d}",
                    "end_time": f"{(i+1)//2:02d}:{((i+1)%2)*30:02d}" if i < 47 else "24:00",
                    "status": "ON",
                }
                for i in range(48)
            ]
            fresh_payload = {
                "region_id": "kyiv",
                "region_name": "м. Київ",
                "date": "2026-11-12",
                "updated_at": fresh_time,
                "is_emergency": False,
                "emergency_message": None,
                "data_source": "direct_scrape",
                "stale": False,
                "groups": {
                    "GPV1.1": {
                        "group_id": 1,
                        "subgroup_id": "1.1",
                        "slots": slot_dicts,
                    }
                },
            }
            import hashlib
            payload_str = json.dumps(fresh_payload)
            await repo.cache.set_cache(
                region_code="kyiv",
                schedule_date="2026-11-12",
                payload_json=payload_str,
                payload_hash=hashlib.sha256(payload_str.encode()).hexdigest(),
            )
            # Manually backdate updated_at in DB
            async with cm.get_connection() as conn:
                await conn.execute(
                    "UPDATE schedule_cache SET updated_at = ? WHERE region_code = 'kyiv';",
                    (fresh_time,),
                )
                await conn.commit()

            # Fail Tiers 1-3
            with (
                patch.object(scraper, "_fetch_tier1_direct", side_effect=ConnectionError("T1 fail")),
                patch.object(scraper, "_fetch_tier2_mirror", side_effect=RuntimeError("T2 fail")),
                patch.object(scraper, "_fetch_tier3_yasno", side_effect=RuntimeError("T3 fail")),
            ):
                resp_fresh = await scraper.get_schedule(DSORegion.KYIV)
                self.assertEqual(resp_fresh.data_source, "cache")
                self.assertTrue(resp_fresh.stale)

            # 2. Update cache with expired timestamp (50 hours ago)
            expired_time = (datetime.now() - timedelta(hours=50)).isoformat()
            async with cm.get_connection() as conn:
                await conn.execute(
                    "UPDATE schedule_cache SET updated_at = ? WHERE region_code = 'kyiv';",
                    (expired_time,),
                )
                await conn.commit()

            with (
                patch.object(scraper, "_fetch_tier1_direct", side_effect=ConnectionError("T1 fail")),
                patch.object(scraper, "_fetch_tier2_mirror", side_effect=RuntimeError("T2 fail")),
                patch.object(scraper, "_fetch_tier3_yasno", side_effect=RuntimeError("T3 fail")),
            ):
                resp_expired = await scraper.get_schedule(DSORegion.KYIV)
                # Expired cache must fall through to Tier 5 mock
                self.assertEqual(resp_expired.data_source, "mock")
                self.assertTrue(resp_expired.stale)

            await cm.close()
        finally:
            if os.path.exists(temp_db):
                os.remove(temp_db)

    def test_group_extraction_with_missing_and_corrupt_subgroups(self) -> None:
        """
        Verify group extraction resilience when requested subgroup does not exist,
        or slots list in fixture is deficient (<48) or oversized (>48).
        """
        scraper = ScraperCascade()

        # Deficient slots (only 10 slots)
        deficient_payload = {
            "groups": {
                "GPV1.1": {
                    "group_id": 1,
                    "subgroup_id": "1.1",
                    "slots": [IntervalStatus.ON] * 10,
                }
            }
        }
        res_def = scraper._extract_group_schedule("kyiv", deficient_payload, "1.1")
        self.assertEqual(len(res_def.intervals), 48, "Deficient slots must be padded to 48")
        # Remaining 38 slots padded with MAYBE
        for idx in range(10, 48):
            self.assertEqual(res_def.intervals[idx].status, IntervalStatus.MAYBE)

        # Superfluous slots (60 TimelineInterval items) -> truncated to 48
        superfluous_payload = {
            "groups": {
                "GPV1.1": {
                    "group_id": 1,
                    "subgroup_id": "1.1",
                    "slots": [
                        TimelineInterval(
                            slot_index=i % 48,
                            start_time=f"{(i%48)//2:02d}:{((i%48)%2)*30:02d}",
                            end_time=f"{((i%48)+1)//2:02d}:{(((i%48)+1)%2)*30:02d}" if (i % 48) < 47 else "24:00",
                            status=IntervalStatus.OFF,
                        )
                        for i in range(60)
                    ],
                }
            }
        }
        res_sup = scraper._extract_group_schedule("kyiv", superfluous_payload, "1.1")
        self.assertEqual(len(res_sup.intervals), 48, "Superfluous slots must be truncated to 48")

        # Non-existent group requested -> falls back to first available subgroup
        res_missing = scraper._extract_group_schedule("kyiv", deficient_payload, "GPV9.9")
        self.assertEqual(len(res_missing.intervals), 48)
        self.assertEqual(res_missing.group, "GPV9.9")


# ============================================================================
# Area 3: Adversarial Comparison Engine & Combinatorial Matrix Tests
# ============================================================================
class TestAdversarialComparisonEngineCore(unittest.TestCase):
    """Adversarial stress testing for bot/services/comparison.py."""

    def test_exhaustive_3x3_combinatorial_matrix_all_nine_pairs(self) -> None:
        """
        Verify all 9 pairwise combinations of (Slot 1, Slot 2) across ON, OFF, MAYBE:
        - (ON, ON) -> BOTH_ON (#22C55E)
        - (ON, OFF) -> GROUP1_ONLY (#3B82F6)
        - (OFF, ON) -> GROUP2_ONLY (#8B5CF6)
        - (OFF, OFF) -> BOTH_OFF (#1F2937)
        - (ON, MAYBE) -> GROUP1_ON_GROUP2_MAYBE (#059669)
        - (MAYBE, ON) -> GROUP1_MAYBE_GROUP2_ON (#7C3AED)
        - (OFF, MAYBE) -> GROUP1_OFF_GROUP2_MAYBE (#4B5563)
        - (MAYBE, OFF) -> GROUP1_MAYBE_GROUP2_OFF (#374151)
        - (MAYBE, MAYBE) -> BOTH_MAYBE (#EAB308)
        """
        for (s1, s2), (expected_status_str, expected_hex, _) in COMBINATORIAL_MATRIX.items():
            joint = evaluate_pair(s1, s2)
            self.assertEqual(
                joint.value,
                expected_status_str,
                f"Pair ({s1}, {s2}) failed to map to {expected_status_str}",
            )
            # Verify color mapping
            self.assertEqual(COLOR_MAP[joint], expected_hex)
            self.assertEqual(COLOR_MAP[joint.value], expected_hex)

    def test_emergency_override_all_seven_combinations(self) -> None:
        """
        Ukrenergo Emergency Override Invariant:
        If EITHER Slot 1 OR Slot 2 is EMERGENCY, joint status MUST be EMERGENCY.
        Exhaustively test all 7 combinations involving EMERGENCY.
        """
        emergency_pairs = [
            ("EMERGENCY", "ON"),
            ("EMERGENCY", "OFF"),
            ("EMERGENCY", "MAYBE"),
            ("EMERGENCY", "EMERGENCY"),
            ("ON", "EMERGENCY"),
            ("OFF", "EMERGENCY"),
            ("MAYBE", "EMERGENCY"),
        ]
        for s1, s2 in emergency_pairs:
            joint = evaluate_pair(s1, s2)
            self.assertEqual(
                joint,
                JointStatus.EMERGENCY,
                f"Emergency pair ({s1}, {s2}) must yield JointStatus.EMERGENCY, got {joint}",
            )
            self.assertEqual(COLOR_MAP[joint], "#EF4444")
            self.assertEqual(LABEL_MAP[joint], "Аварійні відключення")

    def test_single_slot_fallback_evaluations(self) -> None:
        """
        Verify Single-Slot Fallback mode when Slot 2 is unconfigured (slot2=None):
        - ON -> BOTH_ON
        - OFF -> BOTH_OFF
        - MAYBE -> BOTH_MAYBE
        - EMERGENCY -> EMERGENCY
        Verify slot_b is None, group2_only_hours is 0.0, and recommendation is valid.
        """
        # Test evaluate_pair with single slot
        self.assertEqual(evaluate_pair("ON", None), JointStatus.BOTH_ON)
        self.assertEqual(evaluate_pair("OFF", None), JointStatus.BOTH_OFF)
        self.assertEqual(evaluate_pair("MAYBE", None), JointStatus.BOTH_MAYBE)
        self.assertEqual(evaluate_pair("EMERGENCY", None), JointStatus.EMERGENCY)

        # Full compare single-slot run
        single_schedule = ["ON"] * 24 + ["OFF"] * 24
        resp = ComparisonEngine.compare(slot1=single_schedule, slot2=None, alias_a="Дім")
        self.assertIsNone(resp.slot_b)
        self.assertIsNotNone(resp.slot_a)
        self.assertEqual(resp.slot_a.alias, "Дім")
        self.assertEqual(resp.summary.both_on_hours, 12.0)
        self.assertEqual(resp.summary.both_off_hours, 12.0)
        self.assertEqual(resp.summary.group2_only_hours, 0.0)
        self.assertEqual(resp.summary.group1_only_hours, 0.0)

    def test_daily_summary_metric_extremes_and_invariants(self) -> None:
        """
        Verify daily summary metrics under extreme boundary schedules:
        1. All-day power (48 ON intervals): both_on_hours == 24.0, others == 0.0
        2. All-day outage (48 OFF intervals): both_off_hours == 24.0, others == 0.0
        3. All-day grey zone (48 MAYBE intervals): uncertain_hours == 24.0, others == 0.0
        4. All-day emergency: is_emergency is True, emergency banner populated.
        5. Invariant: No negative hours ever returned.
        """
        # 1. All-day ON
        resp_on = ComparisonEngine.compare(["ON"] * 48, ["ON"] * 48)
        self.assertEqual(resp_on.summary.both_on_hours, 24.0)
        self.assertEqual(resp_on.summary.both_off_hours, 0.0)
        self.assertEqual(resp_on.summary.group1_only_hours, 0.0)
        self.assertEqual(resp_on.summary.group2_only_hours, 0.0)
        self.assertEqual(resp_on.summary.uncertain_hours, 0.0)

        # 2. All-day OFF
        resp_off = ComparisonEngine.compare(["OFF"] * 48, ["OFF"] * 48)
        self.assertEqual(resp_off.summary.both_off_hours, 24.0)
        self.assertEqual(resp_off.summary.both_on_hours, 0.0)

        # 3. All-day MAYBE
        resp_maybe = ComparisonEngine.compare(["MAYBE"] * 48, ["MAYBE"] * 48)
        self.assertEqual(resp_maybe.summary.uncertain_hours, 24.0)
        self.assertEqual(resp_maybe.summary.both_on_hours, 0.0)
        self.assertEqual(resp_maybe.summary.both_off_hours, 0.0)

        # 4. All-day EMERGENCY
        resp_em = ComparisonEngine.compare(["EMERGENCY"] * 48, ["ON"] * 48)
        self.assertTrue(resp_em.is_emergency)
        self.assertIsNotNone(resp_em.emergency_message)
        self.assertIn("Укренерго", resp_em.summary.smart_recommendation)

        # 5. Invariant: metrics >= 0.0
        for m in (
            resp_on.summary.both_on_hours,
            resp_on.summary.both_off_hours,
            resp_on.summary.group1_only_hours,
            resp_on.summary.group2_only_hours,
            resp_on.summary.uncertain_hours,
        ):
            self.assertGreaterEqual(m, 0.0)

    def test_current_slot_index_boundaries_and_timezones(self) -> None:
        """
        Verify get_current_slot_index edge cases:
        - Midnight 00:00:00 -> slot 0
        - 00:29:59 -> slot 0
        - 00:30:00 -> slot 1
        - Midday 12:00:00 -> slot 24
        - 23:59:59 -> slot 47
        - UTC time object correctly converted to Europe/Kyiv.
        """
        # Timezone-aware Kyiv datetime
        dt_0000 = datetime(2026, 11, 12, 0, 0, tzinfo=KYIV_TZ)
        dt_0029 = datetime(2026, 11, 12, 0, 29, 59, tzinfo=KYIV_TZ)
        dt_0030 = datetime(2026, 11, 12, 0, 30, 0, tzinfo=KYIV_TZ)
        dt_1200 = datetime(2026, 11, 12, 12, 0, 0, tzinfo=KYIV_TZ)
        dt_2359 = datetime(2026, 11, 12, 23, 59, 59, tzinfo=KYIV_TZ)

        self.assertEqual(get_current_slot_index(dt_0000), 0)
        self.assertEqual(get_current_slot_index(dt_0029), 0)
        self.assertEqual(get_current_slot_index(dt_0030), 1)
        self.assertEqual(get_current_slot_index(dt_1200), 24)
        self.assertEqual(get_current_slot_index(dt_2359), 47)

        # UTC input: 22:00 UTC on a UTC+2 date is 00:00 Kyiv next day
        utc_tz = ZoneInfo("UTC")
        dt_utc = datetime(2026, 11, 11, 22, 0, 0, tzinfo=utc_tz)
        self.assertEqual(get_current_slot_index(dt_utc), 0)

        # time object input
        self.assertEqual(get_current_slot_index(time(14, 15)), 28)
        self.assertEqual(get_current_slot_index(time(0, 5)), 0)

    def test_next_transition_countdown_and_end_of_day(self) -> None:
        """
        Verify transition detection and countdown calculation:
        - Midday transition detection
        - Uniform schedule until midnight -> '24:00' and countdown to 1440 mins
        - Slot index at boundary (47)
        - Out-of-bounds slot index gracefully handled without IndexError.
        """
        # Schedule: slots 0..9 BOTH_ON, slots 10..47 BOTH_OFF
        timeline_items = [
            {"joint_status": "BOTH_ON", "start_time": f"{i//2:02d}:{(i%2)*30:02d}"}
            for i in range(10)
        ] + [
            {"joint_status": "BOTH_OFF", "start_time": f"{i//2:02d}:{(i%2)*30:02d}"}
            for i in range(10, 48)
        ]

        # Current time: 04:10 (slot index 8, 50 minutes before slot 10 starts at 05:00)
        now_dt = datetime(2026, 11, 12, 4, 10, tzinfo=KYIV_TZ)
        next_time, mins = calculate_next_transition(timeline_items, current_slot_index=8, now=now_dt)
        self.assertEqual(next_time, "05:00")
        self.assertEqual(mins, 50)

        # Uniform schedule till midnight: slot index 40 to 47 all BOTH_OFF
        now_late = datetime(2026, 11, 12, 23, 10, tzinfo=KYIV_TZ)
        next_time_eod, mins_eod = calculate_next_transition(timeline_items, current_slot_index=46, now=now_late)
        self.assertEqual(next_time_eod, "24:00")
        self.assertEqual(mins_eod, 50)  # 24:00 is 1440 min; 23:10 is 1390 min; diff = 50 min

        # Out-of-bounds index (48) -> returns ('24:00', 0) without raising IndexError
        next_oob, mins_oob = calculate_next_transition(timeline_items, current_slot_index=48, now=now_late)
        self.assertEqual(next_oob, "24:00")
        self.assertEqual(mins_oob, 0)

    def test_polymorphic_payload_inputs(self) -> None:
        """
        Verify ComparisonEngine resilience to polymorphic slot representations:
        scalar strings, scalar enums, truncated lists, oversized lists, None.
        """
        # Scalar enum -> replicated 48 times
        resp_scalar = ComparisonEngine.compare(IntervalStatus.ON, IntervalStatus.OFF)
        self.assertEqual(resp_scalar.summary.group1_only_hours, 24.0)

        # Truncated list (5 items) -> padded to 48 with MAYBE
        resp_trunc = ComparisonEngine.compare(["ON"] * 5, ["OFF"] * 5)
        self.assertEqual(len(resp_trunc.timeline), 48)
        self.assertEqual(resp_trunc.summary.group1_only_hours, 2.5)

        # Oversized list (60 items) -> truncated to 48
        resp_over = ComparisonEngine.compare(["ON"] * 60, ["ON"] * 60)
        self.assertEqual(len(resp_over.timeline), 48)
        self.assertEqual(resp_over.summary.both_on_hours, 24.0)

        # Subscripting on TimelineResponse and ComparisonTimelineSummary
        self.assertEqual(resp_over["comparison_date"], resp_over.comparison_date)
        self.assertEqual(resp_over.summary["both_on_hours"], 24.0)
        self.assertTrue("both_on_hours" in resp_over.summary)


# ============================================================================
# Area 4: Adversarial Database & SQLite Persistence Tests
# ============================================================================
class TestAdversarialDatabaseStorageCore(unittest.IsolatedAsyncioTestCase):
    """Adversarial stress testing for bot/database persistence and concurrency."""

    async def asyncSetUp(self) -> None:
        self.fd, self.temp_db = tempfile.mkstemp(suffix=".sqlite3")
        os.close(self.fd)
        self.cm = DatabaseConnectionManager(self.temp_db)
        self.repo = DatabaseRepository(self.cm)
        await self.repo.init_schema()

    async def asyncTearDown(self) -> None:
        await self.cm.close()
        for p in [self.temp_db, f"{self.temp_db}-wal", f"{self.temp_db}-shm"]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    async def test_wal_pragmas_and_isolation_level(self) -> None:
        """
        Verify SQLite WAL PRAGMAs:
        - journal_mode == 'wal'
        - foreign_keys == 1 (ON)
        - busy_timeout == 30000 ms
        - synchronous == 1 (NORMAL)
        """
        async with self.cm.get_connection() as conn:
            cur = await conn.execute("PRAGMA journal_mode;")
            row = await cur.fetchone()
            self.assertEqual(row[0].lower(), "wal")

            cur = await conn.execute("PRAGMA foreign_keys;")
            row = await cur.fetchone()
            self.assertEqual(row[0], 1)

            cur = await conn.execute("PRAGMA busy_timeout;")
            row = await cur.fetchone()
            self.assertEqual(row[0], 30000)

            cur = await conn.execute("PRAGMA synchronous;")
            row = await cur.fetchone()
            self.assertEqual(row[0], 1)

    async def test_foreign_key_violation_rejection(self) -> None:
        """
        Verify foreign key constraint rejection:
        Attempting to insert into user_slots, notification_settings, or notification_history
        for a non-existent user_id MUST raise sqlite3.IntegrityError.
        """
        async with self.cm.get_connection() as conn:
            # 1. user_slots orphan insertion
            with self.assertRaises(sqlite3.IntegrityError):
                await conn.execute(
                    "INSERT INTO user_slots (user_id, slot_number, region_code, group_id, subgroup_id) "
                    "VALUES (?, ?, ?, ?, ?);",
                    (99999, 1, "kyiv", 1, "1.1"),
                )

            # 2. notification_settings orphan insertion
            with self.assertRaises(sqlite3.IntegrityError):
                await conn.execute(
                    "INSERT INTO notification_settings (user_id) VALUES (?);",
                    (99999,),
                )

            # 3. notification_history orphan insertion
            with self.assertRaises(sqlite3.IntegrityError):
                await conn.execute(
                    "INSERT INTO notification_history (user_id, slot_number, event_type, target_time, hash_sent) "
                    "VALUES (?, ?, ?, ?, ?);",
                    (99999, 1, "30m_before_off", "2026-11-12 10:00:00", "dummyhash"),
                )

    async def test_cascade_deletion_across_all_related_tables(self) -> None:
        """
        Verify ON DELETE CASCADE foreign key behavior:
        Deleting a user MUST automatically delete all associated rows in:
        user_slots, notification_settings, and notification_history.
        """
        user_id = 7001
        # Create user
        await self.repo.users.upsert_user(user_id=user_id, chat_id=user_id, first_name="Cascade User")
        # Add 2 slots
        await self.repo.slots.upsert_slot(user_id=user_id, slot_number=1, region_code="kyiv", group_id=1, subgroup_id="1.1")
        await self.repo.slots.upsert_slot(user_id=user_id, slot_number=2, region_code="dnipro", group_id=2, subgroup_id="2.1")
        # Add notification settings
        await self.repo.settings.upsert_settings(user_id=user_id, notify_30m=True)
        # Add 3 notification history entries
        for i in range(3):
            await self.repo.history.record_notification(
                user_id=user_id,
                slot_number=1,
                event_type="30m_before_off",
                target_time=f"2026-11-12 1{i}:00:00",
                hash_sent=f"hash_{i}",
            )

        # Verify records exist
        self.assertEqual(len(await self.repo.slots.get_slots_for_user(user_id)), 2)
        self.assertTrue(await self.repo.history.is_notification_sent(user_id, 1, "30m_before_off", "2026-11-12 10:00:00"))

        # Delete user
        async with self.cm.get_connection() as conn:
            await conn.execute("DELETE FROM users WHERE user_id = ?;", (user_id,))
            await conn.commit()

        # Verify all cascading deletions
        self.assertIsNone(await self.repo.users.get_user(user_id))
        self.assertEqual(len(await self.repo.slots.get_slots_for_user(user_id)), 0)
        self.assertFalse(await self.repo.history.is_notification_sent(user_id, 1, "30m_before_off", "2026-11-12 10:00:00"))

        async with self.cm.get_connection() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM notification_settings WHERE user_id = ?;", (user_id,))
            self.assertEqual((await cur.fetchone())[0], 0)
            cur = await conn.execute("SELECT COUNT(*) FROM notification_history WHERE user_id = ?;", (user_id,))
            self.assertEqual((await cur.fetchone())[0], 0)

    async def test_check_constraint_enforcement(self) -> None:
        """
        Verify SQLite column CHECK constraints:
        - slot_number IN (1, 2)
        - group_id BETWEEN 1 AND 6
        - region_code IN ('kyiv', 'kyiv-region', 'dnipro', 'odesa')
        """
        user_id = 7002
        await self.repo.users.upsert_user(user_id=user_id, chat_id=user_id, first_name="Check User")

        async with self.cm.get_connection() as conn:
            # Invalid slot_number: 0
            with self.assertRaises(sqlite3.IntegrityError):
                await conn.execute(
                    "INSERT INTO user_slots (user_id, slot_number, region_code, group_id, subgroup_id) VALUES (?, ?, ?, ?, ?);",
                    (user_id, 0, "kyiv", 1, "1.1"),
                )
            # Invalid slot_number: 3
            with self.assertRaises(sqlite3.IntegrityError):
                await conn.execute(
                    "INSERT INTO user_slots (user_id, slot_number, region_code, group_id, subgroup_id) VALUES (?, ?, ?, ?, ?);",
                    (user_id, 3, "kyiv", 1, "1.1"),
                )
            # Invalid group_id: 0
            with self.assertRaises(sqlite3.IntegrityError):
                await conn.execute(
                    "INSERT INTO user_slots (user_id, slot_number, region_code, group_id, subgroup_id) VALUES (?, ?, ?, ?, ?);",
                    (user_id, 1, "kyiv", 0, "0.1"),
                )
            # Invalid group_id: 7
            with self.assertRaises(sqlite3.IntegrityError):
                await conn.execute(
                    "INSERT INTO user_slots (user_id, slot_number, region_code, group_id, subgroup_id) VALUES (?, ?, ?, ?, ?);",
                    (user_id, 1, "kyiv", 7, "7.1"),
                )
            # Invalid region_code: 'kharkiv'
            with self.assertRaises(sqlite3.IntegrityError):
                await conn.execute(
                    "INSERT INTO user_slots (user_id, slot_number, region_code, group_id, subgroup_id) VALUES (?, ?, ?, ?, ?);",
                    (user_id, 1, "kharkiv", 1, "1.1"),
                )

    async def test_concurrent_async_wal_transactions(self) -> None:
        """
        Stress Test: 20 concurrent async workers writing and reading simultaneously
        under WAL mode. Must achieve zero database lock errors.
        """
        async def worker_task(worker_id: int) -> None:
            uid = 8000 + worker_id
            # 1. Upsert user
            await self.repo.users.upsert_user(user_id=uid, chat_id=uid, first_name=f"Worker {worker_id}")
            # 2. Upsert slot
            await self.repo.slots.upsert_slot(
                user_id=uid,
                slot_number=1,
                region_code="kyiv",
                group_id=1,
                subgroup_id="1.1",
                alias=f"Worker Alias {worker_id}",
            )
            # 3. Upsert settings
            await self.repo.settings.upsert_settings(user_id=uid, notify_30m=True, notify_15m=False)
            # 4. Record history
            await self.repo.history.record_notification(
                user_id=uid,
                slot_number=1,
                event_type="30m_before_off",
                target_time="2026-11-12 12:00:00",
                hash_sent=f"hash_{worker_id}",
            )
            # 5. Read back
            u = await self.repo.users.get_user(uid)
            self.assertIsNotNone(u)
            self.assertEqual(u.first_name, f"Worker {worker_id}")

        tasks = [worker_task(i) for i in range(20)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for idx, res in enumerate(results):
            if isinstance(res, Exception):
                self.fail(f"Worker {idx} failed with unexpected exception: {res}")

    async def test_notification_deduplication_index_precision(self) -> None:
        """
        Verify notification deduplication index (idx_notif_dedup):
        Exact match must report duplicate; any field variation must report False.
        """
        user_id = 9001
        await self.repo.users.upsert_user(user_id=user_id, chat_id=user_id, first_name="Dedup User")

        # Initial check -> not sent
        sent_before = await self.repo.history.is_notification_sent(
            user_id=user_id,
            slot_number=1,
            event_type="30m_before_off",
            target_time="2026-11-12 15:00:00",
        )
        self.assertFalse(sent_before)

        # Record notification
        await self.repo.history.record_notification(
            user_id=user_id,
            slot_number=1,
            event_type="30m_before_off",
            target_time="2026-11-12 15:00:00",
            hash_sent="hash_1500",
        )

        # Exact match -> sent
        sent_after = await self.repo.history.is_notification_sent(
            user_id=user_id,
            slot_number=1,
            event_type="30m_before_off",
            target_time="2026-11-12 15:00:00",
        )
        self.assertTrue(sent_after)

        # Mismatch variations
        # 1. Different user
        self.assertFalse(await self.repo.history.is_notification_sent(9999, 1, "30m_before_off", "2026-11-12 15:00:00"))
        # 2. Different slot number
        self.assertFalse(await self.repo.history.is_notification_sent(user_id, 2, "30m_before_off", "2026-11-12 15:00:00"))
        # 3. Different event type
        self.assertFalse(await self.repo.history.is_notification_sent(user_id, 1, "15m_before_off", "2026-11-12 15:00:00"))
        # 4. Different target time
        self.assertFalse(await self.repo.history.is_notification_sent(user_id, 1, "30m_before_off", "2026-11-12 15:30:00"))

    async def test_cache_repository_upsert_and_pruning(self) -> None:
        """
        Verify schedule cache upsert idempotence and retention pruning.
        """
        # Upsert cache entry for dnipro
        record = await self.repo.cache.set_cache(
            region_code="dnipro",
            schedule_date="2026-11-12",
            payload_json='{"status": "ok"}',
            payload_hash="hash_dnipro_1",
            data_source="yasno_api",
        )
        self.assertEqual(record.region_code, "dnipro")
        self.assertEqual(record.payload_hash, "hash_dnipro_1")

        # Update cache for same region -> replaces existing row
        record_updated = await self.repo.cache.set_cache(
            region_code="dnipro",
            schedule_date="2026-11-12",
            payload_json='{"status": "updated"}',
            payload_hash="hash_dnipro_2",
            data_source="yasno_api",
        )
        self.assertEqual(record_updated.payload_hash, "hash_dnipro_2")

        fetched = await self.repo.cache.get_cache("dnipro")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.payload_hash, "hash_dnipro_2")

        # Prune test: backdate updated_at by 10 days
        old_time = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        async with self.cm.get_connection() as conn:
            await conn.execute(
                "UPDATE schedule_cache SET updated_at = ? WHERE region_code = 'dnipro';",
                (old_time,),
            )
            await conn.commit()

        pruned_count = await self.repo.cache.prune_stale_cache(days=7)
        self.assertEqual(pruned_count, 1)
        self.assertIsNone(await self.repo.cache.get_cache("dnipro"))


if __name__ == "__main__":
    unittest.main()
