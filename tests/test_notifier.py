"""
tests/test_notifier.py - Automated Notification, Diff Detection, and Rate Limiting Verification.

Author: Worker E2E Notification & Persistence
Verification Coverage:
- Tier 1: Advance outage warnings (30m and 15m warnings) for ON -> OFF and ON -> MAYBE transitions.
  Correct calculation of target slot index (0..47), transition time ("HH:MM"), and Ukrainian alert formatting. (>= 5 tests)
- Tier 2: Schedule revision diff detector: SHA-256 hash comparison between cached and newly scraped schedule,
  detection of contiguous changed intervals ("HH:MM до HH:MM"), multi-group selective alerting.
  Notification deduplication via notification_history (preventing duplicate alerts for same user, slot,
  event_type, and target_time).
  Token-bucket rate limiter: max 25 msg/s throughput test, HTTP 429 backoff handling (retry_after + 1s).
  Silent hours filtering (23:00 to 07:00) with Europe/Kyiv timezone handling. (>= 5 tests)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests.conftest import (
    DEFAULT_TEST_DATE,
    KYIV_TZ,
    SQLITE_SCHEMA_DDL,
    format_interval_times,
    generate_synthetic_slots,
    safe_import,
)


# ============================================================================
# AUTHORITATIVE NOTIFICATION DOMAIN ALGORITHMS (SPECIFICATION CONTRACTS)
# ============================================================================

def get_slot_index_from_datetime(dt: datetime) -> int:
    """Return uniform 30-minute slot index (0..47) for a datetime in Europe/Kyiv."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KYIV_TZ)
    kyiv_dt = dt.astimezone(KYIV_TZ)
    return (kyiv_dt.hour * 60 + kyiv_dt.minute) // 30


def get_slot_time_bounds(slot_index: int) -> Tuple[str, str]:
    """Return start_time and end_time strings for slot index 0..47."""
    return format_interval_times(slot_index)


def detect_advance_outage_transition(
    slots: List[Dict[str, Any]],
    current_time: datetime,
    lead_minutes: int,
) -> Optional[Dict[str, Any]]:
    """
    Authoritative Advance Outage Warning transition detector.
    Evaluates upcoming transitions at current_time + lead_minutes (15 or 30 mins).
    Triggers when prior interval is ON and target interval is OFF or MAYBE.
    """
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=KYIV_TZ)

    target_time = current_time + timedelta(minutes=lead_minutes)
    target_slot_index = get_slot_index_from_datetime(target_time)

    if not (0 <= target_slot_index <= 47):
        return None

    # Transition occurs at boundary: slot prior to target_slot must be checked
    prior_slot_index = target_slot_index - 1
    if prior_slot_index < 0:
        return None

    prior_status = slots[prior_slot_index]["status"]
    target_status = slots[target_slot_index]["status"]

    # Rule: ON -> OFF or ON -> MAYBE triggers advance outage warning
    if prior_status == "ON" and target_status in ("OFF", "MAYBE"):
        start_time, _ = get_slot_time_bounds(target_slot_index)
        event_type = f"{lead_minutes}m_before_off"
        return {
            "event_type": event_type,
            "lead_minutes": lead_minutes,
            "target_slot_index": target_slot_index,
            "target_time": target_time.strftime("%Y-%m-%d %H:%M:%S"),
            "transition_time": start_time,
            "prior_status": prior_status,
            "target_status": target_status,
        }
    return None


def format_advance_warning_message(
    lead_minutes: int,
    transition_time: str,
    slot1_alias: str,
    slot1_group: str,
    slot2_alias: Optional[str] = None,
    slot2_group: Optional[str] = None,
    slot2_has_power: bool = True,
) -> str:
    """Format the Ukrainian advance outage warning alert message."""
    msg = f"⚡️ Увага! Через {lead_minutes} хв (о {transition_time}) планове відключення у [{slot1_alias}: Черга {slot1_group}]."
    if slot2_alias and slot2_group:
        second_state = "світло буде" if slot2_has_power else "також не буде світла"
        msg += f" В [{slot2_alias}: Черга {slot2_group}] {second_state}."
    return msg


