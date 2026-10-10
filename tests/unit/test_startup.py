import logging
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram.utils.token import TokenValidationError

from flamenco_bot.database.repository import DatabaseUnavailableError
from flamenco_bot.main import main as run_bot
from flamenco_bot.runtime.security import SupportRateLimiter
from tests.support import FakeDispatcher, FakeRepository


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_uses_mocks_and_closes_resources(self):
        repository = FakeRepository()
        logger = logging.Logger("test.startup")
        session = SimpleNamespace(
            middleware=Mock(),
            close=AsyncMock(),
        )
        bot = SimpleNamespace(
            session=session,
            set_my_commands=AsyncMock(),
            send_message=AsyncMock(),
        )
        dispatcher = FakeDispatcher()

        with (
            patch("flamenco_bot.main.configure_logging", return_value=logger),
            patch("flamenco_bot.main.Config.ENV", "development"),
            patch("flamenco_bot.main.is_database_configured", return_value=True),
            patch(
                "flamenco_bot.main.PostgresRepository.connect",
                new=AsyncMock(return_value=repository),
            ) as connect,
            patch("flamenco_bot.main.Bot", return_value=bot),
            patch("flamenco_bot.main.Dispatcher", return_value=dispatcher),
        ):
            await run_bot()

        connect.assert_awaited_once()
        repository.initialize.assert_awaited_once()
        dispatcher.start_polling.assert_awaited_once_with(
            bot,
            tasks_concurrency_limit=64,
        )
        self.assertIsInstance(
            getattr(dispatcher, "support_limiter"),
            SupportRateLimiter,
        )
        dispatcher.update.outer_middleware.assert_not_called()
        dispatcher.message.middleware.assert_called_once()
        dispatcher.message.outer_middleware.assert_called_once()
        dispatcher.callback_query.outer_middleware.assert_called_once()
        dispatcher.callback_query.middleware.assert_called_once()
        repository.close.assert_awaited_once()
        session.close.assert_awaited_once()

    async def test_development_also_refuses_missing_database_url(self):
        """Хранилища в памяти нет: без PostgreSQL бот не стартует нигде."""
        logger = logging.Logger("test.startup.no-database-dev")
        output = StringIO()

        with (
            redirect_stderr(output),
            patch("flamenco_bot.main.configure_logging", return_value=logger),
            patch("flamenco_bot.main.Config.ENV", "development"),
            patch("flamenco_bot.main.is_database_configured", return_value=False),
            patch("flamenco_bot.main.Bot") as bot,
        ):
            with self.assertRaises(SystemExit) as exit_error:
                await run_bot()

        self.assertEqual(exit_error.exception.code, 1)
        self.assertIn("DATABASE_URL", output.getvalue())
        bot.assert_not_called()

    async def test_invalid_token_fails_without_raw_traceback(self):
        repository = FakeRepository()
        logger = logging.Logger("test.startup.invalid-token")
        output = StringIO()

        with (
            redirect_stderr(output),
            patch("flamenco_bot.main.configure_logging", return_value=logger),
            patch("flamenco_bot.main.is_database_configured", return_value=True),
            patch(
                "flamenco_bot.main.PostgresRepository.connect",
                new=AsyncMock(return_value=repository),
            ),
            patch("flamenco_bot.main.Bot", side_effect=TokenValidationError("invalid")),
        ):
            with self.assertRaises(SystemExit) as error:
                await run_bot()

        self.assertEqual(error.exception.code, 1)
        self.assertIn("BOT_TOKEN некорректен", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())
        repository.initialize.assert_not_awaited()
        repository.close.assert_not_awaited()

    async def test_production_refuses_missing_database_url(self):
        logger = logging.Logger("test.startup.no-database")

        with (
            patch("flamenco_bot.main.configure_logging", return_value=logger),
            patch("flamenco_bot.main.Config.ENV", "production"),
            patch("flamenco_bot.main.Config.DATABASE_URL", ""),
            patch("flamenco_bot.main.is_database_configured", return_value=False),
            patch("flamenco_bot.main.Bot") as bot,
        ):
            with self.assertRaises(SystemExit) as exit_error:
                await run_bot()

        self.assertEqual(exit_error.exception.code, 1)
        bot.assert_not_called()

    async def test_startup_database_connection_failure_exits_without_restart(self):
        logger = logging.Logger("test.startup.database-unavailable")
        session = SimpleNamespace(close=AsyncMock(), middleware=Mock())
        bot = SimpleNamespace(session=session)
        output = StringIO()

        with (
            redirect_stderr(output),
            patch("flamenco_bot.main.configure_logging", return_value=logger),
            patch("flamenco_bot.main.Config.ENV", "development"),
            patch(
                "flamenco_bot.main.Config.DATABASE_URL", "postgresql://localhost/bot"
            ),
            patch("flamenco_bot.main.is_database_configured", return_value=True),
            patch("flamenco_bot.main.Bot", return_value=bot),
            patch(
                "flamenco_bot.main.PostgresRepository.connect",
                new=AsyncMock(
                    side_effect=DatabaseUnavailableError("PostgreSQL is unavailable")
                ),
            ),
            patch("flamenco_bot.main.restart_process") as restart,
        ):
            with self.assertRaises(SystemExit) as exit_error:
                await run_bot()

        self.assertEqual(exit_error.exception.code, 1)
        self.assertIn("сервер PostgreSQL недоступен", output.getvalue())
        restart.assert_not_called()
        session.close.assert_awaited_once()

    async def test_critical_polling_error_notifies_admins_and_restarts_process(self):
        repository = FakeRepository()
        logger = logging.Logger("test.startup.critical")
        session = SimpleNamespace(
            middleware=Mock(),
            close=AsyncMock(),
        )
        bot = SimpleNamespace(
            session=session,
            set_my_commands=AsyncMock(),
            send_message=AsyncMock(),
        )
        dispatcher = FakeDispatcher()
        dispatcher.start_polling.side_effect = RuntimeError(
            "private connection details"
        )
        # Адресаты системных уведомлений — администраторы из БД.
        repository.list_admin_ids.return_value = [8373364453, 123456789]
        output = StringIO()

        with (
            redirect_stdout(output),
            patch("flamenco_bot.main.configure_logging", return_value=logger),
            patch("flamenco_bot.main.Config.ENV", "development"),
            patch("flamenco_bot.main.is_database_configured", return_value=True),
            patch(
                "flamenco_bot.main.PostgresRepository.connect",
                new=AsyncMock(return_value=repository),
            ),
            patch("flamenco_bot.main.Bot", return_value=bot),
            patch("flamenco_bot.main.Dispatcher", return_value=dispatcher),
            patch("flamenco_bot.main.restart_process") as restart,
        ):
            await run_bot()

        restart.assert_called_once()
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertTrue(
            all(
                "RuntimeError" in call.kwargs["text"]
                and "private connection" not in call.kwargs["text"]
                for call in bot.send_message.await_args_list
            )
        )
        dispatcher.stop_polling.assert_awaited_once()
        repository.close.assert_awaited_once()
        session.close.assert_awaited_once()

    async def test_production_fatal_error_exits_for_process_supervisor(self):
        repository = FakeRepository()
        logger = logging.Logger("test.startup.production")
        session = SimpleNamespace(
            middleware=Mock(),
            close=AsyncMock(),
        )
        bot = SimpleNamespace(
            session=session,
            set_my_commands=AsyncMock(),
            send_message=AsyncMock(),
        )
        dispatcher = FakeDispatcher()
        dispatcher.start_polling.side_effect = RuntimeError("private details")
        repository.list_admin_ids.return_value = [123456789]

        with (
            patch("flamenco_bot.main.configure_logging", return_value=logger),
            patch("flamenco_bot.main.Config.ENV", "production"),
            patch("flamenco_bot.main.is_database_configured", return_value=True),
            patch(
                "flamenco_bot.main.PostgresRepository.connect",
                new=AsyncMock(return_value=repository),
            ),
            patch("flamenco_bot.main.Bot", return_value=bot),
            patch("flamenco_bot.main.Dispatcher", return_value=dispatcher),
            patch("flamenco_bot.main.restart_process") as restart,
        ):
            with self.assertRaises(SystemExit) as exit_error:
                await run_bot()

        self.assertEqual(exit_error.exception.code, 1)
        restart.assert_not_called()
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertNotIn(
            "private details",
            bot.send_message.await_args.kwargs["text"],
        )
        repository.close.assert_awaited_once()
        session.close.assert_awaited_once()
