"""Сквозные сценарии продукта на PostgreSQL: бот, API и outbox вместе.

У «бота» и «API» — свои пулы соединений, как у двух процессов. Проверяется:
уведомление о записи с сайта уходит в Telegram только после commit и ровно
один раз (а при сбое между отправкой и отметкой — повторно, но без
повторной финансовой операции); отмена занятия студией возвращает занятия и
видна на сайте и в истории; вход на сайт через бота от ссылки до сессии.
"""

import asyncio
import hashlib
import hmac
import os
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
from tests.support import apply_migrations
import httpx
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from flamenco_bot.api.app import create_app
from flamenco_bot.database.repository import PostgresRepository
from flamenco_bot.handlers import router as bot_router
from flamenco_bot.runtime.notifications import dispatch_due_notifications
from flamenco_bot.runtime.security import AuthRateLimiter, SupportRateLimiter
from flamenco_bot.services import AuthService, ScheduleService
from tests.unit.test_bot_routing import RecordingSession


DATABASE_URL = os.getenv("TEST_DATABASE_URL")
BOT_TOKEN = "123456:integration-token"
ADMIN_ID = 9001
LOGGER = SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
    exception=lambda *a, **k: None,
)


def signed(telegram_id):
    data = {"id": telegram_id, "first_name": "Анна", "auth_date": int(time.time())}
    check_string = "\n".join("{}={}".format(k, data[k]) for k in sorted(data))
    secret = hashlib.sha256(BOT_TOKEN.encode("utf-8")).digest()
    data["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return data


@unittest.skipUnless(
    DATABASE_URL,
    "TEST_DATABASE_URL is not set; configure a dedicated test PostgreSQL database",
)
class ProductFlowsPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "test_flows_{}_{}".format(os.getpid(), time.time_ns())
        self.admin_pool = await asyncpg.create_pool(DATABASE_URL)
        async with self.admin_pool.acquire() as connection:
            await connection.execute('CREATE SCHEMA "{}"'.format(self.schema))
        self.pools = []
        self.bot_repo = await self._repository()
        await apply_migrations(self.pools[-1])
        await self.bot_repo.initialize()
        self.api_repo = await self._repository()
        await self.bot_repo.get_or_create_profile(ADMIN_ID, "Админ")
        async with self.admin_pool.acquire() as connection:
            await connection.execute(
                'INSERT INTO "{}".staff_members (telegram_id, role) '
                "VALUES ($1, 'admin')".format(self.schema),
                ADMIN_ID,
            )
        self.session = RecordingSession()
        self.bot = Bot(token="42:TEST", session=self.session)
        self.client = self._api_client()

    async def asyncTearDown(self):
        await self.client.aclose()
        for pool in self.pools:
            await pool.close()
        async with self.admin_pool.acquire() as connection:
            await connection.execute(
                'DROP SCHEMA IF EXISTS "{}" CASCADE'.format(self.schema)
            )
        await self.admin_pool.close()
        bot_router._parent_router = None

    async def _repository(self):
        pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            server_settings={"search_path": self.schema},
        )
        self.pools.append(pool)
        return PostgresRepository(pool)

    def _api_client(self):
        app = create_app()
        app.state.repository = self.api_repo
        app.state.auth_service = AuthService(self.api_repo, BOT_TOKEN)
        app.state.bot = AsyncMock()
        app.state.bot.get_me.return_value = SimpleNamespace(username="mirada_bot")
        app.state.support_limiter = SupportRateLimiter()
        app.state.auth_limiter = AuthRateLimiter(max_attempts=1000)
        app.state.payment_gateway = SimpleNamespace(is_configured=False)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        return httpx.AsyncClient(transport=transport, base_url="https://testserver")

    async def _login(self, telegram_id):
        self.client.cookies.clear()
        response = await self.client.post(
            "/api/auth/telegram", json=signed(telegram_id)
        )
        self.assertEqual(response.status_code, 200, response.text)

    async def _paid(self, telegram_id, lessons=4):
        await self.bot_repo.get_or_create_profile(telegram_id, "Участник")
        attempt = await self.bot_repo.begin_lesson_payment_attempt(
            telegram_id, "pack_4", "Абонемент на 4 занятия", lessons, 360000
        )
        payment = await self.bot_repo.create_lesson_payment(
            telegram_id=telegram_id,
            package_key="pack_4",
            package_title="Абонемент на 4 занятия",
            lessons=lessons,
            amount_minor=360000,
            provider_payment_id="yk-{}".format(uuid.uuid4().hex),
            confirmation_url="https://yoomoney.ru/x",
            idempotence_key=attempt.idempotence_key,
        )
        await self.bot_repo.complete_lesson_payment(payment.id, telegram_id)
        return payment.id

    def _sent_to(self, telegram_id):
        return [
            method
            for method in self.session.requests
            if isinstance(method, SendMessage) and method.chat_id == telegram_id
        ]

    async def test_web_booking_notification_is_sent_after_commit_exactly_once(self):
        await self._paid(501)
        slot = await self.bot_repo.create_class_slot(
            "beginner", datetime.now(timezone.utc) + timedelta(days=3), 5, ADMIN_ID
        )
        await self._login(501)

        response = await self.client.post("/api/bookings", json={"slot_id": slot.id})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["source"]["title"], "Абонемент на 4 занятия")

        # Бот (другой процесс) отправляет уведомление из outbox.
        sent = await dispatch_due_notifications(self.bot_repo, self.bot, LOGGER)
        self.assertEqual(sent, 1)
        messages = self._sent_to(501)
        self.assertEqual(len(messages), 1)
        self.assertIn("Вы записаны", messages[0].text)
        self.assertIn("«Абонемент на 4 занятия»", messages[0].text)
        # Повторная обработка не отправляет снова.
        self.assertEqual(
            await dispatch_due_notifications(self.bot_repo, self.bot, LOGGER), 0
        )
        self.assertEqual(len(self._sent_to(501)), 1)

    async def test_redelivery_after_crash_does_not_repeat_financial_operation(self):
        await self._paid(502)
        slot = await self.bot_repo.create_class_slot(
            "beginner", datetime.now(timezone.utc) + timedelta(days=3), 5, ADMIN_ID
        )
        await self._login(502)
        await self.client.post("/api/bookings", json={"slot_id": slot.id})
        balance = await self.bot_repo.get_lesson_credits(502)

        # Процесс забрал уведомление и «упал», не отметив результат.
        claimed = await self.bot_repo.claim_due_notifications()
        self.assertEqual(len(claimed), 1)
        async with self.admin_pool.acquire() as connection:
            await connection.execute(
                'UPDATE "{}".user_notifications '
                "SET next_attempt_at = NOW() - INTERVAL '1 second'".format(self.schema)
            )

        sent = await dispatch_due_notifications(self.bot_repo, self.bot, LOGGER)

        self.assertEqual(sent, 1)
        self.assertEqual(await self.bot_repo.get_lesson_credits(502), balance)
        async with self.admin_pool.acquire() as connection:
            uses = await connection.fetchval(
                'SELECT COUNT(*) FROM "{}".lesson_credit_ledger '
                "WHERE telegram_id = 502 AND entry_type = 'lesson_use'".format(
                    self.schema
                )
            )
        self.assertEqual(uses, 1)

    async def test_studio_cancellation_is_visible_on_website_and_in_history(self):
        slot = await self.bot_repo.create_class_slot(
            "intermediate", datetime.now(timezone.utc) + timedelta(days=2), 5, ADMIN_ID
        )
        for telegram_id in (601, 602):
            await self._paid(telegram_id, lessons=1)
            await self._login(telegram_id)
            response = await self.client.post(
                "/api/bookings", json={"slot_id": slot.id}
            )
            self.assertEqual(response.status_code, 201)
        await dispatch_due_notifications(self.bot_repo, self.bot, LOGGER)
        self.session.requests.clear()

        result = await ScheduleService(self.bot_repo).cancel(
            slot.id, ADMIN_ID, "болезнь преподавателя"
        )
        self.assertEqual(len(result.refunds), 2)

        await dispatch_due_notifications(self.bot_repo, self.bot, LOGGER)
        for telegram_id in (601, 602):
            texts = [m.text for m in self._sent_to(telegram_id)]
            self.assertTrue(
                any("Занятие отменено студией" in text for text in texts), texts
            )
        overview = (await self.client.get("/api/users/me/overview")).json()
        self.assertEqual(overview["packages"]["balance"], 1)
        self.assertEqual(overview["upcoming"], [])
        history = (await self.client.get("/api/history/operations")).json()
        self.assertEqual(history["items"][0]["operation"], "studio_cancellation")
        self.assertEqual(history["items"][0]["reason"], "болезнь преподавателя")
        classes = (await self.client.get("/api/history/classes")).json()
        self.assertEqual(classes["items"][0]["status"], "cancelled_by_studio")
        schedule = (await self.client.get("/api/schedule")).json()
        self.assertNotIn(slot.id, [item["id"] for item in schedule])

    async def test_website_login_through_bot_end_to_end(self):
        started = await self.client.post("/api/auth/telegram/connect", json={})
        self.assertEqual(started.status_code, 201)
        token = started.json()["deep_link"].split("start=c_", 1)[1]

        dispatcher = Dispatcher()
        dispatcher["repository"] = self.bot_repo
        dispatcher["support_limiter"] = SupportRateLimiter()
        dispatcher["payment_gateway"] = SimpleNamespace(is_configured=False)
        dispatcher["restart_controller"] = SimpleNamespace()
        dispatcher.include_router(bot_router)
        user = User(id=701, is_bot=False, first_name="Анна")
        chat = Chat(id=701, type="private")
        await dispatcher.feed_update(
            self.bot,
            Update(
                update_id=1,
                message=Message(
                    message_id=1,
                    date=datetime.now(timezone.utc),
                    chat=chat,
                    from_user=user,
                    text="/start c_" + token,
                ),
            ),
        )
        self.assertIn("Вход на сайт студии", self._sent_to(701)[-1].text)
        await dispatcher.feed_update(
            self.bot,
            Update(
                update_id=2,
                callback_query=CallbackQuery(
                    id="cb",
                    from_user=user,
                    chat_instance="ci",
                    data="tgc:" + token,
                    message=Message(
                        message_id=2,
                        date=datetime.now(timezone.utc),
                        chat=chat,
                        text="экран",
                    ),
                ),
            ),
        )

        completed = await self.client.post("/api/auth/telegram/connect/complete")
        self.assertEqual(completed.json()["status"], "completed", completed.text)
        me = await self.client.get("/api/auth/me")
        self.assertEqual(me.json()["telegram_id"], 701)
        again = await self.client.post("/api/auth/telegram/connect/complete")
        self.assertEqual(again.status_code, 400)

    async def test_concurrent_web_cancellations_refund_once(self):
        await self._paid(801, lessons=1)
        slot = await self.bot_repo.create_class_slot(
            "beginner", datetime.now(timezone.utc) + timedelta(days=3), 5, ADMIN_ID
        )
        await self._login(801)
        await self.client.post("/api/bookings", json={"slot_id": slot.id})

        responses = await asyncio.gather(
            *(self.client.delete("/api/bookings/{}".format(slot.id)) for _ in range(5))
        )

        self.assertTrue(all(r.status_code == 204 for r in responses))
        self.assertEqual(await self.bot_repo.get_lesson_credits(801), 1)
        notices = await self.bot_repo.list_notifications(801, limit=50)
        self.assertEqual([n.kind for n in notices].count("booking_cancelled"), 1)


if __name__ == "__main__":
    unittest.main()
