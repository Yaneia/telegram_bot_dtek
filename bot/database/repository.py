"""Asynchronous CRUD repositories for database persistence."""

from dataclasses import dataclass
import re
from typing import Any, Dict, List, Optional, Union

from bot.database.connection import DatabaseConnectionManager, aiosqlite
from bot.database.schema import init_db
from bot.models.enums import DSORegion, REGION_NAMES
from bot.models.schedule import SlotConfig


# Data Transfer Objects (DTOs)
@dataclass
class UserRecord:
    user_id: int
    chat_id: int
    first_name: str
    username: Optional[str] = None
    thread_id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    is_active: bool = True


@dataclass
class UserSlotRecord:
    id: int
    user_id: int
    slot_number: int
    alias: str
    region_code: str
    group_id: int
    subgroup_id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_slot_config(self) -> SlotConfig:
        region = DSORegion(self.region_code) if self.region_code in DSORegion._value2member_map_ else self.region_code
        region_name = REGION_NAMES.get(region) if isinstance(region, DSORegion) else None
        return SlotConfig(
            slot_number=self.slot_number,
            alias=self.alias,
            region_code=region,
            region_name=region_name,
            group_id=self.group_id,
            subgroup_id=self.subgroup_id,
        )


@dataclass
class NotificationSettingsRecord:
    user_id: int
    notify_30m: bool = True
    notify_15m: bool = True
    notify_schedule_change: bool = True
    silent_hours_start: int = 23
    silent_hours_end: int = 7
    show_joint_timeline: bool = False
    updated_at: Optional[str] = None


@dataclass
class ScheduleCacheRecord:
    region_code: str
    schedule_date: str
    payload_json: str
    payload_hash: str
    is_emergency: bool = False
    emergency_message: Optional[str] = None
    data_source: str = "direct_scrape"
    updated_at: Optional[str] = None


@dataclass
class NotificationHistoryRecord:
    id: int
    user_id: int
    slot_number: int
    event_type: str
    target_slot_index: Optional[int]
    target_time: str
    sent_at: str
    hash_sent: str


