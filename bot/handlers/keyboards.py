"""
bot/handlers/keyboards.py - Inline and WebApp Keyboards for Telegram Bot.

Constructs interactive UI keyboards for status presentation, TMA bridge navigation,
slot configuration wizard, notification settings, and help.
Supports Aiogram 3.15+ with graceful standard library fallback stubs.
"""

from __future__ import annotations

from typing import Any, List, Optional

try:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo  # type: ignore[import-untyped]
    _AIOGRAM_AVAILABLE = True
except ImportError:
    _AIOGRAM_AVAILABLE = False

    class WebAppInfo:  # type: ignore[no-redef]
        def __init__(self, url: str) -> None:
            self.url = url

        def __repr__(self) -> str:
            return f"WebAppInfo(url={self.url!r})"

    class InlineKeyboardButton:  # type: ignore[no-redef]
        def __init__(
            self,
            text: str,
            callback_data: Optional[str] = None,
            web_app: Optional[WebAppInfo] = None,
            url: Optional[str] = None,
        ) -> None:
            self.text = text
            self.callback_data = callback_data
            self.web_app = web_app
            self.url = url

        def __repr__(self) -> str:
            return f"InlineKeyboardButton(text={self.text!r}, callback_data={self.callback_data!r})"

    class InlineKeyboardMarkup:  # type: ignore[no-redef]
        def __init__(self, inline_keyboard: List[List[InlineKeyboardButton]]) -> None:
            self.inline_keyboard = inline_keyboard

        def __repr__(self) -> str:
            return f"InlineKeyboardMarkup(rows={len(self.inline_keyboard)})"


# ============================================================================
# KEYBOARD BUILDERS
# ============================================================================

def _tma_button(text: str, tma_url: str, is_group: bool = False) -> InlineKeyboardButton:
    """Use WebAppInfo in private chats and plain URL in groups/topics (Telegram API forbids web_app in groups)."""
    if is_group:
        return InlineKeyboardButton(text=text, url=tma_url)
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=tma_url))


