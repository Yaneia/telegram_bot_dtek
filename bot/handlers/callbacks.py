"""
bot/handlers/callbacks.py - Inline Callback Handlers and TMA web_app_data Event Router.

Processes button interactions for schedule refresh, render mode toggling,
interactive slot configuration wizard (Region -> Group -> Subgroup),
notification settings toggles, and Telegram Mini App web_app_data sync payloads.
Supports Aiogram 3.15+ with graceful standard library fallback stubs.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from bot.config import get_settings
from bot.database.connection import DatabaseConnectionManager
from bot.database.repository import DatabaseRepository
from bot.handlers.commands import USER_RENDER_MODES, cmd_status, resolve_chat_scope
from bot.handlers.keyboards import (
    build_group_keyboard,
    build_help_keyboard,
    build_main_menu_keyboard,
    build_region_keyboard,
    build_settings_keyboard,
    build_slots_keyboard,
    build_subgroup_keyboard,
)

logger = logging.getLogger(__name__)

# Aiogram 3.15+ imports with fallback stubs
try:
    from aiogram import F, Router  # type: ignore[import-untyped]
    from aiogram.fsm.context import FSMContext  # type: ignore[import-untyped]
    from aiogram.fsm.state import State, StatesGroup  # type: ignore[import-untyped]
    from aiogram.types import CallbackQuery, Message  # type: ignore[import-untyped]
    _AIOGRAM_AVAILABLE = True
except ImportError:
    _AIOGRAM_AVAILABLE = False
    FSMContext = Any  # type: ignore[assignment,misc]

    class StatesGroup:  # type: ignore[no-redef]
        pass

    class State:  # type: ignore[no-redef]
        pass

    class _MagicFilter:
        def __init__(self, path: str = "") -> None:
            self._path = path

        def __getattr__(self, item: str) -> _MagicFilter:
            return _MagicFilter(f"{self._path}.{item}" if self._path else item)

        def __eq__(self, other: Any) -> _MagicFilter:  # type: ignore[override]
            return _MagicFilter(f"{self._path}=={other}")

        def startswith(self, prefix: str) -> _MagicFilter:
            return _MagicFilter(f"{self._path}.startswith({prefix})")

        def __call__(self, obj: Any) -> bool:
            return True

    F = _MagicFilter()  # type: ignore[assignment,misc]

    class Router:  # type: ignore[no-redef]
        def __init__(self, name: Optional[str] = None) -> None:
            self.name = name
            self.message_handlers: List[Tuple[Any, Callable]] = []
            self.callback_handlers: List[Tuple[Any, Callable]] = []

        def callback_query(self, *filters: Any) -> Callable:
            def decorator(fn: Callable) -> Callable:
                self.callback_handlers.append((filters, fn))
                return fn
            return decorator

        def message(self, *filters: Any) -> Callable:
            def decorator(fn: Callable) -> Callable:
                self.message_handlers.append((filters, fn))
                return fn
            return decorator

    class CallbackQuery:  # type: ignore[no-redef]
        pass

    class Message:  # type: ignore[no-redef]
        pass


router = Router(name="callbacks")

# Default singleton repository
_db_repo: Optional[DatabaseRepository] = None


async def _get_db() -> DatabaseRepository:
    global _db_repo
    if _db_repo is None:
        settings = get_settings()
        mgr = DatabaseConnectionManager(settings.DATABASE_PATH)
        _db_repo = DatabaseRepository(mgr)
        await _db_repo.init_schema()
    return _db_repo


# ============================================================================
# SCHEDULE REFRESH & RENDER MODE TOGGLE
# ============================================================================

async def _ensure_user(db: DatabaseRepository, event: Any) -> int:
    """Ensure a user/group/topic row exists before inserting/updating child tables with FK constraints."""
    owner_id, chat_id, thread_id, _, first_name, username = resolve_chat_scope(event)
    if owner_id != 0 and hasattr(db, "users") and hasattr(db.users, "upsert_user"):
        await db.users.upsert_user(
            user_id=owner_id,
            chat_id=chat_id,
            thread_id=thread_id,
            first_name=first_name,
            username=username,
        )
    return owner_id


async def _edit_or_answer(
    msg: Any,
    text: str,
    reply_markup: Any = None,
    parse_mode: str = "HTML",
) -> None:
    """Edit message text if it has text, otherwise send a new message (e.g. from photo cards)."""
    if not msg:
        return
    if hasattr(msg, "edit_text") and getattr(msg, "text", None) is not None:
        try:
            await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            return
        except Exception as exc:
            if "there is no text in the message to edit" not in str(exc).lower():
                raise
    if hasattr(msg, "answer"):
        await msg.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)


@router.callback_query(F.data == "refresh_schedule")
async def cb_refresh_schedule(
    query: Any,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """Handle 🔄 refresh button: re-scrape and re-render schedule."""
    if hasattr(query, "answer"):
        try:
            await query.answer("Оновлення графіка... ⚡")
        except Exception:
            pass

    user_id, _, _, _, _, _ = resolve_chat_scope(query)

    msg = getattr(query, "message", None)
    if msg:
        # Delegate to cmd_status using the resolved chat/topic scope
        await cmd_status(msg, db=db, user_id=user_id, **kwargs)


@router.callback_query(F.data.startswith("status_date:"))
async def cb_status_date(
    query: Any,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """Switch between today's and tomorrow's schedule in /status."""
    data = getattr(query, "data", "") or ""
    date_arg = data.split(":", 1)[1] if ":" in data else "today"
    target_date = None if date_arg == "today" else date_arg

    if hasattr(query, "answer"):
        try:
            await query.answer("Завантаження графіка... 📅")
        except Exception:
            pass

    user_id, _, _, _, _, _ = resolve_chat_scope(query)
    msg = getattr(query, "message", None)
    if msg:
        await cmd_status(msg, db=db, user_id=user_id, target_date=target_date, **kwargs)


@router.callback_query(F.data == "toggle_render_mode")
async def cb_toggle_render_mode(
    query: Any,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """Toggle between Image and Text rendering modes for user/group/topic."""
    user_id, _, _, _, _, _ = resolve_chat_scope(query)

    current = USER_RENDER_MODES.get(user_id, get_settings().DEFAULT_RENDER_MODE)
    new_mode = "text" if current == "image" else "image"
    USER_RENDER_MODES[user_id] = new_mode

    mode_label = "текстовий" if new_mode == "text" else "графічний"
    if hasattr(query, "answer"):
        try:
            await query.answer(f"Режим змінено на {mode_label} 🔄")
        except Exception:
            pass

    msg = getattr(query, "message", None)
    if msg:
        await cmd_status(msg, db=db, user_id=user_id, **kwargs)


# ============================================================================
# SLOT WIZARD CALLBACKS
# ============================================================================

@router.callback_query(F.data.startswith("slot_edit:"))
async def cb_slot_edit(query: Any, **kwargs: Any) -> None:
    """Prompt user to choose region for slot."""
    data = getattr(query, "data", "") or ""
    parts = data.split(":")
    slot_num = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1

    text = (
        f"📍 <b>Налаштування Слота {slot_num}</b>\n\n"
        "Оберіть ваш регіон / оператора системи розподілу (ДТЕК):"
    )
    msg = getattr(query, "message", None)
    if msg and hasattr(msg, "edit_text"):
        await msg.edit_text(text, parse_mode="HTML", reply_markup=build_region_keyboard(slot_num))
    elif msg and hasattr(msg, "answer"):
        await msg.answer(text, parse_mode="HTML", reply_markup=build_region_keyboard(slot_num))

    if hasattr(query, "answer"):
        try:
            await query.answer()
        except Exception:
            pass


@router.callback_query(F.data.startswith("slot_reg:"))
async def cb_slot_region(query: Any, **kwargs: Any) -> None:
    """Prompt user to choose group (1..6) after selecting region."""
    data = getattr(query, "data", "") or ""
    parts = data.split(":")
    slot_num = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    region_code = parts[2] if len(parts) > 2 else "kyiv"

    text = (
        f"📍 <b>Слот {slot_num}</b> | Регіон: <code>{region_code}</code>\n\n"
        "Оберіть номер вашої групи (черги 1..6):"
    )
    msg = getattr(query, "message", None)
    if msg and hasattr(msg, "edit_text"):
        await msg.edit_text(text, parse_mode="HTML", reply_markup=build_group_keyboard(slot_num, region_code))
    elif msg and hasattr(msg, "answer"):
        await msg.answer(text, parse_mode="HTML", reply_markup=build_group_keyboard(slot_num, region_code))

    if hasattr(query, "answer"):
        try:
            await query.answer()
        except Exception:
            pass


@router.callback_query(F.data.startswith("slot_grp:"))
async def cb_slot_group(query: Any, **kwargs: Any) -> None:
    """Prompt user to choose subgroup (X.1 or X.2) after selecting group."""
    data = getattr(query, "data", "") or ""
    parts = data.split(":")
    slot_num = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    region_code = parts[2] if len(parts) > 2 else "kyiv"
    group_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1

    text = (
        f"📍 <b>Слот {slot_num}</b> | Регіон: <code>{region_code}</code> | Група: {group_id}\n\n"
        "Оберіть підгрупу:"
    )
    msg = getattr(query, "message", None)
    if msg and hasattr(msg, "edit_text"):
        await msg.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=build_subgroup_keyboard(slot_num, region_code, group_id),
        )
    elif msg and hasattr(msg, "answer"):
        await msg.answer(
            text,
            parse_mode="HTML",
            reply_markup=build_subgroup_keyboard(slot_num, region_code, group_id),
        )

    if hasattr(query, "answer"):
        try:
            await query.answer()
        except Exception:
            pass


@router.callback_query(F.data.startswith("slot_sub:"))
async def cb_slot_subgroup(
    query: Any,
    db: Optional[DatabaseRepository] = None,
    state: Optional[FSMContext] = None,
    **kwargs: Any,
) -> None:
    """Save selected slot configuration and prompt for optional custom slot name."""
    if db is None:
        db = await _get_db()

    user_id = await _ensure_user(db, query)

    data = getattr(query, "data", "") or ""
    parts = data.split(":")
    slot_num = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    region_code = parts[2] if len(parts) > 2 else "kyiv"
    group_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
    subgroup_id = parts[4] if len(parts) > 4 else f"{group_id}.1"

    existing = await db.slots.get_slot(user_id, slot_num)
    alias = existing.alias if (existing and existing.alias) else f"Слот {slot_num}"

    await db.slots.upsert_slot(
        user_id=user_id,
        slot_number=slot_num,
        region_code=region_code,
        group_id=group_id,
        subgroup_id=subgroup_id,
        alias=alias,
    )

    if state is not None and hasattr(state, "set_state"):
        await state.update_data(rename_slot_num=slot_num, rename_owner_id=user_id)
        await state.set_state(SlotRenameState.waiting_for_alias)

    from bot.handlers.keyboards import InlineKeyboardButton, InlineKeyboardMarkup
    keep_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✅ Залишити «{alias}»",
                    callback_data=f"slot_keep_alias:{slot_num}",
                )
            ]
        ]
    )

    text = (
        f"📍 <b>Слот {slot_num}</b> ({region_code}, Черга <b>{subgroup_id}</b>)\n\n"
        f"✏️ Введіть назву для цього слота (до 16 символів, наприклад: <i>Дім</i>, <i>Офіс</i>, <i>Квартира</i>),\n"
        f"або натисніть кнопку нижче, щоб залишити <b>{alias}</b>:"
    )

    msg = getattr(query, "message", None)
    await _edit_or_answer(msg, text, reply_markup=keep_kb, parse_mode="HTML")

    if hasattr(query, "answer"):
        try:
            await query.answer()
        except Exception:
            pass


class SlotRenameState(StatesGroup):
    """FSM state for naming a user's outage slot during edit/add flow."""

    waiting_for_alias = State()


