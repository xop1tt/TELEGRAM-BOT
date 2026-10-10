"""Бот и веб-API поверх одной настоящей PostgreSQL.

У "бота" и "API" — по своему пулу соединений, как у двух процессов в
продакшене. Проверяется то, чего не видно в юнит-тестах на InMemory:
запись/отмена/баланс синхронны между интерфейсами, гонка за последнее
место между ними, поведение при падении PostgreSQL и сверка платежей без
бота.
"""

import asyncio
import hashlib
import hmac
import logging
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
from tests.support import apply_migrations
import httpx
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

from flamenco_bot.api.app import DATABASE_UNAVAILABLE_DETAIL, create_app
from flamenco_bot.database.repository import PostgresRepository, SlotUnavailableError
from flamenco_bot.handlers import router as bot_router
from flamenco_bot.handlers.errors import SERVICE_UNAVAILABLE_TEXT
from flamenco_bot.keyboards.user.screens import my_classes_screen
from flamenco_bot.payments import LessonPackage, ProviderPayment
from flamenco_bot.runtime.bot_logging import UpdateLoggingMiddleware
from flamenco_bot.runtime.payment_reconciliation import (
    RECONCILIATION_LOCK_ID,
    reconcile_pending_payments,
)
from flamenco_bot.runtime.security import (
    AuthRateLimiter,
    SecurityMiddleware,
    SupportRateLimiter,
)
from flamenco_bot.services import AdminNotifier, AuthService, BookingService
from flamenco_bot.services.payments import PaymentService
from tests.unit.test_bot_routing import RecordingSession


DATABASE_URL = os.getenv("TEST_DATABASE_URL")
BOT_TOKEN = "123456:integration-token"


