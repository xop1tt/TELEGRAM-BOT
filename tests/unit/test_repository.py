import errno
import socket
import ssl
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from flamenco_bot.database.repository import (
    DatabaseUnavailableError,
    REQUIRED_SCHEMA_VERSION,
    PostgresRepository,
    SchemaVersionError,
    hash_session_token,
    is_database_configured,
)
from tests.pg import PostgresTestCase, fetchval, sql
from tests.support import FakePool

# Публичный корневой сертификат Supabase (не секрет).
SUPABASE_CA = (
    Path(__file__).resolve().parent.parent / "fixtures" / ("supabase-root-2021-ca.crt")
)


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool = FakePool()
        self.repository = PostgresRepository(self.pool)

    async def test_profile_operations_use_parameterized_queries(self):
        profile = await self.repository.get_or_create_profile(1001, "Анна", False)
        self.assertEqual(profile.telegram_id, 1001)
        await self.repository.update_phone(1001, "+79990000000")
        await self.repository.update_user_name(1001, "Новое имя")
        update_queries = self.pool.connection.calls[1:]
        self.assertEqual(update_queries[0][1], (1001, "+79990000000"))
        self.assertEqual(update_queries[1][1], (1001, "Новое имя"))

    async def test_profile_load_preserves_admin_status_set_in_database(self):
        self.pool.connection.profile_admin_status[1001] = True

        profile = await self.repository.get_or_create_profile(1001, "Анна", False)

        self.assertTrue(profile.is_admin)
        query, _ = self.pool.connection.calls[-1]
        self.assertIn("SET is_admin = bot_users.is_admin", query)
        self.assertNotIn("EXCLUDED.is_admin", query)

    async def test_profile_search_and_exact_lookup(self):
        profiles = await self.repository.search_profiles("Анна", limit=10)
        self.assertEqual(profiles[0].telegram_id, 1001)
        query, args = self.pool.connection.calls[-1]
        self.assertIn("user_name ILIKE $2", query)
        self.assertEqual(args, ("Анна", "%Анна%", 10))

        profile = await self.repository.get_profile(1001)
        if profile is None:
            self.fail("Profile lookup returned no profile")
        self.assertEqual(profile.telegram_id, 1001)
        query, args = self.pool.connection.calls[-1]
        self.assertIn("WHERE telegram_id = $1", query)
        self.assertEqual(args, (1001,))

        with self.assertRaises(ValueError):
            await self.repository.search_profiles("")
        with self.assertRaises(ValueError):
            await self.repository.search_profiles("x", limit=101)

    async def test_request_operations_and_validation(self):
        request = await self.repository.create_lesson_request(
            1001,
            "booking",
            "вечером",
        )
        self.assertEqual(request.id, 7)
        self.assertEqual(len(await self.repository.list_pending_requests()), 1)
        self.assertTrue(await self.repository.complete_request(7))
        with self.assertRaises(ValueError):
            await self.repository.create_lesson_request(1001, "invalid", "x")
        with self.assertRaises(ValueError):
            await self.repository.list_pending_requests(101)

    async def test_connect_requires_database_url(self):
        with self.assertRaisesRegex(ValueError, "DATABASE_URL"):
            await PostgresRepository.connect("")

    async def test_connect_rejects_documentation_placeholder_hostname(self):
        with self.assertRaisesRegex(ValueError, "примерный hostname"):
            await PostgresRepository.connect(
                "postgresql://bot@db.example.com:5432/flamenco_bot"
            )

    async def test_connect_reports_refused_local_postgres_without_connection_details(
        self,
    ):
        with patch(
            "flamenco_bot.database.repository.asyncpg.create_pool",
            new=AsyncMock(
                side_effect=OSError(errno.ECONNREFUSED, "Connection refused")
            ),
        ):
            with self.assertRaisesRegex(
                DatabaseUnavailableError,
                "сервер запущен",
            ):
                await PostgresRepository.connect(
                    "postgresql://bot:private-password@localhost:5432/bot",
                    ssl_mode="disable",
                    allow_insecure_local=True,
                )

    async def test_connect_reports_postgres_hostname_resolution_failure(self):
        with patch(
            "flamenco_bot.database.repository.asyncpg.create_pool",
            new=AsyncMock(
                side_effect=socket.gaierror(
                    socket.EAI_NONAME, "Name or service not known"
                )
            ),
        ):
            with self.assertRaisesRegex(
                DatabaseUnavailableError,
                "разрешить hostname",
            ):
                await PostgresRepository.connect(
                    "postgresql://bot@db.invalid:5432/bot",
                )

    async def test_connect_enforces_verified_tls_without_connecting(self):
        pool = FakePool()
        with patch(
            "flamenco_bot.database.repository.asyncpg.create_pool",
            new=AsyncMock(return_value=pool),
        ) as create_pool:
            repository = await PostgresRepository.connect(
                "postgresql://bot@db.test.internal/bot"
            )

        self.assertIs(repository._pool, pool)
        create_pool.assert_awaited_once()
        pool_call = create_pool.await_args
        if pool_call is None:
            self.fail("PostgreSQL pool was not constructed")
        self.assertEqual(pool_call.kwargs["min_size"], 1)
        self.assertEqual(pool_call.kwargs["max_size"], 10)
        ssl_context = pool_call.kwargs["ssl"]
        self.assertIsInstance(ssl_context, ssl.SSLContext)
        self.assertEqual(ssl_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ssl_context.check_hostname)

    async def test_private_ca_keeps_verification_without_strict_x509(self):
        # Промежуточный CA пулера Supabase без keyUsage: Python 3.13 в строгом
        # режиме X.509 отклоняет цепочку, хотя libpq verify-full её принимает.
        pool = FakePool()
        with patch(
            "flamenco_bot.database.repository.asyncpg.create_pool",
            new=AsyncMock(return_value=pool),
        ) as create_pool:
            await PostgresRepository.connect(
                "postgresql://bot@db.test.internal/bot", ssl_ca_path=str(SUPABASE_CA)
            )

        pool_call = create_pool.await_args
        if pool_call is None:
            self.fail("PostgreSQL pool was not constructed")
        ssl_context = pool_call.kwargs["ssl"]
        self.assertEqual(ssl_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ssl_context.check_hostname)
        strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
        self.assertFalse(ssl_context.verify_flags & strict)

    async def test_development_can_disable_tls_for_loopback_database_only(self):
        pool = FakePool()
        with patch(
            "flamenco_bot.database.repository.asyncpg.create_pool",
            new=AsyncMock(return_value=pool),
        ) as create_pool:
            await PostgresRepository.connect(
                "postgresql://bot@localhost:5432/bot",
                ssl_mode="disable",
                allow_insecure_local=True,
            )

        pool_call = create_pool.await_args
        if pool_call is None:
            self.fail("PostgreSQL pool was not constructed")
        self.assertIs(pool_call.kwargs["ssl"], False)

    async def test_disabling_tls_is_rejected_for_remote_or_production_database(self):
        with self.assertRaisesRegex(ValueError, "только для localhost"):
            await PostgresRepository.connect(
                "postgresql://bot@db.example.net:5432/bot",
                ssl_mode="disable",
                allow_insecure_local=True,
            )
        with self.assertRaisesRegex(ValueError, "только для localhost"):
            await PostgresRepository.connect(
                "postgresql://bot@localhost:5432/bot",
                ssl_mode="disable",
                allow_insecure_local=False,
            )

    async def test_database_health_check_executes_probe_and_reports_pool(self):
        health = await self.repository.health_check()

        self.assertEqual(
            health.backend,
            "postgres",
        )
        self.assertEqual((health.pool_size, health.idle_connections), (1, 1))
        self.assertEqual(self.pool.connection.calls[-1], ("SELECT 1", ()))

    async def test_initialize_accepts_current_or_newer_schema_without_changing_it(self):
        for versions in (
            {"001", REQUIRED_SCHEMA_VERSION},
            {
                "001",
                REQUIRED_SCHEMA_VERSION,
                "{:03d}".format(int(REQUIRED_SCHEMA_VERSION) + 1),
            },
        ):
            with self.subTest(versions=sorted(versions)):
                self.pool.connection.applied_migrations = versions
                self.pool.connection.calls.clear()
                await self.repository.initialize()
                # Только чтение: схему меняет DATABASE (migrate.py).
                self.assertFalse(
                    [
                        q
                        for q, _ in self.pool.connection.calls
                        if "CREATE" in q or "INSERT" in q
                    ]
                )
        await self.repository.close()

    async def test_initialize_refuses_outdated_or_missing_schema(self):
        older = "{:03d}".format(int(REQUIRED_SCHEMA_VERSION) - 1)
        for versions in (set(), {"001", older}):
            with self.subTest(versions=sorted(versions)):
                self.pool.connection.applied_migrations = versions
                with self.assertRaisesRegex(SchemaVersionError, "migrate.py"):
                    await self.repository.initialize()

    async def test_database_configuration_detects_missing_and_placeholder_urls(self):
        self.assertFalse(is_database_configured(""))
        self.assertFalse(
            is_database_configured("postgresql://bot@db.example.com/flamenco_bot")
        )
        self.assertTrue(
            is_database_configured("postgresql://bot@db.studio.internal/flamenco_bot")
        )