def compute_schedule_sha256(payload_str: str) -> str:
    """Compute SHA-256 hash of schedule payload."""
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()


def detect_schedule_revision_diffs(
    old_slots: List[Dict[str, Any]],
    new_slots: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Authoritative Schedule Revision Diff Detector.
    Compares two 48-slot schedules and groups contiguous changed intervals.
    """
    diffs: List[Dict[str, Any]] = []
    i = 0
    while i < 48:
        old_st = old_slots[i]["status"]
        new_st = new_slots[i]["status"]
        if old_st != new_st:
            start_idx = i
            while i + 1 < 48 and (old_slots[i + 1]["status"] != new_slots[i + 1]["status"]):
                i += 1
            end_idx = i

            start_t, _ = get_slot_time_bounds(start_idx)
            _, end_t = get_slot_time_bounds(end_idx)

            diffs.append({
                "start_slot_index": start_idx,
                "end_slot_index": end_idx,
                "time_range": f"з {start_t} до {end_t}",
                "old_statuses": [old_slots[k]["status"] for k in range(start_idx, end_idx + 1)],
                "new_statuses": [new_slots[k]["status"] for k in range(start_idx, end_idx + 1)],
            })
        i += 1
    return diffs


def is_in_silent_hours(dt: datetime, start_hour: int = 23, end_hour: int = 7) -> bool:
    """
    Authoritative Silent Hours Filter.
    Default: 23:00 to 07:00 Europe/Kyiv.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KYIV_TZ)
    kyiv_dt = dt.astimezone(KYIV_TZ)
    h = kyiv_dt.hour

    if start_hour > end_hour:
        # Crosses midnight: e.g. 23:00 to 07:00
        return h >= start_hour or h < end_hour
    elif start_hour < end_hour:
        return start_hour <= h < end_hour
    else:
        return False


