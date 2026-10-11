"""Недоступная PostgreSQL: бот и веб-API отвечают понятной ошибкой.

Бот — через настоящий Dispatcher с теми же middleware, что в ``main.py``:
пользователь получает сообщение, а inline-кнопка не остаётся "в загрузке".
API — 503 с JSON вместо 500 text/plain; детали ошибки клиенту не уходят.
"""

import errno
import logging
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from flamenco_bot.api.app import DATABASE_UNAVAILABLE_DETAIL, create_app
from flamenco_bot.database import is_database_unavailable
from tests.pg import PostgresApiTestCase
from flamenco_bot.database.repository import DatabaseUnavailableError
from flamenco_bot.handlers import router
from flamenco_bot.handlers.errors import SERVICE_UNAVAILABLE_TEXT
from flamenco_bot.runtime.bot_logging import UpdateLoggingMiddleware
from flamenco_bot.runtime.security import (
    AuthRateLimiter,
    SecurityMiddleware,
    SupportRateLimiter,
)
from flamenco_bot.services import AuthService
from tests.support import FakeRepository
from tests.unit.test_bot_routing import USER_ID, RecordingSession


class IsDatabaseUnavailableTests(unittest.TestCase):
    def test_connection_failures_are_unavailability(self):
        for error in (
            DatabaseUnavailableError("down"),
            ConnectionRefusedError(errno.ECONNREFUSED, "refused"),
            asyncpg.ConnectionDoesNotExistError("connection was closed"),
            asyncpg.CannotConnectNowError("starting up"),
            asyncpg.TooManyConnectionsError("too many clients"),
            OSError(errno.EHOSTUNREACH, "No route to host"),
            TimeoutError(),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertTrue(is_database_unavailable(error))

    def test_query_and_programming_errors_are_not_unavailability(self):
        for error in (
            RuntimeError("bug"),
            ValueError("bad input"),
            asyncpg.UniqueViolationError("duplicate key"),
            FileNotFoundError(errno.ENOENT, "missing"),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertFalse(is_database_unavailable(error))


class BotDatabaseUnavailableTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = RecordingSession()
        self.bot = Bot(token="42:TEST", session=self.session)
        self.repository = FakeRepository()
        self.dp = Dispatcher()
        self.dp["repository"] = self.repository
        self.dp["support_limiter"] = SupportRateLimiter()
        self.dp["payment_gateway"] = SimpleNamespace(is_configured=False)
        self.dp["restart_controller"] = SimpleNamespace()
        # Как в main.py: activity пишется в БД на каждое обновление.
        security = SecurityMiddleware()
        self.dp.message.outer_middleware(security)
        self.dp.callback_query.outer_middleware(security)
        update_logger = UpdateLoggingMiddleware(logging.getLogger("test.bot"))
        self.dp.message.middleware(update_logger)
        self.dp.callback_query.middleware(update_logger)
        self.dp.include_router(router)

    async def asyncTearDown(self):
        router._parent_router = None

    def user(self):
        return User(id=USER_ID, is_bot=False, first_name="Анна")

    async def send_text(self, text):
        message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=USER_ID, type="private"),
            from_user=self.user(),
            text=text,
        )
        return await self.dp.feed_update(self.bot, Update(update_id=1, message=message))

    async def press(self, data):
        callback = CallbackQuery(
            id="cb-1",
            from_user=self.user(),
            chat_instance="test",
            data=data,
            message=Message(
                message_id=500,
                date=datetime.now(timezone.utc),
                chat=Chat(id=USER_ID, type="private"),
                text="экран",
            ),
        )
        return await self.dp.feed_update(
            self.bot, Update(update_id=2, callback_query=callback)
        )

    def requests_of(self, method_type):
        return [m for m in self.session.requests if isinstance(m, method_type)]

    async def test_message_gets_service_unavailable_reply(self):
        self.repository.record_activity.side_effect = (
            asyncpg.ConnectionDoesNotExistError("connection was closed")
        )

        with self.assertLogs("bot.handlers.errors", "ERROR"):
            await self.send_text("/start")

        replies = self.requests_of(SendMessage)
        self.assertEqual([m.text for m in replies], [SERVICE_UNAVAILABLE_TEXT])
        self.assertNotIn("connection was closed", replies[0].text)

    async def test_callback_is_answered_so_button_stops_loading(self):
        self.repository.list_bookings_for_telegram_id.side_effect = (
            ConnectionRefusedError(errno.ECONNREFUSED, "Connect call failed")
        )

        with self.assertLogs("bot.handlers.errors", "ERROR"):
            await self.press("my")

        answers = self.requests_of(AnswerCallbackQuery)
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0].text, SERVICE_UNAVAILABLE_TEXT)
        self.assertTrue(answers[0].show_alert)

    async def test_other_errors_are_not_reported_as_unavailable_database(self):
        self.repository.list_bookings_for_telegram_id.side_effect = RuntimeError("bug")

        with self.assertRaises(RuntimeError):
            with self.assertLogs("bot", "ERROR"):
                await self.press("my")

        texts = [m.text for m in self.requests_of(AnswerCallbackQuery)]
        self.assertNotIn(SERVICE_UNAVAILABLE_TEXT, texts)


class ApiDatabaseUnavailableTests(PostgresApiTestCase):
    def setUp(self):
        super().setUp()
        app = create_app()
        app.state.repository = self.repository
        app.state.auth_service = AuthService(self.repository, "123456:test-token")
        app.state.bot = AsyncMock()
        app.state.support_limiter = SupportRateLimiter()
        app.state.auth_limiter = AuthRateLimiter()
        self.client = self.make_client(app, raise_server_exceptions=False)

    def fail_with(self, method_name, error):
        setattr(self.repository, method_name, AsyncMock(side_effect=error))

    def test_public_endpoint_returns_503_json(self):
        self.fail_with(
            "list_class_slots",
            ConnectionRefusedError(errno.ECONNREFUSED, "Connect call failed"),
        )

        with self.assertLogs("bot.api", "ERROR"):
            response = self.client.get("/api/schedule")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.json(), {"detail": DATABASE_UNAVAILABLE_DETAIL})

    def test_session_check_with_database_down_is_503_not_401(self):
        """Сайт не должен считать пользователя разлогиненным из-за сбоя БД."""
        self.fail_with(
            "get_web_session_user_id",
            asyncpg.ConnectionDoesNotExistError("connection was closed"),
        )

        self.client.cookies.set("session", "token")
        with self.assertLogs("bot.api", "ERROR"):
            response = self.client.get("/api/bookings/me")

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("connection was closed", response.text)

    def test_health_reveals_only_status(self):
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_health_returns_503_when_database_is_down(self):
        self.fail_with("health_check", DatabaseUnavailableError("down"))

        with self.assertLogs("bot.api", "ERROR"):
            response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 503)

    def test_other_errors_return_generic_500_json_without_details(self):
        self.fail_with("list_class_slots", RuntimeError("secret internals"))

        response = self.client.get("/api/schedule")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Internal Server Error"})
        self.assertNotIn("secret", response.text)

    def test_unrelated_os_error_is_not_reported_as_database_outage(self):
        self.fail_with("list_class_slots", FileNotFoundError(errno.ENOENT, "x"))

        response = self.client.get("/api/schedule")

        self.assertEqual(response.status_code, 500)


if __name__ == "__main__":
    unittest.main()
