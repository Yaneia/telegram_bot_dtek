"""
bot/handlers/middleware.py - Non-blocking usage telemetry outer middleware.

Logs commands, inline callback prefixes, and Mini App syncs into SQLite `usage_events`
without ever interrupting update processing on logging failures.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from bot.handlers.commands import _get_db, resolve_chat_scope

logger = logging.getLogger(__name__)

try:
    from aiogram import BaseMiddleware  # type: ignore[import-untyped]
except ImportError:
    class BaseMiddleware:  # type: ignore[no-redef]
        pass


def classify_usage_event(event: Any) -> Optional[Tuple[int, str, str]]:
    """
    Extract (user_id, event_type, action) from a Message or CallbackQuery.
    Returns None if the event is not a tracked command, callback, or Mini App action.
    """
    user_id, _, _, _, _, _ = resolve_chat_scope(event)
    if user_id == 0:
        return None

    # Message events
    if getattr(event, "web_app_data", None) is not None:
        return user_id, "webapp", "webapp_data"

    text = getattr(event, "text", None)
    if isinstance(text, str):
        stripped = text.strip()
        if stripped.startswith("/"):
            parts = stripped.split(maxsplit=1)
            cmd = parts[0].split("@", 1)[0].lower()
            if cmd == "/start" and len(parts) > 1 and parts[1].startswith("sync_"):
                return user_id, "webapp", "tma_sync"
            return user_id, "command", cmd
        return None

    # CallbackQuery events
    cb_data = getattr(event, "data", None)
    if isinstance(cb_data, str) and cb_data:
        prefix = cb_data.split(":", 1)[0]
        return user_id, "callback", f"cb:{prefix}"

    return None


class UsageTrackingMiddleware(BaseMiddleware):
    """Outer middleware logging user interactions to SQLite `usage_events`."""

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        classified = classify_usage_event(event)
        if classified is not None:
            user_id, event_type, action = classified
            try:
                db = data.get("db")
                if db is None:
                    db = await _get_db()
                if hasattr(db, "log_usage_event"):
                    await db.log_usage_event(user_id=user_id, event_type=event_type, action=action)
            except Exception as exc:
                logger.debug("Usage event logging failed (non-fatal): %s", exc)

        return await handler(event, data)


__all__ = ["UsageTrackingMiddleware", "classify_usage_event"]
