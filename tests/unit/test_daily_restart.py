"""Плановый перезапуск: ежедневно в полночь по времени студии и при критической
ошибке. Проверяется расписание (пояс студии, а не сервера) и настройка."""

import asyncio
import logging
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from flamenco_bot.config import parse_flag
from flamenco_bot.runtime.bot_lifecycle import (
    RESTART_NOTICE,
    RestartController,
    next_local_midnight,
)

MOSCOW = ZoneInfo("Europe/Moscow")


class ParseFlagTests(unittest.TestCase):
    def test_values(self):
        for value in ("1", "true", "YES", " on "):
            self.assertTrue(parse_flag("X", value, False), value)
        for value in ("0", "false", "No", "off"):
            self.assertFalse(parse_flag("X", value, True), value)

    def test_empty_means_default_and_garbage_is_an_error(self):
        self.assertTrue(parse_flag("X", "", True))
        self.assertFalse(parse_flag("X", "  ", False))
        with self.assertRaisesRegex(ValueError, "BOT_DAILY_RESTART"):
            parse_flag("BOT_DAILY_RESTART", "maybe", True)


class MidnightScheduleTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, now):
        waits = []

        async def record_wait(awaitable, timeout):
            waits.append(timeout)
            awaitable.close()
            raise asyncio.TimeoutError

        bot = SimpleNamespace(send_message=AsyncMock())
        controller = RestartController(
            bot=bot,
            dispatcher=None,
            logger=logging.getLogger("test"),
            source_root=".",
            clock=lambda: now,
            wait_for=record_wait,
        )
        controller.admin_ids = AsyncMock(return_value=[1])
        return controller, waits, bot

    async def test_waits_for_midnight_in_the_studio_time_zone(self):
        now = datetime(2026, 10, 1, 21, 30, tzinfo=MOSCOW)  # 18:30 UTC
        controller, waits, bot = self.controller(now)
        await controller.schedule_midnight_restart()
        # до напоминания (23:00 МСК) — 1.5 ч, потом до 00:00 МСК — ещё 1.5 ч
        self.assertEqual(waits, [90 * 60, 150 * 60])
        self.assertEqual(controller.restart_reason, "daily_midnight")
        bot.send_message.assert_awaited_once_with(chat_id=1, text=RESTART_NOTICE)

    async def test_utc_clock_schedules_by_utc_midnight(
        self,
    ):
        # Часы сервера в UTC без пояса студии дали бы полночь UTC (03:00 МСК).
        utc_now = datetime(2026, 10, 1, 18, 30, tzinfo=ZoneInfo("UTC"))
        controller, waits, _ = self.controller(utc_now)
        await controller.schedule_midnight_restart()
        self.assertEqual(waits[0], 4.5 * 3600)  # 23:00 UTC, а не 23:00 МСК

    async def test_naive_clock_still_works(self):
        controller, waits, _ = self.controller(datetime(2026, 10, 1, 12, 0))
        await controller.schedule_midnight_restart()
        self.assertEqual(len(waits), 2)
        self.assertEqual(controller.restart_reason, "daily_midnight")

    def test_midnight_keeps_the_zone_and_crosses_month_and_year(self):
        for now, expected in (
            (
                datetime(2026, 10, 31, 23, 59, tzinfo=MOSCOW),
                datetime(2026, 11, 1, tzinfo=MOSCOW),
            ),
            (
                datetime(2026, 12, 31, 12, 0, tzinfo=MOSCOW),
                datetime(2027, 1, 1, tzinfo=MOSCOW),
            ),
        ):
            self.assertEqual(next_local_midnight(now), expected)
            self.assertGreater(next_local_midnight(now) - now, timedelta(0))

    async def test_critical_error_requests_restart_once_and_notifies(self):
        controller, _, bot = self.controller(datetime(2026, 10, 1, 12, tzinfo=MOSCOW))
        await controller.notify_critical_error(RuntimeError("boom"))
        await controller.notify_critical_error(RuntimeError("again"))
        self.assertEqual(controller.restart_reason, "critical_error")
        self.assertTrue(controller.restart_event.is_set())
        bot.send_message.assert_awaited_once()
        self.assertIn("RuntimeError", bot.send_message.await_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
