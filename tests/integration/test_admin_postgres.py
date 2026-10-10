"""Выборки админ-панели сайта и права веб-администратора на настоящей
PostgreSQL (SQL из database/admin_queries.py и _WEB_USER_SELECT)."""

import os
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
from tests.support import apply_migrations, db_migrate

from flamenco_bot.database.repository import (
    EmailAlreadyRegisteredError,
    PostgresRepository,
)
from flamenco_bot.services import AdminService

MIGRATIONS = db_migrate().MIGRATIONS_DIRECTORY


DATABASE_URL = os.getenv("TEST_DATABASE_URL")
ADMIN_ID = 9001


@unittest.skipUnless(
    DATABASE_URL,
    "TEST_DATABASE_URL is not set; configure a dedicated test PostgreSQL database",
)
class AdminQueriesPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "test_admin_{}_{}".format(os.getpid(), time.time_ns())
        self.admin_pool = await asyncpg.create_pool(DATABASE_URL)
        async with self.admin_pool.acquire() as connection:
            await connection.execute('CREATE SCHEMA "{}"'.format(self.schema))
        self.pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            server_settings={"search_path": self.schema},
        )
        self.repo = PostgresRepository(self.pool)
        await apply_migrations(self.pool)
        await self.repo.initialize()
        await self.repo.get_or_create_profile(ADMIN_ID, "Админ")
        async with self.pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO staff_members (telegram_id, role) VALUES ($1, 'admin') "
                "ON CONFLICT (telegram_id) DO UPDATE SET role = 'admin'",
                ADMIN_ID,
            )

    async def asyncTearDown(self):
        await self.pool.close()
        async with self.admin_pool.acquire() as connection:
            await connection.execute(
                'DROP SCHEMA IF EXISTS "{}" CASCADE'.format(self.schema)
            )
        await self.admin_pool.close()

    async def test_site_rights_come_from_linked_telegram_role(self):
        user = await self.repo.create_web_user("a@example.com", "hash", "Анна")
        self.assertFalse(user.is_admin)
        self.assertIsNone(user.role)

        # Сотрудник с привязанным Telegram — сотрудник и на сайте.
        other = await self.repo.create_web_user("b@example.com", "hash", "Боб")
        await self.repo.link_telegram_to_web_user(other.id, ADMIN_ID)
        linked = await self.repo.get_web_user_by_email("b@example.com")
        self.assertTrue(linked.is_admin)
        self.assertEqual(linked.role, "admin")

    async def test_migration_016_moves_existing_admins_to_staff(self):
        # ADMIN_ID назначен в setUp; миграция переносит и старые флаги:
        # проверяем это на её же SQL поверх данных «до 016».
        await self.repo.get_or_create_profile(321, "Старый админ")
        async with self.pool.acquire() as connection:
            await connection.execute("DELETE FROM staff_members")
            await connection.execute(
                "UPDATE bot_users SET is_admin = TRUE WHERE telegram_id = 321"
            )
            await connection.execute(
                (MIGRATIONS / "016_staff_roles.sql").read_text(encoding="utf-8")
            )
            roles = await connection.fetch(
                "SELECT telegram_id, role FROM staff_members ORDER BY telegram_id"
            )
        self.assertEqual([tuple(r) for r in roles], [(321, "admin")])

    async def test_set_credentials_only_once_and_unique(self):
        await self.repo.get_or_create_profile(555, "Анна")
        telegram_only = await self.repo.get_or_create_web_user_from_telegram(
            555, "Анна"
        )
        await self.repo.create_web_user("taken@example.com", "hash", "Другой")
        with self.assertRaises(EmailAlreadyRegisteredError):
            await self.repo.set_web_user_credentials(
                telegram_only.id, "taken@example.com", "hash"
            )
        self.assertTrue(
            await self.repo.set_web_user_credentials(
                telegram_only.id, "anna@example.com", "hash"
            )
        )
        self.assertFalse(
            await self.repo.set_web_user_credentials(
                telegram_only.id, "again@example.com", "hash"
            )
        )
        found = await self.repo.get_web_user_by_email("ANNA@example.com")
        self.assertEqual(found.id, telegram_only.id)
        self.assertEqual(found.telegram_id, 555)

    async def test_dashboard_counts(self):
        now = datetime.now(timezone.utc)
        await self.repo.get_or_create_profile(555, "Анна")
        soon = await self.repo.create_class_slot(
            "beginner", now + timedelta(hours=2), 5, ADMIN_ID
        )
        await self.repo.create_class_slot(
            "intermediate", now + timedelta(days=3), 4, ADMIN_ID
        )
        async with self.pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO lesson_bookings (slot_id, telegram_id) VALUES ($1, 555)",
                soon.id,
            )
        payment = await self.repo.create_lesson_payment(
            telegram_id=555,
            package_key="single",
            package_title="Разовое занятие",
            lessons=1,
            amount_minor=150000,
            provider_payment_id="p-1",
            confirmation_url="https://pay.example.test/1",
            idempotence_key=uuid.uuid4(),
        )
        async with self.pool.acquire() as connection:
            await connection.execute(
                "UPDATE lesson_payments SET status = 'succeeded', completed_at = NOW() "
                "WHERE id = $1",
                payment.id,
            )
        await self.repo.create_support_message(555, "Помогите")

        day_start = now - timedelta(hours=1)
        dashboard = await self.repo.get_admin_dashboard(
            day_start, now + timedelta(hours=12), now + timedelta(days=7)
        )
        self.assertEqual(dashboard.slots_today, 1)
        self.assertEqual(dashboard.bookings_today, 1)
        self.assertEqual(dashboard.free_seats_today, 4)
        self.assertEqual(dashboard.slots_week, 2)
        self.assertEqual(dashboard.free_seats_week, 8)
        self.assertEqual(dashboard.sales_today_count, 1)
        self.assertEqual(dashboard.sales_today_minor, 150000)
        self.assertEqual(dashboard.open_tickets, 1)
        self.assertEqual(dashboard.total_profiles, 2)

        # Сервис считает «сегодня» по часовому поясу студии — просто не падает.
        await AdminService(self.repo).dashboard()

        payments = await self.repo.list_admin_payments(10)
        self.assertEqual(payments[0].user_name, "Анна")
        self.assertEqual(payments[0].status, "succeeded")

        tickets = await self.repo.list_admin_support_tickets("open", 10)
        self.assertEqual(tickets[0].user_name, "Анна")
        self.assertEqual(tickets[0].last_message, "Помогите")

    async def test_accounts_search_merges_web_account_and_profile(self):
        await self.repo.get_or_create_profile(555, "Анна Петрова")
        web = await self.repo.create_web_user("anna@example.com", "hash", "Анна")
        await self.repo.link_telegram_to_web_user(web.id, 555)
        await self.repo.create_web_user("solo@example.com", "hash", "Без Telegram")

        everyone = await self.repo.search_admin_accounts("", 50)
        # Админ (только бот), Анна (сайт + бот одной строкой), «Без Telegram».
        self.assertEqual(len(everyone), 3)
        anna = await self.repo.search_admin_accounts("петрова", 50)
        self.assertEqual(len(anna), 1)
        self.assertEqual(anna[0].email, "anna@example.com")
        self.assertEqual(anna[0].telegram_id, 555)
        self.assertEqual(
            [a.email for a in await self.repo.search_admin_accounts("solo@", 50)],
            ["solo@example.com"],
        )
        admins = [a for a in everyone if a.is_admin]
        self.assertEqual([a.telegram_id for a in admins], [ADMIN_ID])

    async def test_database_metrics(self):
        metrics = await self.repo.get_database_metrics()
        self.assertEqual(metrics.backend, "postgres")
        self.assertGreater(metrics.database_size_bytes, 0)
        self.assertGreaterEqual(metrics.connections, 1)
        self.assertGreaterEqual(metrics.ping_ms, 0)


if __name__ == "__main__":
    unittest.main()
