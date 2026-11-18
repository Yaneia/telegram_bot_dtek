"""
tests/test_database.py - SQLite WAL Schema, Constraints, Indexes, and Storage Verification.

Author: Worker E2E Notification & Persistence
Verification Coverage:
- Tier 1: SQLite WAL schema initialization, PRAGMA foreign_keys = ON enforcement,
  creation of 5 authoritative tables (users, user_slots, notification_settings,
  schedule_cache, notification_history), ON DELETE CASCADE deletion verification,
  orphan row rejection, and idempotent schema re-initialization. (>= 5 tests)
- Tier 2: Column CHECK constraints (slot_number IN (1, 2), region_code, group_id),
  UNIQUE(user_id, slot_number), index existence and query plan verification (idx_notif_dedup,
  idx_user_slots_lookup), schedule cache validity window (<= 48h), concurrent connections
  in WAL mode (non-blocking reader/writer), and CRUD lifecycle operations. (>= 5 tests)
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from typing import Any

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests.conftest import SQLITE_SCHEMA_DDL, safe_import


class TestDatabaseTier1(unittest.TestCase):
    """
    Tier 1: SQLite WAL Schema Initialization, PRAGMAs, 5 Tables, and Foreign Keys.
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

    def test_sqlite_wal_schema_initialization(self) -> None:
        """
        Tier 1: Verify that initializing the database executes PRAGMA journal_mode = WAL
        and that SQLite reports 'wal' as the active journal mode.
        """
        cursor = self.conn.execute("PRAGMA journal_mode;")
        journal_mode = cursor.fetchone()[0]
        self.assertEqual(journal_mode.lower(), "wal", f"Expected WAL journal mode, got {journal_mode}")

    def test_foreign_keys_pragma_enforced(self) -> None:
        """
        Tier 1: Verify PRAGMA foreign_keys = ON is enabled and actively enforced by SQLite.
        """
        cursor = self.conn.execute("PRAGMA foreign_keys;")
        fk_status = cursor.fetchone()[0]
        self.assertEqual(fk_status, 1, f"Expected foreign_keys pragma == 1, got {fk_status}")

    def test_five_authoritative_tables_created(self) -> None:
        """
        Tier 1: Verify all 5 authoritative tables defined in PROMPT_SPECIFICATION.md
        are created: users, user_slots, notification_settings, schedule_cache, notification_history.
        """
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
        )
        existing_tables = {row[0] for row in cursor.fetchall()}

        expected_tables = {
            "users",
            "user_slots",
            "notification_settings",
            "schedule_cache",
            "notification_history",
        }
        missing_tables = expected_tables - existing_tables
        self.assertFalse(missing_tables, f"Missing required database tables: {missing_tables}")

    def test_foreign_key_user_slots_cascade_delete(self) -> None:
        """
        Tier 1: Verify ON DELETE CASCADE foreign key from user_slots to users.
        Deleting a user record MUST automatically cascade and purge associated user_slots.
        """
        self.conn.execute(
            "INSERT INTO users (user_id, chat_id, first_name) VALUES (?, ?, ?);",
            (1001, 1001, "Test User Cascade Slots"),
        )
        self.conn.execute(
            """
            INSERT INTO user_slots (user_id, slot_number, alias, region_code, group_id, subgroup_id)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (1001, 1, "Дім", "kyiv", 1, "1.1"),
        )
        self.conn.commit()

        cursor = self.conn.execute("SELECT COUNT(*) FROM user_slots WHERE user_id = ?;", (1001,))
        self.assertEqual(cursor.fetchone()[0], 1, "Failed to insert child user_slot record")

        self.conn.execute("DELETE FROM users WHERE user_id = ?;", (1001,))
        self.conn.commit()

        cursor = self.conn.execute("SELECT COUNT(*) FROM user_slots WHERE user_id = ?;", (1001,))
        remaining_slots = cursor.fetchone()[0]
        self.assertEqual(remaining_slots, 0, f"Expected 0 user_slots after cascade delete, found {remaining_slots}")

    def test_foreign_key_notification_settings_cascade_delete(self) -> None:
        """
        Tier 1: Verify ON DELETE CASCADE foreign key from notification_settings to users.
        Deleting a user record MUST automatically cascade and purge notification_settings.
        """
        self.conn.execute(
            "INSERT INTO users (user_id, chat_id, first_name) VALUES (?, ?, ?);",
            (1002, 1002, "Test User Cascade Settings"),
        )
        self.conn.execute(
            """
            INSERT INTO notification_settings (user_id, notify_30m, notify_15m, silent_hours_start, silent_hours_end)
            VALUES (?, ?, ?, ?, ?);
            """,
            (1002, 1, 1, 23, 7),
        )
        self.conn.commit()

        cursor = self.conn.execute("SELECT COUNT(*) FROM notification_settings WHERE user_id = ?;", (1002,))
        self.assertEqual(cursor.fetchone()[0], 1, "Failed to insert notification_settings record")

        self.conn.execute("DELETE FROM users WHERE user_id = ?;", (1002,))
        self.conn.commit()

        cursor = self.conn.execute("SELECT COUNT(*) FROM notification_settings WHERE user_id = ?;", (1002,))
        remaining_settings = cursor.fetchone()[0]
        self.assertEqual(remaining_settings, 0, f"Expected 0 notification_settings after cascade delete, found {remaining_settings}")

    def test_foreign_key_notification_history_cascade_delete(self) -> None:
        """
        Tier 1: Verify ON DELETE CASCADE foreign key from notification_history to users.
        Deleting a user record MUST automatically cascade and purge notification_history.
        """
        self.conn.execute(
            "INSERT INTO users (user_id, chat_id, first_name) VALUES (?, ?, ?);",
            (1003, 1003, "Test User Cascade History"),
        )
        self.conn.execute(
            """
            INSERT INTO notification_history (user_id, slot_number, event_type, target_slot_index, target_time, hash_sent)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (1003, 1, "30m_before_off", 28, "2026-11-11 14:00:00", "hash123"),
        )
        self.conn.commit()

        cursor = self.conn.execute("SELECT COUNT(*) FROM notification_history WHERE user_id = ?;", (1003,))
        self.assertEqual(cursor.fetchone()[0], 1, "Failed to insert notification_history record")

        self.conn.execute("DELETE FROM users WHERE user_id = ?;", (1003,))
        self.conn.commit()

        cursor = self.conn.execute("SELECT COUNT(*) FROM notification_history WHERE user_id = ?;", (1003,))
        remaining_history = cursor.fetchone()[0]
        self.assertEqual(remaining_history, 0, f"Expected 0 notification_history after cascade delete, found {remaining_history}")

    def test_foreign_key_violation_raises_integrity_error(self) -> None:
        """
        Tier 1: Verify that attempting to insert child records (user_slots, notification_settings)
        referencing a non-existent user_id raises sqlite3.IntegrityError.
        """
        non_existent_user_id = 999999

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO user_slots (user_id, slot_number, alias, region_code, group_id, subgroup_id)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (non_existent_user_id, 1, "Orphan Slot", "kyiv", 1, "1.1"),
            )
            self.conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO notification_settings (user_id, notify_30m, notify_15m)
                VALUES (?, ?, ?);
                """,
                (non_existent_user_id, 1, 1),
            )
            self.conn.commit()

    def test_schema_idempotent_reinitialization(self) -> None:
        """
        Tier 1: Verify executing SQLITE_SCHEMA_DDL multiple times on the same connection
        is completely idempotent and succeeds without throwing exceptions.
        """
        try:
            self.conn.executescript(SQLITE_SCHEMA_DDL)
            self.conn.commit()
        except Exception as exc:
            self.fail(f"Re-executing DDL script failed unexpectedly: {exc}")


class TestDatabaseTier2(unittest.TestCase):
    """
    Tier 2: Constraints, Indexes, Cache Validity, WAL Concurrency, and CRUD.
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

    def test_user_slots_slot_number_check_constraint(self) -> None:
        """
        Tier 2: Verify user_slots CHECK constraint: slot_number IN (1, 2).
        Values 1 and 2 MUST succeed; values 0, 3, -1, 4 MUST raise IntegrityError.
        """
        self.conn.execute(
            "INSERT INTO users (user_id, chat_id, first_name) VALUES (?, ?, ?);",
            (2001, 2001, "Test User Slots Check"),
        )
        self.conn.commit()

        self.conn.execute(
            "INSERT INTO user_slots (user_id, slot_number, alias, region_code, group_id, subgroup_id) "
            "VALUES (?, 1, 'Slot 1', 'kyiv', 1, '1.1');",
            (2001,),
        )
        self.conn.execute(
            "INSERT INTO user_slots (user_id, slot_number, alias, region_code, group_id, subgroup_id) "
            "VALUES (?, 2, 'Slot 2', 'kyiv', 2, '2.1');",
            (2001,),
        )
        self.conn.commit()

        for invalid_slot in (0, 3, -1, 4):
            with self.assertRaises(sqlite3.IntegrityError, msg=f"Expected IntegrityError for slot {invalid_slot}"):
                self.conn.execute(
                    "INSERT INTO user_slots (user_id, slot_number, alias, region_code, group_id, subgroup_id) "
                    "VALUES (?, ?, 'Bad Slot', 'kyiv', 1, '1.1');",
                    (2001, invalid_slot),
                )
                self.conn.commit()

    def test_user_slots_unique_user_id_and_slot_number_constraint(self) -> None:
        """
        Tier 2: Verify UNIQUE(user_id, slot_number) constraint on user_slots.
        Attempting to insert a duplicate slot_number for the same user MUST raise IntegrityError.
        """
        self.conn.execute(
            "INSERT INTO users (user_id, chat_id, first_name) VALUES (?, ?, ?);",
            (2002, 2002, "Test User Unique Slot"),
        )
        self.conn.execute(
            "INSERT INTO user_slots (user_id, slot_number, alias, region_code, group_id, subgroup_id) "
            "VALUES (?, 1, 'Дім Перший', 'kyiv', 1, '1.1');",
            (2002,),
        )
        self.conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO user_slots (user_id, slot_number, alias, region_code, group_id, subgroup_id) "
                "VALUES (?, 1, 'Дім Другий Дубль', 'kyiv', 2, '2.1');",
                (2002,),
            )
            self.conn.commit()

    def test_user_slots_region_code_check_constraint(self) -> None:
        """
        Tier 2: Verify region_code CHECK (region_code IN ('kyiv', 'kyiv-region', 'dnipro', 'odesa')).
        """
        self.conn.execute(
            "INSERT INTO users (user_id, chat_id, first_name) VALUES (?, ?, ?);",
            (2003, 2003, "Test User Region Check"),
        )
        self.conn.commit()

        for valid_reg in ("kyiv", "kyiv-region", "dnipro", "odesa"):
            user_id = 2003 + hash(valid_reg) % 1000 + 10
            self.conn.execute(
                "INSERT OR IGNORE INTO users (user_id, chat_id, first_name) VALUES (?, ?, ?);",
                (user_id, user_id, f"User {valid_reg}"),
            )
            self.conn.execute(
                "INSERT INTO user_slots (user_id, slot_number, alias, region_code, group_id, subgroup_id) "
                "VALUES (?, 1, 'Alias', ?, 1, '1.1');",
                (user_id, valid_reg),
            )
            self.conn.commit()

        for invalid_reg in ("lviv", "kharkiv", "donetsk", "kyiv_city", "invalid"):
            with self.assertRaises(sqlite3.IntegrityError, msg=f"Expected IntegrityError for region '{invalid_reg}'"):
                self.conn.execute(
                    "INSERT INTO user_slots (user_id, slot_number, alias, region_code, group_id, subgroup_id) "
                    "VALUES (?, 2, 'Alias', ?, 1, '1.1');",
                    (2003, invalid_reg),
                )
                self.conn.commit()

    def test_user_slots_group_id_check_constraint(self) -> None:
        """
        Tier 2: Verify group_id CHECK (group_id BETWEEN 1 AND 6).
        """
        self.conn.execute(
            "INSERT INTO users (user_id, chat_id, first_name) VALUES (?, ?, ?);",
            (2004, 2004, "Test User Group Check"),
        )
        self.conn.commit()

        for valid_gid in (1, 6):
            uid = 2004 + valid_gid
            self.conn.execute(
                "INSERT OR IGNORE INTO users (user_id, chat_id, first_name) VALUES (?, ?, ?);",
                (uid, uid, f"User G{valid_gid}"),
            )
            self.conn.execute(
                "INSERT INTO user_slots (user_id, slot_number, alias, region_code, group_id, subgroup_id) "
                "VALUES (?, 1, 'Alias', 'kyiv', ?, '1.1');",
                (uid, valid_gid),
            )
            self.conn.commit()

        for invalid_gid in (0, 7, -1, 10):
            with self.assertRaises(sqlite3.IntegrityError, msg=f"Expected IntegrityError for group_id {invalid_gid}"):
                self.conn.execute(
                    "INSERT INTO user_slots (user_id, slot_number, alias, region_code, group_id, subgroup_id) "
                    "VALUES (?, 2, 'Alias', 'kyiv', ?, '1.1');",
                    (2004, invalid_gid),
                )
                self.conn.commit()

    def test_notification_history_dedup_index_and_query_plan(self) -> None:
        """
        Tier 2: Verify deduplication index idx_notif_dedup exists on
        notification_history(user_id, slot_number, event_type, target_time)
        and SQLite uses it in EXPLAIN QUERY PLAN.
        """
        cursor = self.conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND name='idx_notif_dedup';"
        )
        index_row = cursor.fetchone()
        self.assertIsNotNone(index_row, "Index idx_notif_dedup not found in sqlite_master")

        self.conn.execute(
            "INSERT INTO users (user_id, chat_id, first_name) VALUES (?, ?, ?);",
            (2005, 2005, "Test User Dedup Index"),
        )
        self.conn.execute(
            """
            INSERT INTO notification_history (user_id, slot_number, event_type, target_slot_index, target_time, hash_sent)
            VALUES (?, 1, '30m_before_off', 28, '2026-11-11 14:00:00', 'hash_sample');
            """,
            (2005,),
        )
        self.conn.commit()

        query = """
        EXPLAIN QUERY PLAN
        SELECT id FROM notification_history
        WHERE user_id = ? AND slot_number = ? AND event_type = ? AND target_time = ?;
        """
        cursor = self.conn.execute(query, (2005, 1, "30m_before_off", "2026-11-11 14:00:00"))
        plan_rows = cursor.fetchall()
        plan_text = " ".join(str(row) for row in plan_rows)

        self.assertIn("idx_notif_dedup", plan_text, f"Query plan did not utilize idx_notif_dedup: {plan_text}")

    def test_user_slots_lookup_index_and_query_plan(self) -> None:
        """
        Tier 2: Verify lookup index idx_user_slots_lookup exists on
        user_slots(region_code, subgroup_id) and is used when querying affected users.
        """
        cursor = self.conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND name='idx_user_slots_lookup';"
        )
        index_row = cursor.fetchone()
        self.assertIsNotNone(index_row, "Index idx_user_slots_lookup not found in sqlite_master")

        query = """
        EXPLAIN QUERY PLAN
        SELECT user_id, slot_number, alias FROM user_slots
        WHERE region_code = ? AND subgroup_id = ?;
        """
        cursor = self.conn.execute(query, ("kyiv", "1.1"))
        plan_rows = cursor.fetchall()
        plan_text = " ".join(str(row) for row in plan_rows)

        self.assertIn("idx_user_slots_lookup", plan_text, f"Query plan did not utilize idx_user_slots_lookup: {plan_text}")

    def test_schedule_cache_validity_window_48h_query(self) -> None:
        """
        Tier 2: Verify schedule_cache freshness query logic:
        Schedules updated within <= 48 hours are returned as valid cached data.
        Schedules older than 48 hours are treated as expired.
        """
        self.conn.execute(
            """
            INSERT INTO schedule_cache (region_code, schedule_date, payload_json, payload_hash, is_emergency, data_source, updated_at)
            VALUES ('kyiv', '2026-11-11', '{"status": "ok"}', 'hash_kyiv', 0, 'direct_scrape', datetime('now', '-1 hour'));
            """
        )
        self.conn.execute(
            """
            INSERT INTO schedule_cache (region_code, schedule_date, payload_json, payload_hash, is_emergency, data_source, updated_at)
            VALUES ('dnipro', '2026-11-11', '{"status": "ok"}', 'hash_dnipro', 0, 'github_mirror', datetime('now', '-40 hours'));
            """
        )
        self.conn.execute(
            """
            INSERT INTO schedule_cache (region_code, schedule_date, payload_json, payload_hash, is_emergency, data_source, updated_at)
            VALUES ('odesa', '2026-11-11', '{"status": "ok"}', 'hash_odesa', 0, 'yasno_api', datetime('now', '-50 hours'));
            """
        )
        self.conn.commit()

        cursor = self.conn.execute(
            """
            SELECT region_code FROM schedule_cache
            WHERE updated_at >= datetime('now', '-48 hours');
            """
        )
        valid_regions = {row[0] for row in cursor.fetchall()}

        self.assertIn("kyiv", valid_regions, "Fresh schedule (1h old) should be valid")
        self.assertIn("dnipro", valid_regions, "Stale schedule (40h old <= 48h) should be valid")
        self.assertNotIn("odesa", valid_regions, "Expired schedule (50h old > 48h) must be excluded")

    def test_concurrent_connection_handling_wal_mode(self) -> None:
        """
        Tier 2: Verify WAL mode handles concurrent readers and writers without database locking errors.
        Connection 1 writes while Connection 2 reads simultaneously.
        """
        conn1 = sqlite3.connect(self.temp_db_path, timeout=5.0)
        conn1.execute("PRAGMA journal_mode = WAL;")
        conn1.execute("PRAGMA foreign_keys = ON;")

        conn1.execute(
            "INSERT INTO users (user_id, chat_id, first_name) VALUES (?, ?, ?);",
            (3001, 3001, "Initial User WAL"),
        )
        conn1.commit()

        conn2 = sqlite3.connect(self.temp_db_path, timeout=5.0)
        conn2.execute("PRAGMA journal_mode = WAL;")
        conn2.execute("PRAGMA foreign_keys = ON;")

        cursor = conn2.execute("SELECT first_name FROM users WHERE user_id = 3001;")
        self.assertEqual(cursor.fetchone()[0], "Initial User WAL")

        conn1.execute("BEGIN IMMEDIATE;")
        conn1.execute(
            "INSERT INTO users (user_id, chat_id, first_name) VALUES (?, ?, ?);",
            (3002, 3002, "Second User In-Flight"),
        )

        # Connection 2 reads concurrently without blocking
        cursor = conn2.execute("SELECT COUNT(*) FROM users;")
        self.assertEqual(cursor.fetchone()[0], 1)

        conn1.commit()

        cursor = conn2.execute("SELECT COUNT(*) FROM users;")
        self.assertEqual(cursor.fetchone()[0], 2)

        conn1.close()
        conn2.close()

    def test_database_sql_crud_lifecycle(self) -> None:
        """
        Tier 2: Verify complete SQL CRUD operations for users, slots, settings, cache, and history.
        Validates end-to-end relational data integrity, default values, and queries across all 5 tables.
        """
        user_id = 4001
        chat_id = 4001
        first_name = "Олександр"

        # 1. Users CRUD
        self.conn.execute(
            "INSERT INTO users (user_id, chat_id, username, first_name, is_active) VALUES (?, ?, ?, ?, 1);",
            (user_id, chat_id, "oleksandr_dtek", first_name),
        )
        self.conn.commit()

        cursor = self.conn.execute(
            "SELECT user_id, chat_id, username, first_name, is_active FROM users WHERE user_id = ?;",
            (user_id,),
        )
        row = cursor.fetchone()
        self.assertEqual(row, (user_id, chat_id, "oleksandr_dtek", first_name, 1))

        self.conn.execute("UPDATE users SET is_active = 0 WHERE user_id = ?;", (user_id,))
        self.conn.commit()
        cursor = self.conn.execute("SELECT is_active FROM users WHERE user_id = ?;", (user_id,))
        self.assertEqual(cursor.fetchone()[0], 0)

        # 2. User Slots CRUD
        self.conn.execute(
            """
            INSERT INTO user_slots (user_id, slot_number, alias, region_code, group_id, subgroup_id)
            VALUES (?, 1, 'Дім (Оболонь)', 'kyiv', 1, '1.1');
            """,
            (user_id,),
        )
        self.conn.execute(
            """
            INSERT INTO user_slots (user_id, slot_number, alias, region_code, group_id, subgroup_id)
            VALUES (?, 2, 'Офіс (Поділ)', 'kyiv', 3, '3.2');
            """,
            (user_id,),
        )
        self.conn.commit()

        cursor = self.conn.execute(
            "SELECT slot_number, alias, subgroup_id FROM user_slots WHERE user_id = ? ORDER BY slot_number;",
            (user_id,),
        )
        slots = cursor.fetchall()
        self.assertEqual(len(slots), 2)
        self.assertEqual(slots[0], (1, "Дім (Оболонь)", "1.1"))
        self.assertEqual(slots[1], (2, "Офіс (Поділ)", "3.2"))

        # 3. Notification Settings CRUD
        self.conn.execute(
            """
            INSERT INTO notification_settings (user_id, notify_30m, notify_15m, notify_schedule_change, silent_hours_start, silent_hours_end)
            VALUES (?, 1, 1, 1, 23, 7);
            """,
            (user_id,),
        )
        self.conn.commit()

        cursor = self.conn.execute(
            "SELECT notify_30m, notify_15m, notify_schedule_change, silent_hours_start, silent_hours_end FROM notification_settings WHERE user_id = ?;",
            (user_id,),
        )
        self.assertEqual(cursor.fetchone(), (1, 1, 1, 23, 7))

        # 4. Schedule Cache Upsert
        schedule_json = '{"region": "kyiv", "date": "2026-11-11", "status": "ok"}'
        schedule_hash = "abc123sha256hash"
        self.conn.execute(
            """
            INSERT INTO schedule_cache (region_code, schedule_date, payload_json, payload_hash, is_emergency, data_source)
            VALUES ('kyiv', '2026-11-11', ?, ?, 0, 'direct_scrape')
            ON CONFLICT(region_code) DO UPDATE SET
                schedule_date = excluded.schedule_date,
                payload_json = excluded.payload_json,
                payload_hash = excluded.payload_hash,
                updated_at = CURRENT_TIMESTAMP;
            """,
            (schedule_json, schedule_hash),
        )
        self.conn.commit()

        cursor = self.conn.execute("SELECT payload_hash, is_emergency FROM schedule_cache WHERE region_code = 'kyiv';")
        self.assertEqual(cursor.fetchone(), (schedule_hash, 0))

        # 5. Notification History CRUD & Dedup
        target_time_str = "2026-11-11 14:00:00"
        self.conn.execute(
            """
            INSERT INTO notification_history (user_id, slot_number, event_type, target_slot_index, target_time, hash_sent)
            VALUES (?, 1, '30m_before_off', 28, ?, ?);
            """,
            (user_id, target_time_str, schedule_hash),
        )
        self.conn.commit()

        cursor = self.conn.execute(
            """
            SELECT COUNT(*) FROM notification_history
            WHERE user_id = ? AND slot_number = ? AND event_type = ? AND target_time = ?;
            """,
            (user_id, 1, "30m_before_off", target_time_str),
        )
        self.assertEqual(cursor.fetchone()[0], 1)

    def test_repository_layer_integration(self) -> None:
        """
        Tier 2: Progressive verification of the bot.database.repository module.
        Safely imported; skipped if the module is under active construction by M1 worker.
        """
        user_repo_cls = safe_import("bot.database.repository", "UserRepository")
        self.assertIsNotNone(user_repo_cls)


