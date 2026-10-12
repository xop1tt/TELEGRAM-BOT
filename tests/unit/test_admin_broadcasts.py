"""Рассылки в боте (handlers/admin_broadcasts.py) на настоящем PostgreSQL."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from flamenco_bot.handlers import admin_broadcasts, admin_panel
from flamenco_bot.handlers.states import AdminForm
from tests.pg import PostgresTestCase, fetch_all, make_admin, set_credits
from tests.support import FakeCallback, FakeMessage, FakeState, callback_data

ADMIN_ID = 77
TEACHER_ID = 55
ANNA = 1001
BORIS = 1002
CLARA = 1003


class AdminBroadcastTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await make_admin(self.repository, ADMIN_ID)
        for telegram_id, name, credits in (
            (ANNA, "Анна", 2),
            (BORIS, "Борис", 0),
            (CLARA, "Клара", 1),
        ):
            await self.repository.get_or_create_profile(telegram_id, name)
            await set_credits(self.repository, telegram_id, credits)
        await self.repository.get_or_create_profile(TEACHER_ID, "Мария")
        await self.repository.set_staff_role(TEACHER_ID, "teacher", ADMIN_ID)
        self.state = FakeState()
        patcher = patch("flamenco_bot.handlers.admin_broadcasts.actions_logger")
        patcher.start()
        self.addCleanup(patcher.stop)

    def press(self, data, telegram_id=ADMIN_ID):
        return FakeCallback(data, telegram_id=telegram_id)

    def say(self, text, telegram_id=ADMIN_ID):
        return FakeMessage(text=text, telegram_id=telegram_id)

    async def _slot(self, teacher=None, days=3):
        return await self.repository.create_class_slot(
            "beginner",
            datetime.now(timezone.utc) + timedelta(days=days),
            5,
            ADMIN_ID,
            teacher_telegram_id=teacher,
        )

    async def _announcements(self):
        rows = await fetch_all(
            self.repository,
            "SELECT telegram_id, payload FROM user_notifications "
            "WHERE kind = 'announcement' ORDER BY telegram_id",
        )
        return [row["telegram_id"] for row in rows]

    # --- права -------------------------------------------------------------

    async def test_every_broadcast_callback_rejects_non_admin(self):
        cases = (
            ("b:m", admin_broadcasts.show_menu, True),
            ("b:h", admin_broadcasts.show_history, False),
            ("b:n", admin_broadcasts.choose_segment, True),
            ("b:s:all", admin_broadcasts.choose_segment_details, True),
            ("b:wc:0", admin_broadcasts.choose_inactive_days, True),
            ("b:dr:beginner", admin_broadcasts.choose_direction, False),
            ("b:dd:beginner:30", admin_broadcasts.choose_direction_days, True),
        )
        for data, handler, with_state in cases:
            with self.subTest(data=data):
                callback = self.press(data, telegram_id=ANNA)
                if with_state:
                    await handler(callback, self.repository, self.state)
                else:
                    await handler(callback, self.repository)
                self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
                callback.message.edit_text.assert_not_awaited()
        self.assertEqual(await self._announcements(), [])

    async def test_menu_command_is_admin_only(self):
        message = self.say("/broadcast", telegram_id=TEACHER_ID)
        await admin_broadcasts.open_broadcasts(message, self.state, self.repository)
        self.assertIn("только администратору", message.last_answer.args[0])
        message = self.say("/broadcast")
        await admin_broadcasts.open_broadcasts(message, self.state, self.repository)
        self.assertIn("b:n", callback_data(message.last_answer.kwargs["reply_markup"]))

    # --- сегменты ----------------------------------------------------------

    async def test_broadcast_to_everyone(self):
        await admin_broadcasts.choose_segment_details(
            self.press("b:s:all"), self.repository, self.state
        )
        self.assertEqual(self.state.current_state, AdminForm.waiting_for_broadcast_text)

        preview = self.say("Завтра студия закрыта")
        await admin_broadcasts.save_broadcast_text(preview, self.state, self.repository)
        text = preview.last_answer.args[0]
        self.assertIn("Получателей: 5", text)  # админ, преподаватель и три участника
        self.assertIn("Завтра студия закрыта", text)
        self.assertEqual(await self._announcements(), [])

        confirm = self.press("b:ok")
        await admin_broadcasts.confirm_broadcast(confirm, self.repository, self.state)
        self.assertIn("поставлена в очередь", confirm.screen[0])
        self.assertEqual(
            await self._announcements(),
            sorted([TEACHER_ID, ADMIN_ID, ANNA, BORIS, CLARA]),
        )

    async def test_broadcast_with_credits_and_inactive_days(self):
        await admin_broadcasts.choose_inactive_days(
            self.press("b:wc:0"), self.repository, self.state
        )
        message = self.say("Напоминаем про занятия")
        await admin_broadcasts.save_broadcast_text(message, self.state, self.repository)
        self.assertIn("Получателей: 2", message.last_answer.args[0])
        await admin_broadcasts.confirm_broadcast(
            self.press("b:ok"), self.repository, self.state
        )
        self.assertEqual(await self._announcements(), [ANNA, CLARA])

    async def test_broadcast_to_slot_participants_from_admin_menu(self):
        slot = await self._slot()
        await self.repository.book_class_slot(slot.id, ANNA)
        listing = self.press("b:s:sl")
        await admin_broadcasts.choose_segment_details(
            listing, self.repository, self.state
        )
        self.assertIn("b:sl:{}".format(slot.id), callback_data(listing.screen[1]))
        await admin_broadcasts.choose_slot(
            self.press("b:sl:{}".format(slot.id)), self.repository, self.state
        )
        message = self.say("Принесите обувь")
        await admin_broadcasts.save_broadcast_text(message, self.state, self.repository)
        self.assertIn("Получателей: 1", message.last_answer.args[0])
        await admin_broadcasts.confirm_broadcast(
            self.press("b:ok"), self.repository, self.state
        )
        self.assertEqual(await self._announcements(), [ANNA])

    async def test_broadcast_by_direction(self):
        slot = await self._slot()
        await self.repository.book_class_slot(slot.id, CLARA)
        await admin_broadcasts.choose_direction_days(
            self.press("b:dd:beginner:30"), self.repository, self.state
        )
        message = self.say("Новая группа")
        await admin_broadcasts.save_broadcast_text(message, self.state, self.repository)
        self.assertIn("Получателей: 1", message.last_answer.args[0])
        self.assertIn("Фламенко для начинающих", message.last_answer.args[0])

    # --- проверки ------------------------------------------------------------

    async def test_empty_and_too_long_text_is_refused(self):
        await admin_broadcasts.choose_segment_details(
            self.press("b:s:all"), self.repository, self.state
        )
        for bad in ("   ", "я" * 2001):
            with self.subTest(length=len(bad)):
                message = self.say(bad)
                await admin_broadcasts.save_broadcast_text(
                    message, self.state, self.repository
                )
                self.assertIn("от 1 до 2000", message.last_answer.args[0])
        self.assertEqual(await self._announcements(), [])

    async def test_empty_segment_cannot_be_sent(self):
        await admin_broadcasts.choose_direction_days(
            self.press("b:dd:beginner:30"), self.repository, self.state
        )
        message = self.say("Никому")
        await admin_broadcasts.save_broadcast_text(message, self.state, self.repository)
        self.assertIn("Получателей: 0", message.last_answer.args[0])
        data = callback_data(message.last_answer.kwargs["reply_markup"])
        self.assertNotIn("b:ok", data)

    async def test_confirmation_expires(self):
        callback = self.press("b:ok")
        await admin_broadcasts.confirm_broadcast(callback, self.repository, self.state)
        self.assertIn("устарело", callback.answer.await_args.args[0])
        self.assertEqual(await self._announcements(), [])

    async def test_repeated_confirmation_does_not_duplicate(self):
        await admin_broadcasts.choose_segment_details(
            self.press("b:s:all"), self.repository, self.state
        )
        await admin_broadcasts.save_broadcast_text(
            self.say("Один раз"), self.state, self.repository
        )
        await admin_broadcasts.confirm_broadcast(
            self.press("b:ok"), self.repository, self.state
        )
        again = self.press("b:ok")
        await admin_broadcasts.confirm_broadcast(again, self.repository, self.state)
        self.assertIn("устарело", again.answer.await_args.args[0])
        self.assertEqual(len(await self.repository.list_broadcasts()), 1)

    # --- преподаватель --------------------------------------------------------

    async def test_teacher_writes_only_to_own_slot_participants(self):
        own = await self._slot(teacher=TEACHER_ID)
        other = await self._slot(days=4)
        await self.repository.book_class_slot(own.id, ANNA)
        await self.repository.book_class_slot(other.id, CLARA)

        card = self.press("a:s:{}".format(own.id), telegram_id=TEACHER_ID)
        await admin_panel.show_slot(card, self.repository)
        self.assertIn("b:t:{}".format(own.id), callback_data(card.screen[1]))

        foreign = self.press("b:t:{}".format(other.id), telegram_id=TEACHER_ID)
        await admin_broadcasts.write_to_slot_participants(
            foreign, self.repository, self.state
        )
        foreign.answer.assert_awaited_with(
            "Это занятие ведёт другой преподаватель.", show_alert=True
        )

        mine = self.press("b:t:{}".format(own.id), telegram_id=TEACHER_ID)
        await admin_broadcasts.write_to_slot_participants(
            mine, self.repository, self.state
        )
        message = self.say("Сегодня без каблуков", telegram_id=TEACHER_ID)
        await admin_broadcasts.save_broadcast_text(message, self.state, self.repository)
        self.assertIn("Получателей: 1", message.last_answer.args[0])
        confirm = self.press("b:ok", telegram_id=TEACHER_ID)
        await admin_broadcasts.confirm_broadcast(confirm, self.repository, self.state)
        self.assertIn("поставлена в очередь", confirm.screen[0])
        self.assertEqual(await self._announcements(), [ANNA])

    async def test_teacher_cannot_send_other_segments(self):
        await self.state.set_state(None)
        await self.state.update_data(
            admin_pending={
                "action": "broadcast",
                "segment": "all",
                "params": {},
                "body": "Всем",
                "expires_at": 9_999_999_999,
            }
        )
        callback = self.press("b:ok", telegram_id=TEACHER_ID)
        await admin_broadcasts.confirm_broadcast(callback, self.repository, self.state)
        self.assertIn(
            "только участникам своего занятия", callback.answer.await_args.args[0]
        )
        self.assertEqual(await self._announcements(), [])

    # --- история --------------------------------------------------------------

    async def test_history_lists_broadcasts_with_counts(self):
        empty = self.press("b:h")
        await admin_broadcasts.show_history(empty, self.repository)
        self.assertIn("Рассылок пока не было", empty.screen[0])

        await self.repository.create_broadcast(ADMIN_ID, "Привет, студия", "all", {})
        screen = self.press("b:h")
        await admin_broadcasts.show_history(screen, self.repository)
        self.assertIn("Привет, студия", screen.screen[0])
        self.assertIn("получателей 5", screen.screen[0])
        self.assertIn("все участники", screen.screen[0])


if __name__ == "__main__":
    unittest.main()