def signed_telegram_payload(telegram_id):
    data = {"id": telegram_id, "first_name": "Анна", "auth_date": int(time.time())}
    check_string = "\n".join("{}={}".format(k, data[k]) for k in sorted(data))
    secret = hashlib.sha256(BOT_TOKEN.encode("utf-8")).digest()
    data["hash"] = hmac.new(
        secret, check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return data


class TcpProxy:
    """TCP-прокси к PostgreSQL: ``stop()`` имитирует падение сервера БД."""

    def __init__(self, target_host, target_port):
        self.target = (target_host, target_port)
        self.server = None
        self.writers = set()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _handle(self, client_reader, client_writer):
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                *self.target
            )
        except OSError:
            client_writer.close()
            return
        self.writers.update((client_writer, upstream_writer))

        async def pipe(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                writer.close()

        await asyncio.gather(
            pipe(client_reader, upstream_writer),
            pipe(upstream_reader, client_writer),
        )

    async def stop(self):
        self.server.close()
        for writer in self.writers:
            writer.transport.abort()
        await self.server.wait_closed()


@unittest.skipUnless(
    DATABASE_URL,
    "TEST_DATABASE_URL is not set; configure a dedicated test PostgreSQL database",
)
class InterfacesSharePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "test_ifaces_{}_{}".format(os.getpid(), time.time_ns())
        self.admin_pool = await asyncpg.create_pool(DATABASE_URL)
        self.pools = []
        async with self.admin_pool.acquire() as connection:
            await connection.execute('CREATE SCHEMA "{}"'.format(self.schema))
        self.bot_repo = await self._repository()
        await apply_migrations(self.pools[-1])
        await self.bot_repo.initialize()
        self.api_repo = await self._repository()
        self.client = None

    async def asyncTearDown(self):
        if self.client is not None:
            await self.client.aclose()
        for pool in self.pools:
            pool.terminate()
        async with self.admin_pool.acquire() as connection:
            await connection.execute(
                'DROP SCHEMA IF EXISTS "{}" CASCADE'.format(self.schema)
            )
        await self.admin_pool.close()
        bot_router._parent_router = None

    async def _repository(self, dsn=None):
        pool = await asyncpg.create_pool(
            dsn or DATABASE_URL,
            min_size=1,
            max_size=5,
            server_settings={"search_path": self.schema},
        )
        self.pools.append(pool)
        return PostgresRepository(pool)

    def _api_client(self, repository):
        app = create_app()
        app.state.repository = repository
        app.state.auth_service = AuthService(repository, BOT_TOKEN)
        app.state.bot = AsyncMock()
        app.state.support_limiter = SupportRateLimiter()
        app.state.auth_limiter = AuthRateLimiter(max_attempts=1000)
        app.state.payment_gateway = SimpleNamespace(is_configured=False)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        self.client = httpx.AsyncClient(
            transport=transport, base_url="https://testserver"
        )
        return self.client

    async def _login(self, client, telegram_id):
        client.cookies.clear()
        response = await client.post(
            "/api/auth/telegram", json=signed_telegram_payload(telegram_id)
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.cookies["session"]

    async def _grant(self, telegram_id, credits):
        await self.bot_repo.get_or_create_profile(telegram_id, "Участник")
        async with self.admin_pool.acquire() as connection:
            await connection.execute(
                'UPDATE "{}".bot_users SET lesson_credits = $2 '
                "WHERE telegram_id = $1".format(self.schema),
                telegram_id,
                credits,
            )

    def _bot_booking(self):
        return BookingService(self.bot_repo, AdminNotifier(AsyncMock(), self.bot_repo))

    async def test_bot_booking_and_website_cancellation_are_shared(self):
        client = self._api_client(self.api_repo)
        slot = await self.bot_repo.create_class_slot(
            "beginner", datetime.now(timezone.utc) + timedelta(days=3), 5, 1
        )
        await self._grant(111, 2)

        # Telegram: запись через тот же сервис, что в keyboards/user/lessons.py.
        await self._bot_booking().book(slot.id, 111)

        await self._login(client, 111)
        bookings = (await client.get("/api/bookings/me")).json()
        self.assertEqual(
            [(b["slot_id"], b["booking_status"]) for b in bookings],
            [(slot.id, "confirmed")],
        )
        profile = (await client.get("/api/users/me/profile")).json()
        self.assertEqual(profile["lesson_credits"], 1)
        schedule = (await client.get("/api/schedule")).json()
        self.assertEqual(schedule[0]["remaining"], 4)

        # Сайт: отмена → бот видит отмену и вернувшееся занятие.
        response = await client.delete("/api/bookings/{}".format(slot.id))
        self.assertEqual(response.status_code, 204)
        text, _ = await my_classes_screen(self.bot_repo, 111)
        self.assertIn("Предстоящих занятий пока нет", text)
        self.assertIn("Баланс: 2 занятия", text)

        # Сайт: запись → бот видит её в «Мои занятия».
        other_slot = await self.bot_repo.create_class_slot(
            "intermediate", datetime.now(timezone.utc) + timedelta(days=4), 5, 1
        )
        response = await client.post("/api/bookings", json={"slot_id": other_slot.id})
        self.assertEqual(response.status_code, 201)
        text, _ = await my_classes_screen(self.bot_repo, 111)
        self.assertIn("Продолжающая группа", text)
        self.assertIn("Баланс: 1 занятие", text)

    async def test_bot_and_website_cannot_both_take_the_last_place(self):
        client = self._api_client(self.api_repo)
        bot_booking = self._bot_booking()
        for round_number in range(10):
            slot = await self.bot_repo.create_class_slot(
                "beginner",
                datetime.now(timezone.utc) + timedelta(days=2, minutes=round_number),
                1,
                1,
            )
            bot_user, web_user = 2000 + 2 * round_number, 2001 + 2 * round_number
            await self._grant(bot_user, 1)
            await self._grant(web_user, 1)
            await self._login(client, web_user)

            bot_result, web_response = await asyncio.gather(
                bot_booking.book(slot.id, bot_user),
                client.post("/api/bookings", json={"slot_id": slot.id}),
                return_exceptions=True,
            )

            bot_won = not isinstance(bot_result, BaseException)
            web_won = web_response.status_code == 201
            with self.subTest(round=round_number):
                self.assertTrue(bot_won != web_won, (bot_result, web_response))
                if not bot_won:
                    self.assertIsInstance(bot_result, SlotUnavailableError)
                else:
                    self.assertEqual(web_response.status_code, 409)
            confirmed = [
                b
                for user in (bot_user, web_user)
                for b in await self.bot_repo.list_bookings_for_telegram_id(user)
                if b.booking_status == "confirmed"
            ]
            self.assertEqual(len(confirmed), 1)

    async def _repository_behind_proxy(self):
        parts = urlsplit(DATABASE_URL)
        proxy = TcpProxy(parts.hostname, parts.port or 5432)
        port = await proxy.start()
        netloc = parts.netloc.rsplit("@", 1)
        host_port = "127.0.0.1:{}".format(port)
        netloc = "{}@{}".format(netloc[0], host_port) if len(netloc) == 2 else host_port
        dsn = urlunsplit(parts._replace(netloc=netloc))
        return proxy, await self._repository(dsn)

    async def test_api_answers_503_json_when_postgres_goes_down(self):
        proxy, repository = await self._repository_behind_proxy()
        client = self._api_client(repository)
        self.assertEqual((await client.get("/api/schedule")).status_code, 200)
        await self._grant(333, 0)
        await self._login(client, 333)

        await proxy.stop()

        with self.assertLogs("bot.api", "ERROR"):
            for path in ("/api/schedule", "/api/bookings/me", "/api/health"):
                response = await client.get(path)
                with self.subTest(path=path):
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(
                        response.json(), {"detail": DATABASE_UNAVAILABLE_DETAIL}
                    )

    async def test_bot_replies_service_unavailable_when_postgres_goes_down(self):
        proxy, repository = await self._repository_behind_proxy()
        session = RecordingSession()
        bot = Bot(token="42:TEST", session=session)
        dp = Dispatcher()
        dp["repository"] = repository
        dp["support_limiter"] = SupportRateLimiter()
        dp["payment_gateway"] = SimpleNamespace(is_configured=False)
        dp["restart_controller"] = SimpleNamespace()
        security = SecurityMiddleware()
        dp.message.outer_middleware(security)
        update_logger = UpdateLoggingMiddleware(logging.getLogger("test.bot"))
        dp.message.middleware(update_logger)
        dp.include_router(bot_router)

        await proxy.stop()

        message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=444, type="private"),
            from_user=User(id=444, is_bot=False, first_name="Анна"),
            text="/start",
        )
        with self.assertLogs("bot.handlers.errors", "ERROR"):
            await dp.feed_update(bot, Update(update_id=1, message=message))

        texts = [m.text for m in session.requests if isinstance(m, SendMessage)]
        self.assertEqual(texts, [SERVICE_UNAVAILABLE_TEXT])

    async def _pending_payment(self, telegram_id):
        await self._grant(telegram_id, 0)
        package = LessonPackage("pack_4", "Абонемент на 4 занятия", 4, 3600)
        attempt = await self.api_repo.begin_lesson_payment_attempt(
            telegram_id=telegram_id,
            package_key=package.key,
            package_title=package.title,
            lessons=package.lessons,
            amount_minor=package.price_rub * 100,
        )
        payment = await self.api_repo.create_lesson_payment(
            telegram_id=telegram_id,
            package_key=attempt.package_key,
            package_title=attempt.package_title,
            lessons=attempt.lessons,
            amount_minor=attempt.amount_minor,
            provider_payment_id="yk-{}".format(telegram_id),
            confirmation_url="https://yoomoney.ru/checkout",
            idempotence_key=attempt.idempotence_key,
        )
        async with self.admin_pool.acquire() as connection:
            await connection.execute(
                'UPDATE "{}".lesson_payments '
                "SET created_at = NOW() - INTERVAL '1 hour' WHERE id = $1".format(
                    self.schema
                ),
                payment.id,
            )
        gateway = SimpleNamespace(
            is_configured=True,
            get_payment=AsyncMock(
                return_value=ProviderPayment(
                    payment_id="yk-{}".format(telegram_id),
                    status="succeeded",
                    amount_minor=360000,
                    currency="RUB",
                    confirmation_url=None,
                    metadata={"telegram_id": str(telegram_id), "package_key": "pack_4"},
                )
            ),
        )
        return payment, gateway

    async def _run_reconciliation(self, repository, gateway, on_confirmed, ticks=1):
        class _Stop(Exception):
            pass

        calls = {"n": 0}

        async def sleep(_seconds):
            calls["n"] += 1
            if calls["n"] > ticks:
                raise _Stop

        with self.assertRaises(_Stop):
            await reconcile_pending_payments(
                PaymentService(repository, gateway),
                repository,
                logging.getLogger("test.reconciliation"),
                sleep=sleep,
                on_confirmed=on_confirmed,
            )

    async def test_reconciliation_from_api_credits_once_without_bot(self):
        """Бот выключен: сверку делает процесс API; повтор не зачисляет снова."""
        payment, gateway = await self._pending_payment(555)
        on_confirmed = AsyncMock()

        await self._run_reconciliation(self.api_repo, gateway, on_confirmed, ticks=3)

        self.assertEqual(await self.api_repo.get_lesson_credits(555), 4)
        on_confirmed.assert_awaited_once()
        stored = await self.api_repo.get_lesson_payment(payment.id, 555)
        self.assertEqual(stored.status, "succeeded")
        async with self.admin_pool.acquire() as connection:
            purchases = await connection.fetchval(
                'SELECT COUNT(*) FROM "{}".lesson_credit_ledger '
                "WHERE payment_id = $1 AND entry_type = 'purchase'".format(self.schema),
                payment.id,
            )
        self.assertEqual(purchases, 1)

    async def test_bot_and_api_reconcilers_together_credit_and_notify_once(self):
        _payment, gateway = await self._pending_payment(666)
        on_confirmed = AsyncMock()

        await asyncio.gather(
            self._run_reconciliation(self.bot_repo, gateway, on_confirmed),
            self._run_reconciliation(self.api_repo, gateway, on_confirmed),
        )
        # И без блокировки двойного зачисления нет: прямые параллельные сверки.
        results = await asyncio.gather(
            PaymentService(self.bot_repo, gateway).check_payment(_payment.id, 666),
            PaymentService(self.api_repo, gateway).check_payment(_payment.id, 666),
        )

        self.assertEqual(await self.api_repo.get_lesson_credits(666), 4)
        on_confirmed.assert_awaited_once()
        self.assertNotIn("confirmed", [result.status.value for result in results])

    async def test_advisory_lock_is_exclusive_between_processes(self):
        async with self.bot_repo.try_advisory_lock(RECONCILIATION_LOCK_ID) as bot_has:
            async with self.api_repo.try_advisory_lock(
                RECONCILIATION_LOCK_ID
            ) as api_has:
                self.assertTrue(bot_has)
                self.assertFalse(api_has)
        async with self.api_repo.try_advisory_lock(RECONCILIATION_LOCK_ID) as api_has:
            self.assertTrue(api_has)


if __name__ == "__main__":
    unittest.main()
