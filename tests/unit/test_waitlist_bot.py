"""Лист ожидания в боте: кнопка 🔔 у заполненного занятия, очередь и выход."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from flamenco_bot.keyboards.user import lessons
from flamenco_bot.keyboards.user.screens import my_classes_screen, slots_screen
from tests.pg import PostgresTestCase, make_admin, set_credits
from tests.support import FakeCallback, callback_data

ADMIN_ID = 77
USER_ID = 1001
OTHER_ID = 1002


class WaitlistBotTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await make_admin(self.repository, ADMIN_ID)
        for telegram_id in (USER_ID, OTHER_ID):
            await self.repository.get_or_create_profile(telegram_id, "Участник")
            await set_credits(self.repository, telegram_id, 1)
        self.slot = await self.repository.create_class_slot(
            "beginner", datetime.now(timezone.utc) + timedelta(days=2), 1, ADMIN_ID
        )
        await self.repository.book_class_slot(self.slot.id, OTHER_ID)

    def press(self, data):
        callback = FakeCallback(data, telegram_id=USER_ID)
        callback.bot = SimpleNamespace(send_message=AsyncMock())
        return callback

    async def test_full_slot_offers_waitlist_and_join_leave(self):
        text, markup = await slots_screen(self.repository, USER_ID)
        self.assertIn("wl:{}".format(self.slot.id), callback_data(markup))
        self.assertIn("лист ожидания", text)

        join = self.press("wl:{}".format(self.slot.id))
        await lessons.join_waitlist(join, self.repository)
        self.assertIn("Лист ожидания", join.screen[0])
        self.assertIn("В очереди: 1", join.screen[0])

        text, markup = await my_classes_screen(self.repository, USER_ID)
        self.assertIn("🔔 Лист ожидания", text)
        self.assertIn("wlx:{}".format(self.slot.id), callback_data(markup))

        leave = self.press("wlx:{}".format(self.slot.id))
        await lessons.leave_waitlist(leave, self.repository)
        self.assertEqual(await self.repository.list_user_waitlist(USER_ID), [])

    async def test_join_without_credits_offers_packages(self):
        await set_credits(self.repository, USER_ID, 0)
        join = self.press("wl:{}".format(self.slot.id))
        await lessons.join_waitlist(join, self.repository)
        self.assertIn("с занятием на балансе", join.screen[0])
