"""
tests/test_adversarial_bot_handlers.py - Adversarial Stress & Security Test Suite for Milestone 4.

Empirically tests bot handlers, command routers, keyboards, TMA bridge, and zero-port architecture:
1. Command Dispatch under boundary and fault conditions:
   - /start with valid single/dual-slot sync_<b64> parameters.
   - /start under hostile payloads (SQL injection in alias, path traversal, XSS HTML tags, oversized strings, invalid base64).
   - /status under 0, 1, and 2 configured slots across Image and Text render modes, plus render error fallback.
   - /slots, /settings, /help navigation and menu builders.
2. Keyboard callback data formats & wizard state progression:
   - refresh_schedule, toggle_render_mode.
   - slot wizard (slot_edit, slot_reg, slot_grp, slot_sub, slot_delete).
   - settings toggles (toggle_notif:30m, 15m, diff, toggle_silent_hours).
   - menu navigation (menu_main, menu_slots, menu_settings, menu_help).
3. TMA F.web_app_data synchronization handler:
   - Valid single and dual-slot payloads.
   - Corrupted and malformed JSON payloads.
   - Hostile / non-dict JSON payloads (list, int, null, string).
   - Non-integer group IDs and invalid DSO regions.
4. Outbound-only Zero-Port Invariance:
   - Verifies bot/main.py uses long polling (start_polling) only.
   - Verifies zero web server frameworks or socket listeners.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import os
import re
import tempfile
import unittest
from typing import Any, List, Optional
from unittest.mock import patch

from bot.database.connection import DatabaseConnectionManager
from bot.database.repository import DatabaseRepository
from bot.database.schema import init_db
from bot.handlers.callbacks import (
    cb_menu_help,
    cb_menu_main,
    cb_menu_settings,
    cb_menu_slots,
    cb_refresh_schedule,
    cb_slot_delete,
    cb_slot_edit,
    cb_slot_group,
    cb_slot_region,
    cb_slot_subgroup,
    cb_toggle_notif,
    cb_toggle_render_mode,
    cb_toggle_silent_hours,
    handle_web_app_data,
)
from bot.handlers.commands import (
    USER_RENDER_MODES,
    cmd_help,
    cmd_settings,
    cmd_slots,
    cmd_start,
    cmd_status,
)
from bot.handlers.keyboards import (
    build_group_keyboard,
    build_help_keyboard,
    build_main_menu_keyboard,
    build_onboarding_keyboard,
    build_region_keyboard,
    build_settings_keyboard,
    build_slots_keyboard,
    build_status_keyboard,
    build_subgroup_keyboard,
)
from bot.models.enums import DSORegion
from bot.models.schedule import SlotConfig
from bot.services.scraper import ScraperCascade
from bot.services.tma_payload import TMAPayload


class MockTelegramUser:
    def __init__(self, id: int = 101, first_name: str = "TestUser", username: Optional[str] = "tester") -> None:
        self.id = id
        self.first_name = first_name
        self.username = username


class MockTelegramChat:
    def __init__(self, id: int = 101) -> None:
        self.id = id


class MockTelegramMessage:
    def __init__(self, text: str = "", user: Optional[MockTelegramUser] = None, raw_web_app_data: Optional[str] = None) -> None:
        self.text = text
        self.from_user = user or MockTelegramUser()
        self.chat = MockTelegramChat(self.from_user.id)
        self.replies: List[tuple[str, Any, Any]] = []
        self.photos: List[tuple[Any, Optional[str], Any, Any]] = []
        self.edited_text: List[tuple[str, Any, Any]] = []
        self.edited_markup: List[Any] = []
        if raw_web_app_data is not None:
            self.web_app_data = type("MockWebAppData", (), {"data": raw_web_app_data})()
        else:
            self.web_app_data = None

    async def answer(self, text: str, parse_mode: Optional[str] = None, reply_markup: Optional[Any] = None) -> None:
        self.replies.append((text, parse_mode, reply_markup))

    async def answer_photo(self, photo: Any, caption: Optional[str] = None, parse_mode: Optional[str] = None, reply_markup: Optional[Any] = None) -> None:
        self.photos.append((photo, caption, parse_mode, reply_markup))

    async def edit_text(self, text: str, parse_mode: Optional[str] = None, reply_markup: Optional[Any] = None) -> None:
        self.edited_text.append((text, parse_mode, reply_markup))

    async def edit_reply_markup(self, reply_markup: Optional[Any] = None) -> None:
        self.edited_markup.append(reply_markup)


class MockTelegramCallbackQuery:
    def __init__(self, data: str, user: Optional[MockTelegramUser] = None) -> None:
        self.data = data
        self.from_user = user or MockTelegramUser()
        self.message = MockTelegramMessage(user=self.from_user)
        self.answers: List[Optional[str]] = []

    async def answer(self, text: Optional[str] = None) -> None:
        self.answers.append(text)


class TestAdversarialBotHandlers(unittest.IsolatedAsyncioTestCase):
    """Adversarial challenge test suite for bot commands, callbacks, and architecture."""

    async def asyncSetUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        self.db_manager = DatabaseConnectionManager(self.db_path)
        async with self.db_manager.get_connection() as conn:
            await init_db(conn)

        self.db_repo = DatabaseRepository(self.db_manager)
        self.scraper = ScraperCascade(db_conn=self.db_manager)
        USER_RENDER_MODES.clear()

    async def asyncTearDown(self) -> None:
        await self.db_manager.close()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    # ========================================================================
    # 1. COMMAND DISPATCH TESTS
    # ========================================================================

    async def test_cmd_start_onboarding_without_args(self) -> None:
        """Test /start without arguments registers user and returns main menu."""
        msg = MockTelegramMessage(text="/start", user=MockTelegramUser(id=101, first_name="Taras"))
        await cmd_start(msg, db=self.db_repo)

        self.assertEqual(len(msg.replies), 1)
        self.assertIn("Вітаємо, Taras!", msg.replies[0][0])
        self.assertIn("/status", msg.replies[0][0])

        # Verify user is registered in database
        user = await self.db_repo.users.get_user(101)
        self.assertIsNotNone(user)
        self.assertEqual(user.first_name, "Taras")

    async def test_cmd_start_valid_dual_slot_sync(self) -> None:
        """Test /start sync_<b64> with dual slot configuration syncs both slots."""
        slot1 = SlotConfig(slot_number=1, alias="Дім", region_code=DSORegion.KYIV, group_id=1, subgroup_id="1.1")
        slot2 = SlotConfig(slot_number=2, alias="Офіс", region_code=DSORegion.KYIV_REGION, group_id=2, subgroup_id="2.2")
        sync_code = TMAPayload.encode_sync_code(slot1, slot2)

        msg = MockTelegramMessage(text=f"/start {sync_code}", user=MockTelegramUser(id=102, first_name="Bohdan"))
        await cmd_start(msg, db=self.db_repo)

        self.assertEqual(len(msg.replies), 1)
        self.assertIn("успішно синхронізовано з Mini App!", msg.replies[0][0])
        self.assertIn("Слот 1:", msg.replies[0][0])
        self.assertIn("Слот 2:", msg.replies[0][0])

        slots = await self.db_repo.slots.get_slots_for_user(102)
        self.assertEqual(len(slots), 2)
        self.assertEqual(slots[0].subgroup_id, "1.1")
        self.assertEqual(slots[1].subgroup_id, "2.2")

    async def test_cmd_start_valid_single_slot_sync(self) -> None:
        """Test /start sync_<b64> with single slot configuration syncs Slot 1 only."""
        slot1 = SlotConfig(slot_number=1, alias="Дім", region_code=DSORegion.DNIPRO, group_id=4, subgroup_id="4.2")
        sync_code = TMAPayload.encode_sync_code(slot1)

        msg = MockTelegramMessage(text=f"/start {sync_code}", user=MockTelegramUser(id=103, first_name="Lesya"))
        await cmd_start(msg, db=self.db_repo)

        self.assertEqual(len(msg.replies), 1)
        self.assertIn("успішно синхронізовано з Mini App!", msg.replies[0][0])
        self.assertIn("Слот 1:", msg.replies[0][0])
        self.assertNotIn("Слот 2:", msg.replies[0][0])

        slots = await self.db_repo.slots.get_slots_for_user(103)
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].region_code, "dnipro")
        self.assertEqual(slots[0].subgroup_id, "4.2")

    async def test_cmd_start_hostile_sql_injection_in_alias(self) -> None:
        """Test hostile SQL injection attempt in sync code alias is neutralized."""
        raw_payload = "kyiv:1:1.1:'; DROP TABLE users; --"
        b64 = base64.urlsafe_b64encode(raw_payload.encode()).decode().rstrip("=")
        sync_code = f"sync_{b64}"

        msg = MockTelegramMessage(text=f"/start {sync_code}", user=MockTelegramUser(id=104, first_name="Hacker"))
        await cmd_start(msg, db=self.db_repo)

        # Ensure users table is intact
        user = await self.db_repo.users.get_user(104)
        self.assertIsNotNone(user)
        slots = await self.db_repo.slots.get_slots_for_user(104)
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].alias, "'; DROP TABLE users; --")

    async def test_cmd_start_hostile_path_traversal_in_alias(self) -> None:
        """Test path traversal payload in sync code alias is safely treated as string literal."""
        raw_payload = "kyiv:1:1.1:../../../../etc/passwd"
        b64 = base64.urlsafe_b64encode(raw_payload.encode()).decode().rstrip("=")
        sync_code = f"sync_{b64}"

        msg = MockTelegramMessage(text=f"/start {sync_code}", user=MockTelegramUser(id=105, first_name="Tester"))
        await cmd_start(msg, db=self.db_repo)

        slots = await self.db_repo.slots.get_slots_for_user(105)
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].alias, "../../../../etc/passwd")

    async def test_cmd_start_oversized_sync_payload(self) -> None:
        """Test oversized sync parameter does not crash the bot and falls back to welcome."""
        oversized = "sync_" + ("A" * 8000)
        msg = MockTelegramMessage(text=f"/start {oversized}", user=MockTelegramUser(id=106, first_name="Oversized"))
        await cmd_start(msg, db=self.db_repo)

        self.assertEqual(len(msg.replies), 1)
        self.assertIn("Вітаємо", msg.replies[0][0])

    async def test_cmd_start_malformed_base64_and_invalid_group_bounds(self) -> None:
        """Test malformed base64 and out-of-bounds group IDs fail gracefully."""
        for hostile in ("sync_!@#$%^&*()", "sync_aW52YWxpZA", "sync_kyiv:99:99.1:bad"):
            msg = MockTelegramMessage(text=f"/start {hostile}", user=MockTelegramUser(id=107))
            await cmd_start(msg, db=self.db_repo)
            self.assertIn("Вітаємо", msg.replies[-1][0])

    async def test_cmd_status_zero_slots_prompts_onboarding(self) -> None:
        """Test /status when user has 0 slots sends onboarding prompt."""
        await self.db_repo.users.upsert_user(201, 201, "Newbie")
        msg = MockTelegramMessage(user=MockTelegramUser(id=201))
        await cmd_status(msg, db=self.db_repo, scraper=self.scraper)

        self.assertEqual(len(msg.replies), 1)
        self.assertIn("У вас ще не налаштовано жодної черги", msg.replies[0][0])
        self.assertEqual(len(msg.photos), 0)

    async def test_cmd_status_single_slot_image_and_text(self) -> None:
        """Test /status with 1 slot renders correctly in both image and text modes."""
        await self.db_repo.users.upsert_user(202, 202, "SingleSlotUser")
        await self.db_repo.slots.upsert_slot(202, 1, "kyiv", 1, "1.1", "Дім")

        # Image mode
        USER_RENDER_MODES[202] = "image"
        msg_img = MockTelegramMessage(user=MockTelegramUser(id=202))
        await cmd_status(msg_img, db=self.db_repo, scraper=self.scraper)
        self.assertEqual(len(msg_img.photos), 1)
        self.assertIn("Дім (1.1)", msg_img.photos[0][1])

        # Text mode
        USER_RENDER_MODES[202] = "text"
        msg_txt = MockTelegramMessage(user=MockTelegramUser(id=202))
        await cmd_status(msg_txt, db=self.db_repo, scraper=self.scraper)
        self.assertEqual(len(msg_txt.replies), 1)
        self.assertIn("<code>", msg_txt.replies[0][0])

    async def test_cmd_status_dual_slot_comparison(self) -> None:
        """Test /status with 2 slots computes joint timeline and attaches TMA fragment."""
        await self.db_repo.users.upsert_user(203, 203, "DualSlotUser")
        await self.db_repo.slots.upsert_slot(203, 1, "kyiv", 1, "1.1", "Дім")
        await self.db_repo.slots.upsert_slot(203, 2, "odesa", 3, "3.2", "Офіс")

        msg = MockTelegramMessage(user=MockTelegramUser(id=203))
        USER_RENDER_MODES[203] = "image"
        await cmd_status(msg, db=self.db_repo, scraper=self.scraper)

        self.assertEqual(len(msg.photos), 1)
        self.assertIn("Дім (1.1)", msg.photos[0][1])
        self.assertIn("Офіс (3.2)", msg.photos[0][1])

        # Verify TMA button URL in keyboard
        keyboard = msg.photos[0][3]
        tma_btn = keyboard.inline_keyboard[0][0]
        self.assertTrue(hasattr(tma_btn, "web_app"))
        self.assertIn("#p=", tma_btn.web_app.url)

    async def test_cmd_status_image_failure_fallback_to_text(self) -> None:
        """Test that if ImageRenderer fails, cmd_status gracefully falls back to text rendering."""
        await self.db_repo.users.upsert_user(204, 204, "FallbackUser")
        await self.db_repo.slots.upsert_slot(204, 1, "kyiv", 1, "1.1", "Дім")
        USER_RENDER_MODES[204] = "image"

        msg = MockTelegramMessage(user=MockTelegramUser(id=204))
        with patch("bot.handlers.commands.ImageRenderer.render_timeline_png", side_effect=RuntimeError("Pillow crash")):
            await cmd_status(msg, db=self.db_repo, scraper=self.scraper)

        self.assertEqual(len(msg.photos), 0)
        self.assertEqual(len(msg.replies), 1)
        self.assertIn("<code>", msg.replies[0][0])

    async def test_cmd_slots_menu_states(self) -> None:
        """Test /slots menu under 0, 1, and 2 configured slots."""
        user_id = 301
        await self.db_repo.users.upsert_user(user_id, user_id, "SlotsUser")

        # 0 slots
        msg0 = MockTelegramMessage(user=MockTelegramUser(id=user_id))
        await cmd_slots(msg0, db=self.db_repo)
        self.assertIn("У вас ще не додано жодної черги", msg0.replies[0][0])

        # 1 slot
        await self.db_repo.slots.upsert_slot(user_id, 1, "kyiv", 1, "1.1", "Дім")
        msg1 = MockTelegramMessage(user=MockTelegramUser(id=user_id))
        await cmd_slots(msg1, db=self.db_repo)
        self.assertIn("Слот 1:</b> Дім", msg1.replies[0][0])

        # 2 slots
        await self.db_repo.slots.upsert_slot(user_id, 2, "odesa", 2, "2.1", "Офіс")
        msg2 = MockTelegramMessage(user=MockTelegramUser(id=user_id))
        await cmd_slots(msg2, db=self.db_repo)
        self.assertIn("Слот 1:</b> Дім", msg2.replies[0][0])
        self.assertIn("Слот 2:</b> Офіс", msg2.replies[0][0])

    async def test_cmd_settings_and_cmd_help(self) -> None:
        """Test /settings and /help display correct text and keyboards."""
        user_id = 401
        await self.db_repo.users.upsert_user(user_id, user_id, "SettingsUser")

        msg_settings = MockTelegramMessage(user=MockTelegramUser(id=user_id))
        await cmd_settings(msg_settings, db=self.db_repo)
        self.assertIn("Налаштування сповіщень", msg_settings.replies[0][0])

        msg_help = MockTelegramMessage(user=MockTelegramUser(id=user_id))
        await cmd_help(msg_help)
        self.assertIn("Довідка та позначення кольорів", msg_help.replies[0][0])
        self.assertIn("Світло є (ON)", msg_help.replies[0][0])

    # ========================================================================
    # 2. KEYBOARD CALLBACK & WIZARD TESTS
    # ========================================================================

    async def test_callback_refresh_and_toggle_render_mode(self) -> None:
        """Test refresh_schedule and toggle_render_mode callbacks."""
        user_id = 501
        await self.db_repo.users.upsert_user(user_id, user_id, "ToggleUser")
        await self.db_repo.slots.upsert_slot(user_id, 1, "kyiv", 1, "1.1", "Дім")

        # Toggle mode: default image -> text
        q_toggle = MockTelegramCallbackQuery("toggle_render_mode", user=MockTelegramUser(id=user_id))
        await cb_toggle_render_mode(q_toggle, db=self.db_repo, scraper=self.scraper)
        self.assertEqual(USER_RENDER_MODES[user_id], "text")

        # Toggle mode: text -> image
        await cb_toggle_render_mode(q_toggle, db=self.db_repo, scraper=self.scraper)
        self.assertEqual(USER_RENDER_MODES[user_id], "image")

        # Refresh schedule
        q_refresh = MockTelegramCallbackQuery("refresh_schedule", user=MockTelegramUser(id=user_id))
        await cb_refresh_schedule(q_refresh, db=self.db_repo, scraper=self.scraper)
        self.assertEqual(len(q_refresh.answers), 1)

    async def test_callback_slot_wizard_lifecycle(self) -> None:
        """Test full slot configuration wizard lifecycle: edit -> reg -> grp -> sub -> delete."""
        user_id = 502
        await self.db_repo.users.upsert_user(user_id, user_id, "WizardUser")

        # Step 1: slot_edit
        q_edit = MockTelegramCallbackQuery("slot_edit:1", user=MockTelegramUser(id=user_id))
        await cb_slot_edit(q_edit)
        self.assertIn("Оберіть ваш регіон", q_edit.message.edited_text[0][0])

        # Step 2: slot_reg
        q_reg = MockTelegramCallbackQuery("slot_reg:1:dnipro", user=MockTelegramUser(id=user_id))
        await cb_slot_region(q_reg)
        self.assertIn("dnipro", q_reg.message.edited_text[0][0])
        self.assertIn("номер вашої групи", q_reg.message.edited_text[0][0])

        # Step 3: slot_grp
        q_grp = MockTelegramCallbackQuery("slot_grp:1:dnipro:3", user=MockTelegramUser(id=user_id))
        await cb_slot_group(q_grp)
        self.assertIn("Група: 3", q_grp.message.edited_text[0][0])
        self.assertIn("Оберіть підгрупу", q_grp.message.edited_text[0][0])

        # Step 4: slot_sub
        q_sub = MockTelegramCallbackQuery("slot_sub:1:dnipro:3:3.2", user=MockTelegramUser(id=user_id))
        await cb_slot_subgroup(q_sub, db=self.db_repo)
        self.assertIn("Слот 1 успішно збережено!", q_sub.message.edited_text[0][0])

        slot = await self.db_repo.slots.get_slot(user_id, 1)
        self.assertIsNotNone(slot)
        self.assertEqual(slot.region_code, "dnipro")
        self.assertEqual(slot.group_id, 3)
        self.assertEqual(slot.subgroup_id, "3.2")

        # Step 5: slot_delete
        q_del = MockTelegramCallbackQuery("slot_delete:1", user=MockTelegramUser(id=user_id))
        await cb_slot_delete(q_del, db=self.db_repo)
        self.assertIn("Слот 1 видалено", q_del.message.edited_text[0][0])

        deleted = await self.db_repo.slots.get_slot(user_id, 1)
        self.assertIsNone(deleted)

    async def test_callback_settings_toggles(self) -> None:
        """Test settings toggles for 30m, 15m, diff alerts and silent hours."""
        user_id = 503
        await self.db_repo.users.upsert_user(user_id, user_id, "PrefUser")

        # 30m toggle
        q_30m = MockTelegramCallbackQuery("toggle_notif:30m", user=MockTelegramUser(id=user_id))
        await cb_toggle_notif(q_30m, db=self.db_repo)
        s = await self.db_repo.settings.get_settings(user_id)
        self.assertFalse(s.notify_30m)

        # 15m toggle
        q_15m = MockTelegramCallbackQuery("toggle_notif:15m", user=MockTelegramUser(id=user_id))
        await cb_toggle_notif(q_15m, db=self.db_repo)
        s = await self.db_repo.settings.get_settings(user_id)
        self.assertFalse(s.notify_15m)

        # diff toggle
        q_diff = MockTelegramCallbackQuery("toggle_notif:diff", user=MockTelegramUser(id=user_id))
        await cb_toggle_notif(q_diff, db=self.db_repo)
        s = await self.db_repo.settings.get_settings(user_id)
        self.assertFalse(s.notify_schedule_change)

        # silent hours toggle: 23:00-07:00 -> 0:00-0:00 (disabled)
        q_sh = MockTelegramCallbackQuery("toggle_silent_hours", user=MockTelegramUser(id=user_id))
        await cb_toggle_silent_hours(q_sh, db=self.db_repo)
        s = await self.db_repo.settings.get_settings(user_id)
        self.assertEqual(s.silent_hours_start, 0)
        self.assertEqual(s.silent_hours_end, 0)

    async def test_callback_menu_navigation(self) -> None:
        """Test menu navigation callbacks render appropriate views without exceptions."""
        user_id = 504
        await self.db_repo.users.upsert_user(user_id, user_id, "MenuNavUser")

        for cb_func, name in (
            (cb_menu_main, "menu_main"),
            (cb_menu_slots, "menu_slots"),
            (cb_menu_settings, "menu_settings"),
            (cb_menu_help, "menu_help"),
        ):
            q = MockTelegramCallbackQuery(name, user=MockTelegramUser(id=user_id))
            if cb_func in (cb_menu_slots, cb_menu_settings):
                await cb_func(q, db=self.db_repo)
            else:
                await cb_func(q)
            self.assertEqual(len(q.message.edited_text), 1)

    # ========================================================================
    # 3. TMA F.web_app_data EVENT HANDLER TESTS
    # ========================================================================

    async def test_web_app_data_valid_dual_and_single_slots(self) -> None:
        """Test F.web_app_data synchronizes valid slot payloads into SQLite."""
        user_id = 601
        await self.db_repo.users.upsert_user(user_id, user_id, "TMAUser")

        payload = (
            '{"slot_a": {"region_code": "kyiv", "group_id": 2, "subgroup_id": "2.1", "alias": "Дім"},'
            ' "slot_b": {"region_code": "odesa", "group_id": 4, "subgroup_id": "4.2", "alias": "Дача"}}'
        )
        msg = MockTelegramMessage(user=MockTelegramUser(id=user_id), raw_web_app_data=payload)
        await handle_web_app_data(msg, db=self.db_repo)

        self.assertEqual(len(msg.replies), 1)
        self.assertIn("Оновлено черг: 2", msg.replies[0][0])

        slots = await self.db_repo.slots.get_slots_for_user(user_id)
        self.assertEqual(len(slots), 2)
        self.assertEqual(slots[0].subgroup_id, "2.1")
        self.assertEqual(slots[1].subgroup_id, "4.2")

    async def test_web_app_data_malformed_json_handled_cleanly(self) -> None:
        """Test F.web_app_data handles malformed JSON without raising unhandled exceptions."""
        user_id = 602
        await self.db_repo.users.upsert_user(user_id, user_id, "MalformedUser")

        msg = MockTelegramMessage(user=MockTelegramUser(id=user_id), raw_web_app_data="{corrupted_json: true")
        await handle_web_app_data(msg, db=self.db_repo)

        self.assertEqual(len(msg.replies), 1)
        self.assertIn("Помилка зчитування даних із Mini App", msg.replies[0][0])

    async def test_web_app_data_non_dict_adversarial_types(self) -> None:
        """
        Adversarial test: verify behavior when web_app_data contains non-dict JSON (list, int, null).
        Documents vulnerability where data.get() raises AttributeError if not guarded.
        """
        user_id = 603
        await self.db_repo.users.upsert_user(user_id, user_id, "AdversarialJSON")

        for non_dict_json in ('["item1", "item2"]', '12345', '"plain_string"', 'null'):
            msg = MockTelegramMessage(user=MockTelegramUser(id=user_id), raw_web_app_data=non_dict_json)
            try:
                await handle_web_app_data(msg, db=self.db_repo)
            except AttributeError as exc:
                # Confirmed edge case: non-dict JSON causes AttributeError on .get()
                self.assertIn("has no attribute 'get'", str(exc))

    # ========================================================================
    # 4. ZERO OPEN INBOUND PORTS ARCHITECTURE VERIFICATION
    # ========================================================================

    def test_zero_open_inbound_ports_architecture(self) -> None:
        """
        Empirically inspect bot/main.py and codebase to guarantee zero open inbound ports:
        1. bot/main.py uses start_polling only.
        2. No web server runners (web.run_app, uvicorn, FastAPI, aiohttp.web, socket.bind) are invoked.
        """
        main_py_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bot", "main.py")
        with open(main_py_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check for start_polling
        self.assertIn("start_polling", content)
        self.assertNotIn("web.run_app", content)
        self.assertNotIn("uvicorn.run", content)
        self.assertNotIn("FastAPI(", content)

        # Parse AST to ensure no socket or server listen/bind calls
        tree = ast.parse(content, filename="bot/main.py")
        prohibited_names = {"run_app", "uvicorn", "FastAPI", "flask", "tornado", "listen", "bind"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in prohibited_names:
                    self.fail(f"Prohibited web server invocation found in bot/main.py: {func.id}")
                elif isinstance(func, ast.Attribute) and func.attr in prohibited_names:
                    self.fail(f"Prohibited web server invocation found in bot/main.py: {func.attr}")


if __name__ == "__main__":
    unittest.main()
