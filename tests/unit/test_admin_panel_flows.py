"""Админ-панель: сверка баланса, вместимость занятия, команда /slot, правка
профиля и выдача абонемента — на настоящем PostgreSQL.

Проверяются и итог в БД (баланс, ledger, число мест), а не только текст
ответа: отказ не должен оставлять побочных эффектов.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from flamenco_bot.handlers import admin_panel
from flamenco_bot.handlers.states import AdminForm
from tests.pg import (
    PostgresTestCase,
    fetchval,
    make_admin,
    set_credits,
    sql,
)
from tests.support import FakeCallback, FakeMessage, FakeState, callback_data

ADMIN_ID = 77
TEACHER_ID = 55
USER_ID = 1001


class AdminFlowTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await make_admin(self.repository, ADMIN_ID)
        await self.repository.get_or_create_profile(USER_ID, "Анна")
        self.state = FakeState()
        patcher = patch("flamenco_bot.handlers.admin_panel.actions_logger")
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _slot(self, capacity=3, teacher=None):
        return await self.repository.create_class_slot(
            "beginner",
            datetime.now(timezone.utc) + timedelta(days=3),
            capacity,
            ADMIN_ID,
            teacher_telegram_id=teacher,
        )

    async def _capacity(self, slot_id):
        return await fetchval(
            self.repository, "SELECT capacity FROM lesson_slots WHERE id = $1", slot_id
        )

    # --- сверка баланса ---------------------------------------------------

    async def test_reconciliation_reports_a_clean_ledger(self):
        await set_credits(self.repository, USER_ID, 3)
        callback = FakeCallback("a:fr", telegram_id=ADMIN_ID)
        await admin_panel.show_reconciliation(callback, self.repository)
        self.assertIn("расхождений нет", callback.screen[0])

    async def test_reconciliation_finds_balance_not_backed_by_ledger_without_fixing_it(
        self,
    ):
        await set_credits(self.repository, USER_ID, 3)
        # Баланс правят в обход журнала — классическая ошибка ручной правки.
        await sql(
            self.repository,
            "UPDATE bot_users SET lesson_credits = 9 WHERE telegram_id = $1",
            USER_ID,
        )
        callback = FakeCallback("a:fr", telegram_id=ADMIN_ID)
        await admin_panel.show_reconciliation(callback, self.repository)
        text = callback.screen[0]
        self.assertIn("расхождения у 1", text)
        self.assertIn(str(USER_ID), text)
        self.assertIn("автоматически не исправляется", text)
        self.assertEqual(
            await fetchval(
                self.repository,
                "SELECT lesson_credits FROM bot_users WHERE telegram_id = $1",
                USER_ID,
            ),
            9,
        )

    async def test_reconciliation_is_admin_only(self):
        callback = FakeCallback("a:fr", telegram_id=USER_ID)
        await admin_panel.show_reconciliation(callback, self.repository)
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        callback.message.edit_text.assert_not_awaited()

    # --- вместимость ------------------------------------------------------

    async def _start_capacity(self, slot_id, telegram_id=ADMIN_ID):
        callback = FakeCallback("a:scap:{}".format(slot_id), telegram_id=telegram_id)
        await admin_panel.start_capacity_change(callback, self.repository, self.state)
        return callback

    async def _enter_capacity(self, text, telegram_id=ADMIN_ID):
        message = FakeMessage(text=text, telegram_id=telegram_id)
        await admin_panel.save_capacity_change(message, self.state, self.repository)
        return message

    async def test_capacity_change_updates_the_slot(self):
        slot = await self._slot(capacity=3)
        callback = await self._start_capacity(slot.id)
        self.assertEqual(
            self.state.current_state, AdminForm.waiting_for_capacity_change.state
        )
        callback.message.answer.assert_awaited_once()

        message = await self._enter_capacity("8")
        self.assertEqual(await self._capacity(slot.id), 8)
        self.assertIn("Вместимость обновлена", message.last_answer.args[0])
        self.assertIsNone(self.state.current_state)

    async def test_non_numeric_capacity_keeps_the_form_open(self):
        slot = await self._slot(capacity=3)
        await self._start_capacity(slot.id)
        message = await self._enter_capacity("много")
        self.assertIn("число от 1 до 100", message.last_answer.args[0])
        self.assertEqual(
            self.state.current_state, AdminForm.waiting_for_capacity_change.state
        )
        self.assertEqual(await self._capacity(slot.id), 3)

    async def test_capacity_below_booked_count_is_rejected(self):
        slot = await self._slot(capacity=3)
        for telegram_id in (2001, 2002):
            await self.repository.get_or_create_profile(telegram_id, "Участник")
            await set_credits(self.repository, telegram_id, 1)
            await self.repository.book_class_slot(slot.id, telegram_id)
        await self._start_capacity(slot.id)
        message = await self._enter_capacity("1")
        self.assertEqual(await self._capacity(slot.id), 3)
        self.assertNotIn("обновлена", message.last_answer.args[0])

    async def test_out_of_range_capacity_never_changes_the_slot(self):
        slot = await self._slot(capacity=3)
        await self._start_capacity(slot.id)
        for value in ("0", "-2", "101", "1000000"):
            await self._enter_capacity(value)
            self.assertEqual(await self._capacity(slot.id), 3, value)

    async def test_teacher_cannot_change_capacity_of_someone_elses_slot(self):
        await self.repository.get_or_create_profile(TEACHER_ID, "Мария")
        await self.repository.set_staff_role(TEACHER_ID, "teacher", ADMIN_ID)
        foreign = await self._slot(capacity=3)
        callback = await self._start_capacity(foreign.id, telegram_id=TEACHER_ID)
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertIsNone(self.state.current_state)
        self.assertEqual(await self._capacity(foreign.id), 3)

    async def test_ordinary_user_cannot_change_capacity(self):
        slot = await self._slot(capacity=3)
        callback = await self._start_capacity(slot.id, telegram_id=USER_ID)
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertIsNone(self.state.current_state)

    async def test_capacity_form_is_dropped_if_rights_disappear_midway(self):
        slot = await self._slot(capacity=3)
        await self._start_capacity(slot.id)
        await self._enter_capacity("5", telegram_id=USER_ID)
        self.assertEqual(await self._capacity(slot.id), 3)
        self.assertIsNone(self.state.current_state)

    # --- команда /slot ----------------------------------------------------

    async def test_slot_command_opens_the_card_or_explains_format(self):
        slot = await self._slot()
        message = FakeMessage(text="/slot {}".format(slot.id), telegram_id=ADMIN_ID)
        await admin_panel.open_slot_command(message, self.repository)
        self.assertIn(str(slot.id), message.last_answer.args[0])

        for text in ("/slot", "/slot abc", "/slot 99999"):
            message = FakeMessage(text=text, telegram_id=ADMIN_ID)
            await admin_panel.open_slot_command(message, self.repository)
            self.assertIn("Формат: /slot ID", message.last_answer.args[0])

    async def test_slot_command_respects_teacher_ownership_and_rights(self):
        await self.repository.get_or_create_profile(TEACHER_ID, "Мария")
        await self.repository.set_staff_role(TEACHER_ID, "teacher", ADMIN_ID)
        foreign = await self._slot()
        message = FakeMessage(
            text="/slot {}".format(foreign.id), telegram_id=TEACHER_ID
        )
        await admin_panel.open_slot_command(message, self.repository)
        self.assertIn("другой преподаватель", message.last_answer.args[0])

        message = FakeMessage(text="/slot 1", telegram_id=USER_ID)
        await admin_panel.open_slot_command(message, self.repository)
        self.assertIn("только сотрудникам", message.last_answer.args[0])

    # --- правка профиля из карточки --------------------------------------

    async def test_profile_edit_buttons_open_the_matching_form(self):
        for data, state, hint in (
            ("a:cn:{}".format(USER_ID), AdminForm.waiting_for_name_value, "имя"),
            ("a:cph:{}".format(USER_ID), AdminForm.waiting_for_phone_value, "телефон"),
        ):
            with self.subTest(data=data):
                callback = FakeCallback(data, telegram_id=ADMIN_ID)
                await admin_panel.start_client_profile_edit(
                    callback, self.repository, self.state
                )
                self.assertEqual(self.state.current_state, state.state)
                self.assertEqual(self.state.data["target_telegram_id"], USER_ID)
                self.assertIn(hint, callback.message.answer.await_args.args[0])

    async def test_profile_edit_is_admin_only(self):
        callback = FakeCallback("a:cn:{}".format(USER_ID), telegram_id=USER_ID)
        await admin_panel.start_client_profile_edit(
            callback, self.repository, self.state
        )
        self.assertIsNone(self.state.current_state)
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])

    # --- выдача абонемента ------------------------------------------------

    async def test_grant_menu_lists_every_package_and_a_way_back(self):
        callback = FakeCallback("a:cg:{}".format(USER_ID), telegram_id=ADMIN_ID)
        await admin_panel.choose_grant_package(callback, self.repository)
        data = callback_data(callback.screen[1])
        self.assertTrue(any(d.startswith("a:cgp:{}:".format(USER_ID)) for d in data))
        self.assertIn("a:c:{}".format(USER_ID), data)

    async def test_copy_week_menu_offers_this_and_next_week(self):
        callback = FakeCallback("a:cw", telegram_id=ADMIN_ID)
        await admin_panel.choose_copy_source_week(callback, self.repository)
        data = callback_data(callback.screen[1])
        self.assertEqual(
            [d for d in data if d.startswith("a:cw:")], ["a:cw:0", "a:cw:1"]
        )

    async def test_open_tickets_screen_lists_tickets_with_open_buttons(self):
        await self.repository.create_support_message(USER_ID, "Не могу записаться")
        text, markup = await admin_panel.tickets_screen(self.repository)
        self.assertIn("Открытые обращения", text)
        data = callback_data(markup)
        self.assertEqual(len(data), 1)
        self.assertTrue(data[0].startswith("a:t:"))

    async def test_empty_tickets_screen(self):
        text, markup = await admin_panel.tickets_screen(self.repository)
        self.assertIn("Открытых обращений нет", text)
        self.assertEqual(callback_data(markup), [])


if __name__ == "__main__":
    unittest.main()
