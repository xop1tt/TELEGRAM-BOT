"""Доменные сценарии на настоящем PostgreSQL (не на InMemory/моках).

Закрытие слота с записями, возврат после частичного использования занятий,
конфликт привязки Telegram, отображение времени из TIMESTAMPTZ в поясе
студии и хранение веб-сессий только в виде SHA-256.
"""

import hashlib
import hmac
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
from tests.support import apply_migrations

from flamenco_bot.database import repository as repository_module
from flamenco_bot.database.repository import (
    BookingCooldownError,
    PostgresRepository,
    SlotUnavailableError,
    TelegramAlreadyLinkedError,
    hash_session_token,
)
from flamenco_bot.presentation import format_class_time
from flamenco_bot.services import AuthService, PaymentService, RefundStatus

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
BOT_TOKEN = "123456:domain-test-token"
# Europe/Moscow — постоянное UTC+3 (без перехода на летнее время с 2014 г.).
MOSCOW_OFFSET = timedelta(hours=3)


def signed_payload(telegram_id):
    data = {"id": telegram_id, "first_name": "Анна", "auth_date": int(time.time())}
    check = "\n".join("{}={}".format(k, data[k]) for k in sorted(data))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return data


@unittest.skipUnless(
    DATABASE_URL,
    "TEST_DATABASE_URL is not set; configure a dedicated test PostgreSQL database",
)
class DomainPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "test_domain_{}_{}".format(os.getpid(), time.time_ns())
        self.admin_pool = await asyncpg.create_pool(DATABASE_URL)
        async with self.admin_pool.acquire() as connection:
            await connection.execute('CREATE SCHEMA "{}"'.format(self.schema))
        self.pool = await asyncpg.create_pool(
            DATABASE_URL, server_settings={"search_path": self.schema}
        )
        self.repo = PostgresRepository(self.pool)
        await apply_migrations(self.pool)
        await self.repo.initialize()

    async def asyncTearDown(self):
        await self.pool.close()
        async with self.admin_pool.acquire() as connection:
            await connection.execute(
                'DROP SCHEMA IF EXISTS "{}" CASCADE'.format(self.schema)
            )
        await self.admin_pool.close()

    async def _user(self, telegram_id, credits):
        await self.repo.get_or_create_profile(telegram_id, "Участник")
        async with self.pool.acquire() as connection:
            await connection.execute(
                "UPDATE bot_users SET lesson_credits = $2 WHERE telegram_id = $1",
                telegram_id,
                credits,
            )

    async def _slot(self, days=3, capacity=5):
        return await self.repo.create_class_slot(
            "beginner", datetime.now(timezone.utc) + timedelta(days=days), capacity, 1
        )

    # 1. close_class_slot при существующих записях — текущее поведение.
    async def test_closing_slot_keeps_existing_bookings_and_credits(self):
        """Закрытие запрещает новые записи, но существующие не отменяет и
        занятия не возвращает (правило «отмена занятия» не определено)."""
        await self._user(100, 1)
        await self._user(101, 1)
        slot = await self._slot()
        await self.repo.book_class_slot(slot.id, 100)

        self.assertTrue(await self.repo.close_class_slot(slot.id))
        self.assertFalse(await self.repo.close_class_slot(slot.id))

        bookings = await self.repo.list_bookings_for_telegram_id(100)
        self.assertEqual(
            [(b.booking_status, b.slot_status) for b in bookings],
            [("confirmed", "closed")],
        )
        self.assertEqual(await self.repo.get_lesson_credits(100), 0)
        self.assertEqual(await self.repo.list_available_class_slots("beginner"), [])
        with self.assertRaises(SlotUnavailableError):
            await self.repo.book_class_slot(slot.id, 101)
        self.assertEqual(await self.repo.get_lesson_credits(101), 1)

        # Участник по-прежнему может отменить запись сам — занятие вернётся.
        self.assertTrue(await self.repo.cancel_class_slot_booking(slot.id, 100))
        self.assertEqual(await self.repo.get_lesson_credits(100), 1)

    # 2. Возврат после частичного использования абонемента.
    async def test_refund_is_rejected_after_lessons_were_partly_used(self):
        telegram_id = 200
        await self._user(telegram_id, 0)
        attempt = await self.repo.begin_lesson_payment_attempt(
            telegram_id, "pack_4", "Абонемент на 4 занятия", 4, 360000
        )
        payment = await self.repo.create_lesson_payment(
            telegram_id=telegram_id,
            package_key="pack_4",
            package_title=attempt.package_title,
            lessons=4,
            amount_minor=360000,
            provider_payment_id="yk-200",
            confirmation_url="https://yoomoney.ru/x",
            idempotence_key=attempt.idempotence_key,
        )
        self.assertTrue(
            await self.repo.complete_lesson_payment(payment.id, telegram_id)
        )
        await self.repo.book_class_slot((await self._slot()).id, telegram_id)
        self.assertEqual(await self.repo.get_lesson_credits(telegram_id), 3)

        gateway = AsyncMock()
        result = await PaymentService(self.repo, gateway).process_refund(
            payment.id, admin_id=1, reason="Просьба участника"
        )

        self.assertEqual(result.status, RefundStatus.INVALID)
        gateway.create_refund.assert_not_awaited()
        self.assertEqual(await self.repo.get_lesson_credits(telegram_id), 3)
        stored = await self.repo.get_lesson_payment(payment.id, telegram_id)
        self.assertEqual(stored.status, "succeeded")
        async with self.pool.acquire() as connection:
            reservations = await connection.fetchval(
                "SELECT COUNT(*) FROM lesson_credit_ledger "
                "WHERE payment_id = $1 AND entry_type = 'refund_reservation'",
                payment.id,
            )
        self.assertEqual(reservations, 0)

    # 3. Telegram уже привязан к другому веб-аккаунту.
    async def test_link_telegram_owned_by_another_web_account_is_rejected(self):
        auth = AuthService(self.repo, BOT_TOKEN)
        owner = await auth.register_with_email(
            "owner@example.com", "long-password", "Владелец"
        )
        await auth.link_telegram(owner.id, signed_payload(300))
        email_user = await auth.register_with_email(
            "anna@example.com", "long-password", "Анна"
        )

        with self.assertRaises(TelegramAlreadyLinkedError):
            await auth.link_telegram(email_user.id, signed_payload(300))

        self.assertIsNone(
            (await self.repo.get_web_user_by_id(email_user.id)).telegram_id
        )
        self.assertEqual(
            (await self.repo.get_web_user_by_id(owner.id)).telegram_id, 300
        )

    async def test_link_telegram_merges_account_created_by_telegram_login(self):
        """Один участник — один аккаунт: аккаунт, созданный только входом
        через Telegram (без email), объединяется с email-аккаунтом."""
        auth = AuthService(self.repo, BOT_TOKEN)
        telegram_user = await auth.login_with_telegram(signed_payload(300))
        email_user = await auth.register_with_email(
            "anna@example.com", "long-password", "Анна"
        )

        linked = await auth.link_telegram(email_user.id, signed_payload(300))

        self.assertEqual(linked.id, email_user.id)
        self.assertIsNone(await self.repo.get_web_user_by_id(telegram_user.id))
        async with self.pool.acquire() as connection:
            web_accounts = await connection.fetchval(
                "SELECT COUNT(*) FROM users WHERE telegram_id = 300"
            )
            profiles = await connection.fetchval(
                "SELECT COUNT(*) FROM bot_users WHERE telegram_id = 300"
            )
        self.assertEqual((web_accounts, profiles), (1, 1))
        again = await auth.login_with_telegram(signed_payload(300))
        self.assertEqual(again.id, email_user.id)

    # 4–5. Время из TIMESTAMPTZ — в Europe/Moscow, со сменой даты.
    async def test_slot_time_from_postgres_is_shown_in_studio_timezone(self):
        base = (datetime.now(timezone.utc) + timedelta(days=5)).date()
        evening = datetime(base.year, base.month, base.day, 16, 0, tzinfo=timezone.utc)
        late = datetime(base.year, base.month, base.day, 22, 30, tzinfo=timezone.utc)
        for starts_at in (evening, late):
            await self.repo.create_class_slot("beginner", starts_at, 5, 1)

        slots = await self.repo.list_class_slots(class_key="beginner")
        shown = [format_class_time(slot.starts_at) for slot in slots]

        self.assertEqual(slots[0].starts_at, evening)  # из БД вернулся тот же момент
        next_day = (late + MOSCOW_OFFSET).strftime("%d.%m")
        self.assertTrue(
            shown[0].endswith("{} · 19:00".format(evening.strftime("%d.%m")))
        )
        # 22:30 UTC — это уже следующие сутки по Москве.
        self.assertTrue(shown[1].endswith("{} · 01:30".format(next_day)))
        self.assertNotEqual(next_day, late.strftime("%d.%m"))

    # Кулдаун повторной записи — одно значение для Python-проверки и SQL.
    async def test_rebook_cooldown_has_single_source(self):
        await self._user(500, 2)
        slot = await self._slot()
        await self.repo.book_class_slot(slot.id, 500)
        await self.repo.cancel_class_slot_booking(slot.id, 500)
        with self.assertRaises(BookingCooldownError):
            await self.repo.book_class_slot(slot.id, 500)

        # Если бы SQL держал свой литерал «12 hours», запись при нулевом
        # кулдауне упала бы на ON CONFLICT ... WHERE (SlotUnavailableError).
        with patch.object(repository_module, "BOOKING_REBOOK_COOLDOWN", timedelta(0)):
            booking = await self.repo.book_class_slot(slot.id, 500)
        self.assertFalse(booking.already_booked)
        self.assertEqual(await self.repo.get_lesson_credits(500), 1)

    # Имя участника — из профиля бота и на сайте.
    async def test_web_account_shows_profile_name_from_bot(self):
        auth = AuthService(self.repo, BOT_TOKEN)
        user = await auth.login_with_telegram(signed_payload(600))
        self.assertEqual(user.display_name, "Анна")

        await self.repo.update_user_name(600, "Анна Петрова")  # «Изменить имя» в боте

        self.assertEqual(
            (await self.repo.get_web_user_by_id(user.id)).display_name, "Анна Петрова"
        )
        self.assertEqual(
            (await self.repo.get_web_user_by_telegram_id(600)).display_name,
            "Анна Петрова",
        )
        again = await auth.login_with_telegram(signed_payload(600))
        self.assertEqual(again.display_name, "Анна Петрова")

        email_only = await auth.register_with_email("e@example.com", "long-pass", "Ева")
        self.assertEqual(
            (await self.repo.get_web_user_by_id(email_only.id)).display_name, "Ева"
        )

    # 6. Токен веб-сессии не хранится в открытом виде.
    async def test_session_token_is_stored_only_as_sha256(self):
        await self.repo.get_or_create_profile(400, "Анна")
        user = await self.repo.get_or_create_web_user_from_telegram(400, "Анна")
        token = "raw-cookie-token"
        await self.repo.create_web_session(
            user.id, token, datetime.now(timezone.utc) + timedelta(days=7)
        )

        async with self.pool.acquire() as connection:
            stored = await connection.fetch("SELECT token_hash FROM web_sessions")
            leaked = await connection.fetchval(
                "SELECT COUNT(*) FROM web_sessions WHERE token_hash = $1", token
            )
            sql_hash = await connection.fetchval(
                "SELECT encode(sha256(convert_to($1, 'UTF8')), 'hex')", token
            )
        self.assertEqual(
            [row["token_hash"] for row in stored], [hash_session_token(token)]
        )
        self.assertEqual(leaked, 0)
        # Миграция 009 переводит старые строки тем же выражением — cookie
        # сессий, созданных до миграции, продолжают работать.
        self.assertEqual(sql_hash, hash_session_token(token))

        self.assertEqual(await self.repo.get_web_session_user_id(token), user.id)
        await self.repo.delete_web_session(token)
        self.assertIsNone(await self.repo.get_web_session_user_id(token))


if __name__ == "__main__":
    unittest.main()
