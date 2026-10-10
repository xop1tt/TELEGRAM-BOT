"""Срок действия абонементов (миграция 018) на настоящем PostgreSQL.

Решения владельца студии: срок — от даты покупки/выдачи; ручные начисления
— отдельный срок; списывается сначала то, что сгорает раньше; занятие,
вернувшееся после конца срока, сгорает при своей отмене участником и не
сгорает при отмене занятия студией; за N дней до конца — предупреждение.
"""

import uuid
from datetime import datetime, timedelta, timezone

from flamenco_bot.database.repository import InsufficientLessonCreditsError
from tests.pg import PostgresTestCase, fetchval, make_admin, sql

ADMIN_ID = 9001
USER_ID = 1501


class CreditExpiryTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await make_admin(self.repository, ADMIN_ID)
        await self.repository.get_or_create_profile(USER_ID, "Анна")

    # --- помощники -------------------------------------------------------

    async def _paid(self, lessons=4, package_key="pack_4"):
        repo = self.repository
        attempt = await repo.begin_lesson_payment_attempt(
            USER_ID, package_key, "Абонемент", lessons, 360000
        )
        payment = await repo.create_lesson_payment(
            telegram_id=USER_ID,
            package_key=package_key,
            package_title="Абонемент",
            lessons=lessons,
            amount_minor=360000,
            provider_payment_id="yk-{}".format(uuid.uuid4().hex),
            confirmation_url="https://yoomoney.ru/x",
            idempotence_key=attempt.idempotence_key,
        )
        self.assertTrue(await repo.complete_lesson_payment(payment.id, USER_ID))
        return payment.id

    async def _expire_payment(self, payment_id, ago=timedelta(minutes=1)):
        await sql(
            self.repository,
            "UPDATE lesson_payments SET expires_at = $2 WHERE id = $1",
            payment_id,
            datetime.now(timezone.utc) - ago,
        )

    async def _slot(self, days=3):
        return await self.repository.create_class_slot(
            "beginner", datetime.now(timezone.utc) + timedelta(days=days), 5, ADMIN_ID
        )

    async def _kinds(self):
        return [n.kind for n in await self.repository.list_notifications(USER_ID)]

    async def _assert_consistent(self):
        ledger = await fetchval(
            self.repository,
            "SELECT COALESCE(SUM(delta), 0) FROM lesson_credit_ledger "
            "WHERE telegram_id = $1",
            USER_ID,
        )
        self.assertEqual(ledger, await self.repository.get_lesson_credits(USER_ID))

    # --- сроки при покупке и выдаче -------------------------------------

    async def test_payment_grant_and_manual_credit_get_their_validity(self):
        payment_id = await self._paid()
        days = await fetchval(
            self.repository,
            "SELECT EXTRACT(DAY FROM expires_at - completed_at)::INT "
            "FROM lesson_payments WHERE id = $1",
            payment_id,
        )
        self.assertEqual(days, 30)  # pack_4 — 30 дней

        grant = await self.repository.grant_lesson_package(
            USER_ID,
            "pack_8",
            "Абонемент на 8 занятий",
            8,
            "наличные",
            ADMIN_ID,
            uuid.uuid4(),
            validity_days=60,
        )
        self.assertEqual(
            await fetchval(
                self.repository,
                "SELECT EXTRACT(DAY FROM expires_at - created_at)::INT "
                "FROM lesson_package_grants WHERE id = $1",
                grant.grant.id,
            ),
            60,
        )

        await sql(
            self.repository,
            "UPDATE studio_settings SET manual_credit_validity_days = 45",
        )
        key = uuid.uuid4()
        await self.repository.adjust_lesson_credits(USER_ID, 2, "бонус", ADMIN_ID, key)
        # Повтор с тем же ключом не создаёт вторую партию.
        await self.repository.adjust_lesson_credits(USER_ID, 2, "бонус", ADMIN_ID, key)
        rows = await self._fetch_manual()
        self.assertEqual([(r["lessons"], r["days"]) for r in rows], [(2, 45)])
        self.assertEqual(await self.repository.get_lesson_credits(USER_ID), 14)
        await self._assert_consistent()

    async def _fetch_manual(self):
        async with self.repository._pool.acquire() as connection:
            return await connection.fetch(
                "SELECT lessons, "
                "EXTRACT(DAY FROM expires_at - created_at)::INT AS days "
                "FROM lesson_package_grants WHERE package_key = 'manual' "
                "AND telegram_id = $1",
                USER_ID,
            )

    # --- сгорание --------------------------------------------------------

    async def test_expired_package_burns_and_cannot_be_booked(self):
        payment_id = await self._paid(lessons=4)
        await self._expire_payment(payment_id)
        slot = await self._slot()

        with self.assertRaises(InsufficientLessonCreditsError):
            await self.repository.book_class_slot(slot.id, USER_ID)

        # Сгорание — отдельной транзакцией: неудачная запись его не откатила.
        self.assertEqual(await self.repository.get_lesson_credits(USER_ID), 0)
        self.assertEqual(
            await fetchval(
                self.repository,
                "SELECT delta FROM lesson_credit_ledger "
                "WHERE entry_type = 'expiration' AND payment_id = $1",
                payment_id,
            ),
            -4,
        )
        self.assertIn("credits_expired", await self._kinds())
        summary = await self.repository.get_package_summary(USER_ID)
        self.assertEqual(summary.packages[0].status, "expired")
        await self._assert_consistent()

    async def test_background_job_burns_once(self):
        payment_id = await self._paid(lessons=2)
        await self._expire_payment(payment_id)

        self.assertEqual(await self.repository.expire_due_lots(), 2)
        self.assertEqual(await self.repository.expire_due_lots(), 0)
        self.assertEqual(await self.repository.get_lesson_credits(USER_ID), 0)
        self.assertIsNotNone(
            await fetchval(
                self.repository,
                "SELECT expiry_processed_at FROM lesson_payments WHERE id = $1",
                payment_id,
            )
        )
        await self._assert_consistent()

    async def test_only_expired_package_burns_others_stay(self):
        old = await self._paid(lessons=2)
        fresh = await self._paid(lessons=3)
        await self._expire_payment(old)

        self.assertEqual(await self.repository.expire_due_lots(), 2)
        summary = await self.repository.get_package_summary(USER_ID)
        self.assertEqual(summary.balance, 3)
        self.assertEqual(summary.next_source.id, fresh)
        await self._assert_consistent()

    # --- возврат после конца срока --------------------------------------

    async def test_own_cancellation_after_expiry_burns_returned_lesson(self):
        payment_id = await self._paid(lessons=1)
        slot = await self._slot(days=3)
        await self.repository.book_class_slot(slot.id, USER_ID)
        await self._expire_payment(payment_id)

        self.assertTrue(
            await self.repository.cancel_class_slot_booking(slot.id, USER_ID)
        )

        self.assertEqual(await self.repository.get_lesson_credits(USER_ID), 0)
        self.assertIn("credits_expired", await self._kinds())
        await self._assert_consistent()

    async def test_studio_cancellation_after_expiry_returns_unexpiring_lesson(self):
        payment_id = await self._paid(lessons=1)
        slot = await self._slot(days=3)
        await self.repository.book_class_slot(slot.id, USER_ID)
        await self._expire_payment(payment_id)

        await self.repository.cancel_class_slot(slot.id, ADMIN_ID, "болезнь")

        self.assertEqual(await self.repository.expire_due_lots(), 0)
        summary = await self.repository.get_package_summary(USER_ID)
        self.assertEqual((summary.balance, summary.unallocated), (1, 1))
        # Бессрочное занятие можно потратить на новую запись.
        other = await self._slot(days=4)
        booking = await self.repository.book_class_slot(other.id, USER_ID)
        self.assertIsNone(booking.source)
        self.assertEqual(await self.repository.get_lesson_credits(USER_ID), 0)
        await self._assert_consistent()

    # --- предупреждения --------------------------------------------------

    async def test_warning_before_expiry_is_sent_once(self):
        payment_id = await self._paid(lessons=2)
        await sql(
            self.repository,
            "UPDATE lesson_payments SET expires_at = NOW() + INTERVAL '3 days' "
            "WHERE id = $1",
            payment_id,
        )

        self.assertEqual(await self.repository.enqueue_expiry_warnings(0), 0)
        self.assertEqual(await self.repository.enqueue_expiry_warnings(2), 0)
        self.assertEqual(await self.repository.enqueue_expiry_warnings(7), 1)
        self.assertEqual(await self.repository.enqueue_expiry_warnings(7), 0)
        self.assertEqual((await self._kinds()).count("package_expiring"), 1)

    async def test_used_up_package_gets_no_warning(self):
        payment_id = await self._paid(lessons=1)
        await self.repository.book_class_slot((await self._slot()).id, USER_ID)
        await sql(
            self.repository,
            "UPDATE lesson_payments SET expires_at = NOW() + INTERVAL '1 day' "
            "WHERE id = $1",
            payment_id,
        )
        self.assertEqual(await self.repository.enqueue_expiry_warnings(7), 0)
