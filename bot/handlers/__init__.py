"""
bot/handlers - Telegram Bot Request Routers & Presentation Layer.

Provides command routers (/start, /status, /slots, /settings, /help),
interactive inline callback routers, and keyboard layout builders.
"""

from __future__ import annotations

from typing import Any

from bot.handlers import callbacks, commands, keyboards, middleware
from bot.handlers.callbacks import router as callbacks_router
from bot.handlers.commands import router as commands_router
from bot.handlers.middleware import UsageTrackingMiddleware

try:
    from aiogram import Router  # type: ignore[import-untyped]
except ImportError:
    from bot.handlers.commands import Router  # type: ignore[no-redef]


def setup_routers() -> Router:
    """
    Assemble and return the root router containing command and callback sub-routers.
    """
    root_router = Router(name="root_handlers")
    tracking_mw = UsageTrackingMiddleware()
    if hasattr(root_router, "message") and hasattr(root_router.message, "outer_middleware"):
        root_router.message.outer_middleware(tracking_mw)
    if hasattr(root_router, "callback_query") and hasattr(root_router.callback_query, "outer_middleware"):
        root_router.callback_query.outer_middleware(tracking_mw)
    root_router.include_router(commands_router)
    root_router.include_router(callbacks_router)
    return root_router


__all__ = [
    "setup_routers",
    "commands_router",
    "callbacks_router",
    "commands",
    "callbacks",
    "keyboards",
    "middleware",
    "UsageTrackingMiddleware",
]
