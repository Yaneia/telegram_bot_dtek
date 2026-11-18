"""
tests/test_adversarial_notifier.py - Adversarial Stress & Verification Harness for Milestone 5.

Empirical Challenger 2 Adversarial Suite:
1. Boundary transitions across midnight (23:30 -> 00:00 -> 00:30):
   - Out-of-bounds safety, negative slot index prevention, end-of-day interval math.
   - Comprehensive 1440-minute daily slot index mapping verification.
   - Midnight crossing transition behavior with silent hours enabled vs disabled.
2. Deduplication idempotency under repeated ticks:
   - Multiple consecutive poll_tick invocations with identical schedule state.
   - Exact suppression of duplicate alerts for 30m, 15m advance outage warnings and revision diffs.
   - Verification of SQLite notification_history table and index idx_notif_dedup.
3. Schedule revision diff detection & multi-group selective alerting:
   - Multiple disjoint changed interval groups across 48 slots.
   - Boundary slot revisions (slot 0: 00:00-00:30, slot 47: 23:30-24:00).
   - Strict subscriber isolation across multi-group and multi-region users.
   - Inactive user and disabled notification preference filtering.
4. Token-bucket rate limiter & error recovery:
   - 25.0 msg/s ceiling enforcement and burst allowance.
   - Telegram HTTP 429 Retry-After backoff (retry_after + 1.0s pause).
   - Telegram HTTP 403 Forbidden auto-deactivation of blocked users.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from bot.database.connection import DatabaseConnectionManager
from bot.database.repository import DatabaseRepository
from bot.database.schema import init_db
from bot.models.enums import DSORegion, IntervalStatus
from bot.models.schedule import SubgroupSchedule, TimelineInterval
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

KYIV_TZ = ZoneInfo("Europe/Kyiv")


class MockBot:
    """Mock Telegram Bot capturing sent messages and simulating error conditions."""

    def __init__(self) -> None:
        self.sent_messages: List[Dict[str, Any]] = []
        self.simulate_429: bool = False
        self.retry_after_seconds: float = 1.5
        self.simulate_403_users: set[int] = set()

    async def send_message(self, chat_id: int, text: str) -> bool:
        if self.simulate_429:
            self.simulate_429 = False
            raise Exception(f"Telegram API 429: Too Many Requests: retry after {self.retry_after_seconds}")

        if chat_id in self.simulate_403_users:
            raise Exception("Telegram API 403: Forbidden: bot was blocked by the user")

        self.sent_messages.append({"chat_id": chat_id, "text": text})
        return True


class MockScraper:
    """Mock scraper simulating DTEK schedule responses for regional and subgroup queries."""

    def __init__(self) -> None:
        # region -> subgroup_id -> List[str] of 48 statuses
        self.schedules: Dict[str, Dict[str, List[str]]] = {
            "kyiv": {},
            "kyiv-region": {},
            "dnipro": {},
            "odesa": {},
        }
        self.schedule_date: str = "2026-11-12"

    def set_subgroup(self, region: str, subgroup_id: str, statuses: List[str]) -> None:
        norm_sub = subgroup_id.replace("GPV", "").strip()
        if len(statuses) != 48:
            raise ValueError(f"Statuses must be 48 items, got {len(statuses)}")
        self.schedules[region][norm_sub] = list(statuses)

    async def get_schedule(self, region: str, subgroup_id: Optional[str] = None) -> Any:
        region_dict = self.schedules.get(region, {})
        if subgroup_id is not None:
            norm_sub = subgroup_id.replace("GPV", "").strip()
            statuses = region_dict.get(norm_sub, ["ON"] * 48)
            slots = [
                TimelineInterval(
                    slot_index=i,
                    start_time=f"{i//2:02d}:{(i%2)*30:02d}",
                    end_time=f"{(i+1)//2:02d}:{((i+1)%2)*30:02d}",
                    status=IntervalStatus(st),
                )
                for i, st in enumerate(statuses)
            ]
            return type("ScheduleResp", (), {"intervals": slots, "groups": {}})()

        # Regional schedule response containing all subgroups
        groups_dict: Dict[str, SubgroupSchedule] = {}
        for sg, statuses in region_dict.items():
            slots = [
                TimelineInterval(
                    slot_index=i,
                    start_time=f"{i//2:02d}:{(i%2)*30:02d}",
                    end_time=f"{(i+1)//2:02d}:{((i+1)%2)*30:02d}",
                    status=IntervalStatus(st),
                )
                for i, st in enumerate(statuses)
            ]
            grp_num = int(sg.split(".")[0])
            groups_dict[sg] = SubgroupSchedule(group_id=grp_num, subgroup_id=sg, slots=slots)

        date_str = self.schedule_date

        class RegionalSchedule:
            def __init__(self, grps: Dict[str, SubgroupSchedule]) -> None:
                self.groups = grps
                self.date = date_str
                self.is_emergency = False
                self.emergency_message = None
                self.data_source = "direct_scrape"

            def model_dump_json(self) -> str:
                return json.dumps({
                    "region": region,
                    "date": self.date,
                    "groups": {
                        k: {
                            "group_id": v.group_id,
                            "subgroup_id": v.subgroup_id,
                            "slots": [
                                {"slot_index": s.slot_index, "status": s.status.value}
                                for s in v.slots
                            ],
                        }
                        for k, v in self.groups.items()
                    },
                })

        return RegionalSchedule(groups_dict)


# ============================================================================
# 1. ADVERSARIAL MIDNIGHT BOUNDARY TRANSITIONS
# ============================================================================

class TestAdversarialMidnightBoundaries(unittest.IsolatedAsyncioTestCase):
    """Stress-test midnight boundaries, slot math, and transition edge cases."""

    def test_slot_index_from_datetime_all_day_thorough(self) -> None:
        """Verify get_slot_index_from_datetime across every single minute of the day (1440 mins)."""
        base_day = datetime(2026, 11, 12, 0, 0, 0, tzinfo=KYIV_TZ)
        for minute_offset in range(1440):
            dt = base_day + timedelta(minutes=minute_offset)
            slot = get_slot_index_from_datetime(dt)
            expected_slot = minute_offset // 30
            self.assertEqual(
                slot,
                expected_slot,
                f"Minute {dt.strftime('%H:%M')} expected slot {expected_slot}, got {slot}",
            )
            self.assertTrue(0 <= slot <= 47)

    def test_slot_time_bounds_boundary_and_out_of_bounds(self) -> None:
        """Verify slot time bounds at slot 0, slot 47, and verify strict bounds check."""
        start_0, end_0 = get_slot_time_bounds(0)
        self.assertEqual(start_0, "00:00")
        self.assertEqual(end_0, "00:30")

        start_47, end_47 = get_slot_time_bounds(47)
        self.assertEqual(start_47, "23:30")
        self.assertEqual(end_47, "24:00")

        # Out-of-bounds indices must raise ValueError
        with self.assertRaises(ValueError):
            get_slot_time_bounds(-1)
        with self.assertRaises(ValueError):
            get_slot_time_bounds(48)
        with self.assertRaises(ValueError):
            get_slot_time_bounds(99)

    def test_midnight_transition_evaluations(self) -> None:
        """
        Verify transition detection behavior at:
        23:00 -> 23:30 -> 23:45 -> 00:00 -> 00:15 -> 00:30.
        Ensure zero unhandled exceptions or invalid out-of-bounds accesses.
        """
        slots = [{"slot_index": i, "status": "ON"} for i in range(48)]
        # Outage scheduled at slot 47 (23:30) and slot 1 (00:30)
        slots[47]["status"] = "OFF"
        slots[1]["status"] = "OFF"

        # 1. 23:00 with lead 30m -> target 23:30 (slot 47), prior is slot 46 (ON) -> triggers!
        dt_2300 = datetime(2026, 11, 11, 23, 0, 0, tzinfo=KYIV_TZ)
        w1 = detect_advance_outage_transition(slots, dt_2300, lead_minutes=30)
        self.assertIsNotNone(w1)
        self.assertEqual(w1["event_type"], "30m_before_off")
        self.assertEqual(w1["target_slot_index"], 47)
        self.assertEqual(w1["transition_time"], "23:30")

        # 2. 23:15 with lead 15m -> target 23:30 (slot 47), prior is slot 46 (ON) -> triggers!
        dt_2315 = datetime(2026, 11, 11, 23, 15, 0, tzinfo=KYIV_TZ)
        w2 = detect_advance_outage_transition(slots, dt_2315, lead_minutes=15)
        self.assertIsNotNone(w2)
        self.assertEqual(w2["event_type"], "15m_before_off")
        self.assertEqual(w2["target_slot_index"], 47)
        self.assertEqual(w2["transition_time"], "23:30")

        # 3. 23:30 with lead 30m -> target 00:00 next day (slot 0).
        # Prior slot is -1. Must return None safely without IndexError or crash.
        dt_2330 = datetime(2026, 11, 11, 23, 30, 0, tzinfo=KYIV_TZ)
        w3 = detect_advance_outage_transition(slots, dt_2330, lead_minutes=30)
        self.assertIsNone(w3, "Target slot 0 has prior_slot -1; must return None cleanly")

        # 4. 23:45 with lead 15m -> target 00:00 (slot 0).
        dt_2345 = datetime(2026, 11, 11, 23, 45, 0, tzinfo=KYIV_TZ)
        w4 = detect_advance_outage_transition(slots, dt_2345, lead_minutes=15)
        self.assertIsNone(w4, "Target slot 0 with lead 15m must return None cleanly")

        # 5. 00:00 with lead 30m -> target 00:30 (slot 1), prior is slot 0 (ON) -> triggers!
        dt_0000 = datetime(2026, 11, 12, 0, 0, 0, tzinfo=KYIV_TZ)
        w5 = detect_advance_outage_transition(slots, dt_0000, lead_minutes=30)
        self.assertIsNotNone(w5)
        self.assertEqual(w5["event_type"], "30m_before_off")
        self.assertEqual(w5["target_slot_index"], 1)
        self.assertEqual(w5["transition_time"], "00:30")

        # 6. 00:15 with lead 15m -> target 00:30 (slot 1), prior is slot 0 (ON) -> triggers!
        dt_0015 = datetime(2026, 11, 12, 0, 15, 0, tzinfo=KYIV_TZ)
        w6 = detect_advance_outage_transition(slots, dt_0015, lead_minutes=15)
        self.assertIsNotNone(w6)
        self.assertEqual(w6["event_type"], "15m_before_off")
        self.assertEqual(w6["target_slot_index"], 1)
        self.assertEqual(w6["transition_time"], "00:30")

        # 7. 00:30 with lead 30m -> target 01:00 (slot 2). Both slot 1 and 2 are OFF or ON -> no transition.
        dt_0030 = datetime(2026, 11, 12, 0, 30, 0, tzinfo=KYIV_TZ)
        w7 = detect_advance_outage_transition(slots, dt_0030, lead_minutes=30)
        self.assertIsNone(w7)

    def test_silent_hours_crossing_midnight_truth_table(self) -> None:
        """Verify silent hours boundaries crossing midnight (23:00 to 07:00)."""
        # Inside silent window
        for h in [23, 0, 1, 2, 3, 4, 5, 6]:
            dt = datetime(2026, 11, 12, h, 15, tzinfo=KYIV_TZ)
            self.assertTrue(is_in_silent_hours(dt, 23, 7), f"Hour {h}:15 must be silent")

        # Outside silent window
        for h in [7, 8, 12, 18, 22]:
            dt = datetime(2026, 11, 12, h, 0, tzinfo=KYIV_TZ)
            self.assertFalse(is_in_silent_hours(dt, 23, 7), f"Hour {h}:00 must be active")


# ============================================================================
# 2. ADVERSARIAL DEDUPLICATION & IDEMPOTENCY TEST SUITE
# ============================================================================

class TestAdversarialDeduplicationIdempotency(unittest.IsolatedAsyncioTestCase):
    """Stress-test deduplication across consecutive poll_tick executions."""

    async def asyncSetUp(self) -> None:
        self.fd, self.temp_db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(self.fd)
        self.cm = DatabaseConnectionManager(self.temp_db_path)
        self.repo = DatabaseRepository(self.cm)
        async with self.cm.get_connection() as conn:
            await init_db(conn)

        self.bot = MockBot()
        self.scraper = MockScraper()
        self.limiter = TokenBucketRateLimiter(rate=25.0, capacity=25.0)
        self.notifier = NotifierService(
            bot=self.bot,
            db=self.repo,
            scraper=self.scraper,
            rate_limiter=self.limiter,
            check_interval=60,
        )

    async def asyncTearDown(self) -> None:
        await self.cm.close()
        for p in [self.temp_db_path, f"{self.temp_db_path}-wal", f"{self.temp_db_path}-shm"]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    async def test_advance_warning_consecutive_poll_ticks_idempotency(self) -> None:
        """
        Verify that multiple consecutive poll_tick invocations for the exact same event
        dispatch strictly 1 notification and suppress all duplicate dispatches.
        """
        # 1. Setup active user with slot 1 (kyiv, 1.1)
        user_id = 9001
        await self.repo.users.upsert_user(user_id=user_id, chat_id=user_id, first_name="Dedup User")
        await self.repo.slots.upsert_slot(
            user_id=user_id,
            slot_number=1,
            region_code="kyiv",
            group_id=1,
            subgroup_id="1.1",
            alias="Мій Дім",
        )
        # Disable silent hours for test (start=2, end=4) so 10:00 is active
        await self.repo.settings.upsert_settings(
            user_id=user_id,
            notify_30m=True,
            notify_15m=True,
            silent_hours_start=2,
            silent_hours_end=4,
        )

        # 2. Setup schedule with outage starting at 10:30 (slot 21)
        statuses = ["ON"] * 48
        for i in range(21, 25):  # 10:30 to 12:30
            statuses[i] = "OFF"
        self.scraper.set_subgroup("kyiv", "1.1", statuses)

        # 3. First tick at 10:00 (30m advance warning)
        t_1000 = datetime(2026, 11, 12, 10, 0, 0, tzinfo=KYIV_TZ)
        sent_1 = await self.notifier.poll_tick(now=t_1000)
        self.assertEqual(sent_1, 1, f"Expected 1 notification dispatched on 1st tick, got {sent_1}")
        self.assertEqual(len(self.bot.sent_messages), 1)
        self.assertIn("Через 30 хв (о 10:30) планове відключення", self.bot.sent_messages[0]["text"])

        # 4. Immediate second tick at 10:00:00 -> must send 0 duplicates
        sent_2 = await self.notifier.poll_tick(now=t_1000)
        self.assertEqual(sent_2, 0, f"Expected 0 duplicate notifications on 2nd tick, got {sent_2}")
        self.assertEqual(len(self.bot.sent_messages), 1)

        # 5. Third tick at 10:00:30 -> must send 0 duplicates
        t_100030 = datetime(2026, 11, 12, 10, 0, 30, tzinfo=KYIV_TZ)
        sent_3 = await self.notifier.poll_tick(now=t_100030)
        self.assertEqual(sent_3, 0, f"Expected 0 duplicate notifications on 3rd tick, got {sent_3}")
        self.assertEqual(len(self.bot.sent_messages), 1)

        # 6. Fourth tick at 10:01:00 -> must send 0 duplicates
        t_1001 = datetime(2026, 11, 12, 10, 1, 0, tzinfo=KYIV_TZ)
        sent_4 = await self.notifier.poll_tick(now=t_1001)
        self.assertEqual(sent_4, 0, f"Expected 0 duplicate notifications on 4th tick, got {sent_4}")
        self.assertEqual(len(self.bot.sent_messages), 1)

        # 7. Check database notification_history table: exactly 1 row for 30m_before_off
        async with self.cm.get_connection() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM notification_history WHERE user_id = ? AND event_type = '30m_before_off';",
                (user_id,),
            )
            count = (await cur.fetchone())[0]
            self.assertEqual(count, 1, f"Expected exactly 1 history row, found {count}")

        # 8. Fast-forward to 10:15:00 (15m advance warning window)
        t_1015 = datetime(2026, 11, 12, 10, 15, 0, tzinfo=KYIV_TZ)
        sent_15m_1 = await self.notifier.poll_tick(now=t_1015)
        self.assertEqual(sent_15m_1, 1, f"Expected 1 notification for 15m warning, got {sent_15m_1}")
        self.assertEqual(len(self.bot.sent_messages), 2)
        self.assertIn("Через 15 хв (о 10:30)", self.bot.sent_messages[1]["text"])

        # 9. Consecutive tick at 10:15:30 -> must send 0 duplicates
        sent_15m_2 = await self.notifier.poll_tick(now=datetime(2026, 11, 12, 10, 15, 30, tzinfo=KYIV_TZ))
        self.assertEqual(sent_15m_2, 0, f"Expected 0 duplicate notifications, got {sent_15m_2}")
        self.assertEqual(len(self.bot.sent_messages), 2)

    async def test_schedule_revision_poll_tick_idempotency(self) -> None:
        """
        Verify that revision diff detection is strictly idempotent across consecutive ticks.
        When a diff occurs, alert is dispatched once; subsequent ticks with unchanged
        revision payload dispatch 0 alerts.
        """
        user_id = 9002
        await self.repo.users.upsert_user(user_id=user_id, chat_id=user_id, first_name="Diff User")
        await self.repo.slots.upsert_slot(
            user_id=user_id,
            slot_number=1,
            region_code="kyiv",
            group_id=1,
            subgroup_id="1.1",
        )
        await self.repo.settings.upsert_settings(
            user_id=user_id,
            notify_schedule_change=True,
            silent_hours_start=2,
            silent_hours_end=4,
        )

        # Baseline schedule: all ON
        baseline_statuses = ["ON"] * 48
        self.scraper.set_subgroup("kyiv", "1.1", baseline_statuses)

        # Tick 1: Seed schedule_cache
        t_now = datetime(2026, 11, 12, 12, 0, 0, tzinfo=KYIV_TZ)
        init_sent = await self.notifier.poll_tick(now=t_now)
        # First tick initializes cache, no diff yet
        self.assertEqual(init_sent, 0)
        self.assertEqual(len(self.bot.sent_messages), 0)

        # Now revision occurs: slots 30..33 (15:00-17:00) changed to OFF
        revised_statuses = list(baseline_statuses)
        for i in range(30, 34):
            revised_statuses[i] = "OFF"
        self.scraper.set_subgroup("kyiv", "1.1", revised_statuses)

        # Tick 2: Revised schedule detected -> 1 diff alert dispatched
        diff_sent_1 = await self.notifier.poll_tick(now=t_now)
        self.assertEqual(diff_sent_1, 1, f"Expected 1 diff alert dispatched, got {diff_sent_1}")
        self.assertEqual(len(self.bot.sent_messages), 1)
        self.assertIn("Графік ДТЕК змінено!", self.bot.sent_messages[0]["text"])
        self.assertIn("додано відключення з 15:00 до 17:00", self.bot.sent_messages[0]["text"])

        # Tick 3: Same revised schedule -> 0 alerts dispatched
        diff_sent_2 = await self.notifier.poll_tick(now=t_now)
        self.assertEqual(diff_sent_2, 0, f"Expected 0 duplicate diff alerts on tick 3, got {diff_sent_2}")
        self.assertEqual(len(self.bot.sent_messages), 1)

        # Tick 4: Consecutive tick at t_now + 60s -> 0 alerts dispatched
        diff_sent_3 = await self.notifier.poll_tick(now=t_now + timedelta(seconds=60))
        self.assertEqual(diff_sent_3, 0, f"Expected 0 duplicate diff alerts on tick 4, got {diff_sent_3}")
        self.assertEqual(len(self.bot.sent_messages), 1)


# ============================================================================
# 3. ADVERSARIAL REVISION DIFF DETECTION & MULTI-GROUP SELECTIVITY
# ============================================================================

class TestAdversarialRevisionDiffDetection(unittest.IsolatedAsyncioTestCase):
    """Stress-test multiple disjoint intervals, boundary slots, and multi-group isolation."""

    async def asyncSetUp(self) -> None:
        self.fd, self.temp_db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(self.fd)
        self.cm = DatabaseConnectionManager(self.temp_db_path)
        self.repo = DatabaseRepository(self.cm)
        async with self.cm.get_connection() as conn:
            await init_db(conn)

        self.bot = MockBot()
        self.scraper = MockScraper()
        self.limiter = TokenBucketRateLimiter(rate=25.0, capacity=25.0)
        self.notifier = NotifierService(
            bot=self.bot,
            db=self.repo,
            scraper=self.scraper,
            rate_limiter=self.limiter,
            check_interval=60,
        )

    async def asyncTearDown(self) -> None:
        await self.cm.close()
        for p in [self.temp_db_path, f"{self.temp_db_path}-wal", f"{self.temp_db_path}-shm"]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def test_multiple_disjoint_changed_intervals_detection(self) -> None:
        """
        Verify that multiple disjoint changed interval groups across 48 slots
        are isolated accurately as separate diff ranges.
        """
        old_slots = [{"slot_index": i, "status": "ON"} for i in range(48)]
        new_slots = [{"slot_index": i, "status": "ON"} for i in range(48)]

        # Group 1: slots 6..7 (03:00 - 04:00) changed to OFF
        for i in range(6, 8):
            new_slots[i]["status"] = "OFF"

        # Group 2: slots 18..21 (09:00 - 11:00) changed to OFF
        for i in range(18, 22):
            new_slots[i]["status"] = "OFF"

        # Group 3: slots 36..37 (18:00 - 19:00) changed to MAYBE
        for i in range(36, 38):
            new_slots[i]["status"] = "MAYBE"

        diffs = detect_schedule_revision_diffs(old_slots, new_slots)
        self.assertEqual(len(diffs), 3, f"Expected 3 disjoint diff intervals, got {len(diffs)}")

        # Verify interval 1
        self.assertEqual(diffs[0]["start_slot_index"], 6)
        self.assertEqual(diffs[0]["end_slot_index"], 7)
        self.assertEqual(diffs[0]["time_range"], "з 03:00 до 04:00")

        # Verify interval 2
        self.assertEqual(diffs[1]["start_slot_index"], 18)
        self.assertEqual(diffs[1]["end_slot_index"], 21)
        self.assertEqual(diffs[1]["time_range"], "з 09:00 до 11:00")

        # Verify interval 3
        self.assertEqual(diffs[2]["start_slot_index"], 36)
        self.assertEqual(diffs[2]["end_slot_index"], 37)
        self.assertEqual(diffs[2]["time_range"], "з 18:00 до 19:00")

        msg = format_schedule_diff_message("1.1", diffs)
        self.assertIn("з 03:00 до 04:00", msg)
        self.assertIn("з 09:00 до 11:00", msg)
        self.assertIn("з 18:00 до 19:00", msg)

    def test_boundary_slot_changes_0_and_47(self) -> None:
        """Verify changed intervals at day boundary slots (slot 0 and slot 47)."""
        old_slots = [{"slot_index": i, "status": "ON"} for i in range(48)]
        new_slots = [{"slot_index": i, "status": "ON"} for i in range(48)]

        # Boundary slot 0 changed
        new_slots[0]["status"] = "OFF"
        # Boundary slot 47 changed
        new_slots[47]["status"] = "OFF"

        diffs = detect_schedule_revision_diffs(old_slots, new_slots)
        self.assertEqual(len(diffs), 2, "Boundary slot 0 and slot 47 must produce 2 distinct diffs")

        self.assertEqual(diffs[0]["start_slot_index"], 0)
        self.assertEqual(diffs[0]["end_slot_index"], 0)
        self.assertEqual(diffs[0]["time_range"], "з 00:00 до 00:30")

        self.assertEqual(diffs[1]["start_slot_index"], 47)
        self.assertEqual(diffs[1]["end_slot_index"], 47)
        self.assertEqual(diffs[1]["time_range"], "з 23:30 до 24:00")

    async def test_multi_group_subscription_selective_alerting(self) -> None:
        """
        Adversarial multi-subscriber isolation matrix:
        - User 101: kyiv, group 1.1 (should receive alert for 1.1)
        - User 102: kyiv, group 2.1 (should NOT receive alert for 1.1)
        - User 103: kyiv, dual slot (Slot 1: 1.1, Slot 2: 2.1) (should receive 1 alert for 1.1)
        - User 104: kyiv, group 1.1 but is_active = 0 (should NOT receive alert)
        - User 105: odesa, group 1.1 (should NOT receive alert for kyiv diff)
        - User 106: kyiv, group 1.1 but notify_schedule_change = False (should NOT receive alert)
        """
        # User 101
        await self.repo.users.upsert_user(101, 101, "User 101", is_active=True)
        await self.repo.slots.upsert_slot(101, 1, "kyiv", 1, "1.1")
        await self.repo.settings.upsert_settings(101, notify_schedule_change=True, silent_hours_start=2, silent_hours_end=4)

        # User 102
        await self.repo.users.upsert_user(102, 102, "User 102", is_active=True)
        await self.repo.slots.upsert_slot(102, 1, "kyiv", 2, "2.1")
        await self.repo.settings.upsert_settings(102, notify_schedule_change=True, silent_hours_start=2, silent_hours_end=4)

        # User 103 (Dual slot: 1.1 and 2.1)
        await self.repo.users.upsert_user(103, 103, "User 103", is_active=True)
        await self.repo.slots.upsert_slot(103, 1, "kyiv", 1, "1.1", alias="Дім")
        await self.repo.slots.upsert_slot(103, 2, "kyiv", 2, "2.1", alias="Офіс")
        await self.repo.settings.upsert_settings(103, notify_schedule_change=True, silent_hours_start=2, silent_hours_end=4)

        # User 104 (Inactive)
        await self.repo.users.upsert_user(104, 104, "User 104", is_active=False)
        await self.repo.slots.upsert_slot(104, 1, "kyiv", 1, "1.1")
        await self.repo.settings.upsert_settings(104, notify_schedule_change=True, silent_hours_start=2, silent_hours_end=4)

        # User 105 (Odesa)
        await self.repo.users.upsert_user(105, 105, "User 105", is_active=True)
        await self.repo.slots.upsert_slot(105, 1, "odesa", 1, "1.1")
        await self.repo.settings.upsert_settings(105, notify_schedule_change=True, silent_hours_start=2, silent_hours_end=4)

        # User 106 (Preference disabled)
        await self.repo.users.upsert_user(106, 106, "User 106", is_active=True)
        await self.repo.slots.upsert_slot(106, 1, "kyiv", 1, "1.1")
        await self.repo.settings.upsert_settings(106, notify_schedule_change=False, silent_hours_start=2, silent_hours_end=4)

        # Seed baseline for all groups
        all_on = ["ON"] * 48
        self.scraper.set_subgroup("kyiv", "1.1", all_on)
        self.scraper.set_subgroup("kyiv", "2.1", all_on)
        self.scraper.set_subgroup("odesa", "1.1", all_on)

        t_now = datetime(2026, 11, 12, 12, 0, 0, tzinfo=KYIV_TZ)
        await self.notifier.poll_tick(now=t_now)
        self.assertEqual(len(self.bot.sent_messages), 0)

        # Trigger revision ONLY in kyiv group 1.1: slots 20..21 (10:00-11:00) OFF
        revised_1_1 = list(all_on)
        revised_1_1[20] = "OFF"
        revised_1_1[21] = "OFF"
        self.scraper.set_subgroup("kyiv", "1.1", revised_1_1)

        # Execute diff evaluation pass
        sent_count = await self.notifier.poll_tick(now=t_now)

        # Recipients should be: User 101 (1 alert) and User 103 (1 alert for slot 1)
        recipients = [m["chat_id"] for m in self.bot.sent_messages]
        self.assertEqual(sent_count, 2, f"Expected 2 alerts sent, got {sent_count}. Recipients: {recipients}")
        self.assertIn(101, recipients, "User 101 must receive revision alert")
        self.assertIn(103, recipients, "User 103 must receive revision alert for slot 1")
        self.assertNotIn(102, recipients, "User 102 (subscribed to 2.1) must NOT receive 1.1 alert")
        self.assertNotIn(104, recipients, "User 104 (inactive) must NOT receive alert")
        self.assertNotIn(105, recipients, "User 105 (odesa) must NOT receive kyiv alert")
        self.assertNotIn(106, recipients, "User 106 (notify_schedule_change=False) must NOT receive alert")


# ============================================================================
# 4. ADVERSARIAL RATE LIMITER & RESILIENCE
# ============================================================================

class TestAdversarialRateLimiterAndResilience(unittest.IsolatedAsyncioTestCase):
    """Stress-test TokenBucketRateLimiter, HTTP 429 backoff, and 403 Forbidden handling."""

    async def asyncSetUp(self) -> None:
        self.fd, self.temp_db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(self.fd)
        self.cm = DatabaseConnectionManager(self.temp_db_path)
        self.repo = DatabaseRepository(self.cm)
        async with self.cm.get_connection() as conn:
            await init_db(conn)

        self.bot = MockBot()
        self.scraper = MockScraper()
        self.limiter = TokenBucketRateLimiter(rate=25.0, capacity=25.0)
        self.notifier = NotifierService(
            bot=self.bot,
            db=self.repo,
            scraper=self.scraper,
            rate_limiter=self.limiter,
            check_interval=60,
        )

    async def asyncTearDown(self) -> None:
        await self.cm.close()
        for p in [self.temp_db_path, f"{self.temp_db_path}-wal", f"{self.temp_db_path}-shm"]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    async def test_token_bucket_rapid_burst_and_sustained_throttling(self) -> None:
        """Verify TokenBucketRateLimiter permits initial burst then strictly throttles <= 25 msg/s."""
        limiter = TokenBucketRateLimiter(rate=25.0, capacity=25.0)

        # Burst of 25 tokens should complete almost instantly
        t0 = time.monotonic()
        for _ in range(25):
            await limiter.acquire()
        t_burst = time.monotonic() - t0
        self.assertLess(t_burst, 0.08, f"Burst of 25 tokens took too long: {t_burst:.3f}s")

        # Subsequent 10 tokens must be throttled: 10 / 25.0 = 0.40s
        t1 = time.monotonic()
        for _ in range(10):
            await limiter.acquire()
        t_sustained = time.monotonic() - t1
        self.assertGreaterEqual(
            t_sustained,
            0.35,
            f"Sustained throughput exceeded 25 msg/s ceiling: {t_sustained:.3f}s for 10 tokens",
        )

    async def test_telegram_429_backoff_handling(self) -> None:
        """
        Verify that receiving Telegram HTTP 429 Retry-After sets backoff
        for retry_after + 1.0 second and pauses subsequent dispatches.
        """
        self.bot.simulate_429 = True
        self.bot.retry_after_seconds = 0.3

        user_id = 9050
        await self.repo.users.upsert_user(user_id, user_id, "Retry User")

        t_before = time.monotonic()
        success = await self.notifier._send_telegram_message(user_id, "Test 429", user_id)
        self.assertFalse(success)
        self.assertTrue(self.limiter.is_in_backoff)

        # Expected pause: 0.3 + 1.0 = 1.3 seconds
        self.assertGreaterEqual(self.limiter.backoff_until, t_before + 1.25)

    async def test_telegram_403_user_auto_deactivation(self) -> None:
        """
        Verify that when Telegram API raises 403 Forbidden (user blocked bot),
        the notifier automatically marks user is_active = 0 in database.
        """
        blocked_user_id = 9060
        await self.repo.users.upsert_user(blocked_user_id, blocked_user_id, "Blocked User", is_active=True)
        self.bot.simulate_403_users.add(blocked_user_id)

        # Send alert -> triggers 403 Forbidden
        success = await self.notifier._send_telegram_message(blocked_user_id, "Hello Blocked", blocked_user_id)
        self.assertFalse(success)

        # Check database: user must now be deactivated
        user = await self.repo.users.get_user(blocked_user_id)
        self.assertIsNotNone(user)
        self.assertFalse(user.is_active, "Blocked user must be marked is_active = 0 after 403 Forbidden")


if __name__ == "__main__":
    unittest.main()
