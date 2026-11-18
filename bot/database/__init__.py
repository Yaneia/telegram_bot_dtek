"""SQLite database persistence layer."""

from bot.database.connection import DatabaseConnectionManager, configure_connection
from bot.database.repository import (
    DatabaseRepository,
    NotificationHistoryRecord,
    NotificationHistoryRepository,
    NotificationSettingsRecord,
    NotificationSettingsRepository,
    ScheduleCacheRecord,
    ScheduleCacheRepository,
    UserRecord,
    UserRepository,
    UserSlotRecord,
    UserSlotsRepository,
)
from bot.database.schema import (
    EXPECTED_INDEXES,
    EXPECTED_TABLES,
    SCHEMA_DDL,
    init_db,
    verify_schema_integrity,
)

__all__ = [
    # Connection management
    "DatabaseConnectionManager",
    "configure_connection",
    # Schema & DDL
    "SCHEMA_DDL",
    "EXPECTED_TABLES",
    "EXPECTED_INDEXES",
    "init_db",
    "verify_schema_integrity",
    # DTO Records
    "UserRecord",
    "UserSlotRecord",
    "NotificationSettingsRecord",
    "ScheduleCacheRecord",
    "NotificationHistoryRecord",
    # Repositories
    "UserRepository",
    "UserSlotsRepository",
    "NotificationSettingsRepository",
    "ScheduleCacheRepository",
    "NotificationHistoryRepository",
    "DatabaseRepository",
]