class UserRepository:
    """Async repository for Telegram user identity management."""

    def __init__(self, connection_manager: DatabaseConnectionManager) -> None:
        self.cm = connection_manager

    async def upsert_user(
        self,
        user_id: int,
        chat_id: int,
        first_name: str,
        username: Optional[str] = None,
        thread_id: Optional[int] = None,
        is_active: bool = True,
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> UserRecord:
        sql = """
        INSERT INTO users (user_id, chat_id, thread_id, first_name, username, is_active, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            chat_id = excluded.chat_id,
            thread_id = excluded.thread_id,
            first_name = excluded.first_name,
            username = excluded.username,
            is_active = excluded.is_active,
            updated_at = CURRENT_TIMESTAMP
        RETURNING user_id, chat_id, thread_id, first_name, username, created_at, updated_at, is_active;
        """
        params = (user_id, chat_id, thread_id, first_name, username, int(is_active))

        if conn is not None:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            await conn.commit()
        else:
            async with self.cm.get_connection() as active_conn:
                cur = await active_conn.execute(sql, params)
                row = await cur.fetchone()
                await active_conn.commit()

        return UserRecord(
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            thread_id=row["thread_id"],
            first_name=row["first_name"],
            username=row["username"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            is_active=bool(row["is_active"]),
        )

    async def get_user(
        self,
        user_id: int,
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> Optional[UserRecord]:
        sql = "SELECT user_id, chat_id, thread_id, first_name, username, created_at, updated_at, is_active FROM users WHERE user_id = ?;"
        if conn is not None:
            cur = await conn.execute(sql, (user_id,))
            row = await cur.fetchone()
        else:
            async with self.cm.get_connection() as active_conn:
                cur = await active_conn.execute(sql, (user_id,))
                row = await cur.fetchone()

        if row is None:
            return None
        return UserRecord(
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            thread_id=row["thread_id"],
            first_name=row["first_name"],
            username=row["username"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            is_active=bool(row["is_active"]),
        )

    async def set_user_active(
        self,
        user_id: int,
        is_active: bool,
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> None:
        sql = "UPDATE users SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;"
        params = (int(is_active), user_id)
        if conn is not None:
            await conn.execute(sql, params)
            await conn.commit()
        else:
            async with self.cm.get_connection() as active_conn:
                await active_conn.execute(sql, params)
                await active_conn.commit()


class UserSlotsRepository:
    """Async repository for user monitored location slots."""

    def __init__(self, connection_manager: DatabaseConnectionManager) -> None:
        self.cm = connection_manager

    @staticmethod
    def normalize_subgroup(group_id: Optional[int], subgroup_id: str) -> tuple[Optional[int], str]:
        """Normalize subgroup identifiers (e.g. 'GPV1.1' -> (1, '1.1'))."""
        raw = subgroup_id.strip()
        match = re.match(
            r"^(?:GPV|Черга|Група|Group|Subgroup)?\s*([1-6])\.([1-2])$",
            raw,
            re.IGNORECASE,
        )
        if match:
            extracted_group = int(match.group(1))
            norm_subgroup = f"{match.group(1)}.{match.group(2)}"
            if group_id is not None and group_id != extracted_group:
                return group_id, norm_subgroup
            return extracted_group, norm_subgroup
        return group_id, raw

    async def upsert_slot(
        self,
        user_id: int,
        slot_number: int,
        region_code: Union[DSORegion, str],
        group_id: int,
        subgroup_id: str,
        alias: str = "Моя черга",
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> UserSlotRecord:
        reg_str = region_code.value if isinstance(region_code, DSORegion) else str(region_code)
        norm_group, norm_subgroup = self.normalize_subgroup(group_id, subgroup_id)

        sql = """
        INSERT INTO user_slots (user_id, slot_number, alias, region_code, group_id, subgroup_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id, slot_number) DO UPDATE SET
            alias = excluded.alias,
            region_code = excluded.region_code,
            group_id = excluded.group_id,
            subgroup_id = excluded.subgroup_id,
            updated_at = CURRENT_TIMESTAMP
        RETURNING id, user_id, slot_number, alias, region_code, group_id, subgroup_id, created_at, updated_at;
        """
        params = (user_id, slot_number, alias, reg_str, norm_group, norm_subgroup)

        if conn is not None:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            await conn.commit()
        else:
            async with self.cm.get_connection() as active_conn:
                cur = await active_conn.execute(sql, params)
                row = await cur.fetchone()
                await active_conn.commit()

        return UserSlotRecord(
            id=row["id"],
            user_id=row["user_id"],
            slot_number=row["slot_number"],
            alias=row["alias"],
            region_code=row["region_code"],
            group_id=row["group_id"],
            subgroup_id=row["subgroup_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_slots_for_user(
        self,
        user_id: int,
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> List[UserSlotRecord]:
        sql = """
        SELECT id, user_id, slot_number, alias, region_code, group_id, subgroup_id, created_at, updated_at
        FROM user_slots
        WHERE user_id = ?
        ORDER BY slot_number ASC;
        """
        if conn is not None:
            cur = await conn.execute(sql, (user_id,))
            rows = await cur.fetchall()
        else:
            async with self.cm.get_connection() as active_conn:
                cur = await active_conn.execute(sql, (user_id,))
                rows = await cur.fetchall()

        return [
            UserSlotRecord(
                id=r["id"],
                user_id=r["user_id"],
                slot_number=r["slot_number"],
                alias=r["alias"],
                region_code=r["region_code"],
                group_id=r["group_id"],
                subgroup_id=r["subgroup_id"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    async def get_slot(
        self,
        user_id: int,
        slot_number: int,
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> Optional[UserSlotRecord]:
        sql = """
        SELECT id, user_id, slot_number, alias, region_code, group_id, subgroup_id, created_at, updated_at
        FROM user_slots
        WHERE user_id = ? AND slot_number = ?;
        """
        if conn is not None:
            cur = await conn.execute(sql, (user_id, slot_number))
            row = await cur.fetchone()
        else:
            async with self.cm.get_connection() as active_conn:
                cur = await active_conn.execute(sql, (user_id, slot_number))
                row = await cur.fetchone()

        if row is None:
            return None
        return UserSlotRecord(
            id=row["id"],
            user_id=row["user_id"],
            slot_number=row["slot_number"],
            alias=row["alias"],
            region_code=row["region_code"],
            group_id=row["group_id"],
            subgroup_id=row["subgroup_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_slot_configs_for_user(
        self,
        user_id: int,
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> List[SlotConfig]:
        records = await self.get_slots_for_user(user_id, conn=conn)
        return [r.to_slot_config() for r in records]

    async def delete_slot(
        self,
        user_id: int,
        slot_number: int,
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> bool:
        sql = "DELETE FROM user_slots WHERE user_id = ? AND slot_number = ?;"
        if conn is not None:
            cur = await conn.execute(sql, (user_id, slot_number))
            await conn.commit()
            return cur.rowcount > 0
        else:
            async with self.cm.get_connection() as active_conn:
                cur = await active_conn.execute(sql, (user_id, slot_number))
                await active_conn.commit()
                return cur.rowcount > 0

    async def get_subscribers_by_subgroup(
        self,
        region_code: Union[DSORegion, str],
        subgroup_id: str,
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> List[Dict[str, Any]]:
        reg_str = region_code.value if isinstance(region_code, DSORegion) else str(region_code)
        _, norm_subgroup = self.normalize_subgroup(None, subgroup_id)

        sql = """
        SELECT u.user_id, u.chat_id, u.thread_id, u.first_name, s.slot_number, s.alias, s.region_code, s.group_id, s.subgroup_id
        FROM user_slots s
        JOIN users u ON s.user_id = u.user_id
        WHERE s.region_code = ? AND s.subgroup_id = ? AND u.is_active = 1;
        """
        params = (reg_str, norm_subgroup)
        if conn is not None:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
        else:
            async with self.cm.get_connection() as active_conn:
                cur = await active_conn.execute(sql, params)
                rows = await cur.fetchall()

        return [
            {
                "user_id": r["user_id"],
                "chat_id": r["chat_id"],
                "thread_id": r["thread_id"],
                "first_name": r["first_name"],
                "slot_number": r["slot_number"],
                "alias": r["alias"],
                "region_code": r["region_code"],
                "group_id": r["group_id"],
                "subgroup_id": r["subgroup_id"],
            }
            for r in rows
        ]


class NotificationSettingsRepository:
    """Async repository for user alert preferences."""

    def __init__(self, connection_manager: DatabaseConnectionManager) -> None:
        self.cm = connection_manager

    async def upsert_settings(
        self,
        user_id: int,
        notify_30m: bool = True,
        notify_15m: bool = True,
        notify_schedule_change: bool = True,
        silent_hours_start: int = 23,
        silent_hours_end: int = 7,
        show_joint_timeline: bool = False,
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> NotificationSettingsRecord:
        sql = """
        INSERT INTO notification_settings (
            user_id, notify_30m, notify_15m, notify_schedule_change,
            silent_hours_start, silent_hours_end, show_joint_timeline, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            notify_30m = excluded.notify_30m,
            notify_15m = excluded.notify_15m,
            notify_schedule_change = excluded.notify_schedule_change,
            silent_hours_start = excluded.silent_hours_start,
            silent_hours_end = excluded.silent_hours_end,
            show_joint_timeline = excluded.show_joint_timeline,
            updated_at = CURRENT_TIMESTAMP
        RETURNING user_id, notify_30m, notify_15m, notify_schedule_change, silent_hours_start, silent_hours_end, show_joint_timeline, updated_at;
        """
        params = (
            user_id,
            int(notify_30m),
            int(notify_15m),
            int(notify_schedule_change),
            silent_hours_start,
            silent_hours_end,
            int(show_joint_timeline),
        )
        if conn is not None:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            await conn.commit()
        else:
            async with self.cm.get_connection() as active_conn:
                cur = await active_conn.execute(sql, params)
                row = await cur.fetchone()
                await active_conn.commit()

        return NotificationSettingsRecord(
            user_id=row["user_id"],
            notify_30m=bool(row["notify_30m"]),
            notify_15m=bool(row["notify_15m"]),
            notify_schedule_change=bool(row["notify_schedule_change"]),
            silent_hours_start=row["silent_hours_start"],
            silent_hours_end=row["silent_hours_end"],
            show_joint_timeline=bool(row["show_joint_timeline"]),
            updated_at=row["updated_at"],
        )

    async def get_settings(
        self,
        user_id: int,
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> NotificationSettingsRecord:
        sql = """
        SELECT user_id, notify_30m, notify_15m, notify_schedule_change,
               silent_hours_start, silent_hours_end, show_joint_timeline, updated_at
        FROM notification_settings
        WHERE user_id = ?;
        """
        if conn is not None:
            cur = await conn.execute(sql, (user_id,))
            row = await cur.fetchone()
        else:
            async with self.cm.get_connection() as active_conn:
                cur = await active_conn.execute(sql, (user_id,))
                row = await cur.fetchone()

        if row is None:
            # Default preferences
            return NotificationSettingsRecord(user_id=user_id)

        return NotificationSettingsRecord(
            user_id=row["user_id"],
            notify_30m=bool(row["notify_30m"]),
            notify_15m=bool(row["notify_15m"]),
            notify_schedule_change=bool(row["notify_schedule_change"]),
            silent_hours_start=row["silent_hours_start"],
            silent_hours_end=row["silent_hours_end"],
            show_joint_timeline=bool(row["show_joint_timeline"]),
            updated_at=row["updated_at"],
        )

    async def update_settings(
        self,
        user_id: int,
        notify_30m: Optional[bool] = None,
        notify_15m: Optional[bool] = None,
        notify_schedule_change: Optional[bool] = None,
        silent_hours_start: Optional[int] = None,
        silent_hours_end: Optional[int] = None,
        show_joint_timeline: Optional[bool] = None,
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> NotificationSettingsRecord:
        current = await self.get_settings(user_id, conn=conn)
        return await self.upsert_settings(
            user_id=user_id,
            notify_30m=current.notify_30m if notify_30m is None else notify_30m,
            notify_15m=current.notify_15m if notify_15m is None else notify_15m,
            notify_schedule_change=current.notify_schedule_change if notify_schedule_change is None else notify_schedule_change,
            silent_hours_start=current.silent_hours_start if silent_hours_start is None else silent_hours_start,
            silent_hours_end=current.silent_hours_end if silent_hours_end is None else silent_hours_end,
            show_joint_timeline=current.show_joint_timeline if show_joint_timeline is None else show_joint_timeline,
            conn=conn,
        )


class ScheduleCacheRepository:
    """Async repository for Stale-While-Revalidate regional schedule caching."""

    def __init__(self, connection_manager: DatabaseConnectionManager) -> None:
        self.cm = connection_manager

    async def set_cache(
        self,
        region_code: Union[DSORegion, str],
        schedule_date: str,
        payload_json: str,
        payload_hash: str,
        is_emergency: bool = False,
        emergency_message: Optional[str] = None,
        data_source: str = "direct_scrape",
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> ScheduleCacheRecord:
        reg_str = region_code.value if isinstance(region_code, DSORegion) else str(region_code)
        sql = """
        INSERT INTO schedule_cache (
            region_code, schedule_date, payload_json, payload_hash,
            is_emergency, emergency_message, data_source, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(region_code) DO UPDATE SET
            schedule_date = excluded.schedule_date,
            payload_json = excluded.payload_json,
            payload_hash = excluded.payload_hash,
            is_emergency = excluded.is_emergency,
            emergency_message = excluded.emergency_message,
            data_source = excluded.data_source,
            updated_at = CURRENT_TIMESTAMP
        RETURNING region_code, schedule_date, payload_json, payload_hash, is_emergency, emergency_message, data_source, updated_at;
        """
        params = (
            reg_str,
            schedule_date,
            payload_json,
            payload_hash,
            int(is_emergency),
            emergency_message,
            data_source,
        )
        if conn is not None:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            await conn.commit()
        else:
            async with self.cm.get_connection() as active_conn:
                cur = await active_conn.execute(sql, params)
                row = await cur.fetchone()
                await active_conn.commit()

        return ScheduleCacheRecord(
            region_code=row["region_code"],
            schedule_date=row["schedule_date"],
            payload_json=row["payload_json"],
            payload_hash=row["payload_hash"],
            is_emergency=bool(row["is_emergency"]),
            emergency_message=row["emergency_message"],
            data_source=row["data_source"],
            updated_at=row["updated_at"],
        )

    async def get_cache(
        self,
        region_code: Union[DSORegion, str],
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> Optional[ScheduleCacheRecord]:
        reg_str = region_code.value if isinstance(region_code, DSORegion) else str(region_code)
        sql = """
        SELECT region_code, schedule_date, payload_json, payload_hash,
               is_emergency, emergency_message, data_source, updated_at
        FROM schedule_cache
        WHERE region_code = ?;
        """
        if conn is not None:
            cur = await conn.execute(sql, (reg_str,))
            row = await cur.fetchone()
        else:
            async with self.cm.get_connection() as active_conn:
                cur = await active_conn.execute(sql, (reg_str,))
                row = await cur.fetchone()

        if row is None:
            return None

        return ScheduleCacheRecord(
            region_code=row["region_code"],
            schedule_date=row["schedule_date"],
            payload_json=row["payload_json"],
            payload_hash=row["payload_hash"],
            is_emergency=bool(row["is_emergency"]),
            emergency_message=row["emergency_message"],
            data_source=row["data_source"],
            updated_at=row["updated_at"],
        )

    async def prune_stale_cache(
        self,
        days: int = 7,
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> int:
        sql = f"DELETE FROM schedule_cache WHERE updated_at < datetime('now', '-{days} days');"
        if conn is not None:
            cur = await conn.execute(sql)
            await conn.commit()
            return cur.rowcount
        else:
            async with self.cm.get_connection() as active_conn:
                cur = await active_conn.execute(sql)
                await active_conn.commit()
                return cur.rowcount


class NotificationHistoryRepository:
    """Async repository for dispatched notification deduplication."""

    def __init__(self, connection_manager: DatabaseConnectionManager) -> None:
        self.cm = connection_manager

    async def record_notification(
        self,
        user_id: int,
        slot_number: int,
        event_type: str,
        target_time: str,
        hash_sent: str,
        target_slot_index: Optional[int] = None,
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> NotificationHistoryRecord:
        sql = """
        INSERT INTO notification_history (
            user_id, slot_number, event_type, target_slot_index, target_time, hash_sent, sent_at
        )
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        RETURNING id, user_id, slot_number, event_type, target_slot_index, target_time, sent_at, hash_sent;
        """
        params = (
            user_id,
            slot_number,
            event_type,
            target_slot_index,
            target_time,
            hash_sent,
        )
        if conn is not None:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            await conn.commit()
        else:
            async with self.cm.get_connection() as active_conn:
                cur = await active_conn.execute(sql, params)
                row = await cur.fetchone()
                await active_conn.commit()

        return NotificationHistoryRecord(
            id=row["id"],
            user_id=row["user_id"],
            slot_number=row["slot_number"],
            event_type=row["event_type"],
            target_slot_index=row["target_slot_index"],
            target_time=row["target_time"],
            sent_at=row["sent_at"],
            hash_sent=row["hash_sent"],
        )

    async def is_notification_sent(
        self,
        user_id: int,
        slot_number: int,
        event_type: str,
        target_time: str,
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> bool:
        """Check whether a notification alert has already been dispatched using index idx_notif_dedup."""
        sql = """
        SELECT 1 FROM notification_history
        WHERE user_id = ? AND slot_number = ? AND event_type = ? AND target_time = ?
        LIMIT 1;
        """
        params = (user_id, slot_number, event_type, target_time)
        if conn is not None:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
        else:
            async with self.cm.get_connection() as active_conn:
                cur = await active_conn.execute(sql, params)
                row = await cur.fetchone()

        return row is not None

    async def has_notification_been_sent(
        self,
        user_id: int,
        slot_number: int,
        event_type: str,
        target_time: str,
        conn: Optional[Union[aiosqlite.Connection, Any]] = None,
    ) -> bool:
        return await self.is_notification_sent(user_id, slot_number, event_type, target_time, conn=conn)


class DatabaseRepository:
    """Unified repository facade providing access to all storage entities."""

    def __init__(self, connection_manager: DatabaseConnectionManager) -> None:
        self.cm = connection_manager
        self.users = UserRepository(connection_manager)
        self.slots = UserSlotsRepository(connection_manager)
        self.settings = NotificationSettingsRepository(connection_manager)
        self.cache = ScheduleCacheRepository(connection_manager)
        self.history = NotificationHistoryRepository(connection_manager)

    async def init_schema(self) -> None:
        async with self.cm.get_connection() as conn:
            await init_db(conn)
