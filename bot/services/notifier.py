"""
bot/services/notifier.py - Automated Notification, Diff Detection, and Rate Limiting Service.

Provides:
- 30m and 15m advance outage warnings for upcoming ON -> OFF and ON -> MAYBE transitions.
- Authoritative calculation of target slot index (0..47) and transition times ("HH:MM").
- Schedule revision diff detection via SHA-256 hash comparison.
- Multi-group selective alerting for subscribed users.
- Notification deduplication via notification_history.
- Silent hours suppression (default 23:00 to 07:00 Europe/Kyiv).
- NotifierService background polling orchestrator.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

from bot.models.enums import DSORegion, IntervalStatus
from bot.services.rate_limiter import TokenBucketRateLimiter

logger = logging.getLogger("bot.services.notifier")
KYIV_TZ = ZoneInfo("Europe/Kyiv")

# Try to import Aiogram exception classes if available
try:
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
except ImportError:
    TelegramForbiddenError = None
    TelegramRetryAfter = None


# ============================================================================
# AUTHORITATIVE NOTIFICATION DOMAIN ALGORITHMS
# ============================================================================

def get_slot_index_from_datetime(dt: datetime) -> int:
    """Return uniform 30-minute slot index (0..47) for a datetime in Europe/Kyiv."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KYIV_TZ)
    kyiv_dt = dt.astimezone(KYIV_TZ)
    return (kyiv_dt.hour * 60 + kyiv_dt.minute) // 30


def get_slot_time_bounds(slot_index: int) -> Tuple[str, str]:
    """Return start_time and end_time strings for slot index 0..47."""
    if not (0 <= slot_index <= 47):
        raise ValueError(f"Slot index out of range 0..47: {slot_index}")
    start_minutes = slot_index * 30
    end_minutes = (slot_index + 1) * 30
    start_h, start_m = divmod(start_minutes, 60)
    end_h, end_m = divmod(end_minutes, 60)
    start_str = f"{start_h:02d}:{start_m:02d}"
    end_str = "24:00" if end_h == 24 else f"{end_h:02d}:{end_m:02d}"
    return start_str, end_str


