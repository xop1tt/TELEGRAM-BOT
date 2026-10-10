import os
import time
import unittest
import asyncio
from datetime import datetime, timedelta, timezone

import asyncpg
from tests.support import apply_migrations, db_migrate

from flamenco_bot.database.repository import (
    PaymentAttemptUnresolved,
    PostgresRepository,
    SlotUnavailableError,
)


DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(
    DATABASE_URL,
    "TEST_DATABASE_URL is not set; configure a dedicated test PostgreSQL database",
)
class PostgresIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.admin_pool = await asyncpg.create_pool(DATABASE_URL)
        self.pool = None
        self.repository = None
        self.schema = "test_bot_{}_{}".format(os.getpid(), time.time_ns())
        self.telegram_id = time.time_ns()
        try:
            async with self.admin_pool.acquire() as connection:
                await connection.execute('CREATE SCHEMA "{}"'.format(self.schema))
            self.pool = await asyncpg.create_pool(
                DATABASE_URL,
                server_settings={"search_path": self.schema},
            )
            async with self.pool.acquire() as connection:
                await connection.execute(
                    """
                    CREATE TABLE bot_users (
                        telegram_id BIGINT PRIMARY KEY CHECK (telegram_id > 0),
                        phone TEXT,
                        user_name TEXT NOT NULL,
                        registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        is_admin BOOLEAN NOT NULL DEFAULT FALSE
                    );

                    CREATE TABLE lesson_requests (
                        id BIGSERIAL PRIMARY KEY,
                        telegram_id BIGINT NOT NULL
                            REFERENCES bot_users(telegram_id) ON DELETE CASCADE,
                        kind TEXT NOT NULL
                            CHECK (kind IN ('booking', 'purchase')),
                        details TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'completed')),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )
            self.repository = PostgresRepository(self.pool)
            await apply_migrations(self.pool)
            await self.repository.initialize()
            await self.repository.get_or_create_profile(
                self.telegram_id,
                "Integration test",
                False,
            )
        except BaseException:
            await self._cleanup()
            raise

    async def asyncTearDown(self):
        await self._cleanup()

    def _require_pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise AssertionError("PostgreSQL test pool was not initialized")
        return self.pool

    def _require_repository(self) -> PostgresRepository:
        if self.repository is None:
            raise AssertionError("PostgreSQL test repository was not initialized")
        return self.repository

    async def _grant_credits(self, telegram_id: int, credits: int) -> None:
        """Выдаёт lesson_credits напрямую — бронирование теперь их списывает."""
        async with self._require_pool().acquire() as connection:
            await connection.execute(
                "UPDATE bot_users SET lesson_credits = $2 WHERE telegram_id = $1",
                telegram_id,
                credits,
            )

    async def _cleanup(self):
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
        try:
            if getattr(self, "schema", None) and self.admin_pool is not None:
                async with self.admin_pool.acquire() as connection:
                    await connection.execute(
                        'DROP SCHEMA IF EXISTS "{}" CASCADE'.format(self.schema)
                    )
        finally:
            if self.admin_pool is not None:
                await self.admin_pool.close()
                self.admin_pool = None

    async def test_migration_upgrades_legacy_schema_and_repository_queries_work(self):
        pool = self._require_pool()
        repository = self._require_repository()
        async with pool.acquire() as connection:
            versions = await connection.fetch(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
            index_name = await connection.fetchval(
                "SELECT to_regclass('lesson_requests_pending_created_idx')"
            )
        # Сверяемся со списком файлов миграций, а не с застывшим списком версий,
        # чтобы тест не расходился с реальностью при добавлении новых миграций
        # (так уже произошло: здесь проверялись только "001"-"004", когда в
        # каталоге появилась "005_web_accounts.sql").
        expected_versions = [version for version, _ in db_migrate().migration_files()]
        self.assertEqual(
            [record["version"] for record in versions],
            expected_versions,
        )
        self.assertEqual(index_name, "lesson_requests_pending_created_idx")

        self.assertEqual(await apply_migrations(pool), [])
        await repository.initialize()
        async with pool.acquire() as connection:
            legacy_payment_id = await connection.fetchval(
                """
                INSERT INTO lesson_payments (
                    telegram_id, package_key, lessons, amount_minor,
                    provider_payment_id, confirmation_url
                )
                VALUES ($1, 'single', 1, 100000, $2, $3)
                RETURNING id
                """,
                self.telegram_id,
                "legacy-payment-{}".format(self.telegram_id),
                "https://example.test/payment",
            )
        with self.assertRaisesRegex(PaymentAttemptUnresolved, "до включения"):
            await repository.begin_lesson_payment_attempt(
                self.telegram_id,
                "single",
                "Single lesson",
                1,
                100000,
            )
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE lesson_payments SET status = 'canceled' WHERE id = $1",
                legacy_payment_id,
            )

        profiles = await repository.search_profiles(
            "Integration test",
            limit=10,
        )

        self.assertTrue(
            any(profile.telegram_id == self.telegram_id for profile in profiles)
        )

        attempt = await repository.begin_lesson_payment_attempt(
            telegram_id=self.telegram_id,
            package_key="pack_4",
            package_title="Test pack",
            lessons=4,
            amount_minor=360000,
        )
        payment = await repository.create_lesson_payment(
            telegram_id=self.telegram_id,
            package_key="pack_4",
            package_title="Test pack",
            lessons=4,
            amount_minor=360000,
            provider_payment_id="integration-payment-{}".format(self.telegram_id),
            confirmation_url="https://example.test/payment",
            idempotence_key=attempt.idempotence_key,
        )
        self.assertTrue(
            await repository.complete_lesson_payment(payment.id, self.telegram_id)
        )
        self.assertFalse(
            await repository.complete_lesson_payment(payment.id, self.telegram_id)
        )
        self.assertEqual(await repository.get_lesson_credits(self.telegram_id), 4)
        next_attempt = await repository.begin_lesson_payment_attempt(
            telegram_id=self.telegram_id,
            package_key="pack_4",
            package_title="Test pack",
            lessons=4,
            amount_minor=360000,
        )
        self.assertNotEqual(next_attempt.idempotence_key, attempt.idempotence_key)
        canceled_payment = await repository.create_lesson_payment(
            telegram_id=self.telegram_id,
            package_key="pack_4",
            package_title="Test pack",
            lessons=4,
            amount_minor=360000,
            provider_payment_id="integration-canceled-{}".format(self.telegram_id),
            confirmation_url="https://example.test/payment",
            idempotence_key=next_attempt.idempotence_key,
        )
        self.assertTrue(
            await repository.cancel_lesson_payment(
                canceled_payment.id,
                self.telegram_id,
            )
        )
        retry_attempt = await repository.begin_lesson_payment_attempt(
            telegram_id=self.telegram_id,
            package_key="pack_4",
            package_title="Test pack",
            lessons=4,
            amount_minor=360000,
        )
        self.assertNotEqual(retry_attempt.idempotence_key, next_attempt.idempotence_key)

        refund = await repository.prepare_lesson_refund(
            payment.id,
            self.telegram_id,
            "integration refund",
        )
        self.assertIsNotNone(refund)
        self.assertEqual(await repository.get_lesson_credits(self.telegram_id), 0)
        await repository.record_provider_refund(
            payment.id, "canceled-refund-{}".format(payment.id)
        )
        self.assertTrue(await repository.release_lesson_refund(payment.id))
        self.assertEqual(await repository.get_lesson_credits(self.telegram_id), 4)
        retry_refund = await repository.prepare_lesson_refund(
            payment.id,
            self.telegram_id,
            "retry after provider cancellation",
        )
        self.assertIsNotNone(retry_refund)
        self.assertNotEqual(retry_refund.idempotence_key, refund.idempotence_key)
        await repository.record_provider_refund(
            payment.id, "refund-{}".format(payment.id)
        )
        self.assertTrue(await repository.complete_lesson_refund(payment.id))
        self.assertEqual(await repository.get_lesson_credits(self.telegram_id), 0)

        used_attempt = await repository.begin_lesson_payment_attempt(
            telegram_id=self.telegram_id,
            package_key="single",
            package_title="Single lesson",
            lessons=1,
            amount_minor=100000,
        )
        used_payment = await repository.create_lesson_payment(
            telegram_id=self.telegram_id,
            package_key="single",
            package_title="Single lesson",
            lessons=1,
            amount_minor=100000,
            provider_payment_id="integration-used-{}".format(self.telegram_id),
            confirmation_url="https://example.test/payment",
            idempotence_key=used_attempt.idempotence_key,
        )
        self.assertTrue(
            await repository.complete_lesson_payment(
                used_payment.id,
                self.telegram_id,
            )
        )
        slot = await repository.create_class_slot(
            "beginner",
            datetime.now(timezone.utc) + timedelta(days=2),
            5,
            self.telegram_id,
        )
        await repository.book_class_slot(slot.id, self.telegram_id)
        with self.assertRaisesRegex(ValueError, "неиспользованных"):
            await repository.prepare_lesson_refund(
                used_payment.id,
                self.telegram_id,
                "used credit",
            )

        await repository.record_activity(self.telegram_id)
        statistics = await repository.get_user_statistics(
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        self.assertEqual((statistics.total_users, statistics.online_users), (1, 1))

    async def test_slot_capacity_and_support_ticket_flows_are_transactional(self):
        repository = self._require_repository()
        additional_users = (self.telegram_id + 1, self.telegram_id + 2)
        for telegram_id in additional_users:
            await repository.get_or_create_profile(
                telegram_id,
                "Integration test participant",
                False,
            )
        for telegram_id in (self.telegram_id, *additional_users):
            await self._grant_credits(telegram_id, 1)

        slot = await repository.create_class_slot(
            "beginner",
            datetime.now(timezone.utc) + timedelta(days=1),
            2,
            self.telegram_id,
        )
        outcomes = await asyncio.gather(
            *(
                repository.book_class_slot(slot.id, telegram_id)
                for telegram_id in (self.telegram_id, *additional_users)
            ),
            return_exceptions=True,
        )
        self.assertEqual(
            sum(not isinstance(item, BaseException) for item in outcomes),
            2,
        )
        self.assertEqual(
            sum(isinstance(item, SlotUnavailableError) for item in outcomes),
            1,
        )
        with self.assertRaisesRegex(ValueError, "ниже числа"):
            await repository.update_class_slot_capacity(slot.id, 1)

        ticket_id, created = await repository.create_support_message(
            self.telegram_id,
            "Integration support message",
        )
        same_ticket_id, created_again = await repository.create_support_message(
            self.telegram_id,
            "Follow-up",
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(ticket_id, same_ticket_id)
        self.assertEqual(
            (await repository.list_open_support_tickets())[0].last_message,
            "Follow-up",
        )
        self.assertEqual(
            await repository.reply_support_ticket(
                ticket_id,
                self.telegram_id + 10,
                "Admin response",
            ),
            self.telegram_id,
        )
        self.assertEqual(
            await repository.close_support_ticket(ticket_id, self.telegram_id + 10),
            self.telegram_id,
        )

    async def test_capacity_guard_trigger_rejects_overbooking_bypassing_repository(
        self,
    ):
        """Защита на уровне БД, а не только в `book_class_slot`.

        `book_class_slot` сам не допускает овербукинг (см. тест выше), но это
        единственная линия защиты в коде. Здесь намеренно обходим репозиторий
        и вставляем вторую подтверждённую запись в заполненный слот прямым
        SQL — триггер `lesson_bookings_capacity_guard` (миграция 006) должен
        отклонить её независимо от того, какой код инициировал запись.
        """
        repository = self._require_repository()
        pool = self._require_pool()
        other_telegram_id = self.telegram_id + 1
        await repository.get_or_create_profile(
            other_telegram_id,
            "Second participant",
            False,
        )
        slot = await repository.create_class_slot(
            "beginner",
            datetime.now(timezone.utc) + timedelta(days=1),
            1,
            self.telegram_id,
        )
        await self._grant_credits(self.telegram_id, 1)
        await repository.book_class_slot(slot.id, self.telegram_id)

        async with pool.acquire() as connection:
            with self.assertRaisesRegex(asyncpg.PostgresError, "capacity exceeded"):
                await connection.execute(
                    "INSERT INTO lesson_bookings (slot_id, telegram_id) "
                    "VALUES ($1, $2)",
                    slot.id,
                    other_telegram_id,
                )

    async def test_booking_credits_and_cancellation_lifecycle(self):
        """Бизнес-правило, подтверждённое при аудите: 1 credit за запись,

        возврат при отмене не позднее 24ч до начала, 12ч кулдаун на повторную
        запись тем же пользователем. Защита от овербукинга (миграция 006) и
        идемпотентность бронирования уже покрыты другими тестами — здесь
        специально проверяется связка с балансом против настоящего Postgres.
        """
        from flamenco_bot.database.repository import (
            BookingCooldownError,
            CancellationWindowExpiredError,
            InsufficientLessonCreditsError,
        )

        repository = self._require_repository()
        far_slot = await repository.create_class_slot(
            "beginner",
            datetime.now(timezone.utc) + timedelta(days=2),
            2,
            self.telegram_id,
        )

        with self.assertRaises(InsufficientLessonCreditsError):
            await repository.book_class_slot(far_slot.id, self.telegram_id)

        await self._grant_credits(self.telegram_id, 1)
        await repository.book_class_slot(far_slot.id, self.telegram_id)
        self.assertEqual(await repository.get_lesson_credits(self.telegram_id), 0)

        cancelled = await repository.cancel_class_slot_booking(
            far_slot.id, self.telegram_id
        )
        self.assertTrue(cancelled)
        self.assertEqual(await repository.get_lesson_credits(self.telegram_id), 1)
        self.assertFalse(
            await repository.cancel_class_slot_booking(far_slot.id, self.telegram_id)
        )
        self.assertEqual(await repository.get_lesson_credits(self.telegram_id), 1)

        with self.assertRaises(BookingCooldownError):
            await repository.book_class_slot(far_slot.id, self.telegram_id)

        soon_slot = await repository.create_class_slot(
            "beginner",
            datetime.now(timezone.utc) + timedelta(hours=1),
            1,
            self.telegram_id,
        )
        await repository.book_class_slot(soon_slot.id, self.telegram_id)
        with self.assertRaises(CancellationWindowExpiredError):
            await repository.cancel_class_slot_booking(soon_slot.id, self.telegram_id)

    async def test_web_session_round_trip_and_revocation(self):
        """Серверные сессии (миграция 008) против настоящего Postgres.

        Логика идентична покрытой в ``test_repository.py`` для
        ``InMemoryRepository`` — здесь проверяется, что те же гарантии
        (просрочка, отзыв) держатся на реальном ``NOW()``/constraint'ах, а
        не только в питоновской имитации.
        """
        repository = self._require_repository()
        web_user = await repository.get_or_create_web_user_from_telegram(
            self.telegram_id, "Integration web user"
        )

        future = datetime.now(timezone.utc) + timedelta(days=7)
        await repository.create_web_session(web_user.id, "pg-token-a", future)
        self.assertEqual(
            await repository.get_web_session_user_id("pg-token-a"), web_user.id
        )

        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        await repository.create_web_session(web_user.id, "pg-token-expired", past)
        self.assertIsNone(await repository.get_web_session_user_id("pg-token-expired"))

        await repository.delete_web_session("pg-token-a")
        self.assertIsNone(await repository.get_web_session_user_id("pg-token-a"))
