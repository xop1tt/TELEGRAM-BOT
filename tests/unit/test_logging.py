import logging
import os
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from aiogram.types import Chat, Message, TelegramObject, Update, User

from flamenco_bot.runtime.bot_logging import (
    UpdateLoggingMiddleware,
    UpdateMetrics,
    configure_logging,
)
from flamenco_bot.runtime.logging_utils import (
    RetentionRotatingFileHandler,
    create_admin_actions_logger,
)
from flamenco_bot.runtime.monitoring import monitor_health
from flamenco_bot.runtime.paths import get_log_directory


class LoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_log_directory_can_be_configured_by_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"BOT_LOG_DIR": directory}):
                self.assertEqual(get_log_directory(), Path(directory))

    async def test_log_files_are_private_and_expired_backups_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "bot.log"
            expired_backup = Path(str(log_path) + ".1")
            expired_backup.write_text("expired", encoding="utf-8")
            expired_time = time.time() - 31 * 24 * 60 * 60
            os.utime(expired_backup, (expired_time, expired_time))

            handler = RetentionRotatingFileHandler(
                log_path,
                encoding="utf-8",
                maxBytes=100,
                backupCount=5,
            )
            try:
                self.assertFalse(expired_backup.exists())
                self.assertEqual(
                    stat.S_IMODE(log_path.stat().st_mode),
                    0o600,
                )
            finally:
                handler.close()

    async def test_update_logger_records_success_and_propagates_errors(self):
        logger = logging.Logger("test.updates")
        records = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger.addHandler(CaptureHandler())
        middleware = UpdateLoggingMiddleware(logger)
        event = Update(
            update_id=55,
            message=Message(
                message_id=1,
                date=datetime.now(timezone.utc),
                chat=Chat(id=55, type="private"),
                from_user=User(id=55, is_bot=False, first_name="Test"),
            ),
        )
        handler = AsyncMock(return_value="ok")

        self.assertEqual(await middleware(handler, event, {}), "ok")
        handler.assert_awaited_once_with(event, {})
        self.assertTrue(any("telegram_id=55" in entry for entry in records))

    async def test_update_logger_records_activity_for_registered_user(self):
        repository = SimpleNamespace(record_activity=AsyncMock())
        middleware = UpdateLoggingMiddleware(logging.Logger("test.activity"))
        event = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=55, type="private"),
            from_user=User(id=55, is_bot=False, first_name="Test"),
        )

        await middleware(
            AsyncMock(return_value=None), event, {"repository": repository}
        )

        repository.record_activity.assert_awaited_once_with(55)

        async def failing_handler(
            event: TelegramObject,
            data: dict[str, Any],
        ) -> Any:
            _ = (event, data)
            raise RuntimeError("failed")

        with self.assertRaises(RuntimeError):
            await middleware(failing_handler, event, {})

    async def test_update_error_is_counted_without_requesting_process_restart(self):
        logger = logging.Logger("test.updates.failed")
        metrics = UpdateMetrics()
        middleware = UpdateLoggingMiddleware(logger, metrics=metrics)
        event = Update(
            update_id=56,
            message=Message(
                message_id=1,
                date=datetime.now(timezone.utc),
                chat=Chat(id=56, type="private"),
                from_user=User(id=56, is_bot=False, first_name="Test"),
            ),
        )

        async def failing_handler(
            event: TelegramObject,
            data: dict[str, Any],
        ) -> Any:
            _ = (event, data)
            raise RuntimeError("handler failure")

        with self.assertRaisesRegex(RuntimeError, "handler failure"):
            await middleware(failing_handler, event, {})
        snapshot = metrics.snapshot()
        self.assertEqual(
            (snapshot.started, snapshot.succeeded, snapshot.failed),
            (1, 0, 1),
        )
        self.assertGreaterEqual(snapshot.total_duration_ms, 0)

    async def test_update_metrics_are_bounded_to_a_sliding_window(self):
        now = [10.0]
        metrics = UpdateMetrics(
            window_seconds=5,
            max_samples=2,
            clock=lambda: now[0],
        )
        metrics.record(10, failed=False)
        now[0] += 1
        metrics.record(20, failed=True)
        now[0] += 1
        metrics.record(30, failed=False)

        capped = metrics.snapshot()
        self.assertEqual(
            (capped.started, capped.succeeded, capped.failed),
            (2, 1, 1),
        )
        self.assertEqual(capped.average_duration_ms, 25)
        self.assertEqual(capped.p95_duration_ms, 30)

        now[0] += 5.001
        expired = metrics.snapshot()
        self.assertEqual(
            (expired.started, expired.succeeded, expired.failed),
            (0, 0, 0),
        )
        self.assertEqual(expired.average_duration_ms, 0)

    async def test_health_monitor_logs_update_metrics_and_database_health(self):
        logger = logging.Logger("test.health")
        records = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger.addHandler(CaptureHandler())
        repository = AsyncMock()
        repository.health_check.return_value = SimpleNamespace(
            backend="postgres",
            pool_size=4,
            idle_connections=2,
        )
        metrics = UpdateMetrics()
        metrics.record(12, failed=False)

        class StopAfterOneProbe(Exception):
            pass

        sleeps = 0

        async def sleep(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                raise StopAfterOneProbe()

        with self.assertRaises(StopAfterOneProbe):
            await monitor_health(
                repository,
                metrics,
                logger,
                interval_seconds=60,
                sleep=sleep,
            )

        repository.health_check.assert_awaited_once()
        self.assertTrue(any("Health status=ok" in message for message in records))
        self.assertTrue(any("pool_size=4" in message for message in records))
        self.assertTrue(any("updates_succeeded=1" in message for message in records))

    async def test_health_monitor_logs_database_probe_failure_and_keeps_running(self):
        logger = logging.Logger("test.health.failure")
        records = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger.addHandler(CaptureHandler())
        repository = AsyncMock()
        repository.health_check.side_effect = OSError("database unavailable")

        class StopAfterOneProbe(Exception):
            pass

        sleeps = 0

        async def sleep(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                raise StopAfterOneProbe()

        with self.assertRaises(StopAfterOneProbe):
            await monitor_health(
                repository,
                UpdateMetrics(),
                logger,
                interval_seconds=60,
                sleep=sleep,
            )

        repository.health_check.assert_awaited_once()
        self.assertTrue(any("Health check failed" in message for message in records))
        self.assertTrue(any("error_type=OSError" in message for message in records))

    async def test_logging_config_creates_bot_log_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = logging.getLogger()
            old_handlers = root.handlers[:]
            root.handlers.clear()
            terminal = StringIO()
            try:
                with redirect_stderr(terminal):
                    logger = configure_logging(Path(directory))
                    logger.info("Important runtime status")
                self.assertTrue((Path(directory) / "bot.log").exists())
                self.assertIn("Important runtime status", terminal.getvalue())
            finally:
                for handler in root.handlers:
                    handler.close()
                root.handlers[:] = old_handlers

    async def test_admin_action_logger_writes_to_separate_file(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = logging.getLogger("bot.admin_actions")
            previous_handlers = logger.handlers[:]
            logger = create_admin_actions_logger(Path(directory))
            try:
                logger.info("action=edit_name admin_id=8373364453 target_id=1001")
                for handler in logger.handlers:
                    handler.flush()
                contents = (Path(directory) / "admin_actions_log").read_text(
                    encoding="utf-8"
                )
                self.assertIn("action=edit_name", contents)
                self.assertIn("admin_id=8373364453", contents)
            finally:
                for handler in logger.handlers[:]:
                    if handler not in previous_handlers:
                        logger.removeHandler(handler)
                        handler.close()
