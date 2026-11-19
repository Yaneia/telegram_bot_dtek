"""
bot/main.py - Telegram Outbound Bot Core Runner & Async Lifecycle Manager.

Initializes Aiogram 3.15+ Dispatcher with MemoryStorage, registers command and
callback routers, initializes SQLite WAL schema, writes healthcheck heartbeats,
and handles graceful shutdown signals (SIGINT, SIGTERM).
Operates strictly via outbound HTTP long-polling with zero open inbound ports.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from bot.config import Settings, get_settings
from bot.database.connection import DatabaseConnectionManager
from bot.database.repository import DatabaseRepository
from bot.database.schema import init_db
from bot.handlers import setup_routers
from bot.services.github_trigger import GitHubWorkflowTrigger
from bot.services.notifier import NotifierService
from bot.services.scraper import ScraperCascade

logger = logging.getLogger("bot.main")

# Aiogram 3.15+ imports with graceful fallback stubs
try:
    from aiogram import Bot, Dispatcher  # type: ignore[import-untyped]
    from aiogram.fsm.storage.memory import MemoryStorage  # type: ignore[import-untyped]
    _AIOGRAM_AVAILABLE = True
except ImportError:
    _AIOGRAM_AVAILABLE = False

    class MemoryStorage:  # type: ignore[no-redef]
        def __init__(self) -> None:
            self._storage: dict[str, Any] = {}

    class Bot:  # type: ignore[no-redef]
        def __init__(self, token: str, **kwargs: Any) -> None:
            self.token = token
            self.session = _DummySession()

    class _DummySession:
        async def close(self) -> None:
            pass

    class Dispatcher:  # type: ignore[no-redef]
        def __init__(self, storage: Optional[Any] = None) -> None:
            self.storage = storage or MemoryStorage()
            self.workflow_data: dict[str, Any] = {}
            self._routers: list[Any] = []
            self._is_polling = False

        def __setitem__(self, key: str, value: Any) -> None:
            self.workflow_data[key] = value

        def __getitem__(self, key: str) -> Any:
            return self.workflow_data[key]

        def include_router(self, router: Any) -> None:
            self._routers.append(router)

        async def start_polling(self, bot: Bot, **kwargs: Any) -> None:
            self._is_polling = True
            logger.info("Dispatcher polling started (outbound-only).")
            while self._is_polling:
                await asyncio.sleep(1.0)

        def stop_polling(self) -> None:
            self._is_polling = False
            logger.info("Dispatcher polling stopped.")


def touch_heartbeat(path: str) -> None:
    """Touch healthcheck heartbeat file with current epoch timestamp."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(int(time.time())), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to update heartbeat file at %s: %s", path, exc)


async def heartbeat_loop(path: str, interval_seconds: int = 30) -> None:
    """Continuous background task writing periodic healthcheck heartbeats."""
    while True:
        touch_heartbeat(path)
        await asyncio.sleep(interval_seconds)