class RepositoryBehaviourTests(PostgresTestCase):
    """Поведение репозитория на настоящей базе: сессии, платежи, возвраты."""

    async def test_profile_round_trip_keeps_name_phone_and_admin_flag(self):
        repository = self.repository
        profile = await repository.get_or_create_profile(1001, "Анна", False)
        self.assertEqual(profile.user_name, "Анна")
        await repository.update_phone(1001, "+79990000000")
        await repository.update_user_name(1001, "Анна Мария")
        updated = await repository.get_or_create_profile(1001, "Ignored", False)
        self.assertEqual(updated.phone, "+79990000000")
        self.assertEqual(updated.user_name, "Анна Мария")
        self.assertFalse(updated.is_admin)

    async def test_web_session_round_trip_and_expiry(self):
        repository = self.repository
        user = await repository.create_web_user("anna@example.com", "hash", "Анна")
        future = datetime.now(timezone.utc) + timedelta(days=7)

        await repository.create_web_session(user.id, "token-a", future)
        self.assertEqual(await repository.get_web_session_user_id("token-a"), user.id)
        self.assertIsNone(await repository.get_web_session_user_id("unknown-token"))

        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        await repository.create_web_session(user.id, "token-expired", past)
        self.assertIsNone(await repository.get_web_session_user_id("token-expired"))

        await repository.delete_web_session("token-a")
        self.assertIsNone(await repository.get_web_session_user_id("token-a"))

    async def test_creating_a_session_prunes_the_users_expired_ones(self):
        repository = self.repository
        user = await repository.create_web_user("anna@example.com", "hash", "Анна")
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        future = datetime.now(timezone.utc) + timedelta(days=7)

        await repository.create_web_session(user.id, "stale-token", past)
        await repository.create_web_session(user.id, "fresh-token", future)

        self.assertEqual(
            await fetchval(
                repository,
                "SELECT COUNT(*) FROM web_sessions WHERE token_hash = $1",
                hash_session_token("stale-token"),
            ),
            0,
        )
        self.assertEqual(
            await repository.get_web_session_user_id("fresh-token"), user.id
        )

    async def test_payment_adds_credits_only_once_and_tracks_activity(self):
        repository = self.repository
        await repository.get_or_create_profile(1001, "Анна", False)
        attempt = await repository.begin_lesson_payment_attempt(
            telegram_id=1001,
            package_key="pack_4",
            package_title="Абонемент на 4 занятия",
            lessons=4,
            amount_minor=360000,
        )
        retry = await repository.begin_lesson_payment_attempt(
            telegram_id=1001,
            package_key="pack_4",
            package_title="Изменённое название",
            lessons=5,
            amount_minor=400000,
        )
        self.assertEqual(retry.idempotence_key, attempt.idempotence_key)
        payment = await repository.create_lesson_payment(
            telegram_id=1001,
            package_key="pack_4",
            package_title="Абонемент на 4 занятия",
            lessons=4,
            amount_minor=360000,
            provider_payment_id="provider-payment",
            confirmation_url="https://pay.example.test",
            idempotence_key=attempt.idempotence_key,
        )

        self.assertTrue(await repository.complete_lesson_payment(payment.id, 1001))
        self.assertFalse(await repository.complete_lesson_payment(payment.id, 1001))
        self.assertEqual(await repository.get_lesson_credits(1001), 4)
        self.assertFalse(await repository.complete_lesson_payment(payment.id, 1001))
        self.assertEqual(await repository.get_lesson_credits(1001), 4)
        next_attempt = await repository.begin_lesson_payment_attempt(
            telegram_id=1001,
            package_key="pack_4",
            package_title="Абонемент на 4 занятия",
            lessons=4,
            amount_minor=360000,
        )
        self.assertNotEqual(next_attempt.idempotence_key, attempt.idempotence_key)
        await repository.record_activity(1001)
        stats = await repository.get_user_statistics(
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        self.assertEqual((stats.total_users, stats.online_users), (1, 1))

    async def test_payment_history_is_scoped_and_ordered(self):
        repository = self.repository
        await repository.get_or_create_profile(1001, "Анна", False)
        await repository.get_or_create_profile(1002, "Мария", False)

        first_attempt = await repository.begin_lesson_payment_attempt(
            1001, "single", "Разовое занятие", 1, 100000
        )
        await repository.create_lesson_payment(
            telegram_id=1001,
            package_key="single",
            package_title="Разовое занятие",
            lessons=1,
            amount_minor=100000,
            provider_payment_id="provider-payment-1",
            confirmation_url="https://pay.example.test/1",
            idempotence_key=first_attempt.idempotence_key,
        )
        second_attempt = await repository.begin_lesson_payment_attempt(
            1001, "pack_4", "Абонемент на 4 занятия", 4, 360000
        )
        second_payment = await repository.create_lesson_payment(
            telegram_id=1001,
            package_key="pack_4",
            package_title="Абонемент на 4 занятия",
            lessons=4,
            amount_minor=360000,
            provider_payment_id="provider-payment-2",
            confirmation_url="https://pay.example.test/2",
            idempotence_key=second_attempt.idempotence_key,
        )
        other_user_attempt = await repository.begin_lesson_payment_attempt(
            1002, "single", "Разовое занятие", 1, 100000
        )
        await repository.create_lesson_payment(
            telegram_id=1002,
            package_key="single",
            package_title="Разовое занятие",
            lessons=1,
            amount_minor=100000,
            provider_payment_id="provider-payment-3",
            confirmation_url="https://pay.example.test/3",
            idempotence_key=other_user_attempt.idempotence_key,
        )

        history = await repository.list_lesson_payments_for_telegram_id(1001)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].id, second_payment.id)
        self.assertEqual(history[0].package_key, "pack_4")
        self.assertEqual(history[0].status, "pending")
        self.assertEqual(history[1].package_key, "single")

    async def test_lists_only_pending_payments_older_than_cutoff(self):
        repository = self.repository
        await repository.get_or_create_profile(1001, "Анна", False)
        await repository.get_or_create_profile(1002, "Мария", False)

        stale_attempt = await repository.begin_lesson_payment_attempt(
            1001, "single", "Разовое занятие", 1, 100000
        )
        stale_payment = await repository.create_lesson_payment(
            telegram_id=1001,
            package_key="single",
            package_title="Разовое занятие",
            lessons=1,
            amount_minor=100000,
            provider_payment_id="provider-payment-stale",
            confirmation_url="https://pay.example.test/1",
            idempotence_key=stale_attempt.idempotence_key,
        )
        # Задним числом "состарим" платёж — так же, как если бы он
        # действительно провисел в pending дольше порога сверки.
        await sql(
            repository,
            "UPDATE lesson_payments SET created_at = NOW() - INTERVAL '1 hour' "
            "WHERE id = $1",
            stale_payment.id,
        )

        fresh_attempt = await repository.begin_lesson_payment_attempt(
            1002, "single", "Разовое занятие", 1, 100000
        )
        await repository.create_lesson_payment(
            telegram_id=1002,
            package_key="single",
            package_title="Разовое занятие",
            lessons=1,
            amount_minor=100000,
            provider_payment_id="provider-payment-fresh",
            confirmation_url="https://pay.example.test/2",
            idempotence_key=fresh_attempt.idempotence_key,
        )

        succeeded_attempt = await repository.begin_lesson_payment_attempt(
            1001, "pack_4", "Абонемент на 4 занятия", 4, 360000
        )
        succeeded_payment = await repository.create_lesson_payment(
            telegram_id=1001,
            package_key="pack_4",
            package_title="Абонемент на 4 занятия",
            lessons=4,
            amount_minor=360000,
            provider_payment_id="provider-payment-succeeded",
            confirmation_url="https://pay.example.test/3",
            idempotence_key=succeeded_attempt.idempotence_key,
        )
        await sql(
            repository,
            "UPDATE lesson_payments SET created_at = NOW() - INTERVAL '1 hour' "
            "WHERE id = $1",
            succeeded_payment.id,
        )
        await repository.complete_lesson_payment(succeeded_payment.id, 1001)

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        pending = await repository.list_pending_lesson_payments_older_than(cutoff)

        self.assertEqual([item.id for item in pending], [stale_payment.id])
        self.assertEqual(pending[0].telegram_id, 1001)

    async def test_refund_reserves_and_reconciles_credits_once(self):
        repository = self.repository
        await repository.get_or_create_profile(1001, "Анна", False)
        attempt = await repository.begin_lesson_payment_attempt(
            1001, "pack_4", "Абонемент на 4 занятия", 4, 360000
        )
        payment = await repository.create_lesson_payment(
            telegram_id=1001,
            package_key="pack_4",
            package_title="Абонемент на 4 занятия",
            lessons=4,
            amount_minor=360000,
            provider_payment_id="provider-payment",
            confirmation_url="https://pay.example.test",
            idempotence_key=attempt.idempotence_key,
        )
        await repository.complete_lesson_payment(payment.id, 1001)

        refund = await repository.prepare_lesson_refund(payment.id, 77, "Duplicate")
        self.assertIsNotNone(refund)
        self.assertEqual(await repository.get_lesson_credits(1001), 0)
        retry = await repository.prepare_lesson_refund(payment.id, 77, "ignored")
        self.assertEqual(retry.idempotence_key, refund.idempotence_key)
        await repository.record_provider_refund(payment.id, "canceled-refund")
        self.assertTrue(await repository.release_lesson_refund(payment.id))
        self.assertEqual(await repository.get_lesson_credits(1001), 4)

        refund = await repository.prepare_lesson_refund(payment.id, 77, "Retry")
        self.assertNotEqual(refund.idempotence_key, retry.idempotence_key)
        await repository.record_provider_refund(payment.id, "successful-refund")
        self.assertTrue(await repository.complete_lesson_refund(payment.id))
        self.assertFalse(await repository.complete_lesson_refund(payment.id))
        self.assertEqual(await repository.get_lesson_credits(1001), 0)

        updated = await repository.get_lesson_payment(payment.id, 1001)
        self.assertEqual(updated.status, "refunded")

    async def test_canceled_payment_allows_a_new_checkout_attempt(self):
        repository = self.repository
        await repository.get_or_create_profile(1001, "Анна", False)
        attempt = await repository.begin_lesson_payment_attempt(
            1001, "single", "Разовое занятие", 1, 100000
        )
        payment = await repository.create_lesson_payment(
            telegram_id=1001,
            package_key="single",
            package_title="Разовое занятие",
            lessons=1,
            amount_minor=100000,
            provider_payment_id="provider-payment",
            confirmation_url="https://pay.example.test",
            idempotence_key=attempt.idempotence_key,
        )
        self.assertTrue(await repository.cancel_lesson_payment(payment.id, 1001))

        retry = await repository.begin_lesson_payment_attempt(
            1001, "single", "Разовое занятие", 1, 100000
        )
        self.assertNotEqual(retry.idempotence_key, attempt.idempotence_key)

    async def test_refund_rejects_credits_already_used_by_a_booking(self):
        repository = self.repository
        await repository.get_or_create_profile(1001, "Анна", False)
        attempt = await repository.begin_lesson_payment_attempt(
            1001, "pack_4", "Абонемент на 4 занятия", 4, 360000
        )
        payment = await repository.create_lesson_payment(
            telegram_id=1001,
            package_key="pack_4",
            package_title="Абонемент на 4 занятия",
            lessons=4,
            amount_minor=360000,
            provider_payment_id="used-credit-payment",
            confirmation_url="https://pay.example.test",
            idempotence_key=attempt.idempotence_key,
        )
        await repository.complete_lesson_payment(payment.id, 1001)
        slot = await repository.create_class_slot(
            "beginner", datetime.now(timezone.utc) + timedelta(days=2), 5, 77
        )
        await repository.book_class_slot(slot.id, 1001)

        with self.assertRaisesRegex(ValueError, "неиспользованных"):
            await repository.prepare_lesson_refund(
                payment.id,
                77,
                "Already used",
            )
        self.assertEqual(await repository.get_lesson_credits(1001), 3)
