"""
Standalone CLI container healthcheck for DTEK Telegram Bot.

Verifies:
1. SQLite database connectivity via `SELECT 1;`.
2. Heartbeat timestamp freshness (file mtime <= 180.0 seconds).

Exits:
- 0: Both checks succeeded (container healthy).
- 1: One or more checks failed or unhandled exception encountered.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Optional, Tuple


def resolve_paths(
    cli_db_path: Optional[str] = None,
    cli_heartbeat_path: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Resolve SQLite database path and heartbeat file path.
    Precedence: CLI flag -> Environment variable -> bot.config -> Static fallback.
    """
    # 1. Resolve database path
    db_path = cli_db_path or os.environ.get("DATABASE_PATH")
    if not db_path:
        try:
            from bot.config import get_settings

            settings = get_settings()
            db_path = getattr(settings, "DATABASE_PATH", None)
        except Exception:
            pass

    if not db_path:
        db_path = "/app/data/dtek_bot.sqlite3"

    # 2. Resolve heartbeat path
    hb_path = cli_heartbeat_path or os.environ.get("HEARTBEAT_PATH")
    if not hb_path:
        try:
            from bot.config import get_settings

            settings = get_settings()
            hb_path = getattr(settings, "HEARTBEAT_PATH", None)
        except Exception:
            pass

    if not hb_path:
        hb_path = "/tmp/bot_heartbeat"

    return str(db_path), str(hb_path)


def check_database(db_path: str, timeout: float = 5.0) -> bool:
    """
    Verify SQLite database connectivity by executing SELECT 1;.
    Returns True if healthy, False otherwise.
    """
    if not os.path.exists(db_path):
        sys.stderr.write(f"[HEALTHCHECK FAIL] SQLite database file not found: {db_path}\n")
        return False

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(db_path, timeout=timeout)
        cursor = conn.cursor()
        cursor.execute("SELECT 1;")
        result = cursor.fetchone()
        if not result or result[0] != 1:
            sys.stderr.write(
                f"[HEALTHCHECK FAIL] SQLite query 'SELECT 1;' returned unexpected value: {result}\n"
            )
            return False
        return True
    except Exception as exc:
        sys.stderr.write(
            f"[HEALTHCHECK FAIL] SQLite connectivity check failed on '{db_path}': {exc}\n"
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def check_heartbeat(heartbeat_path: str, max_age_seconds: float = 180.0) -> bool:
    """
    Verify heartbeat file freshness (mtime age <= max_age_seconds).
    Returns True if fresh, False otherwise.
    """
    if not os.path.exists(heartbeat_path):
        sys.stderr.write(f"[HEALTHCHECK FAIL] Heartbeat file not found: {heartbeat_path}\n")
        return False

    try:
        mtime = os.path.getmtime(heartbeat_path)
        age = time.time() - mtime
        if age > max_age_seconds:
            sys.stderr.write(
                f"[HEALTHCHECK FAIL] Heartbeat is stale ({age:.1f}s > {max_age_seconds:.1f}s) "
                f"at {heartbeat_path}\n"
            )
            return False
        return True
    except Exception as exc:
        sys.stderr.write(
            f"[HEALTHCHECK FAIL] Failed inspecting heartbeat mtime at '{heartbeat_path}': {exc}\n"
        )
        return False


def run_healthcheck(
    db_path: Optional[str] = None,
    heartbeat_path: Optional[str] = None,
    max_age_seconds: float = 180.0,
    timeout: float = 5.0,
) -> int:
    """
    Run complete container healthcheck.
    Returns 0 on success, 1 on any failure.
    """
    try:
        resolved_db, resolved_hb = resolve_paths(db_path, heartbeat_path)

        # 1. SQLite database check
        if not check_database(resolved_db, timeout=timeout):
            return 1

        # 2. Heartbeat freshness check
        if not check_heartbeat(resolved_hb, max_age_seconds=max_age_seconds):
            return 1

        sys.stdout.write(
            f"[HEALTHCHECK OK] Database ({resolved_db}) and Heartbeat ({resolved_hb}) healthy.\n"
        )
        return 0
    except Exception as exc:
        sys.stderr.write(f"[HEALTHCHECK UNHANDLED EXCEPTION] {exc}\n")
        return 1


def main() -> int:
    """CLI entrypoint for python -m bot.healthcheck."""
    parser = argparse.ArgumentParser(description="DTEK Bot container healthcheck CLI")
    parser.add_argument("--db", "--database", dest="db_path", default=None, help="SQLite database path")
    parser.add_argument("--heartbeat", dest="heartbeat_path", default=None, help="Heartbeat file path")
    parser.add_argument(
        "--max-age", dest="max_age", type=float, default=180.0, help="Max heartbeat age in seconds"
    )
    parser.add_argument(
        "--timeout", dest="timeout", type=float, default=5.0, help="SQLite query timeout in seconds"
    )
    args, _ = parser.parse_known_args()

    return run_healthcheck(
        db_path=args.db_path,
        heartbeat_path=args.heartbeat_path,
        max_age_seconds=args.max_age,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