@router.callback_query(F.data.startswith("slot_keep_alias:"))
async def cb_slot_keep_alias(
    query: Any,
    state: Optional[FSMContext] = None,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """Finish slot wizard keeping the current/default alias."""
    if state is not None and hasattr(state, "clear"):
        await state.clear()

    if db is None:
        db = await _get_db()

    user_id, _, _, _, _, _ = resolve_chat_scope(query)

    data = getattr(query, "data", "") or ""
    parts = data.split(":")
    slot_num = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1

    slots = await db.slots.get_slots_for_user(user_id)
    slot = next((s for s in slots if getattr(s, "slot_number", None) == slot_num), None)
    alias = getattr(slot, "alias", None) or f"Слот {slot_num}"
    region_code = getattr(slot, "region_code", "") if slot else ""
    if hasattr(region_code, "value"):
        region_code = region_code.value
    subgroup_id = getattr(slot, "subgroup_id", "") if slot else ""

    if slot_num == 2:
        from bot.handlers.keyboards import InlineKeyboardButton, InlineKeyboardMarkup
        joint_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Так, увімкнути", callback_data="slot_joint:1"),
                    InlineKeyboardButton(text="❌ Ні, вимкнути", callback_data="slot_joint:0"),
                ]
            ]
        )
        text = (
            f"✅ <b>Слот {slot_num} збережено!</b> ({alias}, Черга {subgroup_id})\n\n"
            "📊 <b>Увімкнути спільний графік («Спільний»)?</b>\n"
            "Він показує спільну наявність світла для обох локацій."
        )
        msg = getattr(query, "message", None)
        await _edit_or_answer(msg, text, reply_markup=joint_kb, parse_mode="HTML")
    else:
        text = (
            f"✅ <b>Слот {slot_num} успішно збережено!</b>\n\n"
            f"• Назва: <b>{alias}</b>\n"
            f"• Регіон: <code>{region_code}</code>\n"
            f"• Черга: <b>{subgroup_id}</b>\n\n"
            "Тепер ви можете переглянути актуальний графік або налаштувати інший слот."
        )
        msg = getattr(query, "message", None)
        await _edit_or_answer(msg, text, reply_markup=build_slots_keyboard(slots), parse_mode="HTML")

    if hasattr(query, "answer"):
        try:
            await query.answer("Чергу збережено! ✅")
        except Exception:
            pass


