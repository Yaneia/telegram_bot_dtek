"""
bot/handlers - Telegram Bot Request Routers & Presentation Layer.

Provides command routers (/start, /status, /slots, /settings, /help),
interactive inline callback routers, and keyboard layout builders.
"""

from __future__ import annotations

from typing import Any

from bot.handlers import callbacks, commands, keyboards
from bot.handlers.callbacks import router as callbacks_router
from bot.handlers.commands import router as commands_router

try:
    from aiogram import Router  # type: ignore[import-untyped]
except ImportError:
    from bot.handlers.commands import Router  # type: ignore[no-redef]


def setup_routers() -> Router:
    """
    Assemble and return the root router containing command and callback sub-routers.
    """
    root_router = Router(name="root_handlers")
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
]
