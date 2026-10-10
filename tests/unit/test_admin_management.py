import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from flamenco_bot.handlers.admin import show_bot_status
from flamenco_bot.keyboards.admin import ADMIN_MENU, BOT_STATUS
from flamenco_bot.keyboards.user import main_menu_keyboard
from tests.support import FakeMessage, FakeRepository


class BotStatusTests(unittest.IsolatedAsyncioTestCase):
    """«📊 Состояние бота» — только просмотр; перезапуска из чата больше нет:
    процесс перезапускает платформа (supervisor и Render)."""

    def setUp(self):
        self.message = FakeMessage(telegram_id=1001)
        self.repository = FakeRepository()
        self.repository.profile = replace(
            self.repository.profile, is_admin=True, role="admin"
        )
        self.repository.get_profile.return_value = self.repository.profile
        self.controller = SimpleNamespace(started_at=time.monotonic() - 61)

    async def test_status_shows_uptime_resources_and_users(self):
        with patch(
            "flamenco_bot.handlers.admin.get_process_resources",
            return_value=(42.5, 3.25),
        ):
            await show_bot_status(self.message, self.controller, self.repository)
        text = self.message.last_answer.args[0]
        self.assertIn("Бот работает", text)
        self.assertIn("0 ч 1 мин", text)
        self.assertIn("CPU с запуска: 3.2 сек", text)
        self.assertIn("Пиковая память: 42.5 МБ", text)
        self.assertIn("Пользователей всего: 10", text)
        self.assertIn("Активны за 5 минут: 2", text)
        self.assertNotIn("перезапуск", text.lower())
        self.repository.get_user_statistics.assert_awaited_once()

    async def test_status_is_admin_only(self):
        self.message.from_user.id = 1002
        self.repository.get_profile.return_value = None
        await show_bot_status(self.message, self.controller, self.repository)

        self.assertIn("только администратору", self.message.last_answer.args[0])
        self.repository.get_user_statistics.assert_not_awaited()

    async def test_status_is_rejected_outside_private_chat(self):
        self.message.chat.type = "group"
        await show_bot_status(self.message, self.controller, self.repository)

        self.assertIn("только администратору", self.message.last_answer.args[0])

    def test_admin_main_menu_has_admin_menu_and_status(self):
        labels = {
            button.text
            for row in main_menu_keyboard(role="admin").keyboard
            for button in row
        }
        self.assertIn(ADMIN_MENU, labels)
        self.assertIn(BOT_STATUS, labels)


if __name__ == "__main__":
    unittest.main()