def build_status_keyboard(tma_url: str, current_mode: str = "image", is_group: bool = False) -> InlineKeyboardMarkup:
    """Build timeline presentation keyboard with TMA WebAppInfo button and actions."""
    toggle_text = "📝 Текстовий режим" if current_mode == "image" else "🖼 Графічний режим"

    keyboard = [
        [
            _tma_button("📱 Відкрити інтерактивний графік (TMA)", tma_url, is_group=is_group)
        ],
        [
            InlineKeyboardButton(text="🔄 Оновити", callback_data="refresh_schedule"),
            InlineKeyboardButton(text=toggle_text, callback_data="toggle_render_mode"),
        ],
        [
            InlineKeyboardButton(text="📍 Мої черги", callback_data="menu_slots"),
            InlineKeyboardButton(text="⚙️ Налаштування", callback_data="menu_settings"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_main_menu_keyboard(webapp_url: Optional[str] = None, is_group: bool = False) -> InlineKeyboardMarkup:
    """Build bot welcome and main menu keyboard."""
    row1 = [
        InlineKeyboardButton(text="⚡ Поточний графік", callback_data="refresh_schedule"),
        InlineKeyboardButton(text="📍 Мої черги", callback_data="menu_slots"),
    ]
    row2 = [
        InlineKeyboardButton(text="⚙️ Налаштування", callback_data="menu_settings"),
        InlineKeyboardButton(text="❓ Довідка", callback_data="menu_help"),
    ]
    rows = [row1, row2]
    if webapp_url:
        rows.insert(
            0,
            [
                _tma_button("📱 Відкрити Mini App", webapp_url, is_group=is_group)
            ],
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_onboarding_keyboard() -> InlineKeyboardMarkup:
    """Build keyboard for new users with no slots configured."""
    keyboard = [
        [
            InlineKeyboardButton(
                text="➕ Налаштувати Слот 1 (Дім)",
                callback_data="slot_edit:1",
            )
        ],
        [
            InlineKeyboardButton(text="❓ Довідка та команди", callback_data="menu_help"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_slots_keyboard(slots: List[Any]) -> InlineKeyboardMarkup:
    """Build slot management menu with slot editing and deletion."""
    keyboard: List[List[InlineKeyboardButton]] = []

    has_slot1 = False
    has_slot2 = False

    for s in slots:
        slot_num = getattr(s, "slot_number", None) or (s.get("slot_number") if isinstance(s, dict) else 1)
        alias = getattr(s, "alias", None) or (s.get("alias") if isinstance(s, dict) else "Моя черга")
        region = getattr(s, "region_code", None) or (s.get("region_code") if isinstance(s, dict) else "kyiv")
        if hasattr(region, "value"):
            region = region.value
        subgroup = getattr(s, "subgroup_id", None) or (s.get("subgroup_id") if isinstance(s, dict) else "")

        if slot_num == 1:
            has_slot1 = True
            keyboard.append([
                InlineKeyboardButton(
                    text=f"📍 Слот 1: {alias} ({subgroup})",
                    callback_data="slot_edit:1",
                ),
            ])
        elif slot_num == 2:
            has_slot2 = True
            keyboard.append([
                InlineKeyboardButton(
                    text=f"📍 Слот 2: {alias} ({subgroup})",
                    callback_data="slot_edit:2",
                ),
                InlineKeyboardButton(
                    text="🗑 Видалити Слот 2",
                    callback_data="slot_delete:2",
                ),
            ])

    if not has_slot1:
        keyboard.append([
            InlineKeyboardButton(text="➕ Налаштувати Слот 1 (Дім)", callback_data="slot_edit:1")
        ])

    if not has_slot2:
        keyboard.append([
            InlineKeyboardButton(text="➕ Додати Слот 2 (Офіс)", callback_data="slot_edit:2")
        ])

    keyboard.append([
        InlineKeyboardButton(text="⚡ До графіка", callback_data="refresh_schedule"),
        InlineKeyboardButton(text="🔙 Головне меню", callback_data="menu_main"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_region_keyboard(slot_number: int) -> InlineKeyboardMarkup:
    """Build region selection keyboard for 4 DTEK DSO regions."""
    keyboard = [
        [
            InlineKeyboardButton(
                text="🏙 м. Київ (ДТЕК КЕМ)",
                callback_data=f"slot_reg:{slot_number}:kyiv",
            )
        ],
        [
            InlineKeyboardButton(
                text="🌲 Київська обл. (ДТЕК КРЕМ)",
                callback_data=f"slot_reg:{slot_number}:kyiv-region",
            )
        ],
        [
            InlineKeyboardButton(
                text="🏭 Дніпропетровська обл. (ДТЕК ДНЕМ)",
                callback_data=f"slot_reg:{slot_number}:dnipro",
            )
        ],
        [
            InlineKeyboardButton(
                text="⚓ Одеська обл. (ДТЕК ОЕМ)",
                callback_data=f"slot_reg:{slot_number}:odesa",
            )
        ],
        [
            InlineKeyboardButton(text="🔙 Скасувати", callback_data="menu_slots"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_group_keyboard(slot_number: int, region_code: str) -> InlineKeyboardMarkup:
    """Build group selection keyboard (1 through 6)."""
    row1 = [
        InlineKeyboardButton(text="1", callback_data=f"slot_grp:{slot_number}:{region_code}:1"),
        InlineKeyboardButton(text="2", callback_data=f"slot_grp:{slot_number}:{region_code}:2"),
        InlineKeyboardButton(text="3", callback_data=f"slot_grp:{slot_number}:{region_code}:3"),
    ]
    row2 = [
        InlineKeyboardButton(text="4", callback_data=f"slot_grp:{slot_number}:{region_code}:4"),
        InlineKeyboardButton(text="5", callback_data=f"slot_grp:{slot_number}:{region_code}:5"),
        InlineKeyboardButton(text="6", callback_data=f"slot_grp:{slot_number}:{region_code}:6"),
    ]
    row3 = [
        InlineKeyboardButton(text="🔙 Назад до регіонів", callback_data=f"slot_edit:{slot_number}"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, row3])


def build_subgroup_keyboard(
    slot_number: int, region_code: str, group_id: int
) -> InlineKeyboardMarkup:
    """Build subgroup selection keyboard (group.1 and group.2)."""
    sub1 = f"{group_id}.1"
    sub2 = f"{group_id}.2"
    row1 = [
        InlineKeyboardButton(
            text=f"Підгрупа {sub1}",
            callback_data=f"slot_sub:{slot_number}:{region_code}:{group_id}:{sub1}",
        ),
        InlineKeyboardButton(
            text=f"Підгрупа {sub2}",
            callback_data=f"slot_sub:{slot_number}:{region_code}:{group_id}:{sub2}",
        ),
    ]
    row2 = [
        InlineKeyboardButton(
            text="🔙 Назад до груп",
            callback_data=f"slot_reg:{slot_number}:{region_code}",
        ),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2])


def build_settings_keyboard(settings: Any) -> InlineKeyboardMarkup:
    """Build settings menu with notification toggles and silent hours."""
    n30 = bool(getattr(settings, "notify_30m", True))
    n15 = bool(getattr(settings, "notify_15m", True))
    n_diff = bool(getattr(settings, "notify_schedule_change", True))
    show_joint = bool(getattr(settings, "show_joint_timeline", False))

    sh_start = getattr(settings, "silent_hours_start", 23)
    sh_end = getattr(settings, "silent_hours_end", 7)

    status_30 = "🔔 Увімк" if n30 else "🔕 Вимк"
    status_15 = "🔔 Увімк" if n15 else "🔕 Вимк"
    status_diff = "🔔 Увімк" if n_diff else "🔕 Вимк"
    status_joint = "✅ Увімк" if show_joint else "❌ Вимк"

    keyboard = [
        [
            InlineKeyboardButton(text=f"📊 Спільний графік: {status_joint}", callback_data="toggle_joint_timeline"),
        ],
        [
            InlineKeyboardButton(text=f"Попередження за 30 хв: {status_30}", callback_data="toggle_notif:30m"),
        ],
        [
            InlineKeyboardButton(text=f"Попередження за 15 хв: {status_15}", callback_data="toggle_notif:15m"),
        ],
        [
            InlineKeyboardButton(text=f"Зміни у графіку: {status_diff}", callback_data="toggle_notif:diff"),
        ],
        [
            InlineKeyboardButton(
                text=f"🌙 Тихі години: {sh_start:02d}:00 - {sh_end:02d}:00",
                callback_data="toggle_silent_hours",
            ),
        ],
        [
            InlineKeyboardButton(text="⚡ До графіка", callback_data="refresh_schedule"),
            InlineKeyboardButton(text="🔙 Головне меню", callback_data="menu_main"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_help_keyboard(tma_url: Optional[str] = None, is_group: bool = False) -> InlineKeyboardMarkup:
    """Build help screen keyboard."""
    rows: List[List[InlineKeyboardButton]] = []
    if tma_url:
        rows.append([
            _tma_button("📱 Відкрити Mini App", tma_url, is_group=is_group)
        ])
    rows.append([
        InlineKeyboardButton(text="⚡ Поточний графік", callback_data="refresh_schedule"),
        InlineKeyboardButton(text="📍 Налаштувати черги", callback_data="menu_slots"),
    ])
    rows.append([
        InlineKeyboardButton(text="🔙 Головне меню", callback_data="menu_main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Aliases for compatibility
get_status_keyboard = build_status_keyboard
get_timeline_keyboard = build_status_keyboard
get_main_menu_keyboard = build_main_menu_keyboard
get_onboarding_keyboard = build_onboarding_keyboard
get_slots_menu_keyboard = build_slots_keyboard
get_region_selection_keyboard = build_region_keyboard
get_group_selection_keyboard = build_group_keyboard
get_subgroup_selection_keyboard = build_subgroup_keyboard
get_settings_keyboard = build_settings_keyboard
get_help_keyboard = build_help_keyboard

__all__ = [
    "InlineKeyboardMarkup",
    "InlineKeyboardButton",
    "WebAppInfo",
    "build_status_keyboard",
    "build_main_menu_keyboard",
    "build_onboarding_keyboard",
    "build_slots_keyboard",
    "build_region_keyboard",
    "build_group_keyboard",
    "build_subgroup_keyboard",
    "build_settings_keyboard",
    "build_help_keyboard",
    "get_status_keyboard",
    "get_timeline_keyboard",
    "get_main_menu_keyboard",
    "get_onboarding_keyboard",
    "get_slots_menu_keyboard",
    "get_region_selection_keyboard",
    "get_group_selection_keyboard",
    "get_subgroup_selection_keyboard",
    "get_settings_keyboard",
    "get_help_keyboard",
]