@router.message(SlotRenameState.waiting_for_alias)
async def msg_slot_rename(
    message: Any,
    state: Optional[FSMContext] = None,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """Save new custom alias for the slot from user's text message."""
    raw_text = (getattr(message, "text", None) or "").strip()
    if raw_text.lower().startswith("/cancel"):
        if state is not None and hasattr(state, "clear"):
            await state.clear()
        if hasattr(message, "answer"):
            await message.answer("Перейменування скасовано.")
        return

    new_alias = raw_text[:16]
    if not new_alias:
        if hasattr(message, "answer"):
            await message.answer("Назва не може бути порожньою. Введіть назву до 16 символів або /cancel:")
        return

    user_id, _, _, _, _, _ = resolve_chat_scope(message)
    if state is not None and hasattr(state, "get_data"):
        fsm_data = await state.get_data()
        slot_num = int(fsm_data.get("rename_slot_num", 1))
        user_id = int(fsm_data.get("rename_owner_id", user_id))
        await state.clear()

    if db is None:
        db = await _get_db()

    existing = await db.slots.get_slot(user_id, slot_num)
    if existing is None:
        if hasattr(message, "answer"):
            await message.answer(f"Слот {slot_num} ще не налаштовано. Спочатку оберіть регіон та чергу через /slots.")
        return

    reg_val = existing.region_code.value if hasattr(existing.region_code, "value") else str(existing.region_code)
    await db.slots.upsert_slot(
        user_id=user_id,
        slot_number=slot_num,
        region_code=reg_val,
        group_id=existing.group_id,
        subgroup_id=existing.subgroup_id,
        alias=new_alias,
    )

    slots = await db.slots.get_slots_for_user(user_id)
    if slot_num == 2:
        from bot.handlers.keyboards import InlineKeyboardButton, InlineKeyboardMarkup
        joint_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Так, увімкнути", callback_data="slot_joint:1"),
                    InlineKeyboardButton(text="❌ Ні, вимкнути", callback_data="slot_joint:0"),
                ]
            ]
        )
        if hasattr(message, "answer"):
            await message.answer(
                f"✅ Назву <b>Слота {slot_num}</b> змінено на <b>{new_alias}</b>.\n\n"
                "📊 <b>Увімкнути спільний графік («Спільний»)?</b>\n"
                "Він показує спільну наявність світла для обох локацій.",
                parse_mode="HTML",
                reply_markup=joint_kb,
            )
    else:
        if hasattr(message, "answer"):
            await message.answer(
                f"✅ Назву <b>Слота {slot_num}</b> змінено на <b>{new_alias}</b>.",
                parse_mode="HTML",
                reply_markup=build_slots_keyboard(slots),
            )


