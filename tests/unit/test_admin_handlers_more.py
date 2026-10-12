"""Админ-команды: возвраты, поиск и правка участника, «Состояние бота».

Работают на подменённом репозитории: проверяются ответы администратору,
права и то, что при отказе данные не меняются.
"""

import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from flamenco_bot.handlers import admin
from flamenco_bot.handlers.states import AdminForm
from flamenco_bot.services import RefundStatus
from flamenco_bot.services.payments import RefundResult
from tests.support import FakeMessage, FakeRepository, FakeState

ADMIN_ID = 1001


def make_repository(admin_role=True):
    repository = FakeRepository()
    repository.profile = replace(
        repository.profile, is_admin=admin_role, role="admin" if admin_role else None
    )
    repository.get_profile.return_value = repository.profile
    return repository


class BaseAdminTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = make_repository()
        self.message = FakeMessage(telegram_id=ADMIN_ID)
        self.state = FakeState()
        log = patch("flamenco_bot.handlers.admin.actions_logger")
        self.log = log.start().return_value
        self.addCleanup(log.stop)

    @property
    def reply(self):
        return self.message.last_answer.args[0]


class RefundCommandTests(BaseAdminTest):
    async def _refund(self, text, status, **fields):
        self.message.text = text
        result = RefundResult(status, **fields)
        gateway = SimpleNamespace()
        with patch(
            "flamenco_bot.handlers.admin.PaymentService.process_refund",
            AsyncMock(return_value=result),
        ) as process:
            await admin.refund_lesson_payment(self.message, self.repository, gateway)
        return process

    async def test_non_admin_cannot_refund(self):
        self.repository = make_repository(admin_role=False)
        self.repository.get_profile.return_value = None
        process = await self._refund("/refund 5 причина", RefundStatus.COMPLETED)
        self.assertIn("только администратору", self.reply)
        process.assert_not_awaited()

    async def test_bad_format_is_rejected_without_calling_the_provider(self):
        for text in ("/refund", "/refund 5", "/refund abc причина", "/refund 5   "):
            with self.subTest(text=text):
                process = await self._refund(text, RefundStatus.COMPLETED)
                self.assertIn("Формат команды", self.reply)
                process.assert_not_awaited()

    async def test_every_refund_outcome_has_its_own_message(self):
        cases = {
            RefundStatus.INVALID: (
                {"error_message": "Причина слишком длинная"},
                "Причина слишком длинная",
            ),
            RefundStatus.UNAVAILABLE: ({}, "Возврат недоступен"),
            RefundStatus.PROVIDER_UNAVAILABLE: ({}, "/refund_check 5"),
            RefundStatus.MISMATCH: ({}, "не совпали"),
            RefundStatus.COMPLETED: (
                {"lessons": 3, "provider_refund_id": "r1"},
                "3 неиспользованных",
            ),
            RefundStatus.ALREADY_PROCESSED: ({}, "уже обработана"),
            RefundStatus.PENDING: ({}, "ещё обрабатывается"),
            RefundStatus.RELEASED: ({}, "Резерв занятий снят"),
            RefundStatus.UNRESOLVED: ({"provider_status": "weird"}, "weird"),
        }
        self.assertEqual(set(cases), set(RefundStatus))
        for status, (fields, expected) in cases.items():
            with self.subTest(status=status):
                await self._refund("/refund 5 причина", status, **fields)
                self.assertIn(expected, self.reply)

    async def test_only_a_completed_refund_is_logged_as_a_refund(self):
        await self._refund("/refund 5 причина", RefundStatus.PENDING)
        self.log.warning.assert_not_called()
        await self._refund(
            "/refund 5 причина",
            RefundStatus.COMPLETED,
            lessons=1,
            provider_refund_id="r1",
        )
        self.log.warning.assert_called_once()
        self.assertIn("refund", self.log.warning.call_args.args[0])

    async def test_reason_is_passed_to_the_service_for_refund_but_not_for_check(self):
        process = await self._refund("/refund 5 дубль оплаты", RefundStatus.PENDING)
        process.assert_awaited_once_with(5, ADMIN_ID, "дубль оплаты")

        self.message.text = "/refund_check 5"
        with patch(
            "flamenco_bot.handlers.admin.PaymentService.process_refund",
            AsyncMock(return_value=RefundResult(RefundStatus.PENDING)),
        ) as process:
            await admin.check_lesson_refund(self.message, self.repository, object())
        process.assert_awaited_once_with(5, ADMIN_ID, None)

    async def test_refund_check_validates_format_and_rights(self):
        for text in ("/refund_check", "/refund_check x", "/refund_check 1 2"):
            self.message.text = text
            await admin.check_lesson_refund(self.message, self.repository, object())
            self.assertIn("Формат команды", self.reply)
        self.repository.get_profile.return_value = None
        self.message.text = "/refund_check 5"
        await admin.check_lesson_refund(self.message, self.repository, object())
        self.assertIn("только администратору", self.reply)


