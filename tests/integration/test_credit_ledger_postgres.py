"""Единая точка изменения баланса занятий на настоящем PostgreSQL.

Баланс (``bot_users.lesson_credits``) и ledger меняются согласованно во всех
операциях; ручная корректировка и сверка; конкурентные сценарии; защита
финансовой истории при удалении профиля; миграция 010 на существующих данных.
"""

import asyncio
import os
import shutil
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
from tests.support import apply_migrations, db_migrate

from flamenco_bot.database.repository import (
    InsufficientLessonCreditsError,
    PostgresRepository,
)

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
MIGRATIONS = db_migrate().MIGRATIONS_DIRECTORY
ADMIN_ID = 9001


@unittest.skipUnless(
    DATABASE_URL,
    "TEST_DATABASE_URL is not set; configure a dedicated test PostgreSQL database",
)
class CreditLedgerPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "test_credits_{}_{}".format(os.getpid(), time.time_ns())
        self.admin_pool = await asyncpg.create_pool(DATABASE_URL)
        async with self.admin_pool.acquire() as connection:
            await connection.execute('CREATE SCHEMA "{}"'.format(self.schema))
        self.pool = await asyncpg.create_pool(
            DATABASE_URL, server_settings={"search_path": self.schema}
        )
        self.repo = PostgresRepository(self.pool)
        await apply_migrations(self.pool)
        await self.repo.initialize()
        await self.repo.get_or_create_profile(ADMIN_ID, "Админ", True)
        async with self.pool.acquire() as connection:
            await connection.execute(
                "UPDATE bot_users SET is_admin = TRUE WHERE telegram_id = $1",
                ADMIN_ID,
            )

    async def asyncTearDown(self):
        await self.pool.close()
        async with self.admin_pool.acquire() as connection:
            await connection.execute(
                'DROP SCHEMA IF EXISTS "{}" CASCADE'.format(self.schema)
            )
        await self.admin_pool.close()

    # --- помощники -------------------------------------------------------

    async def _user(self, telegram_id):
        await self.repo.get_or_create_profile(telegram_id, "Участник", False)

    async def _balance(self, telegram_id):
        return await self.repo.get_lesson_credits(telegram_id)

    async def _ledger(self, telegram_id):
        async with self.pool.acquire() as connection:
            return await connection.fetch(
                "SELECT * FROM lesson_credit_ledger WHERE telegram_id = $1 ORDER BY id",
                telegram_id,
            )

    async def _assert_consistent(self, telegram_id):
        """Баланс == сумма ledger, и сверка не видит расхождения."""
        ledger_sum = sum(row["delta"] for row in await self._ledger(telegram_id))
        self.assertEqual(await self._balance(telegram_id), ledger_sum)
        report = await self.repo.get_credit_reconciliation()
        self.assertNotIn(telegram_id, [m.telegram_id for m in report.mismatches])

    async def _paid(self, telegram_id, lessons=4, tag=None):
        """Создаёт и подтверждает оплату; возвращает id платежа."""
        tag = tag or uuid.uuid4().hex
        attempt = await self.repo.begin_lesson_payment_attempt(
            telegram_id, "pack_4", "Абонемент", lessons, 360000
        )
        payment = await self.repo.create_lesson_payment(
            telegram_id=telegram_id,
            package_key="pack_4",
            package_title="Абонемент",
            lessons=lessons,
            amount_minor=360000,
            provider_payment_id="yk-{}".format(tag),
            confirmation_url="https://yoomoney.ru/x",
            idempotence_key=attempt.idempotence_key,
        )
        self.assertTrue(
            await self.repo.complete_lesson_payment(payment.id, telegram_id)
        )
        return payment.id

    async def _slot(self, days=3, capacity=5):
        return await self.repo.create_class_slot(
            "beginner", datetime.now(timezone.utc) + timedelta(days=days), capacity, 1
        )

    async def _adjust(
        self, telegram_id, delta, reason="тест", key=None, actor=ADMIN_ID
    ):
        return await self.repo.adjust_lesson_credits(
            telegram_id, delta, reason, actor, key or uuid.uuid4()
        )

    # --- операции согласованы с ledger ------------------------------------

    async def test_purchase_credits_balance_and_ledger_together_once(self):
        await self._user(100)
        payment_id = await self._paid(100, lessons=4)

        self.assertEqual(await self._balance(100), 4)
        (row,) = await self._ledger(100)
        self.assertEqual(
            (row["entry_type"], row["delta"], row["payment_id"]),
            ("purchase", 4, payment_id),
        )
        self.assertEqual(row["reference_key"], "payment:{}".format(payment_id))
        # Повторное подтверждение того же платежа ничего не начисляет.
        self.assertFalse(await self.repo.complete_lesson_payment(payment_id, 100))
        self.assertEqual(await self._balance(100), 4)
        await self._assert_consistent(100)

    async def test_booking_and_cancellation_keep_balance_and_ledger_consistent(self):
        await self._user(100)
        await self._paid(100, lessons=2)
        slot = await self._slot()

        booking = await self.repo.book_class_slot(slot.id, 100)
        self.assertEqual(await self._balance(100), 1)
        self.assertEqual(
            [(r["entry_type"], r["delta"]) for r in await self._ledger(100)],
            [("purchase", 2), ("lesson_use", -1)],
        )
        self.assertTrue(
            (await self._ledger(100))[1]["reference_key"].startswith(
                "slot_booking:{}:use:".format(booking.id)
            )
        )
        await self._assert_consistent(100)

        # Повторное нажатие «Записаться» не списывает второй раз.
        self.assertTrue((await self.repo.book_class_slot(slot.id, 100)).already_booked)
        self.assertEqual(await self._balance(100), 1)

        self.assertTrue(await self.repo.cancel_class_slot_booking(slot.id, 100))
        self.assertFalse(await self.repo.cancel_class_slot_booking(slot.id, 100))
        self.assertEqual(await self._balance(100), 2)
        self.assertEqual((await self._ledger(100))[-1]["entry_type"], "adjustment")
        await self._assert_consistent(100)

    async def test_booking_without_credits_changes_nothing(self):
        await self._user(100)
        slot = await self._slot()

        with self.assertRaisesRegex(InsufficientLessonCreditsError, "Купите абонемент"):
            await self.repo.book_class_slot(slot.id, 100)

        self.assertEqual(await self._balance(100), 0)
        self.assertEqual(await self._ledger(100), [])
        self.assertEqual(await self.repo.list_bookings_for_telegram_id(100), [])

    async def test_refund_reserves_once_and_release_restores_once(self):
        await self._user(100)
        payment_id = await self._paid(100, lessons=4)

        refund = await self.repo.prepare_lesson_refund(payment_id, ADMIN_ID, "тест")
        self.assertEqual(await self._balance(100), 0)
        # Повторный запрос возврата не резервирует занятия второй раз.
        again = await self.repo.prepare_lesson_refund(payment_id, ADMIN_ID, "тест")
        self.assertEqual(again.idempotence_key, refund.idempotence_key)
        self.assertEqual(await self._balance(100), 0)
        await self._assert_consistent(100)

        self.assertTrue(await self.repo.release_lesson_refund(payment_id))
        self.assertFalse(await self.repo.release_lesson_refund(payment_id))
        self.assertEqual(await self._balance(100), 4)
        await self._assert_consistent(100)

        await self.repo.prepare_lesson_refund(payment_id, ADMIN_ID, "тест")
        self.assertTrue(await self.repo.complete_lesson_refund(payment_id))
        self.assertFalse(await self.repo.complete_lesson_refund(payment_id))
        self.assertEqual(await self._balance(100), 0)
        # complete_lesson_refund переименовывает все резервации платежа в
        # «refund», в том числе ранее снятую (существующее поведение).
        types = [r["entry_type"] for r in await self._ledger(100)]
        self.assertEqual(types.count("refund_release"), 1)
        self.assertNotIn("refund_reservation", types)
        await self._assert_consistent(100)

    async def test_change_credits_refuses_to_run_outside_a_transaction(self):
        await self._user(100)
        async with self.pool.acquire() as connection:
            with self.assertRaisesRegex(RuntimeError, "транзакции"):
                await PostgresRepository._change_credits(
                    connection, 100, 1, "purchase", "x:1"
                )
            async with connection.transaction():
                with self.assertRaises(ValueError):
                    await PostgresRepository._change_credits(
                        connection, 100, 0, "purchase", "x:2"
                    )
        self.assertEqual(await self._balance(100), 0)
        self.assertEqual(await self._ledger(100), [])

    # --- ручная корректировка ---------------------------------------------

    async def test_positive_adjustment_records_actor_reason_and_delta(self):
        await self._user(100)

        result = await self._adjust(100, 3, reason="  оплата наличными ")

        self.assertEqual((result.delta, result.balance, result.applied), (3, 3, True))
        (row,) = await self._ledger(100)
        self.assertEqual(
            (row["entry_type"], row["delta"], row["actor_telegram_id"], row["reason"]),
            ("admin_adjustment", 3, ADMIN_ID, "оплата наличными"),
        )
        self.assertEqual(row["id"], result.ledger_id)
        await self._assert_consistent(100)

    async def test_negative_adjustment_reduces_balance(self):
        await self._user(100)
        await self._adjust(100, 5)

        result = await self._adjust(100, -2, reason="ошибочное начисление")

        self.assertEqual(result.balance, 3)
        self.assertEqual(await self._balance(100), 3)
        await self._assert_consistent(100)

    async def test_adjustment_cannot_make_balance_negative(self):
        await self._user(100)
        await self._adjust(100, 1)

        with self.assertRaises(InsufficientLessonCreditsError):
            await self._adjust(100, -2)

        self.assertEqual(await self._balance(100), 1)
        self.assertEqual(len(await self._ledger(100)), 1)

    async def test_adjustment_requires_reason_nonzero_delta_and_admin_actor(self):
        await self._user(100)
        await self._user(101)

        for reason in ("", "   ", "я" * 301):
            with self.assertRaises(ValueError):
                await self._adjust(100, 1, reason=reason)
        with self.assertRaises(ValueError):
            await self._adjust(100, 0)
        with self.assertRaises(PermissionError):
            await self._adjust(100, 1, actor=101)
        with self.assertRaises(PermissionError):
            await self._adjust(100, 1, actor=424242)
        with self.assertRaises(LookupError):
            await self._adjust(777, 1)

        self.assertEqual(await self._balance(100), 0)
        self.assertEqual(await self._ledger(100), [])

    async def test_database_rejects_admin_adjustment_without_actor_or_reason(self):
        await self._user(100)
        for actor, reason in ((None, "причина"), (ADMIN_ID, None), (ADMIN_ID, "  ")):
            async with self.pool.acquire() as connection:
                with self.assertRaises(asyncpg.CheckViolationError):
                    await connection.execute(
                        """
                        INSERT INTO lesson_credit_ledger (
                            telegram_id, entry_type, delta, reference_key,
                            actor_telegram_id, reason
                        )
                        VALUES (100, 'admin_adjustment', 1, $1, $2, $3)
                        """,
                        uuid.uuid4().hex,
                        actor,
                        reason,
                    )

    async def test_repeated_adjustment_with_same_key_is_applied_once(self):
        await self._user(100)
        key = uuid.uuid4()

        first = await self._adjust(100, 2, key=key)
        second = await self._adjust(100, 2, key=key)

        self.assertTrue(first.applied)
        self.assertFalse(second.applied)
        self.assertEqual(second.ledger_id, first.ledger_id)
        self.assertEqual(second.balance, 2)
        self.assertEqual(await self._balance(100), 2)
        self.assertEqual(len(await self._ledger(100)), 1)
        # Тот же ключ для другой операции — ошибка, а не молчаливый повтор.
        with self.assertRaises(ValueError):
            await self._adjust(100, 3, key=key)
        await self._user(101)
        with self.assertRaises(ValueError):
            await self._adjust(101, 2, key=key)
        self.assertEqual(await self._balance(101), 0)

    # --- конкурентность ---------------------------------------------------

    async def test_concurrent_identical_adjustments_apply_exactly_once(self):
        await self._user(100)
        key = uuid.uuid4()

        results = await asyncio.gather(
            *(self._adjust(100, 2, key=key) for _ in range(8))
        )

        self.assertEqual(sum(1 for r in results if r.applied), 1)
        self.assertEqual({r.balance for r in results}, {2})
        self.assertEqual(await self._balance(100), 2)
        await self._assert_consistent(100)

    async def test_concurrent_debits_never_overdraw_the_balance(self):
        await self._user(100)
        await self._adjust(100, 3)

        results = await asyncio.gather(
            *(self._adjust(100, -1) for _ in range(10)), return_exceptions=True
        )

        succeeded = [r for r in results if not isinstance(r, Exception)]
        failed = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(succeeded), 3)
        self.assertTrue(
            all(isinstance(r, InsufficientLessonCreditsError) for r in failed)
        )
        self.assertEqual(await self._balance(100), 0)
        await self._assert_consistent(100)

    async def test_booking_and_adjustment_race_for_the_last_credit(self):
        await self._user(100)
        await self._adjust(100, 1)
        slot = await self._slot()

        booked, adjusted = await asyncio.gather(
            self.repo.book_class_slot(slot.id, 100),
            self._adjust(100, -1),
            return_exceptions=True,
        )

        # Занятие досталось ровно одной из конкурирующих операций.
        self.assertEqual(
            [isinstance(booked, Exception), isinstance(adjusted, Exception)].count(
                True
            ),
            1,
        )
        self.assertEqual(await self._balance(100), 0)
        await self._assert_consistent(100)

    async def test_concurrent_bookings_of_two_slots_with_one_credit(self):
        await self._user(100)
        await self._adjust(100, 1)
        first, second = await self._slot(days=3), await self._slot(days=4)

        results = await asyncio.gather(
            self.repo.book_class_slot(first.id, 100),
            self.repo.book_class_slot(second.id, 100),
            return_exceptions=True,
        )

        self.assertEqual(sum(1 for r in results if isinstance(r, Exception)), 1)
        self.assertEqual(await self._balance(100), 0)
        bookings = await self.repo.list_bookings_for_telegram_id(100)
        self.assertEqual([b.booking_status for b in bookings], ["confirmed"])
        await self._assert_consistent(100)

    # --- сверка -----------------------------------------------------------

    async def test_reconciliation_is_clean_for_consistent_balances(self):
        await self._user(100)
        await self._paid(100)
        await self._adjust(100, -1)

        report = await self.repo.get_credit_reconciliation()

        self.assertEqual(report.mismatched_users, 0)
        self.assertEqual(report.mismatches, [])
        self.assertGreaterEqual(report.checked_users, 2)

    async def test_reconciliation_finds_corrupted_balance_without_repairing_it(self):
        await self._user(100)
        await self._user(101)
        await self._paid(100, lessons=4)
        await self._paid(101, lessons=4)
        async with self.pool.acquire() as connection:
            # Искусственная порча в обход единой точки изменения.
            await connection.execute(
                "UPDATE bot_users SET lesson_credits = lesson_credits + 5 "
                "WHERE telegram_id = 100"
            )
            await connection.execute(
                "UPDATE bot_users SET lesson_credits = lesson_credits - 1 "
                "WHERE telegram_id = 101"
            )

        report = await self.repo.get_credit_reconciliation()

        self.assertEqual(report.mismatched_users, 2)
        # Сначала самое большое расхождение.
        first, second = report.mismatches
        self.assertEqual(
            (first.telegram_id, first.lesson_credits, first.ledger_total),
            (100, 9, 4),
        )
        self.assertEqual((first.difference, first.ledger_entries), (5, 1))
        self.assertIsNotNone(first.last_entry_at)
        self.assertEqual((second.telegram_id, second.difference), (101, -1))
        # Диагностика ничего не исправляет.
        self.assertEqual(await self._balance(100), 9)
        self.assertEqual(await self._balance(101), 3)

    async def test_reconciliation_flags_balance_without_any_ledger_entries(self):
        await self._user(100)
        async with self.pool.acquire() as connection:
            await connection.execute(
                "UPDATE bot_users SET lesson_credits = 2 WHERE telegram_id = 100"
            )

        (mismatch,) = (await self.repo.get_credit_reconciliation()).mismatches

        self.assertEqual(
            (mismatch.telegram_id, mismatch.ledger_entries, mismatch.last_entry_at),
            (100, 0, None),
        )
        self.assertEqual(mismatch.ledger_total, 0)

    async def test_reconciliation_limit_caps_rows_but_not_the_total(self):
        for telegram_id in (100, 101, 102):
            await self._user(telegram_id)
        async with self.pool.acquire() as connection:
            await connection.execute(
                "UPDATE bot_users SET lesson_credits = 1 "
                "WHERE telegram_id IN (100, 101, 102)"
            )

        report = await self.repo.get_credit_reconciliation(limit=2)

        self.assertEqual(report.mismatched_users, 3)
        self.assertEqual(len(report.mismatches), 2)
        with self.assertRaises(ValueError):
            await self.repo.get_credit_reconciliation(limit=0)

    # --- финансовая история и миграция ------------------------------------

    async def test_deleting_a_profile_cannot_erase_financial_history(self):
        await self._user(100)
        await self._user(101)
        payment_id = await self._paid(100)

        async with self.pool.acquire() as connection:
            # PostgreSQL 16 сообщает RESTRICT как foreign_key_violation (23503), 18 —
            # как restrict_violation (23001); защита одна и та же.
            with self.assertRaises(asyncpg.IntegrityConstraintViolationError):
                await connection.execute(
                    "DELETE FROM bot_users WHERE telegram_id = 100"
                )
            # Профиль без финансовой истории по-прежнему удаляется.
            await connection.execute("DELETE FROM bot_users WHERE telegram_id = 101")

        self.assertEqual(len(await self._ledger(100)), 1)
        self.assertIsNotNone(await self.repo.get_lesson_payment(payment_id, 100))

    async def test_migration_010_keeps_existing_data_and_tightens_constraints(self):
        schema = "test_credits_mig_{}_{}".format(os.getpid(), time.time_ns())
        before = Path(tempfile.mkdtemp())
        try:
            # Схема «до 010»: только миграции с меньшим номером (более поздние,
            # 011+, опираются на столбцы из 010).
            for migration in MIGRATIONS.glob("*.sql"):
                if migration.name.split("_", 1)[0] < "010":
                    shutil.copy(migration, before / migration.name)
            async with self.admin_pool.acquire() as connection:
                await connection.execute('CREATE SCHEMA "{}"'.format(schema))
            pool = await asyncpg.create_pool(
                DATABASE_URL, server_settings={"search_path": schema}
            )
            try:
                repo = PostgresRepository(pool)
                await apply_migrations(pool, before)
                await repo.get_or_create_profile(100, "Старый", False)
                async with pool.acquire() as connection:
                    await connection.execute(
                        "UPDATE bot_users SET lesson_credits = 3 "
                        "WHERE telegram_id = 100"
                    )
                    await connection.execute(
                        """
                        INSERT INTO lesson_credit_ledger
                            (telegram_id, entry_type, delta, reference_key)
                        VALUES (100, 'adjustment', 1, 'old:1'),
                               (100, 'purchase', 2, 'old:2')
                        """
                    )

                await apply_migrations(pool)  # применяет 010 к существующей схеме

                async with pool.acquire() as connection:
                    rows = await connection.fetch(
                        "SELECT entry_type, delta, actor_telegram_id, reason "
                        "FROM lesson_credit_ledger ORDER BY id"
                    )
                    delete_rules = await connection.fetch(
                        """
                        SELECT conrelid::regclass::text AS table_name, confdeltype
                        FROM pg_constraint
                        WHERE contype = 'f'
                          AND confrelid = 'bot_users'::regclass
                          AND conrelid::regclass::text IN (
                              'lesson_payments', 'lesson_payment_attempts',
                              'lesson_credit_ledger', 'lesson_bookings'
                          )
                        """
                    )
                self.assertEqual(
                    [tuple(r) for r in rows],
                    [("adjustment", 1, None, None), ("purchase", 2, None, None)],
                )
                self.assertEqual(
                    {r["table_name"]: r["confdeltype"].decode() for r in delete_rules},
                    {
                        "lesson_payments": "r",
                        "lesson_payment_attempts": "r",
                        "lesson_credit_ledger": "r",
                        # Не финансовая таблица: поведение не менялось.
                        "lesson_bookings": "c",
                    },
                )
                await repo.get_or_create_profile(ADMIN_ID, "Админ", True)
                async with pool.acquire() as connection:
                    await connection.execute(
                        "UPDATE bot_users SET is_admin = TRUE WHERE telegram_id = $1",
                        ADMIN_ID,
                    )
                result = await repo.adjust_lesson_credits(
                    100, 1, "после миграции", ADMIN_ID, uuid.uuid4()
                )
                self.assertEqual(result.balance, 4)
            finally:
                await pool.close()
        finally:
            shutil.rmtree(before, ignore_errors=True)
            async with self.admin_pool.acquire() as connection:
                await connection.execute(
                    'DROP SCHEMA IF EXISTS "{}" CASCADE'.format(schema)
                )


if __name__ == "__main__":
    unittest.main()
