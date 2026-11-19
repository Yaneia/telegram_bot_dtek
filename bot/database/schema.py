"""SQLite database schema definitions, DDL execution, and integrity verification."""

from typing import Any, Dict, List, Set, Union
from bot.database.connection import aiosqlite


SCHEMA_DDL = """
-- 1. Users registry
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER DEFAULT NULL,
    username TEXT,
    first_name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT 1
);

-- 2. User monitored location slots (Dual-slot architecture: Slot 1 and Slot 2)
CREATE TABLE IF NOT EXISTS user_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    slot_number INTEGER NOT NULL CHECK (slot_number IN (1, 2)),
    alias TEXT NOT NULL DEFAULT 'Моя черга',
    region_code TEXT NOT NULL CHECK (region_code IN ('kyiv', 'kyiv-region', 'dnipro', 'odesa')),
    group_id INTEGER NOT NULL CHECK (group_id BETWEEN 1 AND 6),
    subgroup_id TEXT NOT NULL, -- '1.1' through '6.2'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, slot_number)
);

-- Subgroup subscriber lookup index for notification broadcasts
CREATE INDEX IF NOT EXISTS idx_user_slots_lookup 
ON user_slots(region_code, subgroup_id);

-- 3. Per-user notification settings
CREATE TABLE IF NOT EXISTS notification_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    notify_30m BOOLEAN DEFAULT 1,
    notify_15m BOOLEAN DEFAULT 1,
    notify_schedule_change BOOLEAN DEFAULT 1,
    silent_hours_start INTEGER DEFAULT 23, -- 23:00
    silent_hours_end INTEGER DEFAULT 7,     -- 07:00
    show_joint_timeline BOOLEAN DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. Stale-While-Revalidate regional schedule cache
CREATE TABLE IF NOT EXISTS schedule_cache (
    region_code TEXT PRIMARY KEY,
    schedule_date TEXT NOT NULL,          -- 'YYYY-MM-DD'
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,           -- SHA-256 of payload_json
    is_emergency BOOLEAN DEFAULT 0,
    emergency_message TEXT,
    data_source TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 5. Dispatched notification audit and deduplication log
CREATE TABLE IF NOT EXISTS notification_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    slot_number INTEGER NOT NULL,
    event_type TEXT NOT NULL,             -- '30m_before_off', '15m_before_off', 'schedule_diff'
    target_slot_index INTEGER,            -- 0..47
    target_time TIMESTAMP NOT NULL,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hash_sent TEXT NOT NULL
);

-- Notification deduplication index to prevent duplicate alerts
CREATE INDEX IF NOT EXISTS idx_notif_dedup 
ON notification_history(user_id, slot_number, event_type, target_time);

-- 6. Usage analytics events (lightweight telemetry for admin bot)
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,   -- 'command', 'callback', 'webapp'
    action TEXT NOT NULL,       -- e.g. '/start', '/status', 'tma_sync', 'cb:refresh_schedule', 'webapp_data'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_usage_events_time ON usage_events(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_events_action ON usage_events(action, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_events_user ON usage_events(user_id, created_at);
"""

EXPECTED_TABLES: Set[str] = {
    "users",
    "user_slots",
    "notification_settings",
    "schedule_cache",
    "notification_history",
    "usage_events",
}

EXPECTED_INDEXES: Set[str] = {
    "idx_user_slots_lookup",
    "idx_notif_dedup",
    "idx_usage_events_time",
    "idx_usage_events_action",
    "idx_usage_events_user",
}


async def init_db(conn: Union[aiosqlite.Connection, Any]) -> None:
    """Initialize SQLite database schema tables and indexes in a transactional block."""
    try:
        await conn.executescript(SCHEMA_DDL)
        try:
            await conn.execute(
                "ALTER TABLE notification_settings ADD COLUMN show_joint_timeline BOOLEAN DEFAULT 0;"
            )
        except Exception:
            pass
        try:
            await conn.execute(
                "ALTER TABLE users ADD COLUMN thread_id INTEGER DEFAULT NULL;"
            )
        except Exception:
            pass
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def verify_schema_integrity(conn: Union[aiosqlite.Connection, Any]) -> Dict[str, Any]:
    """Verify that all required tables and indexes exist and foreign keys are valid.
    
    Returns:
        Dict with 'tables', 'indexes', 'fk_violations', and 'is_valid'.
    """
    # Check tables
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
    )
    rows = await cur.fetchall()
    actual_tables: Set[str] = {row[0] for row in rows}

    # Check indexes
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%';"
    )
    rows = await cur.fetchall()
    actual_indexes: Set[str] = {row[0] for row in rows}

    # Foreign key check
    cur = await conn.execute("PRAGMA foreign_key_check;")
    fk_violations = await cur.fetchall()

    missing_tables = EXPECTED_TABLES - actual_tables
    missing_indexes = EXPECTED_INDEXES - actual_indexes

    is_valid = (
        len(missing_tables) == 0
        and len(missing_indexes) == 0
        and len(fk_violations) == 0
    )

    return {
        "is_valid": is_valid,
        "tables": actual_tables,
        "indexes": actual_indexes,
        "missing_tables": missing_tables,
        "missing_indexes": missing_indexes,
        "fk_violations": fk_violations,
    }
