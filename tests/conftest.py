"""
tests/conftest.py - Shared Pytest Fixtures and Test Infrastructure for DTEK Outage Tracker.

Provides:
- Deterministic test data & timezone fixtures (ZoneInfo("Europe/Kyiv"))
- Embedded dictionary representations of the mock fixtures (mock_kyiv, mock_kyiv_region, mock_dnipro, mock_odesa)
- Synthetic 48-interval schedule generators
- Lazy/safe import utility `safe_import(module_path, attribute_name)`
- Shared async & sync temporary SQLite database fixtures (with WAL mode & foreign keys)
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime
import importlib
import json
import os
import sqlite3
import tempfile
from typing import Any, Callable, Dict, Generator, List, Optional, Union
from zoneinfo import ZoneInfo

try:
    import pytest
except ImportError:
    # Minimal compatibility layer when pytest is not yet installed in the environment
    import unittest

    class _PytestStub:
        def fixture(self, *args: Any, **kwargs: Any) -> Any:
            def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
                return func

            if len(args) == 1 and callable(args[0]):
                return args[0]
            return decorator

        def skip(self, reason: str = "") -> None:
            raise unittest.SkipTest(reason)

    pytest = _PytestStub()  # type: ignore[misc]


# ============================================================================
# 1. TIMEZONE & TEMPORAL FIXTURES
# ============================================================================

KYIV_TZ = ZoneInfo("Europe/Kyiv")
DEFAULT_TEST_DATE = "2026-11-11"
DEFAULT_TEST_DATETIME_ISO = "2026-11-11T12:00:00+02:00"


@pytest.fixture(scope="session")
def kyiv_tz() -> ZoneInfo:
    """Return the authoritative Europe/Kyiv ZoneInfo instance."""
    return KYIV_TZ


@pytest.fixture
def fixed_kyiv_now() -> datetime:
    """
    Return fixed daytime moment: 2026-11-11 14:15:00 Europe/Kyiv.
    Corresponds to slot index 28 (14:00 - 14:30), 15 minutes before the 14:30 boundary.
    """
    return datetime(2026, 11, 11, 14, 15, 0, tzinfo=KYIV_TZ)


@pytest.fixture
def fixed_date_str() -> str:
    """Authoritative test date string YYYY-MM-DD."""
    return DEFAULT_TEST_DATE


# ============================================================================
# 2. SYNTHETIC 48-INTERVAL SCHEDULE GENERATORS
# ============================================================================

def format_interval_times(slot_index: int) -> tuple[str, str]:
    """
    Convert slot index (0..47) to start_time and end_time strings.
    Index 0: ("00:00", "00:30")
    Index 47: ("23:30", "24:00")
    """
    if not (0 <= slot_index <= 47):
        raise ValueError(f"slot_index must be between 0 and 47, got {slot_index}")

    start_min = slot_index * 30
    end_min = (slot_index + 1) * 30

    start_h, start_m = divmod(start_min, 60)
    end_h, end_m = divmod(end_min, 60)

    start_time = f"{start_h:02d}:{start_m:02d}"
    end_time = "24:00" if end_min == 1440 else f"{end_h:02d}:{end_m:02d}"
    return start_time, end_time


def generate_synthetic_slots(
    pattern: Union[str, List[str]] = "ON"
) -> List[Dict[str, Any]]:
    """
    Generate exactly 48 30-minute interval dictionaries.
    `pattern` can be:
    - A single status string: "ON", "OFF", "MAYBE", "EMERGENCY" (applied to all 48 slots).
    - A list of status strings (cycled or matched to 48 slots).
    """
    if isinstance(pattern, str):
        statuses = [pattern] * 48
    elif isinstance(pattern, list):
        if len(pattern) == 48:
            statuses = list(pattern)
        elif len(pattern) > 0:
            # Cycle pattern up to 48
            statuses = [pattern[i % len(pattern)] for i in range(48)]
        else:
            statuses = ["ON"] * 48
    else:
        raise TypeError(f"pattern must be str or list of str, got {type(pattern)}")

    slots: List[Dict[str, Any]] = []
    for idx in range(48):
        start_time, end_time = format_interval_times(idx)
        slots.append({
            "slot_index": idx,
            "start_time": start_time,
            "end_time": end_time,
            "status": statuses[idx],
        })
    return slots


@pytest.fixture
def synthetic_schedule_factory() -> Callable[..., List[Dict[str, Any]]]:
    """Pytest fixture providing the synthetic 48-slot generator callable."""
    return generate_synthetic_slots


# ============================================================================
# 3. DETERMINISTIC DSO MOCK FIXTURES (EMBEDDED)
# ============================================================================

def _build_slots_from_ranges(ranges: List[tuple[int, int, str]]) -> List[Dict[str, Any]]:
    """Build 48 slots from a list of (start_idx, end_idx_inclusive, status)."""
    status_map: Dict[int, str] = {}
    for start_i, end_i, status in ranges:
        for i in range(start_i, end_i + 1):
            status_map[i] = status

    slots: List[Dict[str, Any]] = []
    for idx in range(48):
        start_t, end_t = format_interval_times(idx)
        slots.append({
            "slot_index": idx,
            "start_time": start_t,
            "end_time": end_t,
            "status": status_map.get(idx, "ON"),
        })
    return slots


# Kyiv City (KEM): GPV1.1 and GPV3.2
_KYIV_GPV1_1_SLOTS = _build_slots_from_ranges([
    (0, 3, "ON"),
    (4, 10, "OFF"),
    (11, 17, "ON"),
    (18, 21, "MAYBE"),
    (22, 27, "ON"),
    (28, 34, "OFF"),
    (35, 41, "ON"),
    (42, 43, "MAYBE"),
    (44, 47, "ON"),
])

_KYIV_GPV3_2_SLOTS = _build_slots_from_ranges([
    (0, 15, "ON"),
    (16, 23, "OFF"),
    (24, 35, "ON"),
    (36, 39, "OFF"),
    (40, 47, "ON"),
])

MOCK_KYIV_RAW: Dict[str, Any] = {
    "region_id": "kyiv",
    "region_name": "м. Київ (ДТЕК КЕМ)",
    "date": DEFAULT_TEST_DATE,
    "updated_at": DEFAULT_TEST_DATETIME_ISO,
    "is_emergency": False,
    "emergency_message": None,
    "data_source": "mock",
    "stale": False,
    "groups": {
        "GPV1.1": {
            "group_id": 1,
            "subgroup_id": "1.1",
            "slots": _KYIV_GPV1_1_SLOTS,
        },
        "GPV3.2": {
            "group_id": 3,
            "subgroup_id": "3.2",
            "slots": _KYIV_GPV3_2_SLOTS,
        },
    },
}

# Kyiv Oblast (KREM): GPV2.1
_KYIV_REGION_GPV2_1_SLOTS = _build_slots_from_ranges([
    (0, 3, "OFF"),
    (4, 11, "ON"),
    (12, 15, "OFF"),
    (16, 23, "ON"),
    (24, 27, "MAYBE"),
    (28, 31, "ON"),
    (32, 35, "OFF"),
    (36, 43, "ON"),
    (44, 47, "OFF"),
])

MOCK_KYIV_REGION_RAW: Dict[str, Any] = {
    "region_id": "kyiv-region",
    "region_name": "Київська область (ДТЕК КРЕМ)",
    "date": DEFAULT_TEST_DATE,
    "updated_at": DEFAULT_TEST_DATETIME_ISO,
    "is_emergency": False,
    "emergency_message": None,
    "data_source": "mock",
    "stale": False,
    "groups": {
        "GPV2.1": {
            "group_id": 2,
            "subgroup_id": "2.1",
            "slots": _KYIV_REGION_GPV2_1_SLOTS,
        },
    },
}

# Dnipro (DNEM): GPV4.1
_DNIPRO_GPV4_1_SLOTS = _build_slots_from_ranges([
    (0, 5, "ON"),
    (6, 11, "OFF"),
    (12, 21, "ON"),
    (22, 27, "OFF"),
    (28, 31, "ON"),
    (32, 33, "MAYBE"),
    (34, 37, "ON"),
    (38, 41, "OFF"),
    (42, 47, "ON"),
])

MOCK_DNIPRO_RAW: Dict[str, Any] = {
    "region_id": "dnipro",
    "region_name": "Дніпропетровська область (ДТЕК ДНЕМ)",
    "date": DEFAULT_TEST_DATE,
    "updated_at": DEFAULT_TEST_DATETIME_ISO,
    "is_emergency": False,
    "emergency_message": None,
    "data_source": "mock",
    "stale": False,
    "groups": {
        "GPV4.1": {
            "group_id": 4,
            "subgroup_id": "4.1",
            "slots": _DNIPRO_GPV4_1_SLOTS,
        },
    },
}

# Odesa (OEM): GPV5.2
_ODESA_GPV5_2_SLOTS = _build_slots_from_ranges([
    (0, 9, "ON"),
    (10, 15, "OFF"),
    (16, 19, "ON"),
    (20, 21, "MAYBE"),
    (22, 27, "ON"),
    (28, 33, "OFF"),
    (34, 39, "ON"),
    (40, 43, "OFF"),
    (44, 47, "ON"),
])

MOCK_ODESA_RAW: Dict[str, Any] = {
    "region_id": "odesa",
    "region_name": "Одеська область (ДТЕК ОЕМ)",
    "date": DEFAULT_TEST_DATE,
    "updated_at": DEFAULT_TEST_DATETIME_ISO,
    "is_emergency": False,
    "emergency_message": None,
    "data_source": "mock",
    "stale": False,
    "groups": {
        "GPV5.2": {
            "group_id": 5,
            "subgroup_id": "5.2",
            "slots": _ODESA_GPV5_2_SLOTS,
        },
    },
}

ALL_MOCKS_RAW: Dict[str, Dict[str, Any]] = {
    "kyiv": MOCK_KYIV_RAW,
    "kyiv-region": MOCK_KYIV_REGION_RAW,
    "dnipro": MOCK_DNIPRO_RAW,
    "odesa": MOCK_ODESA_RAW,
}


@pytest.fixture
def mock_kyiv() -> Dict[str, Any]:
    """Deep copy of Kyiv City deterministic mock fixture."""
    return copy.deepcopy(MOCK_KYIV_RAW)


@pytest.fixture
def mock_kyiv_region() -> Dict[str, Any]:
    """Deep copy of Kyiv Oblast deterministic mock fixture."""
    return copy.deepcopy(MOCK_KYIV_REGION_RAW)


@pytest.fixture
def mock_dnipro() -> Dict[str, Any]:
    """Deep copy of Dnipro deterministic mock fixture."""
    return copy.deepcopy(MOCK_DNIPRO_RAW)


@pytest.fixture
def mock_odesa() -> Dict[str, Any]:
    """Deep copy of Odesa deterministic mock fixture."""
    return copy.deepcopy(MOCK_ODESA_RAW)


@pytest.fixture
def all_mock_fixtures() -> Dict[str, Dict[str, Any]]:
    """Deep copy of all 4 regional mock fixtures keyed by region_code."""
    return copy.deepcopy(ALL_MOCKS_RAW)


@pytest.fixture
def populate_fixtures_dir(tmp_path: Any) -> str:
    """
    Populate a temporary directory with mock_*.json files
    matching the schema in bot/fixtures/.
    """
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    mapping = {
        "mock_kyiv.json": MOCK_KYIV_RAW,
        "mock_kyiv_region.json": MOCK_KYIV_REGION_RAW,
        "mock_dnipro.json": MOCK_DNIPRO_RAW,
        "mock_odesa.json": MOCK_ODESA_RAW,
    }
    for filename, data in mapping.items():
        filepath = fixtures_dir / filename
        filepath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(fixtures_dir)


# ============================================================================
# 4. PROGRESSIVE SAFE IMPORT UTILITY
# ============================================================================

def safe_import(module_path: str, attribute_name: Optional[str] = None) -> Any:
    """
    Safely import a module or attribute.
    If the module or attribute does not exist (e.g. still under implementation
    by a concurrent milestone worker), calls pytest.skip() with an informative message.
    """
    try:
        mod = importlib.import_module(module_path)
    except (ImportError, ModuleNotFoundError) as err:
        pytest.skip(f"Module '{module_path}' not available ({err}); skipping test.")

    if attribute_name is not None:
        try:
            return getattr(mod, attribute_name)
        except AttributeError as err:
            pytest.skip(
                f"Attribute '{attribute_name}' not found in '{module_path}' ({err}); skipping test."
            )

    return mod


# ============================================================================
# 5. SQLITE WAL DATABASE FIXTURES (ASYNC & SYNC)
# ============================================================================

SQLITE_SCHEMA_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    username TEXT,
    first_name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT 1
);

CREATE TABLE IF NOT EXISTS user_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    slot_number INTEGER NOT NULL CHECK (slot_number IN (1, 2)),
    alias TEXT NOT NULL DEFAULT 'Моя черга',
    region_code TEXT NOT NULL CHECK (region_code IN ('kyiv', 'kyiv-region', 'dnipro', 'odesa')),
    group_id INTEGER NOT NULL CHECK (group_id BETWEEN 1 AND 6),
    subgroup_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, slot_number)
);

CREATE INDEX IF NOT EXISTS idx_user_slots_lookup 
ON user_slots(region_code, subgroup_id);

CREATE TABLE IF NOT EXISTS notification_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    notify_30m BOOLEAN DEFAULT 1,
    notify_15m BOOLEAN DEFAULT 1,
    notify_schedule_change BOOLEAN DEFAULT 1,
    silent_hours_start INTEGER DEFAULT 23,
    silent_hours_end INTEGER DEFAULT 7,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS schedule_cache (
    region_code TEXT PRIMARY KEY,
    schedule_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    is_emergency BOOLEAN DEFAULT 0,
    emergency_message TEXT,
    data_source TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notification_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    slot_number INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    target_slot_index INTEGER,
    target_time TIMESTAMP NOT NULL,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hash_sent TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notif_dedup 
ON notification_history(user_id, slot_number, event_type, target_time);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_usage_events_time ON usage_events(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_events_action ON usage_events(action, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_events_user ON usage_events(user_id, created_at);
"""