class BotApplication:
    """Encapsulates bot lifecycle, storage connections, and polling loop."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.db_manager: Optional[DatabaseConnectionManager] = None
        self.db_repo: Optional[DatabaseRepository] = None
        self.scraper: Optional[ScraperCascade] = None
        self.notifier: Optional[NotifierService] = None
        self.github_trigger: Optional[GitHubWorkflowTrigger] = None
        self.bot: Optional[Bot] = None
        self.dp: Optional[Dispatcher] = None
        self.heartbeat_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()

    async def initialize(self) -> None:
        """Initialize database, repositories, scrapers, and Aiogram dispatcher."""
        # Ensure database directory exists
        db_path = Path(self.settings.DATABASE_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize SQLite WAL connection manager and schema
        self.db_manager = DatabaseConnectionManager(self.settings.DATABASE_PATH)
        async with self.db_manager.get_connection() as conn:
            await init_db(conn)

        self.db_repo = DatabaseRepository(self.db_manager)
        self.scraper = ScraperCascade(
            db_conn=self.db_manager,
            enable_direct_scrape=self.settings.ENABLE_DIRECT_SCRAPE,
        )

        # Initialize Bot and Dispatcher
        token = self.settings.BOT_TOKEN or "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ_mock"
        self.bot = Bot(token=token)
        self.dp = Dispatcher(storage=MemoryStorage())

        # Dependency injection into dispatcher workflow_data
        self.dp["db"] = self.db_repo
        self.dp["scraper"] = self.scraper
        self.dp["settings"] = self.settings

        # Register assembled root handlers router
        root_router = setup_routers()
        self.dp.include_router(root_router)

        self.notifier = NotifierService(
            bot=self.bot,
            db=self.db_repo,
            scraper=self.scraper,
            check_interval=self.settings.NOTIFICATION_CHECK_INTERVAL_SECONDS,
            scrape_interval=self.settings.SCRAPE_INTERVAL_SECONDS,
        )
        self.github_trigger = GitHubWorkflowTrigger(
            token=self.settings.GITHUB_TOKEN,
            repo=self.settings.GITHUB_REPO,
            workflow=self.settings.GITHUB_SCRAPE_WORKFLOW,
            ref=self.settings.GITHUB_SCRAPE_REF,
            interval_seconds=self.settings.GITHUB_SCRAPE_INTERVAL_SECONDS,
        )

        # Write initial heartbeat
        touch_heartbeat(self.settings.HEARTBEAT_PATH)

    async def start(self) -> None:
        """Start outbound-only polling loop and heartbeat worker."""
        await self.initialize()

        # Start periodic heartbeat task
        self.heartbeat_task = asyncio.create_task(
            heartbeat_loop(self.settings.HEARTBEAT_PATH, interval_seconds=30)
        )
        if self.notifier is not None:
            await self.notifier.start()
        if self.github_trigger is not None:
            await self.github_trigger.start()

        logger.info(
            "Starting Telegram bot '%s' with outbound long-polling (zero open ports)...",
            self.settings.BOT_USERNAME,
        )

        assert self.dp is not None
        assert self.bot is not None

        try:
            if _AIOGRAM_AVAILABLE:
                await self.dp.start_polling(
                    self.bot,
                    db=self.db_repo,
                    scraper=self.scraper,
                    settings=self.settings,
                    handle_signals=False,  # Custom signal handling
                )
            else:
                await self.dp.start_polling(self.bot)
        except asyncio.CancelledError:
            logger.info("Bot polling cancelled via shutdown signal.")
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Gracefully release all resources, sessions, and connections."""
        logger.info("Initiating graceful bot shutdown...")

        if self.notifier is not None:
            await self.notifier.stop()
        if self.github_trigger is not None:
            await self.github_trigger.stop()

        if self.heartbeat_task and not self.heartbeat_task.done():
            self.heartbeat_task.cancel()
            try:
                await self.heartbeat_task
            except asyncio.CancelledError:
                pass

        if self.dp and hasattr(self.dp, "stop_polling"):
            self.dp.stop_polling()

        if self.bot and hasattr(self.bot, "session") and hasattr(self.bot.session, "close"):
            await self.bot.session.close()

        if self.db_manager and hasattr(self.db_manager, "close"):
            await self.db_manager.close()

        logger.info("Bot shutdown completed cleanly.")


def setup_signal_handlers(app: BotApplication, loop: asyncio.AbstractEventLoop) -> None:
    """Attach SIGINT and SIGTERM handlers to trigger clean shutdown."""
    def _signal_handler() -> None:
        logger.info("Received termination signal, stopping bot...")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, RuntimeError):
            # Windows or restricted environments
            pass


async def main() -> None:
    """Main asynchronous entry point."""
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = BotApplication(settings)
    loop = asyncio.get_running_loop()
    setup_signal_handlers(app, loop)

    try:
        await app.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Bot exited via signal.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