@router.callback_query(F.data.startswith("slot_joint:"))
async def cb_slot_joint(
    query: Any,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """Save joint timeline preference at the end of Slot 2 setup."""
    if db is None:
        db = await _get_db()

    user_id = await _ensure_user(db, query)

    data = getattr(query, "data", "") or ""
    enable_joint = data.endswith(":1")
    await db.settings.update_settings(user_id, show_joint_timeline=enable_joint)

    slots = await db.slots.get_slots_for_user(user_id)
    joint_label = "увімкнено ✅" if enable_joint else "вимкнено ❌"
    text = (
        f"✅ <b>Слот 2 налаштовано!</b>\n"
        f"• Спільний графік: <b>{joint_label}</b>\n\n"
        "Тепер ви можете переглянути актуальний графік або змінити параметри у /settings."
    )
    msg = getattr(query, "message", None)
    await _edit_or_answer(msg, text, reply_markup=build_slots_keyboard(slots), parse_mode="HTML")

    if hasattr(query, "answer"):
        try:
            await query.answer()
        except Exception:
            pass


@router.callback_query(F.data == "toggle_joint_timeline")
async def cb_toggle_joint_timeline(
    query: Any,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """Toggle optional joint timeline visibility in Settings."""
    if db is None:
        db = await _get_db()

    user_id = await _ensure_user(db, query)

    current = await db.settings.get_settings(user_id)
    new_val = not bool(getattr(current, "show_joint_timeline", False))
    updated = await db.settings.update_settings(user_id, show_joint_timeline=new_val)

    msg = getattr(query, "message", None)
    if msg and hasattr(msg, "edit_reply_markup"):
        try:
            await msg.edit_reply_markup(reply_markup=build_settings_keyboard(updated))
        except Exception:
            pass

    if hasattr(query, "answer"):
        try:
            await query.answer(f"Спільний графік {'увімкнено ✅' if new_val else 'вимкнено ❌'}")
        except Exception:
            pass


@router.callback_query(F.data.startswith("slot_delete:"))
async def cb_slot_delete(
    query: Any,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """Delete slot from database (e.g. Slot 2)."""
    if db is None:
        db = await _get_db()

    user_id, _, _, _, _, _ = resolve_chat_scope(query)

    data = getattr(query, "data", "") or ""
    parts = data.split(":")
    slot_num = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 2

    await db.slots.delete_slot(user_id, slot_num)
    slots = await db.slots.get_slots_for_user(user_id)

    text = f"🗑 <b>Слот {slot_num} видалено.</b>\n\nПоточні налаштовані черги:"
    msg = getattr(query, "message", None)
    if msg and hasattr(msg, "edit_text"):
        await msg.edit_text(text, parse_mode="HTML", reply_markup=build_slots_keyboard(slots))
    elif msg and hasattr(msg, "answer"):
        await msg.answer(text, parse_mode="HTML", reply_markup=build_slots_keyboard(slots))

    if hasattr(query, "answer"):
        try:
            await query.answer("Слот видалено 🗑")
        except Exception:
            pass


# ============================================================================
# SETTINGS TOGGLE CALLBACKS
# ============================================================================

@router.callback_query(F.data.startswith("toggle_notif:"))
async def cb_toggle_notif(
    query: Any,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """Toggle individual notification preferences (30m, 15m, diff)."""
    if db is None:
        db = await _get_db()

    user_id = await _ensure_user(db, query)

    data = getattr(query, "data", "") or ""
    setting_key = data.split(":", 1)[1] if ":" in data else ""

    settings_record = await db.settings.get_settings(user_id)

    if setting_key == "30m":
        new_val = not bool(settings_record.notify_30m)
        updated = await db.settings.update_settings(user_id, notify_30m=new_val)
        notice = "Попередження за 30хв: " + ("Увімкнено 🔔" if new_val else "Вимкнено 🔕")
    elif setting_key == "15m":
        new_val = not bool(settings_record.notify_15m)
        updated = await db.settings.update_settings(user_id, notify_15m=new_val)
        notice = "Попередження за 15хв: " + ("Увімкнено 🔔" if new_val else "Вимкнено 🔕")
    elif setting_key == "diff":
        new_val = not bool(settings_record.notify_schedule_change)
        updated = await db.settings.update_settings(user_id, notify_schedule_change=new_val)
        notice = "Сповіщення про зміни: " + ("Увімкнено 🔔" if new_val else "Вимкнено 🔕")
    else:
        updated = settings_record
        notice = "Налаштування оновлено"

    msg = getattr(query, "message", None)
    if msg and hasattr(msg, "edit_reply_markup"):
        await msg.edit_reply_markup(reply_markup=build_settings_keyboard(updated))

    if hasattr(query, "answer"):
        try:
            await query.answer(notice)
        except Exception:
            pass


@router.callback_query(F.data == "toggle_silent_hours")
async def cb_toggle_silent_hours(
    query: Any,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """Toggle silent hours mode (23:00-07:00 vs disabled 00:00-00:00)."""
    if db is None:
        db = await _get_db()

    user_id = await _ensure_user(db, query)

    settings_record = await db.settings.get_settings(user_id)
    cur_start = settings_record.silent_hours_start
    cur_end = settings_record.silent_hours_end

    # If active (23 to 7), disable (0 to 0); if disabled, re-enable (23 to 7)
    if cur_start == 23 and cur_end == 7:
        new_start, new_end = 0, 0
        notice = "Тихі години вимкнено 🔕"
    else:
        new_start, new_end = 23, 7
        notice = "Тихі години встановлено: 23:00 - 07:00 🌙"

    updated = await db.settings.update_settings(
        user_id, silent_hours_start=new_start, silent_hours_end=new_end
    )

    msg = getattr(query, "message", None)
    if msg and hasattr(msg, "edit_reply_markup"):
        await msg.edit_reply_markup(reply_markup=build_settings_keyboard(updated))

    if hasattr(query, "answer"):
        try:
            await query.answer(notice)
        except Exception:
            pass


# ============================================================================
# MENU NAVIGATION CALLBACKS
# ============================================================================

@router.callback_query(F.data == "menu_main")
async def cb_menu_main(query: Any, **kwargs: Any) -> None:
    """Return to main menu."""
    settings = get_settings()
    _, _, _, is_group, first_name, _ = resolve_chat_scope(query)
    text = (
        f"Головне меню | Вітаємо, {first_name}! ⚡\n\n"
        "Оберіть потрібний розділ:"
    )
    msg = getattr(query, "message", None)
    await _edit_or_answer(
        msg,
        text,
        reply_markup=build_main_menu_keyboard(settings.WEBAPP_URL, is_group=is_group),
        parse_mode="HTML",
    )

    if hasattr(query, "answer"):
        try:
            await query.answer()
        except Exception:
            pass


@router.callback_query(F.data == "menu_slots")
async def cb_menu_slots(
    query: Any,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """Show slot management menu."""
    if db is None:
        db = await _get_db()

    user_id, _, _, _, _, _ = resolve_chat_scope(query)
    slots = await db.slots.get_slots_for_user(user_id)

    lines = ["📍 <b>Керування чергами моніторингу</b>\n"]
    if slots:
        for s in slots:
            slot_num = getattr(s, "slot_number", 1)
            alias = getattr(s, "alias", "Черга")
            reg = getattr(s, "region_code", "")
            subgrp = getattr(s, "subgroup_id", "")
            lines.append(f"• <b>Слот {slot_num}:</b> {alias} ({reg}, {subgrp})")
    else:
        lines.append("У вас ще не додано жодної черги.")

    lines.append("\nОберіть слот нижче для зміни налаштувань:")

    msg = getattr(query, "message", None)
    await _edit_or_answer(
        msg,
        "\n".join(lines),
        reply_markup=build_slots_keyboard(slots),
        parse_mode="HTML",
    )

    if hasattr(query, "answer"):
        try:
            await query.answer()
        except Exception:
            pass


@router.callback_query(F.data == "menu_settings")
async def cb_menu_settings(
    query: Any,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """Show notification settings menu."""
    if db is None:
        db = await _get_db()

    user_id, _, _, _, _, _ = resolve_chat_scope(query)
    settings_record = await db.settings.get_settings(user_id)

    text = (
        "⚙️ <b>Налаштування сповіщень</b>\n\n"
        "Керуйте завчасними попередженнями та розкладом тихих годин:"
    )
    msg = getattr(query, "message", None)
    await _edit_or_answer(
        msg,
        text,
        reply_markup=build_settings_keyboard(settings_record),
        parse_mode="HTML",
    )

    if hasattr(query, "answer"):
        try:
            await query.answer()
        except Exception:
            pass


@router.callback_query(F.data == "menu_help")
async def cb_menu_help(query: Any, **kwargs: Any) -> None:
    """Show help screen."""
    settings = get_settings()
    text = (
        "⚡ <b>Довідка та команди</b>\n\n"
        "• /status — перевірити поточний графік\n"
        "• /slots — керування чергами (Дім, Офіс)\n"
        "• /settings — налаштування сповіщень\n\n"
        "🟩 Світло є | ⬛ Світла немає | 🟨 Можливе | 🟥 Аварійні"
    )
    _, _, _, is_group, _, _ = resolve_chat_scope(query)
    msg = getattr(query, "message", None)
    await _edit_or_answer(
        msg,
        text,
        reply_markup=build_help_keyboard(settings.WEBAPP_URL, is_group=is_group),
        parse_mode="HTML",
    )

    if hasattr(query, "answer"):
        try:
            await query.answer()
        except Exception:
            pass


# ============================================================================
# TMA web_app_data EVENT HANDLER
# ============================================================================

@router.message(F.web_app_data)
async def handle_web_app_data(
    message: Any,
    db: Optional[DatabaseRepository] = None,
    **kwargs: Any,
) -> None:
    """
    Handle data emitted by Telegram Mini App via tg.sendData(json_string).
    Synchronizes updated slot configuration directly to SQLite.
    """
    if db is None:
        db = await _get_db()

    user_id = await _ensure_user(db, message)
    _, _, _, is_group, _, _ = resolve_chat_scope(message)

    web_app_data = getattr(message, "web_app_data", None)
    raw_json = getattr(web_app_data, "data", "{}") or "{}"

    try:
        data = json.loads(raw_json)
    except Exception as exc:
        logger.warning("Failed to parse web_app_data JSON: %s", exc)
        if hasattr(message, "answer"):
            await message.answer("⚠️ Помилка зчитування даних із Mini App.")
        return

    # Check for slot data in payload
    slot_a = data.get("slot_a") or data.get("slot1") or (data if "region_code" in data else None)
    slot_b = data.get("slot_b") or data.get("slot2")

    updated_count = 0
    if slot_a and isinstance(slot_a, dict):
        reg_a = slot_a.get("region_code") or slot_a.get("region") or "kyiv"
        grp_a = int(slot_a.get("group_id") or slot_a.get("group") or 1)
        sub_a = slot_a.get("subgroup_id") or slot_a.get("subgroup") or f"{grp_a}.1"
        al_a = slot_a.get("alias", "Дім")
        await db.slots.upsert_slot(
            user_id=user_id,
            slot_number=1,
            region_code=reg_a,
            group_id=grp_a,
            subgroup_id=sub_a,
            alias=al_a,
        )
        updated_count += 1

    if slot_b and isinstance(slot_b, dict):
        reg_b = slot_b.get("region_code") or slot_b.get("region") or "kyiv"
        grp_b = int(slot_b.get("group_id") or slot_b.get("group") or 1)
        sub_b = slot_b.get("subgroup_id") or slot_b.get("subgroup") or f"{grp_b}.1"
        al_b = slot_b.get("alias", "Офіс")
        await db.slots.upsert_slot(
            user_id=user_id,
            slot_number=2,
            region_code=reg_b,
            group_id=grp_b,
            subgroup_id=sub_b,
            alias=al_b,
        )
        updated_count += 1

    if updated_count > 0:
        resp = (
            f"✅ <b>Mini App синхронізовано!</b> Оновлено черг: {updated_count}.\n"
            "Використовуйте /status для перегляду актуального графіка."
        )
    else:
        resp = "✅ Дані із Mini App отримано."

    if hasattr(message, "answer"):
        await message.answer(
            resp,
            parse_mode="HTML",
            reply_markup=build_main_menu_keyboard(get_settings().WEBAPP_URL, is_group=is_group),
        )


__all__ = [
    "router",
    "cb_refresh_schedule",
    "cb_toggle_render_mode",
    "cb_slot_edit",
    "cb_slot_region",
    "cb_slot_group",
    "cb_slot_subgroup",
    "cb_slot_delete",
    "cb_toggle_notif",
    "cb_toggle_silent_hours",
    "cb_menu_main",
    "cb_menu_slots",
    "cb_menu_settings",
    "cb_menu_help",
    "handle_web_app_data",
]
