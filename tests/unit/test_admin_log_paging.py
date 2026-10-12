"""Прокрутка «Последние операции» и «Журнал операций» в админ-панели.

На странице по 7 записей, каждая — отдельным блоком; кнопки «Новее» и
«Старее» ведут по смещению. Данные — настоящие выдачи абонементов в тестовом
PostgreSQL: каждая выдача пишет и запись ledger, и событие журнала.
"""

import unittest
import uuid
from unittest.mock import patch

from flamenco_bot.handlers import admin_panel
from tests.pg import PostgresTestCase, make_admin
from tests.support import FakeCallback, callback_data

ADMIN_ID = 77
USER_ID = 1001
TOTAL = 9


class LogPagingTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await make_admin(self.repository, ADMIN_ID)
        await self.repository.get_or_create_profile(USER_ID, "Анна")
        for index in range(TOTAL):
            await self.repository.grant_lesson_package(
                USER_ID,
                "single",
                "Разовое",
                1,
                "подарок {}".format(index + 1),
                ADMIN_ID,
                uuid.uuid4(),
            )
        patcher = patch("flamenco_bot.handlers.admin_panel.actions_logger")
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _open(self, data, handler):
        callback = FakeCallback(data, telegram_id=ADMIN_ID)
        await handler(callback, self.repository)
        return callback.screen

    @staticmethod
    def _entries(text):
        return [line for line in text.splitlines() if line.startswith("• ")]

    async def test_operations_first_page_has_seven_two_line_entries(self):
        text, markup = await self._open("a:fl", admin_panel.show_ledger)
        entries = self._entries(text)
        self.assertEqual(len(entries), 7)
        # Детали — на второй строке с отступом, не на той же строке.
        for entry in entries:
            index = text.index(entry)
            following = text[index + len(entry) :].split("\n")
            self.assertEqual(following[0], "", entry)
            self.assertTrue(following[1].startswith("   "), entry)
        # Новейшая операция — первой.
        self.assertIn("подарок 9", text.split("• ")[1])
        data = callback_data(markup)
        self.assertIn("a:fl:7", data)
        self.assertNotIn("a:fl:0", data)
        self.assertIn("a:fin", data)

    async def test_operations_second_page_shows_the_rest_and_a_way_back(self):
        text, markup = await self._open("a:fl:7", admin_panel.show_ledger)
        self.assertEqual(len(self._entries(text)), TOTAL - 7)
        self.assertIn("подарок 1", text)
        self.assertNotIn("подарок 9", text)
        data = callback_data(markup)
        self.assertIn("a:fl:0", data)
        self.assertFalse([d for d in data if d.startswith("a:fl:") and d != "a:fl:0"])

    async def test_pages_do_not_overlap_and_cover_everything(self):
        first, _ = await self._open("a:fl", admin_panel.show_ledger)
        second, _ = await self._open("a:fl:7", admin_panel.show_ledger)
        seen = self._entries(first) + self._entries(second)
        self.assertEqual(len(seen), TOTAL)
        for index in range(1, TOTAL + 1):
            reasons = (first + second).count("подарок {}".format(index))
            self.assertEqual(reasons, 1, index)

    async def test_offset_beyond_the_end_shows_empty_page_with_way_back(self):
        text, markup = await self._open("a:fl:70", admin_panel.show_ledger)
        self.assertIn("Операций нет", text)
        self.assertIn("a:fl:63", callback_data(markup))

    async def test_garbage_offset_falls_back_to_the_first_page(self):
        text, _ = await self._open("a:fl:abc", admin_panel.show_ledger)
        self.assertEqual(len(self._entries(text)), 7)

    async def test_client_operations_use_their_own_prefix(self):
        text, markup = await self._open(
            "a:co:{}:0".format(USER_ID), admin_panel.show_client_operations
        )
        self.assertEqual(len(self._entries(text)), 7)
        data = callback_data(markup)
        self.assertIn("a:co:{}:7".format(USER_ID), data)
        self.assertIn("a:c:{}".format(USER_ID), data)
        text, markup = await self._open(
            "a:co:{}:7".format(USER_ID), admin_panel.show_client_operations
        )
        self.assertEqual(len(self._entries(text)), TOTAL - 7)
        self.assertIn("a:co:{}:0".format(USER_ID), callback_data(markup))

    async def test_audit_log_pages_by_seven(self):
        text, markup = await self._open("a:fa", admin_panel.show_audit)
        self.assertIn("Журнал операций", text)
        self.assertEqual(len(self._entries(text)), 7)
        self.assertIn("a:fa:7", callback_data(markup))
        text, markup = await self._open("a:fa:7", admin_panel.show_audit)
        self.assertEqual(len(self._entries(text)), TOTAL - 7)
        self.assertIn("a:fa:0", callback_data(markup))
        self.assertNotIn("a:fa:14", callback_data(markup))

    async def test_audit_page_text_fits_a_telegram_message(self):
        text, _ = await self._open("a:fa", admin_panel.show_audit)
        self.assertLessEqual(len(text), 4000)

    async def test_finance_menu_names_the_journal(self):
        text, markup = admin_panel.finance_screen()
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("🕵️ Журнал операций", labels)
        self.assertIn("🧾 Последние операции", labels)


if __name__ == "__main__":
    unittest.main()