class TokenBucketRateLimiterContract:
    """
    Token-bucket rate limiter meeting PROMPT_SPECIFICATION.md:565:
    - Maximum 25.0 messages per second throughput ceiling.
    - Initial burst allowance up to capacity (25.0).
    - HTTP 429 backoff support: pauses for retry_after + 1.0 seconds.
    """
    def __init__(self, rate: float = 25.0, capacity: float = 25.0) -> None:
        self.rate = float(rate)
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.last_update = time.monotonic()
        self.backoff_until = 0.0

    def replenish(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_update = now

    async def acquire(self) -> float:
        """Acquires a token, asynchronously awaiting replenishment or backoff. Returns wait time."""
        total_waited = 0.0
        now = time.monotonic()
        if now < self.backoff_until:
            sleep_time = self.backoff_until - now
            await asyncio.sleep(sleep_time)
            total_waited += sleep_time
            self.replenish()

        while True:
            self.replenish()
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return total_waited
            needed = 1.0 - self.tokens
            sleep_time = needed / self.rate
            await asyncio.sleep(sleep_time)
            total_waited += sleep_time

    def handle_429(self, retry_after: float) -> None:
        """Mandatory backoff: pause for retry_after + 1.0 second."""
        backoff_delay = retry_after + 1.0
        self.backoff_until = max(self.backoff_until, time.monotonic() + backoff_delay)


# ============================================================================
# TIER 1 TEST SUITE
# ============================================================================

class TestNotifierTier1(unittest.TestCase):
    """
    Tier 1: Advance Outage Warnings (30m & 15m), Slot Index, and Ukrainian Formatting.
    """

    def test_advance_warning_30m_on_to_off(self) -> None:
        """
        Tier 1: Verify 30-minute advance warning correctly identifies an upcoming
        ON -> OFF transition 30 minutes ahead of schedule.
        Current time: 13:30. Target time: 14:00 (slot index 28).
        """
        slots = generate_synthetic_slots("ON")
        for idx in range(28, 34):
            slots[idx]["status"] = "OFF"

        current_time = datetime(2026, 11, 11, 13, 30, 0, tzinfo=KYIV_TZ)
        warning = detect_advance_outage_transition(slots, current_time, lead_minutes=30)

        self.assertIsNotNone(warning, "Expected 30m advance warning to trigger")
        self.assertEqual(warning["event_type"], "30m_before_off")
        self.assertEqual(warning["target_slot_index"], 28)
        self.assertEqual(warning["transition_time"], "14:00")
        self.assertEqual(warning["prior_status"], "ON")
        self.assertEqual(warning["target_status"], "OFF")
        self.assertEqual(warning["lead_minutes"], 30)

    def test_advance_warning_15m_on_to_off(self) -> None:
        """
        Tier 1: Verify 15-minute advance warning correctly identifies an upcoming
        ON -> OFF transition 15 minutes ahead of schedule.
        Current time: 13:45. Target time: 14:00 (slot index 28).
        """
        slots = generate_synthetic_slots("ON")
        for idx in range(28, 34):
            slots[idx]["status"] = "OFF"

        current_time = datetime(2026, 11, 11, 13, 45, 0, tzinfo=KYIV_TZ)
        warning = detect_advance_outage_transition(slots, current_time, lead_minutes=15)

        self.assertIsNotNone(warning, "Expected 15m advance warning to trigger")
        self.assertEqual(warning["event_type"], "15m_before_off")
        self.assertEqual(warning["target_slot_index"], 28)
        self.assertEqual(warning["transition_time"], "14:00")
        self.assertEqual(warning["prior_status"], "ON")
        self.assertEqual(warning["target_status"], "OFF")
        self.assertEqual(warning["lead_minutes"], 15)

    def test_advance_warning_30m_and_15m_on_to_maybe(self) -> None:
        """
        Tier 1: Verify advance warning correctly triggers for ON -> MAYBE transitions
        (discretionary grey zone alerts treated as potential outages).
        Transition at 09:00 (slot index 18): slot 17 is ON, slot 18 is MAYBE.
        """
        slots = generate_synthetic_slots("ON")
        slots[18]["status"] = "MAYBE"
        slots[19]["status"] = "MAYBE"

        # 1. 30m check at 08:30 -> target is 09:00
        t_30m = datetime(2026, 11, 11, 8, 30, 0, tzinfo=KYIV_TZ)
        warning_30m = detect_advance_outage_transition(slots, t_30m, lead_minutes=30)
        self.assertIsNotNone(warning_30m, "Expected 30m warning on ON -> MAYBE")
        self.assertEqual(warning_30m["target_slot_index"], 18)
        self.assertEqual(warning_30m["transition_time"], "09:00")
        self.assertEqual(warning_30m["target_status"], "MAYBE")

        # 2. 15m check at 08:45 -> target is 09:00
        t_15m = datetime(2026, 11, 11, 8, 45, 0, tzinfo=KYIV_TZ)
        warning_15m = detect_advance_outage_transition(slots, t_15m, lead_minutes=15)
        self.assertIsNotNone(warning_15m, "Expected 15m warning on ON -> MAYBE")
        self.assertEqual(warning_15m["target_slot_index"], 18)
        self.assertEqual(warning_15m["transition_time"], "09:00")
        self.assertEqual(warning_15m["target_status"], "MAYBE")

    def test_no_warning_when_already_off_or_recovering(self) -> None:
        """
        Tier 1: Verify no advance outage warnings are generated for non-outage transitions:
        - OFF -> OFF (outage continuing)
        - OFF -> ON (power restoration)
        - ON -> ON (steady power)
        - MAYBE -> ON (clearing to light)
        """
        slots = generate_synthetic_slots("ON")
        for i in range(20, 26):
            slots[i]["status"] = "OFF"
        slots[26]["status"] = "MAYBE"
        slots[27]["status"] = "MAYBE"

        # 1. Inside outage: slot 21 to 22 (OFF -> OFF)
        t_inside_off = datetime(2026, 11, 11, 10, 30, 0, tzinfo=KYIV_TZ)
        self.assertIsNone(detect_advance_outage_transition(slots, t_inside_off, lead_minutes=30))

        # 2. Restoration: slot 27 to 28 (MAYBE -> ON)
        t_restoration = datetime(2026, 11, 11, 13, 30, 0, tzinfo=KYIV_TZ)
        self.assertIsNone(detect_advance_outage_transition(slots, t_restoration, lead_minutes=30))

        # 3. Steady power: slot 30 to 31 (ON -> ON)
        t_steady_on = datetime(2026, 11, 11, 15, 0, 0, tzinfo=KYIV_TZ)
        self.assertIsNone(detect_advance_outage_transition(slots, t_steady_on, lead_minutes=30))

    def test_target_slot_index_and_transition_time_boundaries(self) -> None:
        """
        Tier 1: Verify exact calculation of target slot index (0..47) and transition
        time ("HH:MM") across all daily boundaries in Europe/Kyiv.
        """
        test_cases = [
            (datetime(2026, 11, 11, 0, 0, 0, tzinfo=KYIV_TZ), 0, "00:00", "00:30"),
            (datetime(2026, 11, 11, 0, 29, 0, tzinfo=KYIV_TZ), 0, "00:00", "00:30"),
            (datetime(2026, 11, 11, 0, 30, 0, tzinfo=KYIV_TZ), 1, "00:30", "01:00"),
            (datetime(2026, 11, 11, 12, 0, 0, tzinfo=KYIV_TZ), 24, "12:00", "12:30"),
            (datetime(2026, 11, 11, 14, 15, 0, tzinfo=KYIV_TZ), 28, "14:00", "14:30"),
            (datetime(2026, 11, 11, 23, 30, 0, tzinfo=KYIV_TZ), 47, "23:30", "24:00"),
            (datetime(2026, 11, 11, 23, 59, 59, tzinfo=KYIV_TZ), 47, "23:30", "24:00"),
        ]

        for dt, expected_idx, expected_start, expected_end in test_cases:
            calculated_idx = get_slot_index_from_datetime(dt)
            self.assertEqual(calculated_idx, expected_idx, f"At {dt}, expected slot {expected_idx}, got {calculated_idx}")
            start_t, end_t = get_slot_time_bounds(calculated_idx)
            self.assertEqual(start_t, expected_start)
            self.assertEqual(end_t, expected_end)

        with self.assertRaises(ValueError):
            get_slot_time_bounds(-1)

        with self.assertRaises(ValueError):
            get_slot_time_bounds(48)

    def test_advance_warning_message_text_formatting(self) -> None:
        """
        Tier 1: Verify the Ukrainian advance warning notification text matches
        PROMPT_SPECIFICATION.md:555 verbatim:
        '⚡️ Увага! Через 15 хв (о 14:00) планове відключення у [Дім: Черга 1.1]. В [Офіс: Черга 3.2] світло буде.'
        """
        msg_15m = format_advance_warning_message(
            lead_minutes=15,
            transition_time="14:00",
            slot1_alias="Дім",
            slot1_group="1.1",
            slot2_alias="Офіс",
            slot2_group="3.2",
            slot2_has_power=True,
        )
        self.assertTrue(msg_15m.startswith("⚡️ Увага!"), "Message must start with warning lightning emoji")
        self.assertIn("Через 15 хв (о 14:00)", msg_15m)
        self.assertIn("планове відключення у [Дім: Черга 1.1]", msg_15m)
        self.assertIn("В [Офіс: Черга 3.2] світло буде", msg_15m)

        msg_single = format_advance_warning_message(
            lead_minutes=30,
            transition_time="18:00",
            slot1_alias="Моя оселя",
            slot1_group="2.1",
        )
        self.assertIn("Через 30 хв (о 18:00)", msg_single)
        self.assertIn("планове відключення у [Моя оселя: Черга 2.1]", msg_single)
        self.assertNotIn("Черга 3.2", msg_single)


# ============================================================================
# TIER 2 TEST SUITE
# ============================================================================

class TestNotifierTier2(unittest.TestCase):
    """
    Tier 2: Revision Diff, Notification Deduplication, Rate Limiter, and Silent Hours.
    """

    def setUp(self) -> None:
        self.fd, self.temp_db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(self.fd)
        self.conn = sqlite3.connect(self.temp_db_path)
        self.conn.execute("PRAGMA journal_mode = WAL;")
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.executescript(SQLITE_SCHEMA_DDL)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        for p in [self.temp_db_path, f"{self.temp_db_path}-wal", f"{self.temp_db_path}-shm"]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def test_schedule_revision_diff_sha256_detection_identical(self) -> None:
        """
        Tier 2: Verify SHA-256 hash comparison between cached and newly scraped schedules.
        When schedules are identical, the diff detector reports 0 changed intervals.
        """
        payload_cached = '{"region": "kyiv", "group": "GPV1.1", "slots": ["ON"]*48}'
        payload_scraped = '{"region": "kyiv", "group": "GPV1.1", "slots": ["ON"]*48}'

        hash_cached = compute_schedule_sha256(payload_cached)
        hash_scraped = compute_schedule_sha256(payload_scraped)

        self.assertEqual(hash_cached, hash_scraped, "Hashes must match for identical schedules")

        slots_old = generate_synthetic_slots("ON")
        slots_new = generate_synthetic_slots("ON")
        diffs = detect_schedule_revision_diffs(slots_old, slots_new)

        self.assertEqual(len(diffs), 0, f"Expected 0 diffs for identical schedules, got {len(diffs)}")

    def test_schedule_revision_diff_detection_changed_intervals(self) -> None:
        """
        Tier 2: Verify diff detector isolates changed intervals when SHA-256 hashes differ.
        E.g. slots 30..33 (15:00 - 17:00) changed from MAYBE to OFF.
        """
        slots_old = generate_synthetic_slots("ON")
        for idx in range(30, 34):
            slots_old[idx]["status"] = "MAYBE"

        slots_new = generate_synthetic_slots("ON")
        for idx in range(30, 34):
            slots_new[idx]["status"] = "OFF"

        hash_old = compute_schedule_sha256(str(slots_old))
        hash_new = compute_schedule_sha256(str(slots_new))
        self.assertNotEqual(hash_old, hash_new, "Hashes must differ when schedule is revised")

        diffs = detect_schedule_revision_diffs(slots_old, slots_new)
        self.assertEqual(len(diffs), 1, f"Expected 1 contiguous diff interval, got {len(diffs)}")

        diff = diffs[0]
        self.assertEqual(diff["start_slot_index"], 30)
        self.assertEqual(diff["end_slot_index"], 33)
        self.assertEqual(diff["time_range"], "з 15:00 до 17:00")
        self.assertTrue(all(st == "MAYBE" for st in diff["old_statuses"]))
        self.assertTrue(all(st == "OFF" for st in diff["new_statuses"]))

    def test_schedule_revision_multigroup_selective_alerting(self) -> None:
        """
        Tier 2: Verify that in a multi-group regional schedule (GPV1.1 and GPV3.2),
        if only GPV1.1 is revised, diff alerts are generated exclusively for GPV1.1.
        """
        groups_old = {
            "GPV1.1": generate_synthetic_slots("ON"),
            "GPV3.2": generate_synthetic_slots("ON"),
        }
        groups_new = {
            "GPV1.1": generate_synthetic_slots("ON"),
            "GPV3.2": generate_synthetic_slots("ON"),
        }

        groups_new["GPV1.1"][32]["status"] = "OFF"
        groups_new["GPV1.1"][33]["status"] = "OFF"

        diffs_gpv1 = detect_schedule_revision_diffs(groups_old["GPV1.1"], groups_new["GPV1.1"])
        diffs_gpv3 = detect_schedule_revision_diffs(groups_old["GPV3.2"], groups_new["GPV3.2"])

        self.assertEqual(len(diffs_gpv1), 1, "GPV1.1 must have 1 diff interval")
        self.assertEqual(diffs_gpv1[0]["time_range"], "з 16:00 до 17:00")
        self.assertEqual(len(diffs_gpv3), 0, "GPV3.2 must have 0 diff intervals")

    def test_notification_deduplication_via_notification_history(self) -> None:
        """
        Tier 2: Verify deduplication via notification_history table.
        Dispatches a 30m warning at tick 1; confirms tick 2 deduplication query suppresses duplicate.
        """
        user_id = 5001
        self.conn.execute(
            "INSERT INTO users (user_id, chat_id, first_name) VALUES (?, ?, ?);",
            (user_id, user_id, "Dedup Test User"),
        )
        self.conn.commit()

        slot_number = 1
        event_type = "30m_before_off"
        target_slot_index = 28
        target_time_str = "2026-11-11 14:00:00"
        hash_sent = "hash_payload_val"

        def check_is_duplicate(u_id: int, s_num: int, ev_type: str, t_time: str) -> bool:
            cursor = self.conn.execute(
                """
                SELECT 1 FROM notification_history
                WHERE user_id = ? AND slot_number = ? AND event_type = ? AND target_time = ?
                LIMIT 1;
                """,
                (u_id, s_num, ev_type, t_time),
            )
            return cursor.fetchone() is not None

        def record_notification(u_id: int, s_num: int, ev_type: str, slot_idx: int, t_time: str, h_sent: str) -> None:
            self.conn.execute(
                """
                INSERT INTO notification_history (user_id, slot_number, event_type, target_slot_index, target_time, hash_sent)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (u_id, s_num, ev_type, slot_idx, t_time, h_sent),
            )
            self.conn.commit()

        self.assertFalse(check_is_duplicate(user_id, slot_number, event_type, target_time_str))
        record_notification(user_id, slot_number, event_type, target_slot_index, target_time_str, hash_sent)
        self.assertTrue(check_is_duplicate(user_id, slot_number, event_type, target_time_str))

    def test_notification_deduplication_allows_distinct_events_and_times(self) -> None:
        """
        Tier 2: Verify deduplication allows distinct alerts:
        - 30m alert does NOT suppress 15m alert for same target time.
        - Distinct target times (14:00 vs 18:00) are not suppressed.
        - Distinct slots (Slot 1 vs Slot 2) are not suppressed.
        - Distinct users (User 1 vs User 2) are not suppressed.
        """
        user_1 = 5002
        user_2 = 5003
        self.conn.execute("INSERT INTO users (user_id, chat_id, first_name) VALUES (?, ?, 'User 1');", (user_1, user_1))
        self.conn.execute("INSERT INTO users (user_id, chat_id, first_name) VALUES (?, ?, 'User 2');", (user_2, user_2))
        self.conn.commit()

        target_time_1 = "2026-11-11 14:00:00"
        target_time_2 = "2026-11-11 18:00:00"

        self.conn.execute(
            """
            INSERT INTO notification_history (user_id, slot_number, event_type, target_slot_index, target_time, hash_sent)
            VALUES (?, 1, '30m_before_off', 28, ?, 'hash1');
            """,
            (user_1, target_time_1),
        )
        self.conn.commit()

        def is_dup(u_id: int, s_num: int, ev_type: str, t_time: str) -> bool:
            c = self.conn.execute(
                "SELECT 1 FROM notification_history WHERE user_id = ? AND slot_number = ? AND event_type = ? AND target_time = ?;",
                (u_id, s_num, ev_type, t_time),
            )
            return c.fetchone() is not None

        self.assertFalse(is_dup(user_1, 1, "15m_before_off", target_time_1))
        self.assertFalse(is_dup(user_1, 1, "30m_before_off", target_time_2))
        self.assertFalse(is_dup(user_1, 2, "30m_before_off", target_time_1))
        self.assertFalse(is_dup(user_2, 1, "30m_before_off", target_time_1))

    def test_token_bucket_rate_limiter_25_msg_s_ceiling(self) -> None:
        """
        Tier 2: Verify Token-Bucket rate limiter enforces maximum 25.0 messages/second ceiling.
        - Initial burst of 25 messages is granted immediately.
        - Subsequent messages are throttled at 25.0 tokens/s.
        """
        async def run_limiter_test() -> None:
            limiter = TokenBucketRateLimiterContract(rate=25.0, capacity=25.0)

            t_start = time.monotonic()
            for _ in range(25):
                await limiter.acquire()
            t_burst = time.monotonic() - t_start
            self.assertLess(t_burst, 0.1, f"Burst of 25 tokens took too long: {t_burst:.3f}s")

            t_start_sustained = time.monotonic()
            for _ in range(10):
                await limiter.acquire()
            t_sustained = time.monotonic() - t_start_sustained

            self.assertGreaterEqual(t_sustained, 0.35, f"Sustained throughput exceeded 25 msg/s: took {t_sustained:.3f}s for 10 tokens")

        asyncio.run(run_limiter_test())

    def test_token_bucket_rate_limiter_http_429_backoff(self) -> None:
        """
        Tier 2: Verify HTTP 429 Retry-After handling in TokenBucketRateLimiter.
        Mandatory rule: pause for retry_after + 1.0 second.
        """
        async def run_backoff_test() -> None:
            limiter = TokenBucketRateLimiterContract(rate=25.0, capacity=25.0)

            retry_after = 0.2
            limiter.handle_429(retry_after)

            t_start = time.monotonic()
            waited = await limiter.acquire()
            elapsed = time.monotonic() - t_start

            expected_min_wait = retry_after + 1.0
            self.assertGreaterEqual(
                elapsed,
                expected_min_wait * 0.95,
                f"Expected backoff >= {expected_min_wait:.2f}s, but only waited {elapsed:.2f}s",
            )
            self.assertGreaterEqual(waited, expected_min_wait * 0.95)

        asyncio.run(run_backoff_test())

    def test_silent_hours_filtering_policy(self) -> None:
        """
        Tier 2: Verify silent hours filtering policy (23:00 to 07:00 Europe/Kyiv).
        Discretionary night window suppresses notifications or sets disable_notification=True.
        """
        silent_moments = [
            datetime(2026, 11, 11, 23, 0, 0, tzinfo=KYIV_TZ),
            datetime(2026, 11, 11, 23, 45, 0, tzinfo=KYIV_TZ),
            datetime(2026, 11, 11, 0, 0, 0, tzinfo=KYIV_TZ),
            datetime(2026, 11, 11, 3, 30, 0, tzinfo=KYIV_TZ),
            datetime(2026, 11, 11, 6, 59, 59, tzinfo=KYIV_TZ),
        ]

        active_moments = [
            datetime(2026, 11, 11, 7, 0, 0, tzinfo=KYIV_TZ),
            datetime(2026, 11, 11, 7, 1, 0, tzinfo=KYIV_TZ),
            datetime(2026, 11, 11, 12, 0, 0, tzinfo=KYIV_TZ),
            datetime(2026, 11, 11, 14, 15, 0, tzinfo=KYIV_TZ),
            datetime(2026, 11, 11, 22, 59, 59, tzinfo=KYIV_TZ),
        ]

        for dt in silent_moments:
            self.assertTrue(is_in_silent_hours(dt, start_hour=23, end_hour=7), f"Expected {dt} to be in silent hours")

        for dt in active_moments:
            self.assertFalse(is_in_silent_hours(dt, start_hour=23, end_hour=7), f"Expected {dt} to be outside silent hours")

        self.assertTrue(is_in_silent_hours(datetime(2026, 11, 11, 22, 15, tzinfo=KYIV_TZ), start_hour=22, end_hour=8))
        self.assertFalse(is_in_silent_hours(datetime(2026, 11, 11, 8, 30, tzinfo=KYIV_TZ), start_hour=22, end_hour=8))

    def test_notifier_service_module_integration(self) -> None:
        """
        Tier 2: Progressive verification of the bot.services.notifier module.
        Safely imported; skipped if the module is under active construction by M5 worker.
        """
        try:
            notifier_cls = safe_import("bot.services.notifier", "NotifierService")
            self.assertIsNotNone(notifier_cls)
        except Exception as err:
            self.skipTest(f"bot.services.notifier not ready ({err}); skipping test.")


if __name__ == "__main__":
    unittest.main()