def detect_advance_outage_transition(
    slots: List[Any],
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

    def _extract_status(val: Any) -> str:
        if isinstance(val, dict):
            st = val.get("status", "")
        elif hasattr(val, "status"):
            st = val.status
        else:
            st = val
        if hasattr(st, "value"):
            st = st.value
        return str(st)

    prior_status = _extract_status(slots[prior_slot_index])
    target_status = _extract_status(slots[target_slot_index])

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
    msg = (
        f"⚡️ Увага! Через {lead_minutes} хв (о {transition_time}) "
        f"планове відключення у [{slot1_alias}: Черга {slot1_group}]."
    )
    if slot2_alias and slot2_group:
        second_state = "світло буде" if slot2_has_power else "також не буде світла"
        msg += f" В [{slot2_alias}: Черга {slot2_group}] {second_state}."
    return msg


def compute_schedule_sha256(payload_str: str) -> str:
    """Compute SHA-256 hash of schedule payload."""
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()


def detect_schedule_revision_diffs(
    old_slots: List[Any],
    new_slots: List[Any],
) -> List[Dict[str, Any]]:
    """
    Authoritative Schedule Revision Diff Detector.
    Compares two 48-slot schedules and groups contiguous changed intervals.
    """
    def _extract_status(val: Any) -> str:
        if isinstance(val, dict):
            st = val.get("status", "")
        elif hasattr(val, "status"):
            st = val.status
        else:
            st = val
        if hasattr(st, "value"):
            st = st.value
        return str(st)

    diffs: List[Dict[str, Any]] = []
    i = 0
    limit = min(len(old_slots), len(new_slots), 48)
    while i < limit:
        old_st = _extract_status(old_slots[i])
        new_st = _extract_status(new_slots[i])
        if old_st != new_st:
            start_idx = i
            while (
                i + 1 < limit
                and (_extract_status(old_slots[i + 1]) != _extract_status(new_slots[i + 1]))
            ):
                i += 1
            end_idx = i

            start_t, _ = get_slot_time_bounds(start_idx)
            _, end_t = get_slot_time_bounds(end_idx)

            diffs.append({
                "start_slot_index": start_idx,
                "end_slot_index": end_idx,
                "time_range": f"з {start_t} до {end_t}",
                "old_statuses": [_extract_status(old_slots[k]) for k in range(start_idx, end_idx + 1)],
                "new_statuses": [_extract_status(new_slots[k]) for k in range(start_idx, end_idx + 1)],
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


def format_schedule_diff_message(
    subgroup_id: str,
    diffs: List[Dict[str, Any]],
) -> str:
    """
    Format the Ukrainian schedule revision diff alert message.
    Example:
    '📢 Графік ДТЕК змінено! У вашій черзі 1.1 додано відключення з 16:00 до 18:00.'
    """
    ranges_text = []
    for d in diffs:
        t_range = d["time_range"]
        new_statuses = d.get("new_statuses", [])
        primary_st = new_statuses[0] if new_statuses else "OFF"
        if primary_st == "OFF":
            desc = "додано відключення"
        elif primary_st == "ON":
            desc = "відновлено світло"
        elif primary_st == "MAYBE":
            desc = "можливе відключення (сіра зона)"
        else:
            desc = "змінено графік"
        ranges_text.append(f"{desc} {t_range}")
    summary = "; ".join(ranges_text)
    return f"📢 Графік ДТЕК змінено! У вашій черзі {subgroup_id} {summary}."


# ============================================================================
# NOTIFIER SERVICE IMPLEMENTATION
# ============================================================================

class NotifierService:
    """
    Automated background notifier service monitoring upcoming outages
    and schedule revision diffs, dispatched through TokenBucketRateLimiter.
    """

    def __init__(
        self,
        bot: Any,
        db: Optional[Any] = None,
        scraper: Optional[Any] = None,
        rate_limiter: Optional[TokenBucketRateLimiter] = None,
        check_interval: int = 60,
        scrape_interval: int = 600,
        db_repo: Optional[Any] = None,
    ) -> None:
        self.bot = bot
        self.db = db if db is not None else db_repo
        self.scraper = scraper
        self.rate_limiter = rate_limiter or TokenBucketRateLimiter()
        self.check_interval = check_interval
        self.scrape_interval = scrape_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._cached_slots_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._last_diff_check_at: Optional[datetime] = None

    async def start(self) -> None:
        """Start the continuous background polling loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("NotifierService started with check_interval=%ds", self.check_interval)

    async def stop(self) -> None:
        """Gracefully stop and await termination of the background polling task."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("NotifierService stopped cleanly.")

    async def _run_loop(self) -> None:
        """Internal background polling loop executing every check_interval seconds."""
        while self._running:
            try:
                await self.poll_tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Error during notifier poll_tick: %s", exc)

            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break

    async def poll_tick(self, now: Optional[datetime] = None) -> int:
        """
        Execute one complete monitoring evaluation pass.
        Returns total number of notifications dispatched.
        """
        current_time = now or datetime.now(KYIV_TZ)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=KYIV_TZ)

        self._cached_slots_cache.clear()

        sent_warnings = await self.check_advance_warnings(current_time)
        sent_diffs = 0
        if (
            self._last_diff_check_at is None
            or self.scrape_interval <= 0
            or (current_time - self._last_diff_check_at).total_seconds() >= self.scrape_interval
        ):
            sent_diffs = await self.check_schedule_diffs(current_time)
            self._last_diff_check_at = current_time
        return sent_warnings + sent_diffs

    async def _execute_query(self, sql: str, params: tuple = ()) -> List[Any]:
        """Internal helper to execute SQL across different database repository facades."""
        if hasattr(self.db, "cm") and hasattr(self.db.cm, "get_connection"):
            async with self.db.cm.get_connection() as conn:
                cur = await conn.execute(sql, params)
                return await cur.fetchall()
        elif hasattr(self.db, "get_connection"):
            async with self.db.get_connection() as conn:
                cur = await conn.execute(sql, params)
                return await cur.fetchall()
        elif hasattr(self.db, "execute"):
            res = self.db.execute(sql, params)
            if asyncio.iscoroutine(res):
                cur = await res
            else:
                cur = res
            if hasattr(cur, "fetchall"):
                fetch = cur.fetchall()
                if asyncio.iscoroutine(fetch):
                    return await fetch
                return fetch
            return []
        return []

    async def _execute_write(self, sql: str, params: tuple = ()) -> None:
        """Internal helper to execute SQL write queries with commit."""
        if hasattr(self.db, "cm") and hasattr(self.db.cm, "get_connection"):
            async with self.db.cm.get_connection() as conn:
                await conn.execute(sql, params)
                await conn.commit()
        elif hasattr(self.db, "get_connection"):
            async with self.db.get_connection() as conn:
                await conn.execute(sql, params)
                await conn.commit()
        elif hasattr(self.db, "execute"):
            res = self.db.execute(sql, params)
            if asyncio.iscoroutine(res):
                await res
            if hasattr(self.db, "commit"):
                commit_res = self.db.commit()
                if asyncio.iscoroutine(commit_res):
                    await commit_res

    async def _send_telegram_message(
        self,
        chat_id: int,
        text: str,
        user_id: int,
        thread_id: Optional[int] = None,
    ) -> bool:
        """
        Dispatch message to Telegram API with 429 backoff and 403 Forbidden user deactivation.
        """
        try:
            if hasattr(self.bot, "send_message"):
                send_kwargs: Dict[str, Any] = {"chat_id": chat_id, "text": text}
                if thread_id is not None:
                    send_kwargs["message_thread_id"] = thread_id
                res = self.bot.send_message(**send_kwargs)
                if asyncio.iscoroutine(res):
                    await res
            return True
        except Exception as exc:
            err_str = str(exc).lower()
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None or "retry after" in err_str or "429" in err_str:
                if retry_after is None:
                    match = re.search(r"retry after (\d+(?:\.\d+)?)", err_str)
                    retry_after = float(match.group(1)) if match else 1.0
                self.rate_limiter.handle_retry_after(float(retry_after))
                logger.warning("Telegram 429 received; backed off for %s seconds: %s", retry_after, exc)
            elif (
                "forbidden" in err_str
                or "bot was blocked" in err_str
                or "chat not found" in err_str
                or "user is deactivated" in err_str
                or "message thread not found" in err_str
                or "403" in err_str
                or getattr(exc, "code", None) == 403
            ):
                logger.warning("Telegram chat unavailable for user %s; marking inactive: %s", user_id, exc)
                await self._deactivate_user(user_id)
            else:
                logger.warning("Failed to dispatch alert to chat %s (user %s): %s", chat_id, user_id, exc)
            return False

    async def _deactivate_user(self, user_id: int) -> None:
        """Deactivate blocked/forbidden user in database."""
        try:
            if hasattr(self.db, "users") and hasattr(self.db.users, "set_user_active"):
                await self.db.users.set_user_active(user_id, False)
            else:
                await self._execute_write(
                    "UPDATE users SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;",
                    (user_id,),
                )
        except Exception as exc:
            logger.warning("Failed to deactivate user %s: %s", user_id, exc)

    async def _get_subgroup_slots(self, region_code: str, subgroup_id: str) -> List[Dict[str, Any]]:
        """Retrieve 48-slot schedule list for a given region and subgroup."""
        cache_key = f"{region_code}:{subgroup_id}"
        if cache_key in self._cached_slots_cache:
            return self._cached_slots_cache[cache_key]

        norm_sub = subgroup_id.replace("GPV", "").strip()

        # 1. Try ScraperCascade if available
        if self.scraper is not None and hasattr(self.scraper, "get_schedule"):
            try:
                resp = await self.scraper.get_schedule(region_code, subgroup_id=norm_sub)
                if hasattr(resp, "intervals") and resp.intervals:
                    slots = []
                    for idx, item in enumerate(resp.intervals):
                        st = item.status if hasattr(item, "status") else item
                        if hasattr(st, "value"):
                            st = st.value
                        slots.append({"slot_index": idx, "status": str(st)})
                    self._cached_slots_cache[cache_key] = slots
                    return slots
                elif hasattr(resp, "groups") and resp.groups:
                    for g_key, g_val in resp.groups.items():
                        if g_key.replace("GPV", "").strip() == norm_sub:
                            slots = []
                            for idx, s in enumerate(g_val.slots):
                                st = s.status if hasattr(s, "status") else s
                                if hasattr(st, "value"):
                                    st = st.value
                                slots.append({"slot_index": idx, "status": str(st)})
                            self._cached_slots_cache[cache_key] = slots
                            return slots
            except Exception as exc:
                logger.debug("Scraper lookup for %s failed: %s", cache_key, exc)

        # 2. Try schedule_cache in database
        try:
            cached_record = None
            if hasattr(self.db, "cache") and hasattr(self.db.cache, "get_cache"):
                cached_record = await self.db.cache.get_cache(region_code)
            else:
                rows = await self._execute_query(
                    "SELECT payload_json FROM schedule_cache WHERE region_code = ?;",
                    (region_code,),
                )
                if rows:
                    row = rows[0]
                    cached_record = row["payload_json"] if isinstance(row, dict) else row[0]

            if cached_record:
                payload_str = (
                    cached_record.payload_json
                    if hasattr(cached_record, "payload_json")
                    else cached_record
                )
                payload_data = json.loads(payload_str) if isinstance(payload_str, str) else payload_str
                groups = payload_data.get("groups", {})
                for g_key, g_val in groups.items():
                    if g_key.replace("GPV", "").strip() == norm_sub:
                        raw_slots = g_val.get("slots", []) if isinstance(g_val, dict) else getattr(g_val, "slots", [])
                        slots = []
                        for idx, s in enumerate(raw_slots):
                            st = s.get("status", "") if isinstance(s, dict) else getattr(s, "status", str(s))
                            if hasattr(st, "value"):
                                st = st.value
                            slots.append({"slot_index": idx, "status": str(st)})
                        self._cached_slots_cache[cache_key] = slots
                        return slots
        except Exception as exc:
            logger.debug("Cache lookup for %s failed: %s", cache_key, exc)

        # Fallback: synthetic ON slots to prevent crash
        fallback_slots = [{"slot_index": i, "status": "ON"} for i in range(48)]
        self._cached_slots_cache[cache_key] = fallback_slots
        return fallback_slots

    async def check_advance_warnings(self, current_time: datetime) -> int:
        """
        Evaluates active user slots for upcoming ON -> OFF and ON -> MAYBE transitions
        at T + 30m and T + 15m.
        Filters silent hours, applies deduplication, throttles via rate limiter,
        and records sent alerts in notification_history.
        """
        notifications_sent = 0

        # Query all active users and their slots
        sql = """
        SELECT u.user_id, u.chat_id, u.first_name,
               s.slot_number, s.alias, s.region_code, s.group_id, s.subgroup_id,
               u.thread_id
        FROM users u
        JOIN user_slots s ON u.user_id = s.user_id
        WHERE u.is_active = 1
        ORDER BY u.user_id, s.slot_number ASC;
        """
        try:
            rows = await self._execute_query(sql)
        except Exception as exc:
            logger.warning("Failed to query active user slots for advance warnings: %s", exc)
            return 0

        # Group slots by user
        user_slots_map: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            uid = r["user_id"] if isinstance(r, dict) or hasattr(r, "keys") else r[0]
            cid = r["chat_id"] if isinstance(r, dict) or hasattr(r, "keys") else r[1]
            fname = r["first_name"] if isinstance(r, dict) or hasattr(r, "keys") else r[2]
            snum = r["slot_number"] if isinstance(r, dict) or hasattr(r, "keys") else r[3]
            alias = r["alias"] if isinstance(r, dict) or hasattr(r, "keys") else r[4]
            reg = r["region_code"] if isinstance(r, dict) or hasattr(r, "keys") else r[5]
            grp = r["group_id"] if isinstance(r, dict) or hasattr(r, "keys") else r[6]
            subgrp = r["subgroup_id"] if isinstance(r, dict) or hasattr(r, "keys") else r[7]
            tid = r["thread_id"] if (isinstance(r, dict) or hasattr(r, "keys")) and "thread_id" in r.keys() else (r[8] if not isinstance(r, dict) and len(r) > 8 else None)

            if uid not in user_slots_map:
                user_slots_map[uid] = {
                    "chat_id": cid,
                    "thread_id": tid,
                    "first_name": fname,
                    "slots": {},
                }
            user_slots_map[uid]["slots"][snum] = {
                "slot_number": snum,
                "alias": alias,
                "region_code": reg,
                "group_id": grp,
                "subgroup_id": subgrp,
            }

        for uid, user_data in user_slots_map.items():
            try:
                # 1. Fetch user settings
                notify_30m = True
                notify_15m = True
                silent_start = 23
                silent_end = 7
                if hasattr(self.db, "settings") and hasattr(self.db.settings, "get_settings"):
                    st = await self.db.settings.get_settings(uid)
                    notify_30m = getattr(st, "notify_30m", True)
                    notify_15m = getattr(st, "notify_15m", True)
                    silent_start = getattr(st, "silent_hours_start", 23)
                    silent_end = getattr(st, "silent_hours_end", 7)
                else:
                    settings_rows = await self._execute_query(
                        "SELECT notify_30m, notify_15m, silent_hours_start, silent_hours_end FROM notification_settings WHERE user_id = ?;",
                        (uid,),
                    )
                    if settings_rows:
                        s_row = settings_rows[0]
                        notify_30m = bool(s_row["notify_30m"] if isinstance(s_row, dict) or hasattr(s_row, "keys") else s_row[0])
                        notify_15m = bool(s_row["notify_15m"] if isinstance(s_row, dict) or hasattr(s_row, "keys") else s_row[1])
                        silent_start = int(s_row["silent_hours_start"] if isinstance(s_row, dict) or hasattr(s_row, "keys") else s_row[2])
                        silent_end = int(s_row["silent_hours_end"] if isinstance(s_row, dict) or hasattr(s_row, "keys") else s_row[3])

                # Silent hours check (routine warnings muted during silent hours)
                if is_in_silent_hours(current_time, silent_start, silent_end):
                    continue

                for lead_minutes in (30, 15):
                    if lead_minutes == 30 and not notify_30m:
                        continue
                    if lead_minutes == 15 and not notify_15m:
                        continue

                    for snum, s_info in user_data["slots"].items():
                        slots = await self._get_subgroup_slots(s_info["region_code"], s_info["subgroup_id"])
                        warning = detect_advance_outage_transition(slots, current_time, lead_minutes)
                        if warning is None:
                            continue

                        event_type = warning["event_type"]
                        target_slot_index = warning["target_slot_index"]
                        # Slot transition boundary time (e.g. '2026-11-12 14:00:00')
                        target_date = warning["target_time"][:10]
                        target_time = f"{target_date} {warning['transition_time']}:00"

                        # Check deduplication
                        if hasattr(self.db, "history") and hasattr(self.db.history, "is_notification_sent"):
                            is_sent = await self.db.history.is_notification_sent(
                                uid, snum, event_type, target_time
                            )
                        else:
                            dup_rows = await self._execute_query(
                                "SELECT 1 FROM notification_history WHERE user_id = ? AND slot_number = ? AND event_type = ? AND target_time = ? LIMIT 1;",
                                (uid, snum, event_type, target_time),
                            )
                            is_sent = bool(dup_rows)

                        if is_sent:
                            continue

                        # Check secondary slot availability for dual-slot subscribers
                        other_snum = 2 if snum == 1 else 1
                        other_info = user_data["slots"].get(other_snum)
                        if other_info:
                            other_slots = await self._get_subgroup_slots(
                                other_info["region_code"], other_info["subgroup_id"]
                            )
                            other_status = (
                                other_slots[target_slot_index]["status"]
                                if target_slot_index < len(other_slots)
                                else "ON"
                            )
                            other_has_power = other_status == "ON"
                            msg = format_advance_warning_message(
                                lead_minutes=lead_minutes,
                                transition_time=warning["transition_time"],
                                slot1_alias=s_info["alias"],
                                slot1_group=s_info["subgroup_id"],
                                slot2_alias=other_info["alias"],
                                slot2_group=other_info["subgroup_id"],
                                slot2_has_power=other_has_power,
                            )
                        else:
                            msg = format_advance_warning_message(
                                lead_minutes=lead_minutes,
                                transition_time=warning["transition_time"],
                                slot1_alias=s_info["alias"],
                                slot1_group=s_info["subgroup_id"],
                            )

                        # Acquire token and dispatch
                        await self.rate_limiter.acquire()
                        sent = await self._send_telegram_message(
                            user_data["chat_id"],
                            msg,
                            user_id=uid,
                            thread_id=user_data.get("thread_id"),
                        )
                        if sent:
                            # Record notification in history
                            if hasattr(self.db, "history") and hasattr(self.db.history, "record_notification"):
                                await self.db.history.record_notification(
                                    user_id=uid,
                                    slot_number=snum,
                                    event_type=event_type,
                                    target_time=target_time,
                                    hash_sent="advance_warning",
                                    target_slot_index=target_slot_index,
                                )
                            else:
                                await self._execute_write(
                                    """
                                    INSERT INTO notification_history (
                                        user_id, slot_number, event_type, target_slot_index, target_time, hash_sent, sent_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
                                    """,
                                    (uid, snum, event_type, target_slot_index, target_time, "advance_warning"),
                                )
                            notifications_sent += 1
            except Exception as user_exc:
                logger.warning("Error evaluating advance warnings for user %s: %s", uid, user_exc)

        return notifications_sent

    async def check_schedule_diffs(self, current_time: datetime) -> int:
        """
        Checks latest schedules for monitored DSO regions against schedule_cache.
        On SHA-256 hash change, detects changed intervals per subgroup and dispatches
        revision diff notifications to subscribed active users.
        """
        notifications_sent = 0
        monitored_regions = ["kyiv", "kyiv-region", "dnipro", "odesa"]

        for region in monitored_regions:
            try:
                # 1. Fetch current cached record from database
                cached_record = None
                if hasattr(self.db, "cache") and hasattr(self.db.cache, "get_cache"):
                    cached_record = await self.db.cache.get_cache(region)
                else:
                    rows = await self._execute_query(
                        "SELECT region_code, schedule_date, payload_json, payload_hash FROM schedule_cache WHERE region_code = ?;",
                        (region,),
                    )
                    if rows:
                        row = rows[0]
                        cached_record = {
                            "region_code": row["region_code"] if isinstance(row, dict) or hasattr(row, "keys") else row[0],
                            "schedule_date": row["schedule_date"] if isinstance(row, dict) or hasattr(row, "keys") else row[1],
                            "payload_json": row["payload_json"] if isinstance(row, dict) or hasattr(row, "keys") else row[2],
                            "payload_hash": row["payload_hash"] if isinstance(row, dict) or hasattr(row, "keys") else row[3],
                        }

                if self.scraper is None or not hasattr(self.scraper, "get_schedule"):
                    continue

                # 2. Fetch newly scraped schedule
                new_sched = await self.scraper.get_schedule(region)
                if new_sched is None:
                    continue

                if hasattr(new_sched, "model_dump_json"):
                    new_payload = new_sched.model_dump_json()
                    schedule_date = getattr(new_sched, "date", current_time.strftime("%Y-%m-%d"))
                    is_emergency = getattr(new_sched, "is_emergency", False)
                    emergency_message = getattr(new_sched, "emergency_message", None)
                    data_source = getattr(new_sched, "data_source", "direct_scrape")
                elif isinstance(new_sched, dict):
                    new_payload = json.dumps(new_sched)
                    schedule_date = new_sched.get("date", current_time.strftime("%Y-%m-%d"))
                    is_emergency = new_sched.get("is_emergency", False)
                    emergency_message = new_sched.get("emergency_message", None)
                    data_source = new_sched.get("data_source", "direct_scrape")
                else:
                    new_payload = str(new_sched)
                    schedule_date = current_time.strftime("%Y-%m-%d")
                    is_emergency = False
                    emergency_message = None
                    data_source = "direct_scrape"

                new_hash = compute_schedule_sha256(new_payload)

                # If no cache exists, save initial cache record and proceed
                if cached_record is None:
                    if hasattr(self.db, "cache") and hasattr(self.db.cache, "set_cache"):
                        await self.db.cache.set_cache(
                            region_code=region,
                            schedule_date=schedule_date,
                            payload_json=new_payload,
                            payload_hash=new_hash,
                            is_emergency=is_emergency,
                            emergency_message=emergency_message,
                            data_source=data_source,
                        )
                    continue

                cached_hash = (
                    cached_record.payload_hash
                    if hasattr(cached_record, "payload_hash")
                    else cached_record.get("payload_hash", "")
                )
                cached_json = (
                    cached_record.payload_json
                    if hasattr(cached_record, "payload_json")
                    else cached_record.get("payload_json", "{}")
                )

                # Hashes identical: no schedule revision
                if new_hash == cached_hash:
                    continue

                # Hashes differ: schedule has been revised!
                old_data = json.loads(cached_json) if isinstance(cached_json, str) else cached_json
                old_groups = old_data.get("groups", {}) if isinstance(old_data, dict) else {}

                new_groups = (
                    new_sched.groups
                    if hasattr(new_sched, "groups")
                    else (new_sched.get("groups", {}) if isinstance(new_sched, dict) else {})
                )

                # Check each subgroup for changed intervals
                for g_key, g_new in new_groups.items():
                    norm_sub = g_key.replace("GPV", "").strip()

                    # Extract new slots
                    raw_new_slots = (
                        g_new.slots
                        if hasattr(g_new, "slots")
                        else (g_new.get("slots", []) if isinstance(g_new, dict) else [])
                    )
                    new_slots = []
                    for idx, s in enumerate(raw_new_slots):
                        st = s.get("status", "") if isinstance(s, dict) else getattr(s, "status", str(s))
                        if hasattr(st, "value"):
                            st = st.value
                        new_slots.append({"slot_index": idx, "status": str(st)})

                    # Extract matching old slots
                    old_sub_obj = old_groups.get(g_key) or old_groups.get(norm_sub) or old_groups.get(f"GPV{norm_sub}")
                    raw_old_slots = (
                        old_sub_obj.get("slots", [])
                        if isinstance(old_sub_obj, dict)
                        else getattr(old_sub_obj, "slots", [])
                    )
                    old_slots = []
                    for idx, s in enumerate(raw_old_slots):
                        st = s.get("status", "") if isinstance(s, dict) else getattr(s, "status", str(s))
                        if hasattr(st, "value"):
                            st = st.value
                        old_slots.append({"slot_index": idx, "status": str(st)})

                    diffs = detect_schedule_revision_diffs(old_slots, new_slots)
                    if not diffs:
                        continue

                    # Query active subscribers for this region and subgroup
                    subscribers: List[Dict[str, Any]] = []
                    if hasattr(self.db, "slots") and hasattr(self.db.slots, "get_subscribers_by_subgroup"):
                        subscribers = await self.db.slots.get_subscribers_by_subgroup(region, norm_sub)
                    else:
                        sub_sql = """
                        SELECT u.user_id, u.chat_id, u.first_name, s.slot_number, s.alias, s.region_code, s.group_id, s.subgroup_id, u.thread_id
                        FROM user_slots s
                        JOIN users u ON s.user_id = u.user_id
                        WHERE s.region_code = ? AND s.subgroup_id = ? AND u.is_active = 1;
                        """
                        sub_rows = await self._execute_query(sub_sql, (region, norm_sub))
                        for sr in sub_rows:
                            subscribers.append({
                                "user_id": sr["user_id"] if isinstance(sr, dict) or hasattr(sr, "keys") else sr[0],
                                "chat_id": sr["chat_id"] if isinstance(sr, dict) or hasattr(sr, "keys") else sr[1],
                                "slot_number": sr["slot_number"] if isinstance(sr, dict) or hasattr(sr, "keys") else sr[3],
                                "thread_id": sr["thread_id"] if (isinstance(sr, dict) or hasattr(sr, "keys")) and "thread_id" in sr.keys() else (sr[8] if not isinstance(sr, dict) and len(sr) > 8 else None),
                            })

                    for sub in subscribers:
                        try:
                            uid = sub["user_id"]
                            cid = sub["chat_id"]
                            snum = sub["slot_number"]
                            tid = sub.get("thread_id")

                            # Check user notification settings
                            notify_change = True
                            silent_start = 23
                            silent_end = 7
                            if hasattr(self.db, "settings") and hasattr(self.db.settings, "get_settings"):
                                st = await self.db.settings.get_settings(uid)
                                notify_change = getattr(st, "notify_schedule_change", True)
                                silent_start = getattr(st, "silent_hours_start", 23)
                                silent_end = getattr(st, "silent_hours_end", 7)
                            else:
                                set_rows = await self._execute_query(
                                    "SELECT notify_schedule_change, silent_hours_start, silent_hours_end FROM notification_settings WHERE user_id = ?;",
                                    (uid,),
                                )
                                if set_rows:
                                    s_r = set_rows[0]
                                    notify_change = bool(s_r["notify_schedule_change"] if isinstance(s_r, dict) or hasattr(s_r, "keys") else s_r[0])
                                    silent_start = int(s_r["silent_hours_start"] if isinstance(s_r, dict) or hasattr(s_r, "keys") else s_r[1])
                                    silent_end = int(s_r["silent_hours_end"] if isinstance(s_r, dict) or hasattr(s_r, "keys") else s_r[2])

                            if not notify_change:
                                continue
                            if is_in_silent_hours(current_time, silent_start, silent_end):
                                continue

                            target_slot = diffs[0]["start_slot_index"]
                            start_t, _ = get_slot_time_bounds(target_slot)
                            target_time_str = f"{schedule_date} {start_t}:00"

                            # Deduplication check via notification_history
                            if hasattr(self.db, "history") and hasattr(self.db.history, "is_notification_sent"):
                                is_dup = await self.db.history.is_notification_sent(
                                    uid, snum, "schedule_diff", target_time_str
                                )
                            else:
                                dup_rows = await self._execute_query(
                                    "SELECT 1 FROM notification_history WHERE user_id = ? AND slot_number = ? AND event_type = 'schedule_diff' AND target_time = ? LIMIT 1;",
                                    (uid, snum, target_time_str),
                                )
                                is_dup = bool(dup_rows)

                            if is_dup:
                                continue

                            diff_msg = format_schedule_diff_message(norm_sub, diffs)
                            await self.rate_limiter.acquire()
                            sent = await self._send_telegram_message(cid, diff_msg, user_id=uid, thread_id=tid)
                            if sent:
                                if hasattr(self.db, "history") and hasattr(self.db.history, "record_notification"):
                                    await self.db.history.record_notification(
                                        user_id=uid,
                                        slot_number=snum,
                                        event_type="schedule_diff",
                                        target_time=target_time_str,
                                        hash_sent=new_hash,
                                        target_slot_index=target_slot,
                                    )
                                else:
                                    await self._execute_write(
                                        """
                                        INSERT INTO notification_history (
                                            user_id, slot_number, event_type, target_slot_index, target_time, hash_sent, sent_at
                                        ) VALUES (?, ?, 'schedule_diff', ?, ?, ?, CURRENT_TIMESTAMP);
                                        """,
                                        (uid, snum, target_slot, target_time_str, new_hash),
                                    )
                                notifications_sent += 1
                        except Exception as sub_exc:
                            logger.warning("Error notifying subscriber %s for diff: %s", sub.get("user_id"), sub_exc)

                # Update cache with revised schedule
                if hasattr(self.db, "cache") and hasattr(self.db.cache, "set_cache"):
                    await self.db.cache.set_cache(
                        region_code=region,
                        schedule_date=schedule_date,
                        payload_json=new_payload,
                        payload_hash=new_hash,
                        is_emergency=is_emergency,
                        emergency_message=emergency_message,
                        data_source=data_source,
                    )
            except Exception as reg_exc:
                logger.warning("Error checking schedule diffs for region %s: %s", region, reg_exc)

        return notifications_sent
