"""Админ-панель в боте поверх настоящей логики (PostgreSQL).

Проверяется: права на каждое нажатие, экраны подтверждения опасных
действий (и их срок), отмена занятия с возвратом и уведомлениями,
перенос, создание, закрытие/открытие, выдача и отзыв абонемента,
корректировка баланса из карточки, ответы в поддержке, финансы.
"""

import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from flamenco_bot.handlers import admin_panel
from flamenco_bot.handlers.states import AdminForm
from flamenco_bot.studio_time import to_studio_time
from tests.pg import PostgresTestCase, fetchval, make_admin, set_credits
from tests.support import FakeCallback, FakeMessage, FakeState, callback_data

ADMIN_ID = 77
USER_ID = 1001
TEACHER_ID = 55


class AdminPanelTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await make_admin(self.repository, ADMIN_ID)
        await self.repository.get_or_create_profile(USER_ID, "Анна")
        self.state = FakeState()
        self.logger_patch = patch("flamenco_bot.handlers.admin_panel.actions_logger")
        self.actions = self.logger_patch.start()

    async def asyncTearDown(self):
        self.logger_patch.stop()
        await super().asyncTearDown()

    def press(self, data, telegram_id=ADMIN_ID):
        return FakeCallback(data, telegram_id=telegram_id)

    async def _slot(self, days=3, capacity=5):
        return await self.repository.create_class_slot(
            "beginner", datetime.now(timezone.utc) + timedelta(days=days), capacity, 77
        )

    async def _book(self, slot_id, telegram_id=USER_ID, credits=1):
        profile = await self.repository.get_or_create_profile(telegram_id, "Участник")
        await set_credits(
            self.repository, telegram_id, profile.lesson_credits + credits
        )
        return await self.repository.book_class_slot(slot_id, telegram_id)

    # --- права -------------------------------------------------------------

    async def test_every_admin_callback_rejects_non_admin(self):
        slot = await self._slot()
        with_state = {
            admin_panel.show_schedule,
            admin_panel.request_slot_cancel,
            admin_panel.confirm_slot_cancel,
            admin_panel.start_reschedule,
            admin_panel.show_client,
            admin_panel.confirm_grant,
        }
        cases = (
            ("a:sch", admin_panel.show_schedule),
            ("a:s:{}".format(slot.id), admin_panel.show_slot),
            ("a:sx:{}".format(slot.id), admin_panel.request_slot_cancel),
            ("a:sxok:{}".format(slot.id), admin_panel.confirm_slot_cancel),
            ("a:sm:{}".format(slot.id), admin_panel.start_reschedule),
            ("a:sc:{}".format(slot.id), admin_panel.toggle_slot),
            ("a:c:{}".format(USER_ID), admin_panel.show_client),
            ("a:cgok", admin_panel.confirm_grant),
            ("a:fin", admin_panel.show_finance),
            ("a:fa", admin_panel.show_audit),
            ("a:fl", admin_panel.show_ledger),
        )
        for data, handler in cases:
            with self.subTest(data=data):
                callback = self.press(data, telegram_id=USER_ID)
                if handler in with_state:
                    await handler(callback, self.repository, self.state)
                else:
                    await handler(callback, self.repository)
                text = callback.answer.await_args.args[0]
                self.assertRegex(text, "^Доступно только (администратору|сотрудникам)")
                self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
                callback.message.edit_text.assert_not_awaited()
        self.assertEqual((await self.repository.get_class_slot(slot.id)).status, "open")

    async def test_admin_text_commands_require_admin(self):
        message = FakeMessage(text="/slots", telegram_id=USER_ID)
        await admin_panel.open_schedule(message, self.state, self.repository)
        self.assertIn("только сотрудникам", message.last_answer.args[0])

    # --- преподаватель --------------------------------------------------------

    async def _teacher(self, telegram_id=TEACHER_ID):
        await self.repository.get_or_create_profile(telegram_id, "Мария")
        await self.repository.set_staff_role(telegram_id, "teacher", ADMIN_ID)

    async def test_teacher_sees_and_manages_only_own_slots(self):
        await self._teacher()
        own = await self.repository.create_class_slot(
            "beginner",
            datetime.now(timezone.utc) + timedelta(days=2),
            5,
            ADMIN_ID,
            teacher_telegram_id=TEACHER_ID,
        )
        other = await self._slot(days=3)

        schedule = self.press("a:sch", telegram_id=TEACHER_ID)
        await admin_panel.show_schedule(schedule, self.repository, self.state)
        text, markup = schedule.screen
        data = callback_data(markup)
        self.assertIn("Мои занятия", text)
        self.assertIn("a:s:{}".format(own.id), data)
        self.assertNotIn("a:s:{}".format(other.id), data)
        self.assertNotIn("a:new", data)

        foreign = self.press("a:sc:{}".format(other.id), telegram_id=TEACHER_ID)
        await admin_panel.toggle_slot(foreign, self.repository)
        foreign.answer.assert_awaited_with(
            "Это занятие ведёт другой преподаватель.", show_alert=True
        )
        self.assertEqual(
            (await self.repository.get_class_slot(other.id)).status, "open"
        )

        mine = self.press("a:sc:{}".format(own.id), telegram_id=TEACHER_ID)
        await admin_panel.toggle_slot(mine, self.repository)
        self.assertEqual(
            (await self.repository.get_class_slot(own.id)).status, "closed"
        )
        self.assertIn("Преподаватель: Мария", mine.screen[0])

    async def test_teacher_has_no_client_cards_or_finance(self):
        await self._teacher()
        for data, handler in (
            ("a:c:{}".format(USER_ID), admin_panel.show_client),
            ("a:fin", admin_panel.show_finance),
            ("a:new", admin_panel.choose_new_slot_class),
        ):
            with self.subTest(data=data):
                callback = self.press(data, telegram_id=TEACHER_ID)
                if handler is admin_panel.show_client:
                    await handler(callback, self.repository, self.state)
                else:
                    await handler(callback, self.repository)
                callback.answer.assert_awaited_with(
                    "Доступно только администратору студии.", show_alert=True
                )

    # --- расписание и отмена ---------------------------------------------

    async def test_schedule_lists_slots_with_status_marks(self):
        open_slot = await self._slot(days=2)
        cancelled = await self._slot(days=3)
        await self.repository.cancel_class_slot(cancelled.id, ADMIN_ID)
        message = FakeMessage(text="🗓 Расписание занятий", telegram_id=ADMIN_ID)

        await admin_panel.open_schedule(message, self.state, self.repository)

        markup = message.last_answer.kwargs["reply_markup"]
        data = callback_data(markup)
        self.assertIn("a:s:{}".format(open_slot.id), data)
        self.assertIn("a:s:{}".format(cancelled.id), data)
        self.assertIn("a:new", data)
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertTrue(any(label.startswith("❌") for label in labels))

    async def test_cancel_slot_requires_confirmation_and_refunds_once(self):
        slot = await self._slot()
        await self._book(slot.id)

        first = self.press("a:sx:{}".format(slot.id))
        await admin_panel.request_slot_cancel(first, self.repository, self.state)
        text, markup = first.screen
        self.assertIn("Отменить занятие?", text)
        self.assertIn("Каждому вернётся 1 занятие", text)
        self.assertEqual((await self.repository.get_class_slot(slot.id)).status, "open")

        confirm = self.press("a:sxok:{}".format(slot.id))
        await admin_panel.confirm_slot_cancel(confirm, self.repository, self.state)

        text, _ = confirm.screen
        self.assertIn("Возвращено занятий: 1", text)
        self.assertEqual(
            (await self.repository.get_class_slot(slot.id)).status, "cancelled"
        )
        self.assertEqual(await self.repository.get_lesson_credits(USER_ID), 1)
        notices = await self.repository.list_notifications(USER_ID)
        kinds = [n.kind for n in notices]
        self.assertEqual(kinds.count("slot_cancelled"), 1)
        self.actions.return_value.warning.assert_called()

        # Повторное нажатие той же кнопки ничего не начисляет.
        again = self.press("a:sxok:{}".format(slot.id))
        await admin_panel.confirm_slot_cancel(again, self.repository, self.state)
        self.assertEqual(await self.repository.get_lesson_credits(USER_ID), 1)

    async def test_cancel_with_reason_shows_reason_to_participants(self):
        slot = await self._slot()
        await self._book(slot.id)
        ask = self.press("a:sxr:{}".format(slot.id))
        await admin_panel.ask_cancel_reason(ask, self.repository, self.state)
        self.assertEqual(
            self.state.current_state, AdminForm.waiting_for_cancel_reason.state
        )

        message = FakeMessage(text="Преподаватель заболел", telegram_id=ADMIN_ID)
        await admin_panel.save_cancel_reason(message, self.state, self.repository)
        self.assertIn("Причина: Преподаватель заболел", message.last_answer.args[0])

        confirm = self.press("a:sxok:{}".format(slot.id))
        await admin_panel.confirm_slot_cancel(confirm, self.repository, self.state)
        notice = (await self.repository.list_notifications(USER_ID))[0]
        self.assertEqual(notice.payload["reason"], "Преподаватель заболел")

    async def test_expired_confirmation_does_nothing(self):
        slot = await self._slot()
        await admin_panel.request_slot_cancel(
            self.press("a:sx:{}".format(slot.id)), self.repository, self.state
        )
        self.state.data["admin_pending"]["expires_at"] = time.time() - 1

        confirm = self.press("a:sxok:{}".format(slot.id))
        await admin_panel.confirm_slot_cancel(confirm, self.repository, self.state)

        self.assertIn("устарело", confirm.answer.await_args.args[0])
        self.assertEqual((await self.repository.get_class_slot(slot.id)).status, "open")

    async def test_confirmation_for_other_slot_is_not_reused(self):
        first, second = await self._slot(days=2), await self._slot(days=3)
        await admin_panel.request_slot_cancel(
            self.press("a:sx:{}".format(first.id)), self.repository, self.state
        )
        confirm = self.press("a:sxok:{}".format(second.id))
        await admin_panel.confirm_slot_cancel(confirm, self.repository, self.state)
        self.assertEqual(
            (await self.repository.get_class_slot(second.id)).status, "open"
        )

    # --- перенос, создание, закрытие ---------------------------------------

    async def test_reschedule_flow_notifies_participants(self):
        slot = await self._slot(days=3)
        await self._book(slot.id)
        await admin_panel.start_reschedule(
            self.press("a:sm:{}".format(slot.id)), self.repository, self.state
        )
        new_local = to_studio_time(datetime.now(timezone.utc) + timedelta(days=5))
        message = FakeMessage(
            text=new_local.strftime("%d.%m.%Y 20:30"), telegram_id=ADMIN_ID
        )

        await admin_panel.save_reschedule_time(message, self.state, self.repository)
        self.assertIn("Перенести занятие?", message.last_answer.args[0])

        confirm = self.press("a:smok:{}".format(slot.id))
        await admin_panel.confirm_reschedule(confirm, self.repository, self.state)

        self.assertIn("Уведомлений участникам: 1", confirm.screen[0])
        moved = await self.repository.get_class_slot(slot.id)
        self.assertEqual(to_studio_time(moved.starts_at).strftime("%H:%M"), "20:30")
        kinds = [n.kind for n in await self.repository.list_notifications(USER_ID)]
        self.assertEqual(kinds.count("slot_rescheduled"), 1)

    async def test_reschedule_rejects_past_time(self):
        slot = await self._slot()
        await admin_panel.start_reschedule(
            self.press("a:sm:{}".format(slot.id)), self.repository, self.state
        )
        message = FakeMessage(text="01.01.2020 10:00", telegram_id=ADMIN_ID)
        await admin_panel.save_reschedule_time(message, self.state, self.repository)
        self.assertIn("в будущем", message.last_answer.args[0])

    async def test_create_slot_flow(self):
        await admin_panel.start_new_slot(
            self.press("a:new:intermediate"), self.repository, self.state
        )
        when = to_studio_time(datetime.now(timezone.utc) + timedelta(days=4))
        await admin_panel.save_new_slot_time(
            FakeMessage(text=when.strftime("%d.%m.%Y 19:00"), telegram_id=ADMIN_ID),
            self.state,
            self.repository,
        )
        capacity = FakeMessage(text="8", telegram_id=ADMIN_ID)
        await admin_panel.save_new_slot_capacity(capacity, self.state, self.repository)
        self.assertIn("Создать занятие?", capacity.last_answer.args[0])

        confirm = self.press("a:newok")
        await admin_panel.confirm_new_slot(confirm, self.repository, self.state)

        self.assertIn("Занятие создано", confirm.screen[0])
        created = await self.repository.get_class_slot(
            await fetchval(self.repository, "SELECT MAX(id) FROM lesson_slots")
        )
        self.assertEqual((created.class_key, created.capacity), ("intermediate", 8))

    async def test_close_and_reopen(self):
        slot = await self._slot()
        await admin_panel.toggle_slot(
            self.press("a:sc:{}".format(slot.id)), self.repository
        )
        self.assertEqual(
            (await self.repository.get_class_slot(slot.id)).status, "closed"
        )
        reopen = self.press("a:so:{}".format(slot.id))
        await admin_panel.toggle_slot(reopen, self.repository)
        self.assertEqual((await self.repository.get_class_slot(slot.id)).status, "open")
        self.assertIn("снова открыта", reopen.screen[0])

    # --- клиенты, абонементы, баланс --------------------------------------

    async def test_client_card_and_grant_flow(self):
        callback = self.press("a:c:{}".format(USER_ID))
        await admin_panel.show_client(callback, self.repository, self.state)
        text, markup = callback.screen
        self.assertIn("Анна", text)
        self.assertIn("a:cg:{}".format(USER_ID), callback_data(markup))

        await admin_panel.ask_grant_reason(
            self.press("a:cgp:{}:pack_4".format(USER_ID)), self.repository, self.state
        )
        reason = FakeMessage(text="оплата наличными", telegram_id=ADMIN_ID)
        await admin_panel.save_grant_reason(reason, self.state, self.repository)
        self.assertIn("Баланс: 0 → 4", reason.last_answer.args[0])

        confirm = self.press("a:cgok")
        await admin_panel.confirm_grant(confirm, self.repository, self.state)
        self.assertIn("Абонемент выдан", confirm.screen[0])
        self.assertEqual(await self.repository.get_lesson_credits(USER_ID), 4)

        # Повторное нажатие после успеха: подтверждение уже использовано.
        again = self.press("a:cgok")
        await admin_panel.confirm_grant(again, self.repository, self.state)
        self.assertEqual(await self.repository.get_lesson_credits(USER_ID), 4)
        self.assertIn("устарело", again.answer.await_args.args[0])

    async def test_grant_retry_after_failure_reuses_idempotence_key(self):
        await admin_panel.ask_grant_reason(
            self.press("a:cgp:{}:single".format(USER_ID)), self.repository, self.state
        )
        await admin_panel.save_grant_reason(
            FakeMessage(text="наличные", telegram_id=ADMIN_ID),
            self.state,
            self.repository,
        )
        original = self.repository.grant_lesson_package

        async def grant_then_fail(*args, **kwargs):
            await original(*args, **kwargs)
            raise ConnectionError("БД пропала после commit")

        self.repository.grant_lesson_package = grant_then_fail
        with self.assertRaises(ConnectionError):
            await admin_panel.confirm_grant(
                self.press("a:cgok"), self.repository, self.state
            )
        self.repository.grant_lesson_package = original

        retry = self.press("a:cgok")
        await admin_panel.confirm_grant(retry, self.repository, self.state)
        self.assertEqual(await self.repository.get_lesson_credits(USER_ID), 1)
        self.assertIn("уже была выполнена", retry.screen[0])

    async def test_revoke_grant_flow(self):
        result = await self.repository.grant_lesson_package(
            USER_ID,
            "pack_4",
            "Абонемент",
            4,
            "выдача",
            ADMIN_ID,
            __import__("uuid").uuid4(),
        )
        await admin_panel.ask_revoke_reason(
            self.press("a:gr:{}".format(result.grant.id)), self.repository, self.state
        )
        message = FakeMessage(text="ошибочная выдача", telegram_id=ADMIN_ID)
        await admin_panel.save_revoke_reason(message, self.state, self.repository)
        self.assertIn("неиспользованных занятий: 4", message.last_answer.args[0])

        confirm = self.press("a:grok")
        await admin_panel.confirm_revoke(confirm, self.repository, self.state)
        self.assertEqual(await self.repository.get_lesson_credits(USER_ID), 0)
        self.assertIn("Абонемент отозван", confirm.screen[0])

    async def test_client_adjustment_reuses_confirmed_adjustment_flow(self):
        await admin_panel.start_client_adjustment(
            self.press("a:ca:{}".format(USER_ID)), self.repository, self.state
        )
        message = FakeMessage(text="+2 компенсация", telegram_id=ADMIN_ID)
        await admin_panel.save_client_adjustment(message, self.state, self.repository)
        self.assertEqual(
            self.state.current_state,
            AdminForm.waiting_for_credit_adjustment_confirmation.state,
        )
        self.assertIn("Баланс: 0 → 2", message.last_answer.args[0])

        bad = FakeMessage(text="два занятия", telegram_id=ADMIN_ID)
        await admin_panel.start_client_adjustment(
            self.press("a:ca:{}".format(USER_ID)), self.repository, self.state
        )
        await admin_panel.save_client_adjustment(bad, self.state, self.repository)
        self.assertIn("Формат", bad.last_answer.args[0])

    async def test_client_history_screens(self):
        slot = await self._slot()
        await self._book(slot.id)
        for data, handler, expected in (
            ("a:cb:{}:0".format(USER_ID), admin_panel.show_client_bookings, "Записан"),
            ("a:co:{}".format(USER_ID), admin_panel.show_client_operations, "Запись"),
            ("a:cp:{}".format(USER_ID), admin_panel.show_client_packages, "Баланс"),
            ("a:cpay:{}".format(USER_ID), admin_panel.show_client_payments, "Покупки"),
            ("a:ct:{}".format(USER_ID), admin_panel.show_client_tickets, "Обращения"),
        ):
            with self.subTest(data=data):
                callback = self.press(data)
                await handler(callback, self.repository)
                self.assertIn(expected, callback.screen[0])

    # --- поддержка и финансы ---------------------------------------------

    async def test_ticket_reply_close_and_reopen(self):
        ticket_id, _ = await self.repository.create_support_message(USER_ID, "Вопрос")
        view = self.press("a:t:{}".format(ticket_id))
        await admin_panel.show_ticket(view, self.repository)
        self.assertIn("Вопрос", view.screen[0])

        await admin_panel.start_ticket_reply(
            self.press("a:tr:{}".format(ticket_id)), self.repository, self.state
        )
        reply = FakeMessage(text="Да, можно", telegram_id=ADMIN_ID)
        reply.bot = SimpleNamespace(send_message=AsyncMock())
        await admin_panel.save_ticket_reply(reply, self.state, self.repository)
        reply.bot.send_message.assert_awaited_once()
        self.assertIn("доставлен", reply.last_answer.args[0])

        close = self.press("a:tc:{}".format(ticket_id))
        await admin_panel.close_ticket(close, self.repository)
        self.assertIn("закрыто", close.screen[0])
        reopen = self.press("a:to:{}".format(ticket_id))
        await admin_panel.reopen_ticket(reopen, self.repository)
        self.assertIn("снова открыто", reopen.screen[0])

    async def test_finance_screens(self):
        await self.repository.grant_lesson_package(
            USER_ID,
            "single",
            "Разовое",
            1,
            "подарок",
            ADMIN_ID,
            __import__("uuid").uuid4(),
        )
        for data, handler, expected in (
            ("a:fin", admin_panel.show_finance, "Финансы"),
            ("a:fl", admin_panel.show_ledger, "Абонемент от студии"),
            ("a:fa", admin_panel.show_audit, "выдача абонемента"),
        ):
            with self.subTest(data=data):
                callback = self.press(data)
                await handler(callback, self.repository)
                self.assertIn(expected, callback.screen[0])


if __name__ == "__main__":
    unittest.main()