@pytest.fixture
def sqlite_ddl() -> str:
    """Return raw DDL schema script."""
    return SQLITE_SCHEMA_DDL


@pytest.fixture
def temp_db_path() -> Generator[str, None, None]:
    """Provide a path to a temporary SQLite database file and remove on teardown."""
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        yield path
    finally:
        for p in [path, f"{path}-wal", f"{path}-shm"]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


@pytest.fixture
def sync_db(temp_db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """
    Synchronous SQLite connection fixture with WAL mode and foreign keys enabled,
    pre-initialized with the full schema.
    """
    conn = sqlite3.connect(temp_db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SQLITE_SCHEMA_DDL)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
async def async_db_conn(temp_db_path: str) -> Any:
    """
    Asynchronous aiosqlite connection fixture with WAL mode and foreign keys enabled,
    pre-initialized with the full schema.
    """
    try:
        import aiosqlite
    except ImportError:
        pytest.skip("aiosqlite not installed in current environment; skipping async db fixture.")

    db = await aiosqlite.connect(temp_db_path)
    await db.execute("PRAGMA journal_mode = WAL;")
    await db.execute("PRAGMA foreign_keys = ON;")
    await db.executescript(SQLITE_SCHEMA_DDL)
    await db.commit()
    try:
        yield db
    finally:
        await db.close()
