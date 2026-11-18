"""
bot/handlers/commands.py - Bot Command Handlers (/start, /status, /slots, /settings, /help).

Handles user onboarding, deep-link sync codes (sync_<b64>), timeline rendering
via Image/Text renderers, and interactive management navigation.
Supports Aiogram 3.15+ with graceful standard library fallback stubs.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from bot.config import get_settings
from bot.database.connection import DatabaseConnectionManager
from bot.database.repository import DatabaseRepository
from bot.handlers.keyboards import (
    build_help_keyboard,
    build_main_menu_keyboard,
    build_onboarding_keyboard,
    build_settings_keyboard,
    build_slots_keyboard,
    build_status_keyboard,
)
from bot.rendering.image import ImageRenderer
from bot.rendering.text import TextRenderer
from bot.services.comparison import ComparisonEngine
from bot.services.scraper import ScraperCascade
from bot.services.tma_payload import TMAPayload

logger = logging.getLogger(__name__)
KYIV_TZ = ZoneInfo("Europe/Kyiv")

# User display preferences cache (user_id -> "image" | "text")
USER_RENDER_MODES: Dict[int, str] = {}

# Aiogram 3.15+ imports with fallback stubs
try:
    from aiogram import Bot, F, Router  # type: ignore[import-untyped]
    from aiogram.filters import Command, CommandObject, CommandStart  # type: ignore[import-untyped]
    from aiogram.types import BufferedInputFile, Message  # type: ignore[import-untyped]
    _AIOGRAM_AVAILABLE = True
except ImportError:
    _AIOGRAM_AVAILABLE = False

    class Router:  # type: ignore[no-redef]
        def __init__(self, name: Optional[str] = None) -> None:
            self.name = name
            self.message_handlers: List[Tuple[Any, Callable]] = []
            self.callback_handlers: List[Tuple[Any, Callable]] = []
            self.sub_routers: List[Router] = []

        def message(self, *filters: Any) -> Callable:
            def decorator(fn: Callable) -> Callable:
                self.message_handlers.append((filters, fn))
                return fn
            return decorator

        def callback_query(self, *filters: Any) -> Callable:
            def decorator(fn: Callable) -> Callable:
                self.callback_handlers.append((filters, fn))
                return fn
            return decorator

        def include_router(self, router: Router) -> None:
            self.sub_routers.append(router)

    class Command:  # type: ignore[no-redef]
        def __init__(self, *commands: str) -> None:
            self.commands = commands

    class CommandStart(Command):  # type: ignore[no-redef]
        def __init__(self) -> None:
            super().__init__("start")

    class CommandObject:  # type: ignore[no-redef]
        def __init__(self, prefix: str = "/", command: str = "start", args: Optional[str] = None) -> None:
            self.prefix = prefix
            self.command = command
            self.args = args

    class Message:  # type: ignore[no-redef]
        pass

    class Bot:  # type: ignore[no-redef]
        pass

    class BufferedInputFile:  # type: ignore[no-redef]
        def __init__(self, file: bytes, filename: str) -> None:
            self.data = file
            self.filename = filename


router = Router(name="commands")

# Default singleton instances for standalone/direct invocation
_db_repo: Optional[DatabaseRepository] = None
_scraper: Optional[ScraperCascade] = None


async def _get_db() -> DatabaseRepository:
    global _db_repo
    if _db_repo is None:
        settings = get_settings()
        mgr = DatabaseConnectionManager(settings.DATABASE_PATH)
        _db_repo = DatabaseRepository(mgr)
        await _db_repo.init_schema()
    return _db_repo


def _get_scraper() -> ScraperCascade:
    global _scraper
    if _scraper is None:
        _scraper = ScraperCascade()
    return _scraper


# ============================================================================
# /start COMMAND HANDLER
# ============================================================================

@router.message(CommandStart())
async def cmd_start(
    message: Any,
    command: Optional[Any] = None,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """
    Handle /start command.
    Registers user in SQLite, initializes settings, and processes deep-link
    sync payload (sync_<b64>) if provided.
    """
    if db is None:
        db = await _get_db()

    user = getattr(message, "from_user", None)
    user_id = getattr(user, "id", 0) if user else 0
    chat_id = getattr(getattr(message, "chat", None), "id", user_id)
    first_name = getattr(user, "first_name", "Користувач") or "Користувач"
    username = getattr(user, "username", None)

    # Register or touch user in SQLite
    if user_id > 0:
        await db.users.upsert_user(
            user_id=user_id,
            chat_id=chat_id,
            first_name=first_name,
            username=username,
        )
        # Ensure default notification settings record
        await db.settings.get_settings(user_id)

    # Inspect deep-link argument
    args = getattr(command, "args", None) if command else None
    if not args and hasattr(message, "text") and isinstance(message.text, str):
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            args = parts[1]

    if args and args.startswith("sync_"):
        try:
            slot_a, slot_b = TMAPayload.decode_sync_code(args)
            reg_a = slot_a.region_code.value if hasattr(slot_a.region_code, "value") else str(slot_a.region_code)
            await db.slots.upsert_slot(
                user_id=user_id,
                slot_number=1,
                region_code=reg_a,
                group_id=slot_a.group_id,
                subgroup_id=slot_a.subgroup_id,
                alias=slot_a.alias,
            )

            msg_lines = [
                "✅ <b>Налаштування черг успішно синхронізовано з Mini App!</b>",
                "",
                f"📍 <b>Слот 1:</b> {slot_a.alias} (Черга {slot_a.subgroup_id}, {reg_a})",
            ]

            if slot_b is not None:
                reg_b = slot_b.region_code.value if hasattr(slot_b.region_code, "value") else str(slot_b.region_code)
                await db.slots.upsert_slot(
                    user_id=user_id,
                    slot_number=2,
                    region_code=reg_b,
                    group_id=slot_b.group_id,
                    subgroup_id=slot_b.subgroup_id,
                    alias=slot_b.alias,
                )
                msg_lines.append(f"📍 <b>Слот 2:</b> {slot_b.alias} (Черга {slot_b.subgroup_id}, {reg_b})")

            msg_lines.extend([
                "",
                "Використовуйте /status для перегляду оновленого графіка.",
            ])

            if hasattr(message, "answer"):
                await message.answer(
                    "\n".join(msg_lines),
                    parse_mode="HTML",
                    reply_markup=build_main_menu_keyboard(get_settings().WEBAPP_URL),
                )
            return
        except Exception as exc:
            logger.warning("Failed to decode sync code from /start: %s", exc)

    settings = get_settings()
    welcome_text = (
        f"Вітаємо, {first_name}! ⚡\n\n"
        "Бот відстежує офіційні графіки стабілізаційних та аварійних відключень "
        "електроенергії ДТЕК для 4 регіонів України:\n"
        "• м. Київ\n"
        "• Київська область\n"
        "• Дніпропетровська область\n"
        "• Одеська область\n\n"
        "<b>Доступні команди:</b>\n"
        "• /status — поточний графік та доступність електроенергії\n"
        "• /slots — керування чергами моніторингу (Дім, Офіс)\n"
        "• /settings — налаштування сповіщень та тихих годин\n"
        "• /help — довідка та позначення кольорів"
    )

    if hasattr(message, "answer"):
        await message.answer(
            welcome_text,
            parse_mode="HTML",
            reply_markup=build_main_menu_keyboard(settings.WEBAPP_URL),
        )


# ============================================================================
# /status and /schedule COMMAND HANDLERS
# ============================================================================

@router.message(Command("status", "schedule"))
async def cmd_status(
    message: Any,
    db: Optional[DatabaseRepository] = None,
    scraper: Optional[ScraperCascade] = None,
    bot: Optional[Any] = None,
    user_id: Optional[int] = None,
    **kwargs: Any,
) -> None:
    """
    Handle /status and /schedule commands.
    Retrieves user slots, fetches schedules via 5-tier scraper cascade,
    evaluates joint timeline, renders PNG or text table, and attaches TMA bridge.
    """
    if db is None:
        db = await _get_db()
    if scraper is None:
        scraper = _get_scraper()

    if user_id is None:
        user = getattr(message, "from_user", None)
        user_id = getattr(user, "id", 0) if user else 0

    slots = await db.slots.get_slots_for_user(user_id)
    if not slots:
        onboarding_text = (
            "⚠️ <b>У вас ще не налаштовано жодної черги.</b>\n\n"
            "Щоб отримувати графік відключень, додайте хоча б одну адресу чи чергу (Слот 1)."
        )
        if hasattr(message, "answer"):
            await message.answer(
                onboarding_text,
                parse_mode="HTML",
                reply_markup=build_onboarding_keyboard(),
            )
        return

    slot1 = slots[0]
    slot2 = slots[1] if len(slots) > 1 else None

    # Fetch daily schedules via 5-tier cascade
    sched1 = await scraper.get_schedule(region=slot1.region_code, group=slot1.subgroup_id)
    sched2 = await scraper.get_schedule(region=slot2.region_code, group=slot2.subgroup_id) if slot2 else None

    # Evaluate comparison
    now = datetime.now(KYIV_TZ)
    comparison = ComparisonEngine.compare(
        slot1=sched1,
        slot2=sched2,
        alias_a=slot1.alias,
        alias_b=slot2.alias if slot2 else None,
        now=now,
    )

    # Encode TMA URL fragment
    settings = get_settings()
    bot_username = settings.BOT_USERNAME
    if bot and hasattr(bot, "get_me"):
        try:
            me = await bot.get_me()
            if hasattr(me, "username") and me.username:
                bot_username = me.username
        except Exception:
            pass

    tma_fragment = TMAPayload.encode(comparison, bot_username=bot_username)
    tma_url = f"{settings.WEBAPP_URL}{tma_fragment}"

    # Determine render mode (image vs text)
    current_mode = USER_RENDER_MODES.get(user_id, settings.DEFAULT_RENDER_MODE)
    keyboard = build_status_keyboard(tma_url=tma_url, current_mode=current_mode)

    if current_mode == "image":
        try:
            png_bytes = ImageRenderer.render_timeline_png(comparison)
            caption = (
                f"⚡ <b>Графік відключень на {comparison.comparison_date}</b>\n"
                f"📍 {slot1.alias} ({slot1.subgroup_id})"
                + (f" | {slot2.alias} ({slot2.subgroup_id})" if slot2 else "")
                + f"\n💡 <i>{comparison.summary.smart_recommendation}</i>"
            )
            if hasattr(message, "answer_photo"):
                input_file = BufferedInputFile(png_bytes, filename="timeline.png")
                await message.answer_photo(
                    photo=input_file,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                return
        except Exception as exc:
            logger.warning("Image rendering failed, falling back to text: %s", exc)

    # Text mode rendering (fallback or user choice)
    text_table = TextRenderer.render_timeline_text(comparison)
    msg_body = f"<code>{text_table}</code>"
    if hasattr(message, "answer"):
        await message.answer(
            msg_body,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


# ============================================================================
# /slots COMMAND HANDLER
# ============================================================================

@router.message(Command("slots"))
async def cmd_slots(
    message: Any,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """Handle /slots command: slot configuration and management menu."""
    if db is None:
        db = await _get_db()

    user = getattr(message, "from_user", None)
    user_id = getattr(user, "id", 0) if user else 0

    slots = await db.slots.get_slots_for_user(user_id)

    lines = ["📍 <b>Керування чергами моніторингу</b>\n"]
    if slots:
        for s in slots:
            slot_num = getattr(s, "slot_number", 1)
            alias = getattr(s, "alias", "Черга")
            reg = getattr(s, "region_code", "")
            subgrp = getattr(s, "subgroup_id", "")
            lines.append(f"• <b>Слот {slot_num}:</b> {alias} — Регіон: <code>{reg}</code>, Черга: <code>{subgrp}</code>")
    else:
        lines.append("У вас ще не додано жодної черги.")

    lines.append("\nОберіть слот нижче для зміни регіону та черги:")

    if hasattr(message, "answer"):
        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=build_slots_keyboard(slots),
        )


# ============================================================================
# /settings COMMAND HANDLER
# ============================================================================

@router.message(Command("settings"))
async def cmd_settings(
    message: Any,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """Handle /settings command: configure advance warnings and silent hours."""
    if db is None:
        db = await _get_db()

    user = getattr(message, "from_user", None)
    user_id = getattr(user, "id", 0) if user else 0

    settings_record = await db.settings.get_settings(user_id)

    text = (
        "⚙️ <b>Налаштування сповіщень</b>\n\n"
        "Бот може надсилати автоматичні попередження перед запланованим "
        "відключенням світла та сповіщати про оновлення графіка ДТЕК.\n\n"
        "Натискайте кнопки нижче, щоб увімкнути або вимкнути опції:"
    )

    if hasattr(message, "answer"):
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=build_settings_keyboard(settings_record),
        )


# ============================================================================
# /help COMMAND HANDLER
# ============================================================================

@router.message(Command("help"))
async def cmd_help(message: Any, **kwargs: Any) -> None:
    """Handle /help command: Ukrainian guide and status color legend."""
    settings = get_settings()
    help_text = (
        "⚡ <b>Довідка та позначення кольорів</b>\n\n"
        "<b>Позначення статусів графіків:</b>\n"
        "🟩 <b>Світло є (ON)</b> — за графіком гарантовано є електроенергія.\n"
        "⬛ <b>Світла немає (OFF)</b> — період планового відключення.\n"
        "🟨 <b>Можливе відключення (MAYBE)</b> — сіра зона, відключення можливе за дефіциту потужності.\n"
        "🟥 <b>Аварійне відключення (EMERGENCY)</b> — екстрені відключення поза стандартними графіками.\n\n"
        "<b>Спільні стани двох черг (Дім + Офіс):</b>\n"
        "• 🟢 <i>Світло всюди</i> — обидві локації зі світлом.\n"
        "• 🔵 <i>Світло лише у Слоті 1</i> — зручно їхати додому.\n"
        "• 🟣 <i>Світло лише у Слоті 2</i> — зручно працювати в офісі.\n"
        "• ⚫ <i>Світла немає ніде</i> — зарядіть завчасно пристрої.\n\n"
        "<b>Команди бота:</b>\n"
        "/status — переглянути поточний графік та рекомендацію\n"
        "/slots — налаштувати регіон і чергу\n"
        "/settings — змінити налаштування сповіщень\n"
        "/help — ця довідка"
    )

    if hasattr(message, "answer"):
        await message.answer(
            help_text,
            parse_mode="HTML",
            reply_markup=build_help_keyboard(settings.WEBAPP_URL),
        )


__all__ = [
    "router",
    "cmd_start",
    "cmd_status",
    "cmd_slots",
    "cmd_settings",
    "cmd_help",
    "USER_RENDER_MODES",
]
