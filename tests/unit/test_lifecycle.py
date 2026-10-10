import asyncio
import logging
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

from flamenco_bot.runtime.bot_lifecycle import (
    RESTART_NOTICE,
    RestartController,
    code_snapshot,
    next_local_midnight,
    restart_process,
)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Mock()
        self.bot.send_message = AsyncMock()
        self.dispatcher = Mock()
        self.dispatcher.stop_polling = AsyncMock()
        self.logger = logging.Logger("test.lifecycle")

    def make_controller(self, directory, **kwargs):
        return RestartController(
            bot=self.bot,
            dispatcher=self.dispatcher,
            admins=[8373364453, 123456789],
            logger=self.logger,
            source_root=Path(directory),
            **kwargs,
        )

    async def test_code_snapshot_includes_app_python_and_excludes_tests_logs_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("print('bot')", encoding="utf-8")
            for excluded in (".venv", "tests", "logs"):
                folder = root / excluded
                folder.mkdir()
                (folder / "noise.py").write_text("pass", encoding="utf-8")

            self.assertEqual(list(code_snapshot(root)), ["main.py"])

    async def test_code_change_notifies_admins_then_requests_restart_after_delay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "main.py"
            source.write_text("initial", encoding="utf-8")
            slept = False

            async def change_file(delay):
                nonlocal slept
                _ = delay
                if not slept:
                    slept = True
                    source.write_text("changed code", encoding="utf-8")

            async def timeout_immediately(awaitable, timeout):
                timeouts.append(timeout)
                awaitable.close()
                raise asyncio.TimeoutError

            timeouts = []
            controller = self.make_controller(
                directory,
                check_interval=30,
                code_restart_delay=3600,
                sleep=change_file,
                wait_for=timeout_immediately,
            )
            controller.polling_started = True
            await controller.watch_code_changes()
            await asyncio.sleep(0)

            self.assertEqual(controller.restart_reason, "code_changed")
            self.assertEqual(timeouts, [3600])
            self.dispatcher.stop_polling.assert_awaited_once()
            self.assertEqual(self.bot.send_message.await_count, 2)
            for call in self.bot.send_message.await_args_list:
                self.assertEqual(call.kwargs["text"], RESTART_NOTICE)
            self.assertEqual(
                [
                    call.kwargs["chat_id"]
                    for call in self.bot.send_message.await_args_list
                ],
                [8373364453, 123456789],
            )

    async def test_midnight_scheduler_warns_one_hour_before_restart(self):
        now = datetime(2026, 10, 1, 22, 30, tzinfo=timezone.utc)

        async def timeout_immediately(awaitable, timeout):
            _ = timeout
            awaitable.close()
            raise asyncio.TimeoutError

        controller = self.make_controller(
            ".",
            clock=lambda: now,
            wait_for=timeout_immediately,
        )
        controller.polling_started = True
        await controller.schedule_midnight_restart()
        await asyncio.sleep(0)

        self.assertEqual(controller.restart_reason, "daily_midnight")
        self.bot.send_message.assert_has_awaits(
            [
                call(chat_id=8373364453, text=RESTART_NOTICE),
                call(chat_id=123456789, text=RESTART_NOTICE),
            ]
        )
        self.dispatcher.stop_polling.assert_awaited_once()

    async def test_critical_error_alert_is_sent_and_restart_is_immediate(self):
        controller = self.make_controller(".")
        controller.polling_started = True
        error = RuntimeError("sensitive database connection details")

        await controller.notify_critical_error(error)
        await asyncio.sleep(0)

        self.assertEqual(controller.restart_reason, "critical_error")
        self.assertTrue(controller.restart_event.is_set())
        self.dispatcher.stop_polling.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_count, 2)
        notification = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("RuntimeError", notification)
        self.assertIn("перезапущен", notification)
        self.assertNotIn("sensitive database", notification)

    async def test_midnight_calculation_uses_next_local_calendar_day(self):
        now = datetime(2026, 10, 1, 23, 59, tzinfo=timezone.utc)
        self.assertEqual(
            next_local_midnight(now),
            datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )

    def test_process_restart_replaces_current_python_process(self):
        with patch("flamenco_bot.runtime.bot_lifecycle.os.execv") as execv:
            restart_process()
        args = execv.call_args.args
        self.assertEqual(args[0], sys.executable)
        self.assertEqual(args[1][0], sys.executable)
