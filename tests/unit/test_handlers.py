import hashlib
import time
import uuid
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

from aiogram.types import BotCommandScopeChat, Message

from flamenco_bot.commands import (
    get_bot_commands,
    get_client_commands,
    register_commands,
)
from flamenco_bot.database.repository import ClassSlot, LessonPayment
from flamenco_bot.handlers.admin import (
    admin_command,
    save_participant_name,
    save_participant_phone,
    search_participants,
    start_participant_search,
)
from flamenco_bot.handlers.core import (
    account_command,
    buy_command,
    cancel_command,
    help_command,
    lessons_command,
    schedule_command,
    start_command,
)
from flamenco_bot.handlers.fallback import fallback_message
from flamenco_bot.handlers.states import AccountForm, AdminForm, LessonForm
from flamenco_bot.handlers.support import start_support
from flamenco_bot.keyboards.user import (
    main_menu_keyboard,
    phone_request_keyboard,
)
from flamenco_bot.keyboards.user.account import (
    open_profile,
    request_name,
    request_phone,
    save_name,
    save_phone,
    verify_phone_code,
)
from flamenco_bot.keyboards.user.main_menu import (
    cancel_from_menu,
    ensure_profile,
    open_booking,
    open_legacy_section,
    open_my_classes,
    open_packages,
    open_purchases,
    show_about,
    show_main_menu,
)
from flamenco_bot.keyboards.user.screens import payment_confirmed_notifier
from flamenco_bot.keyboards.user.purchases import (
    check_lesson_payment,
    legacy_purchase_confirmation,
    pay_for_package,
    show_bill,
    show_package,
    show_packages,
)
from flamenco_bot.payments import ProviderPayment
from flamenco_bot.services.payments import PaymentCheckResult, PaymentCheckStatus
from tests.support import (
    FakeCallback,
    FakeMessage,
    FakeRepository,
    FakeState,
    callback_data,
    inline_buttons,
)


def provider_payment(payment_id, status, confirmation_url=None):
    return ProviderPayment(
        payment_id=payment_id,
        status=status,
        amount_minor=100000,
        currency="RUB",
        confirmation_url=confirmation_url,
        metadata={"telegram_id": "1001", "package_key": "single"},
    )


def reply_labels(markup):
    return {button.text for row in markup.keyboard for button in row}


class BotFunctionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.message = FakeMessage()
        self.repository = FakeRepository()
        self.state = FakeState()

    async def test_start_registers_and_shows_main_menu_with_summary(self):
        await start_command(self.message, self.repository, self.state)
        self.repository.get_or_create_profile.assert_awaited_once_with(
            telegram_id=1001,
            user_name="Анна",
        )
        self.state.clear.assert_awaited_once()
        text = self.message.last_answer.args[0]
        self.assertIn("студии фламенко Mirada Studio", text)
        self.assertIn("Баланс: 1 занятие", text)
        self.assertEqual(
            self.message.last_answer.kwargs["reply_markup"],
            main_menu_keyboard(),
        )

    async def test_main_menu_summary_shows_nearest_class(self):
        from flamenco_bot.database.repository import UserBooking

        starts_at = datetime.now(timezone.utc) + timedelta(days=2)
        self.repository.list_bookings_for_telegram_id.return_value = [
            UserBooking(
                id=1,
                slot_id=5,
                class_key="beginner",
                starts_at=starts_at,
                booking_status="confirmed",
                slot_status="open",
            )
        ]
        await show_main_menu(self.message, self.state, self.repository)
        self.assertIn(
            "Ближайшее занятие: ",
            self.message.last_answer.args[0],
        )
        self.assertIn("Фламенко для начинающих", self.message.last_answer.args[0])

    async def test_start_shows_admin_panel_from_database_profile(self):
        self.repository.profile = replace(
            self.repository.profile, is_admin=True, role="admin"
        )
        self.repository.get_or_create_profile.return_value = self.repository.profile

        await start_command(self.message, self.repository, self.state)

        buttons = reply_labels(self.message.last_answer.kwargs["reply_markup"])
        self.assertIn("🛠 Админ-меню", buttons)
        self.assertIn("📊 Состояние бота", buttons)

    async def test_help_shows_admin_commands_only_to_admins(self):
        await help_command(self.message, self.state, self.repository)
        text = self.message.last_answer.args[0]
        self.assertIn("📅 Расписание", text)
        self.assertIn("💳 Покупки", text)
        self.assertNotIn("/client", text)
        self.state.clear.assert_awaited_once()

        self.repository.profile = replace(
            self.repository.profile, is_admin=True, role="admin"
        )
        self.repository.get_profile.return_value = self.repository.profile
        await help_command(self.message, self.state, self.repository)
        self.assertIn("/client", self.message.last_answer.args[0])
        # Возврат оплаты по-прежнему — командой.
        self.assertIn("/refund", self.message.last_answer.args[0])

    async def test_account_command_opens_profile_without_service_fields(self):
        await account_command(self.message, self.repository, self.state)
        text = self.message.last_answer.args[0]
        for expected in ("Профиль", "Анна", "не указан", "Баланс: 0 занятий"):
            self.assertIn(expected, text)
        self.assertNotIn("Telegram ID", text)
        # Быстрые действия профиля — inline-кнопками в том же сообщении.
        self.assertTrue(
            {"prof:name", "prof:phone", "mypacks", "hist:o", "notif"}
            <= set(callback_data(self.message.last_answer.kwargs["reply_markup"]))
        )

    async def test_commands_open_sections(self):
        await lessons_command(self.message, self.repository, self.state)
        self.assertIn("Мои занятия", self.message.last_answer.args[0])

        await schedule_command(self.message, self.state, self.repository)
        self.assertIn("Ближайшие занятия", self.message.last_answer.args[0])
        self.assertIn("свободных занятий нет", self.message.last_answer.args[0])

        await buy_command(self.message, self.repository, self.state)
        self.assertIn("ЮKassa", self.message.last_answer.args[0])
        self.assertEqual(
            callback_data(self.message.last_answer.kwargs["reply_markup"]),
            [
                "pack:single:0",
                "pack:pack_4:0",
                "pack:pack_8:0",
                "purch:hist",
                "mypacks",
                "about",
            ],
        )

    async def test_cancel_clears_fsm(self):
        await cancel_command(self.message, self.state, self.repository)
        self.state.clear.assert_awaited_once()
        self.assertIn("Отменять нечего", self.message.last_answer.args[0])

    async def test_cancel_returns_to_section_where_input_started(self):
        await self.state.set_state(AccountForm.waiting_for_phone_code)
        await cancel_command(self.message, self.state, self.repository)
        # Профиль — inline-экран, поэтому после ввода возвращается главное меню.
        self.assertEqual(
            self.message.last_answer.kwargs["reply_markup"],
            main_menu_keyboard(),
        )
        self.assertIn("«👤 Профиль»", self.message.last_answer.args[0])

        await self.state.set_state(LessonForm.waiting_for_purchase_confirmation)
        await cancel_command(self.message, self.state, self.repository)
        self.assertEqual(
            self.message.last_answer.kwargs["reply_markup"],
            main_menu_keyboard(),
        )

        self.repository.profile = replace(
            self.repository.profile, is_admin=True, role="admin"
        )
        self.repository.get_profile.return_value = self.repository.profile
        await self.state.set_state(AdminForm.waiting_for_search)
        await cancel_command(self.message, self.state, self.repository)
        self.assertIn(
            "👥 Клиенты",
            reply_labels(self.message.last_answer.kwargs["reply_markup"]),
        )

    async def test_ensure_profile_uses_telegram_identity(self):
        profile = await ensure_profile(
            cast(Message, self.message),
            self.repository,
        )
        self.assertEqual(profile.telegram_id, 1001)

    async def test_all_main_menu_sections_answer(self):
        await show_main_menu(self.message, self.state, self.repository)
        await open_booking(self.message, self.state, self.repository)
        await open_my_classes(self.message, self.state, self.repository)
        await open_packages(self.message, self.state, self.repository)
        await open_purchases(self.message, self.state, self.repository)
        await show_about(self.message, self.state)
        await open_profile(self.message, self.state, self.repository)
        await start_support(self.message, self.state)
        await cancel_from_menu(self.message, self.state, self.repository)
        self.assertEqual(self.message.answer.await_count, 9)

    async def test_about_explains_directions_prices_and_rules(self):
        await show_about(self.message, self.state)
        text = self.message.last_answer.args[0]
        for expected in (
            "Mirada Studio",
            "Фламенко для начинающих",
            "Продолжающая группа",
            "Индивидуальное занятие",
            "1\u00a0000 ₽",
            "24 часа",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("формат", text.lower())
        self.assertNotIn("слот", text.lower())
        self.assertEqual(
            callback_data(self.message.last_answer.kwargs["reply_markup"]),
            ["slots:all", "packs:0"],
        )

    async def test_bot_links_to_website_when_configured(self):
        with patch(
            "flamenco_bot.presentation.Config.WEBSITE_URL", "https://studio.example"
        ):
            await show_about(self.message, self.state)
            self.assertIn("https://studio.example", self.message.last_answer.args[0])
            await open_profile(self.message, self.state, self.repository)
            self.assertIn(
                "Личный кабинет: https://studio.example",
                self.message.last_answer.args[0],
            )
        with patch("flamenco_bot.presentation.Config.WEBSITE_URL", None):
            await show_about(self.message, self.state)
            self.assertNotIn("https://", self.message.last_answer.args[0])

    async def test_phone_and_name_input_prompts(self):
        await request_phone(self.message, self.state)
        self.assertTrue(phone_request_keyboard().keyboard[0][0].request_contact)
        await request_name(self.message, self.state)
        self.assertIn("64 символов", self.message.last_answer.args[0])

    async def test_only_own_contact_can_change_phone(self):
        self.message.contact = SimpleNamespace(
            user_id=2002,
            phone_number="+79990000000",
        )
        await save_phone(self.message, self.state)
        self.repository.update_phone.assert_not_awaited()

        self.message.contact.user_id = 1001
        with (
            patch(
                "flamenco_bot.keyboards.user.account.secrets.randbelow",
                return_value=123456,
            ),
            patch("flamenco_bot.keyboards.user.account.time.time", return_value=100),
        ):
            await save_phone(self.message, self.state)
            self.repository.update_phone.assert_not_awaited()
            self.assertEqual(
                self.state.current_state,
                AccountForm.waiting_for_phone_code.state,
            )
            self.assertIn("123456", self.message.last_answer.args[0])
            self.assertEqual(
                self.state.data["phone_code_hash"],
                hashlib.sha256(b"123456").hexdigest(),
            )

            self.message.text = "000000"
            await verify_phone_code(self.message, self.state, self.repository)
            self.repository.update_phone.assert_not_awaited()

            self.message.text = "123456"
            await verify_phone_code(self.message, self.state, self.repository)
        self.repository.update_phone.assert_awaited_once_with(1001, "+79990000000")

    async def test_phone_code_expires_and_limits_attempts(self):
        await self.state.set_state(AccountForm.waiting_for_phone_code)
        self.state.data.update(
            phone_code_hash=hashlib.sha256(b"123456").hexdigest(),
            phone_code_expires_at=time.time() - 1,
            phone_code_attempts=0,
            pending_phone="+79990000000",
        )
        await verify_phone_code(self.message, self.state, self.repository)
        self.repository.update_phone.assert_not_awaited()
        self.assertIsNone(self.state.current_state)
        self.assertIn("истёк", self.message.last_answer.args[0])

        await self.state.set_state(AccountForm.waiting_for_phone_code)
        self.state.data.update(
            phone_code_hash=hashlib.sha256(b"123456").hexdigest(),
            phone_code_expires_at=time.time() + 60,
            phone_code_attempts=0,
            pending_phone="+79990000000",
        )
        self.message.text = "000000"
        for _ in range(5):
            await verify_phone_code(self.message, self.state, self.repository)

        self.repository.update_phone.assert_not_awaited()
        self.assertIsNone(self.state.current_state)
        self.assertIn("Лимит попыток", self.message.last_answer.args[0])

    async def test_name_validation_and_update(self):
        await request_name(self.message, self.state)
        self.message.text = "   "
        await save_name(self.message, self.state, self.repository)
        self.repository.update_user_name.assert_not_awaited()

        self.message.text = "я" * 65
        await save_name(self.message, self.state, self.repository)
        self.repository.update_user_name.assert_not_awaited()
        self.assertIn("от 1 до 64 символов", self.message.last_answer.args[0])

        self.message.text = "  Новое имя  "
        await save_name(self.message, self.state, self.repository)
        self.repository.update_user_name.assert_awaited_once_with(1001, "Новое имя")
        self.assertIn("Имя сохранено", self.message.last_answer.args[0])

    async def test_abandoned_name_input_does_not_rename(self):
        await request_name(self.message, self.state)
        self.state.data["name_input_started_at"] = time.time() - 16 * 60
        self.message.text = "привет"

        await save_name(self.message, self.state, self.repository)

        self.repository.update_user_name.assert_not_awaited()
        self.assertIsNone(self.state.current_state)
        self.assertIn("Ввод имени отменён", self.message.last_answer.args[0])

    async def test_fallback_is_clear_and_actionable(self):
        await fallback_message(self.message, self.repository)
        self.assertIn("/help", self.message.last_answer.args[0])
        self.assertIn("💬 Поддержка", self.message.last_answer.args[0])

    async def test_legacy_menu_buttons_open_new_sections_without_side_effects(self):
        self.message.text = "💳 Покупка занятий"
        await open_legacy_section(self.message, self.state, self.repository)
        first, second = self.message.answer.await_args_list[-2:]
        self.assertEqual(first.kwargs["reply_markup"], main_menu_keyboard())
        self.assertIn("Покупки", second.args[0])

        self.message.text = "💳 Абонементы"
        await open_legacy_section(self.message, self.state, self.repository)
        self.assertIn("🎟 Абонементы", self.message.last_answer.args[0])

        self.message.text = "📖 Мои занятия"
        await open_legacy_section(self.message, self.state, self.repository)
        self.assertIn("Мои занятия", self.message.last_answer.args[0])

        self.message.text = "Разовое занятие — 1000 ₽"
        await open_legacy_section(self.message, self.state, self.repository)
        self.assertIn(
            "pay:single:1000:0",
            callback_data(self.message.last_answer.kwargs["reply_markup"]),
        )
        self.repository.begin_lesson_payment_attempt.assert_not_awaited()

        self.message.text = "Продолжающая группа"
        await open_legacy_section(self.message, self.state, self.repository)
        self.assertEqual(
            self.repository.list_class_slots.await_args.kwargs["class_key"],
            "intermediate",
        )

        self.message.text = "👤 Учетная запись"
        await open_legacy_section(self.message, self.state, self.repository)
        self.assertIn("👤 Профиль", self.message.last_answer.args[0])
        self.assertIn(
            "prof:name",
            callback_data(self.message.last_answer.kwargs["reply_markup"]),
        )

    async def test_admin_commands_are_access_controlled(self):
        self.repository.profile = replace(
            self.repository.profile, is_admin=True, role="admin"
        )
        self.repository.get_profile.return_value = self.repository.profile
        await admin_command(self.message, self.state, self.repository)
        self.assertEqual(
            {
                button.text
                for row in self.message.last_answer.kwargs["reply_markup"].keyboard
                for button in row
            },
            {
                "🗓 Расписание занятий",
                "👥 Клиенты",
                "📨 Обращения поддержки",
                "💰 Финансы",
                "⚙️ Настройки",
                "🏠 Главное меню",
            },
        )
        # Преподаватель: только свои занятия и поддержка.
        self.repository.profile = replace(
            self.repository.profile, is_admin=False, role="teacher"
        )
        self.repository.get_profile.return_value = self.repository.profile
        await admin_command(self.message, self.state, self.repository)
        self.assertEqual(
            {
                button.text
                for row in self.message.last_answer.kwargs["reply_markup"].keyboard
                for button in row
            },
            {"🗓 Расписание занятий", "📨 Обращения поддержки", "🏠 Главное меню"},
        )
        self.repository.profile = replace(
            self.repository.profile, is_admin=False, role=None
        )
        self.repository.get_profile.return_value = self.repository.profile
        await admin_command(self.message, self.state, self.repository)
        self.assertIn("только сотрудникам", self.message.last_answer.args[0])

    async def test_admin_can_search_and_edit_participants(self):
        self.repository.profile = replace(
            self.repository.profile, is_admin=True, role="admin"
        )
        self.repository.get_profile.return_value = self.repository.profile
        with patch("flamenco_bot.handlers.admin.actions_logger") as audit_factory:
            await start_participant_search(self.message, self.state, self.repository)
            self.assertEqual(
                self.state.current_state, AdminForm.waiting_for_search.state
            )
            self.message.text = "Анна"
            await search_participants(self.message, self.state, self.repository)
            self.repository.search_profiles.assert_awaited_once_with("Анна", limit=20)
            self.assertIn("1001", self.message.last_answer.args[0])

            # Имя и телефон меняются из карточки участника (a:cn / a:cph).
            await self.state.set_state(AdminForm.waiting_for_name_value)
            await self.state.update_data(target_telegram_id=1001)
            self.message.text = "Новое имя"
            await save_participant_name(self.message, self.state, self.repository)
            # Правка — с записью в журнал действий в БД (staff_audit_log).
            self.repository.admin_update_profile.assert_awaited_with(
                1001, 1001, user_name="Новое имя"
            )

            await self.state.set_state(AdminForm.waiting_for_phone_value)
            await self.state.update_data(target_telegram_id=1001)
            self.message.text = "+79991234567"
            await save_participant_phone(self.message, self.state, self.repository)
            self.repository.admin_update_profile.assert_awaited_with(
                1001, 1001, phone="+79991234567"
            )
            self.assertEqual(audit_factory.return_value.info.call_count, 3)

    async def test_non_admin_cannot_search_or_edit_participants(self):
        await start_participant_search(self.message, self.state, self.repository)
        self.message.text = "Анна"
        await search_participants(self.message, self.state, self.repository)
        self.repository.search_profiles.assert_not_awaited()
        self.assertIsNone(self.state.current_state)

    async def test_command_registry_hides_admin_commands_from_clients(self):
        client = {command.command for command in get_client_commands()}
        self.assertEqual(
            client,
            {
                "start",
                "schedule",
                "lessons",
                "packages",
                "buy",
                "account",
                "about",
                "help",
                "cancel",
            },
        )
        full = {command.command for command in get_bot_commands()}
        admin_only = {"admin", "slots", "slot", "client", "ledger", "audit"}
        self.assertTrue(admin_only <= full)
        self.assertFalse(admin_only & client)

    async def test_register_commands_scopes_admin_menu_to_admin_chats(self):
        bot = SimpleNamespace(set_my_commands=AsyncMock())
        self.repository.list_staff.return_value = [
            SimpleNamespace(telegram_id=77, role="admin"),
            SimpleNamespace(telegram_id=88, role="teacher"),
        ]

        await register_commands(bot, self.repository, SimpleNamespace(warning=print))

        default_call, admin_call, teacher_call = bot.set_my_commands.await_args_list
        self.assertEqual(teacher_call.kwargs["scope"], BotCommandScopeChat(chat_id=88))
        teacher_commands = {c.command for c in teacher_call.args[0]}
        self.assertIn("slots", teacher_commands)
        self.assertNotIn("client", teacher_commands)
        self.assertNotIn("ledger", teacher_commands)
        self.assertEqual(
            {c.command for c in default_call.args[0]},
            {c.command for c in get_client_commands()},
        )
        self.assertEqual(admin_call.kwargs["scope"], BotCommandScopeChat(chat_id=77))
        self.assertIn("client", {c.command for c in admin_call.args[0]})


class PurchaseFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = FakeRepository()
        self.state = FakeState()
        self.gateway = SimpleNamespace(
            is_configured=True,
            create_payment=AsyncMock(
                return_value=provider_payment(
                    "provider-payment-1",
                    "pending",
                    "https://pay.example.test/confirm/1",
                )
            ),
            get_payment=AsyncMock(
                return_value=provider_payment("provider-payment-1", "succeeded")
            ),
        )

    async def test_package_screen_has_explicit_pay_button_and_no_fsm(self):
        callback = FakeCallback("pack:pack_4:0")

        await show_package(callback)

        text, markup = callback.screen
        self.assertIn("4 занятия за 3\u00a0600 ₽", text)
        pay_button = inline_buttons(markup)[0]
        self.assertEqual(pay_button.text, "Оплатить 3\u00a0600 ₽")
        self.assertEqual(pay_button.callback_data, "pay:pack_4:3600:0")
        self.assertEqual(callback_data(markup)[-1], "packs:0")

    async def test_packages_screen_clears_unfinished_text_input(self):
        await self.state.set_state(LessonForm.waiting_for_purchase_confirmation)
        callback = FakeCallback("packs:0")

        await show_packages(callback, self.repository, self.state)

        self.assertIsNone(self.state.current_state)
        text, _ = callback.screen
        self.assertIn("Баланс: 1 занятие", text)

    async def test_pay_button_creates_payment_and_check_confirms_credits(self):
        callback = FakeCallback("pay:single:1000:0")

        await pay_for_package(callback, self.repository, self.gateway)

        self.gateway.create_payment.assert_awaited_once()
        self.repository.create_lesson_payment.assert_awaited_once_with(
            telegram_id=1001,
            package_key="single",
            package_title="Разовое занятие",
            lessons=1,
            amount_minor=100000,
            provider_payment_id="provider-payment-1",
            confirmation_url="https://pay.example.test/confirm/1",
            idempotence_key=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        )
        text, markup = callback.screen
        self.assertIn("Счёт №8", text)
        self.assertIn("Проверить оплату", text)
        buttons = inline_buttons(markup)
        self.assertEqual(buttons[0].url, "https://pay.example.test/confirm/1")
        self.assertEqual(buttons[1].callback_data, "lesson_payment_check:8:0")

        self.repository.get_lesson_payment.return_value = LessonPayment(
            id=8,
            telegram_id=1001,
            package_key="single",
            lessons=1,
            amount_minor=100000,
            provider_payment_id="provider-payment-1",
            confirmation_url="https://pay.example.test/confirm/1",
            status="pending",
        )
        check = FakeCallback("lesson_payment_check:8:0", message=callback.message)
        await check_lesson_payment(check, self.repository, self.gateway)

        self.repository.complete_lesson_payment.assert_awaited_once_with(8, 1001)
        text = callback.message.last_answer.args[0]
        self.assertIn("Зачислено 1 занятие", text)
        self.assertIn("Баланс: 1 занятие", text)
        self.assertEqual(
            callback_data(callback.message.last_answer.kwargs["reply_markup"]),
            ["slots:all"],
        )

    async def test_after_payment_user_returns_to_selected_class(self):
        starts_at = datetime.now(timezone.utc) + timedelta(days=2)
        self.repository.list_class_slots.return_value = [
            ClassSlot(
                id=5,
                class_key="beginner",
                starts_at=starts_at,
                capacity=8,
                booked_count=2,
            )
        ]
        self.repository.get_lesson_payment.return_value = LessonPayment(
            id=8,
            telegram_id=1001,
            package_key="single",
            lessons=1,
            amount_minor=100000,
            provider_payment_id="provider-payment-1",
            confirmation_url="https://pay.example.test/confirm/1",
            status="pending",
        )
        check = FakeCallback("lesson_payment_check:8:5")

        await check_lesson_payment(check, self.repository, self.gateway)

        markup = check.message.last_answer.kwargs["reply_markup"]
        first = inline_buttons(markup)[0]
        self.assertTrue(first.text.startswith("Записаться на выбранное занятие"))
        self.assertEqual(first.callback_data, "book:beginner:5")
        # Автоматической записи после оплаты нет.
        self.repository.book_class_slot.assert_not_awaited()

    async def test_legacy_check_button_without_slot_still_works(self):
        self.repository.get_lesson_payment.return_value = LessonPayment(
            id=8,
            telegram_id=1001,
            package_key="single",
            lessons=1,
            amount_minor=100000,
            provider_payment_id="provider-payment-1",
            confirmation_url="",
            status="succeeded",
        )
        check = FakeCallback("lesson_payment_check:8")

        await check_lesson_payment(check, self.repository, self.gateway)

        self.assertIn("Оплата уже подтверждена", check.message.last_answer.args[0])

    async def test_stale_price_never_creates_payment(self):
        callback = FakeCallback("pay:single:900:0")

        await pay_for_package(callback, self.repository, self.gateway)

        self.repository.begin_lesson_payment_attempt.assert_not_awaited()
        text, markup = callback.screen
        self.assertIn("Цена изменилась", text)
        self.assertEqual(callback_data(markup)[0], "pay:single:1000:0")

    async def test_unavailable_checkout_explains_and_creates_nothing(self):
        self.gateway.is_configured = False
        callback = FakeCallback("pay:single:1000:0")

        await pay_for_package(callback, self.repository, self.gateway)

        self.repository.begin_lesson_payment_attempt.assert_not_awaited()
        text, _ = callback.screen
        self.assertIn("Оплата недоступна", text)

    async def test_checkout_records_payment_if_provider_omits_confirmation_url(self):
        self.gateway.create_payment = AsyncMock(
            return_value=provider_payment("provider-payment-2", "pending")
        )
        callback = FakeCallback("pay:single:1000:0")

        await pay_for_package(callback, self.repository, self.gateway)

        self.repository.create_lesson_payment.assert_awaited_once()
        self.assertEqual(
            self.repository.create_lesson_payment.await_args.kwargs["confirmation_url"],
            "",
        )
        text, _ = callback.screen
        self.assertIn("зарегистрирован", text)
        self.assertIn("безопасную ссылку", text)

    async def test_unpaid_bill_is_listed_and_can_be_reopened(self):
        self.repository.list_lesson_payments_for_telegram_id.return_value = [
            SimpleNamespace(
                id=8,
                package_key="pack_4",
                package_title="Абонемент на 4 занятия",
                lessons=4,
                amount_minor=360000,
                status="pending",
                created_at=datetime.now(timezone.utc),
            ),
            SimpleNamespace(
                id=7,
                package_key="single",
                package_title="Разовое занятие",
                lessons=1,
                amount_minor=100000,
                status="succeeded",
                created_at=datetime.now(timezone.utc),
            ),
        ]
        callback = FakeCallback("packs:0")
        await show_packages(callback, self.repository, self.state)

        text, markup = callback.screen
        self.assertIn("неоплаченный счёт", text)
        data = callback_data(markup)
        self.assertEqual(data[0], "bill:8:0")
        self.assertNotIn("bill:7:0", data)

        self.repository.get_lesson_payment.return_value = LessonPayment(
            id=8,
            telegram_id=1001,
            package_key="pack_4",
            lessons=4,
            amount_minor=360000,
            provider_payment_id="provider-payment-1",
            confirmation_url="https://pay.example.test/confirm/1",
            status="pending",
            package_title="Абонемент на 4 занятия",
        )
        bill = FakeCallback("bill:8:0")
        await show_bill(bill, self.repository)

        self.repository.get_lesson_payment.assert_awaited_once_with(8, 1001)
        text, markup = bill.screen
        self.assertIn("Счёт №8", text)
        buttons = inline_buttons(markup)
        self.assertEqual(buttons[0].url, "https://pay.example.test/confirm/1")
        self.assertEqual(buttons[1].callback_data, "lesson_payment_check:8:0")
        # Новый платёж при этом не создаётся.
        self.repository.begin_lesson_payment_attempt.assert_not_awaited()

    async def test_closed_bill_is_not_offered_again(self):
        self.repository.get_lesson_payment.return_value = None
        bill = FakeCallback("bill:8:0")

        await show_bill(bill, self.repository)

        self.assertIn("уже оплачен или закрыт", bill.answer.await_args.args[0])
        text, _ = bill.screen
        self.assertIn("Покупки", text)

    async def test_background_confirmation_message_shows_credits_and_next_step(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        notify = payment_confirmed_notifier(bot, self.repository)
        payment = LessonPayment(
            id=8,
            telegram_id=1001,
            package_key="pack_4",
            lessons=4,
            amount_minor=360000,
            provider_payment_id="provider-payment-1",
            confirmation_url="",
            status="succeeded",
        )

        await notify(
            1001,
            PaymentCheckResult(PaymentCheckStatus.CONFIRMED, payment, credits=5),
        )

        user_id, text = bot.send_message.await_args.args
        self.assertEqual(user_id, 1001)
        self.assertEqual(text, "Оплата прошла. Зачислено 4 занятия. Баланс: 5 занятий.")
        self.assertEqual(
            callback_data(bot.send_message.await_args.kwargs["reply_markup"]),
            ["slots:all"],
        )

    async def test_free_text_in_legacy_purchase_state_never_pays(self):
        message = FakeMessage(text="нет")
        await self.state.set_state(LessonForm.waiting_for_purchase_confirmation)

        await legacy_purchase_confirmation(message, self.state)

        self.repository.begin_lesson_payment_attempt.assert_not_awaited()
        self.gateway.create_payment.assert_not_awaited()
        self.assertIsNone(self.state.current_state)
        self.assertIn("кнопку «Оплатить»", message.last_answer.args[0])


if __name__ == "__main__":
    unittest.main()