class TestDatabaseTier3AsyncRepository(unittest.IsolatedAsyncioTestCase):
    """
    Tier 3: Asynchronous Repository CRUD and Schema Verification.
    """

    async def asyncSetUp(self) -> None:
        self.fd, self.temp_db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(self.fd)
        from bot.database.connection import DatabaseConnectionManager
        from bot.database.repository import DatabaseRepository
        from bot.database.schema import init_db

        self.cm = DatabaseConnectionManager(self.temp_db_path)
        self.repo = DatabaseRepository(self.cm)
        async with self.cm.get_connection() as conn:
            await init_db(conn)

    async def asyncTearDown(self) -> None:
        await self.cm.close()
        for p in [self.temp_db_path, f"{self.temp_db_path}-wal", f"{self.temp_db_path}-shm"]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    async def test_schema_integrity_verification(self) -> None:
        """Verify schema integrity function returns valid with all expected tables and indexes."""
        from bot.database.schema import verify_schema_integrity

        async with self.cm.get_connection() as conn:
            result = await verify_schema_integrity(conn)
            self.assertTrue(result["is_valid"])
            self.assertEqual(len(result["missing_tables"]), 0)
            self.assertEqual(len(result["missing_indexes"]), 0)
            self.assertEqual(len(result["fk_violations"]), 0)

    async def test_user_repository_async_crud(self) -> None:
        """Verify UserRepository upsert, retrieve, update, and active toggle."""
        # 1. Upsert new user
        user = await self.repo.users.upsert_user(
            user_id=5001,
            chat_id=5001,
            first_name="Марія",
            username="maria_dtek",
        )
        self.assertEqual(user.user_id, 5001)
        self.assertEqual(user.first_name, "Марія")
        self.assertTrue(user.is_active)

        # 2. Get user
        fetched = await self.repo.users.get_user(5001)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.username, "maria_dtek")

        # 3. Upsert conflict update
        updated = await self.repo.users.upsert_user(
            user_id=5001,
            chat_id=5001,
            first_name="Марія Оновлена",
            username="maria_updated",
        )
        self.assertEqual(updated.first_name, "Марія Оновлена")
        self.assertEqual(updated.username, "maria_updated")

        # 4. Set user active toggle
        await self.repo.users.set_user_active(5001, False)
        inactive = await self.repo.users.get_user(5001)
        self.assertFalse(inactive.is_active)

    async def test_user_slots_repository_async_crud(self) -> None:
        """Verify UserSlotsRepository slot upsert, normalization, fetch, delete, and subscriber queries."""
        await self.repo.users.upsert_user(user_id=5002, chat_id=5002, first_name="Тарас")

        # 1. Upsert slot 1 with raw group format 'GPV1.1' -> normalized to group 1, subgroup '1.1'
        slot1 = await self.repo.slots.upsert_slot(
            user_id=5002,
            slot_number=1,
            region_code="kyiv",
            group_id=1,
            subgroup_id="GPV1.1",
            alias="Дім",
        )
        self.assertEqual(slot1.slot_number, 1)
        self.assertEqual(slot1.group_id, 1)
        self.assertEqual(slot1.subgroup_id, "1.1")
        self.assertEqual(slot1.alias, "Дім")

        # 2. Upsert slot 2
        slot2 = await self.repo.slots.upsert_slot(
            user_id=5002,
            slot_number=2,
            region_code="kyiv-region",
            group_id=2,
            subgroup_id="2.1",
            alias="Дача",
        )
        self.assertEqual(slot2.slot_number, 2)
        self.assertEqual(slot2.subgroup_id, "2.1")

        # 3. Get slots for user
        slots = await self.repo.slots.get_slots_for_user(5002)
        self.assertEqual(len(slots), 2)
        self.assertEqual(slots[0].alias, "Дім")
        self.assertEqual(slots[1].alias, "Дача")

        # 4. Get slot configs
        configs = await self.repo.slots.get_slot_configs_for_user(5002)
        self.assertEqual(len(configs), 2)
        self.assertEqual(configs[0].subgroup_id, "1.1")

        # 5. Query subscribers by subgroup
        subs = await self.repo.slots.get_subscribers_by_subgroup("kyiv", "1.1")
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["user_id"], 5002)

        # 6. Delete slot 2
        deleted = await self.repo.slots.delete_slot(5002, 2)
        self.assertTrue(deleted)
        remaining = await self.repo.slots.get_slots_for_user(5002)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].slot_number, 1)

    async def test_notification_settings_repository_async_crud(self) -> None:
        """Verify NotificationSettingsRepository default values and updates."""
        await self.repo.users.upsert_user(user_id=5003, chat_id=5003, first_name="Оксана")

        # 1. Get settings before explicit upsert returns default
        defaults = await self.repo.settings.get_settings(5003)
        self.assertTrue(defaults.notify_30m)
        self.assertTrue(defaults.notify_15m)
        self.assertEqual(defaults.silent_hours_start, 23)
        self.assertEqual(defaults.silent_hours_end, 7)

        # 2. Upsert custom settings
        custom = await self.repo.settings.upsert_settings(
            user_id=5003,
            notify_30m=False,
            notify_15m=True,
            notify_schedule_change=False,
            silent_hours_start=22,
            silent_hours_end=8,
        )
        self.assertFalse(custom.notify_30m)
        self.assertTrue(custom.notify_15m)
        self.assertFalse(custom.notify_schedule_change)
        self.assertEqual(custom.silent_hours_start, 22)
        self.assertEqual(custom.silent_hours_end, 8)

        # 3. Update settings partially
        part = await self.repo.settings.update_settings(user_id=5003, notify_30m=True)
        self.assertTrue(part.notify_30m)
        self.assertTrue(part.notify_15m)

    async def test_schedule_cache_repository_async_crud(self) -> None:
        """Verify ScheduleCacheRepository set, get, update, and prune."""
        # 1. Set cache
        record = await self.repo.cache.set_cache(
            region_code="dnipro",
            schedule_date="2026-11-11",
            payload_json='{"status": "test"}',
            payload_hash="hash_dnipro_1",
            is_emergency=False,
            data_source="mock",
        )
        self.assertEqual(record.region_code, "dnipro")
        self.assertEqual(record.payload_hash, "hash_dnipro_1")

        # 2. Get cache
        cached = await self.repo.cache.get_cache("dnipro")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.payload_hash, "hash_dnipro_1")
        self.assertFalse(cached.is_emergency)

        # 3. Update cache with emergency
        updated = await self.repo.cache.set_cache(
            region_code="dnipro",
            schedule_date="2026-11-11",
            payload_json='{"status": "emergency"}',
            payload_hash="hash_dnipro_2",
            is_emergency=True,
            emergency_message="Аварійне відключення",
            data_source="direct_scrape",
        )
        self.assertTrue(updated.is_emergency)
        self.assertEqual(updated.emergency_message, "Аварійне відключення")

        # 4. Prune stale cache
        pruned_count = await self.repo.cache.prune_stale_cache(days=7)
        self.assertEqual(pruned_count, 0)

    async def test_notification_history_repository_async_crud(self) -> None:
        """Verify NotificationHistoryRepository record and deduplication check."""
        await self.repo.users.upsert_user(user_id=5004, chat_id=5004, first_name="Ігор")

        target_time = "2026-11-11 16:00:00"

        # 1. Check before recording -> False
        is_sent = await self.repo.history.is_notification_sent(
            user_id=5004,
            slot_number=1,
            event_type="15m_before_off",
            target_time=target_time,
        )
        self.assertFalse(is_sent)

        # 2. Record notification
        record = await self.repo.history.record_notification(
            user_id=5004,
            slot_number=1,
            event_type="15m_before_off",
            target_time=target_time,
            hash_sent="hash_audit_1",
            target_slot_index=32,
        )
        self.assertEqual(record.user_id, 5004)
        self.assertEqual(record.target_slot_index, 32)

        # 3. Check after recording -> True
        is_sent_after = await self.repo.history.is_notification_sent(
            user_id=5004,
            slot_number=1,
            event_type="15m_before_off",
            target_time=target_time,
        )
        self.assertTrue(is_sent_after)
        has_sent = await self.repo.history.has_notification_been_sent(
            user_id=5004,
            slot_number=1,
            event_type="15m_before_off",
            target_time=target_time,
        )
        self.assertTrue(has_sent)


if __name__ == "__main__":
    unittest.main()