class ParticipantSearchAndEditTests(BaseAdminTest):
    async def test_search_requires_admin_and_resets_state(self):
        self.repository.get_profile.return_value = None
        self.message.text = "Анна"
        await admin.search_participants(self.message, self.state, self.repository)
        self.assertIn("только администратору", self.reply)
        self.repository.search_profiles.assert_not_awaited()

    async def test_search_query_length_is_bounded(self):
        for text in ("", "   ", "я" * 101):
            self.message.text = text
            await admin.search_participants(self.message, self.state, self.repository)
            self.assertIn("от 1 до 100", self.reply)
        self.repository.search_profiles.assert_not_awaited()

    async def test_search_without_results_leaves_admin_menu(self):
        self.repository.search_profiles.return_value = []
        self.message.text = "никто"
        await self.state.set_state(AdminForm.waiting_for_search)
        await admin.search_participants(self.message, self.state, self.repository)
        self.assertIn("не найдены", self.reply)
        self.assertIsNone(self.state.current_state)
        self.assertIn("results=0", self.log.info.call_args.args[0])

    async def test_search_lists_participants_with_card_buttons(self):
        self.message.text = "Анна"
        await admin.search_participants(self.message, self.state, self.repository)
        self.assertIn("ID: 1001", self.reply)
        self.assertIn("не указан", self.reply)
        buttons = [
            b
            for row in self.message.last_answer.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertEqual([b.callback_data for b in buttons], ["a:c:1001"])

    async def test_search_query_is_not_logged_only_its_length(self):
        self.message.text = "+79991234567"
        await admin.search_participants(self.message, self.state, self.repository)
        logged = " ".join(str(a) for a in self.log.info.call_args.args)
        self.assertNotIn("79991234567", logged)

    async def test_name_edit_validates_and_updates(self):
        await self.state.update_data(target_telegram_id=2002)
        for bad in ("   ", "я" * 200):
            self.message.text = bad
            await admin.save_participant_name(self.message, self.state, self.repository)
            self.assertIn("от 1 до", self.reply)
        self.repository.admin_update_profile.assert_not_awaited()

        self.message.text = "  Мария Иванова "
        await admin.save_participant_name(self.message, self.state, self.repository)
        self.repository.admin_update_profile.assert_awaited_once_with(
            2002, ADMIN_ID, user_name="Мария Иванова"
        )
        self.assertIn("обновлено", self.reply)
        self.assertIsNone(self.state.current_state)

    async def test_name_edit_for_missing_target_changes_nothing(self):
        self.message.text = "Мария"
        await admin.save_participant_name(self.message, self.state, self.repository)
        self.assertIn("Не удалось определить", self.reply)

        await self.state.update_data(target_telegram_id=2002)

        async def only_admin(telegram_id):
            return self.repository.profile if telegram_id == ADMIN_ID else None

        self.repository.get_profile.side_effect = only_admin
        await admin.save_participant_name(self.message, self.state, self.repository)
        self.assertIn("не найден", self.reply)
        self.repository.admin_update_profile.assert_not_awaited()

    async def test_phone_edit_validates_and_updates(self):
        await self.state.update_data(target_telegram_id=2002)
        for bad in ("не телефон", "123", "+7" + "9" * 40):
            self.message.text = bad
            await admin.save_participant_phone(
                self.message, self.state, self.repository
            )
            self.assertIn("Введите номер телефона", self.reply)
        self.repository.admin_update_profile.assert_not_awaited()

        self.message.text = "+7 999 123-45-67"
        await admin.save_participant_phone(self.message, self.state, self.repository)
        self.repository.admin_update_profile.assert_awaited_once()
        self.assertEqual(
            self.repository.admin_update_profile.await_args.kwargs["phone"],
            "+7 999 123-45-67",
        )
        self.assertIn("обновлён", self.reply)

    async def test_edits_are_denied_to_non_admins(self):
        self.repository.get_profile.return_value = None
        await self.state.update_data(target_telegram_id=2002)
        self.message.text = "Мария"
        await admin.save_participant_name(self.message, self.state, self.repository)
        await admin.save_participant_phone(self.message, self.state, self.repository)
        self.repository.admin_update_profile.assert_not_awaited()


class ExtendedBotStatusTests(BaseAdminTest):
    def setUp(self):
        super().setUp()
        self.controller = SimpleNamespace(
            started_at=time.monotonic() - (2 * 86400 + 3 * 3600 + 4 * 60 + 5),
            polling_started=True,
        )
        self.message.bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username="studio_bot", id=42))
        )

    async def _status(self):
        with patch(
            "flamenco_bot.handlers.admin.get_process_resources",
            return_value=(10.0, 1.0),
        ):
            await admin.show_bot_status(self.message, self.controller, self.repository)
        return self.reply

    async def test_uptime_includes_days_as_hours(self):
        text = await self._status()
        self.assertIn("Время работы: 51 ч 4 мин", text)

    async def test_status_reports_runtime_database_and_bot_identity(self):
        text = await self._status()
        for fragment in (
            "Запущен:",
            "приём обновлений идёт",
            "Задач asyncio:",
            "Потоков:",
            "База данных: отвечает за",
            "Python ",
            "PID ",
            "@studio_bot (id 42)",
        ):
            self.assertIn(fragment, text)

    async def test_status_before_polling_starts(self):
        self.controller.polling_started = False
        self.assertIn("ещё не начат", await self._status())

    async def test_status_survives_telegram_failure(self):
        self.message.bot.get_me.side_effect = RuntimeError("network down")
        text = await self._status()
        self.assertIn("Бот работает", text)
        self.assertNotIn("Telegram:", text)

    async def test_status_without_bot_object_still_answers(self):
        self.message.bot = None
        self.assertIn("Бот работает", await self._status())

    async def test_database_failure_is_not_hidden(self):
        self.repository.get_user_statistics.side_effect = RuntimeError("db down")
        with (
            patch(
                "flamenco_bot.handlers.admin.get_process_resources",
                return_value=(10.0, 1.0),
            ),
            self.assertRaises(RuntimeError),
        ):
            await admin.show_bot_status(self.message, self.controller, self.repository)


if __name__ == "__main__":
    unittest.main()
