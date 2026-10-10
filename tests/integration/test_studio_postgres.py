"""Жизненный цикл занятий, абонементы, история и уведомления на PostgreSQL.

Отмена и перенос занятия студией, возврат занятий ровно один раз (в том
числе при параллельной обработке), привязка списаний к абонементам,
выдача/отзыв абонемента, outbox уведомлений и вход на сайт через бота.
"""

import asyncio
import os
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
from tests.support import apply_migrations

from flamenco_bot.database.repository import (
    BookingNotFoundError,
    CancellationWindowExpiredError,
    PostgresRepository,
    SlotUnavailableError,
    TelegramAlreadyLinkedError,
)
from flamenco_bot.database.studio_models import (
    SlotStateError,
    SupportTicketStateError,
    TelegramConnectError,
)

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
ADMIN_ID = 9001


@unittest.skipUnless(
    DATABASE_URL,
    "TEST_DATABASE_URL is not set; configure a dedicated test PostgreSQL database",
)
class StudioPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "test_studio_{}_{}".format(os.getpid(), time.time_ns())
        self.admin_pool = await asyncpg.create_pool(DATABASE_URL)
        async with self.admin_pool.acquire() as connection:
            await connection.execute('CREATE SCHEMA "{}"'.format(self.schema))
        self.pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=10,
            server_settings={"search_path": self.schema},
        )
        self.repo = PostgresRepository(self.pool)
        await apply_migrations(self.pool)
        await self.repo.initialize()
        await self.repo.get_or_create_profile(ADMIN_ID, "Админ")
        await self._sql(
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

    # --- помощники -------------------------------------------------------

    async def _sql(self, query, *args):
        async with self.pool.acquire() as connection:
            return await connection.execute(query, *args)

    async def _fetch(self, query, *args):
        async with self.pool.acquire() as connection:
            return await connection.fetch(query, *args)

    async def _val(self, query, *args):
        async with self.pool.acquire() as connection:
            return await connection.fetchval(query, *args)

    async def _user(self, telegram_id, credits=0):
        await self.repo.get_or_create_profile(telegram_id, "Участник")
        if credits:
            await self.repo.adjust_lesson_credits(
                telegram_id, credits, "стартовый баланс", ADMIN_ID, uuid.uuid4()
            )

    async def _paid(self, telegram_id, lessons=4):
        attempt = await self.repo.begin_lesson_payment_attempt(
            telegram_id, "pack_4", "Абонемент на 4 занятия", lessons, 360000
        )
        payment = await self.repo.create_lesson_payment(
            telegram_id=telegram_id,
            package_key="pack_4",
            package_title="Абонемент на 4 занятия",
            lessons=lessons,
            amount_minor=360000,
            provider_payment_id="yk-{}".format(uuid.uuid4().hex),
            confirmation_url="https://yoomoney.ru/x",
            idempotence_key=attempt.idempotence_key,
        )
        self.assertTrue(
            await self.repo.complete_lesson_payment(payment.id, telegram_id)
        )
        return payment.id

    async def _slot(self, days=3, capacity=5, class_key="beginner"):
        return await self.repo.create_class_slot(
            class_key,
            datetime.now(timezone.utc) + timedelta(days=days),
            capacity,
            ADMIN_ID,
        )

    async def _balance(self, telegram_id):
        return await self.repo.get_lesson_credits(telegram_id)

    async def _assert_consistent(self, telegram_id):
        ledger_sum = await self._val(
            "SELECT COALESCE(SUM(delta), 0) FROM lesson_credit_ledger "
            "WHERE telegram_id = $1",
            telegram_id,
        )
        self.assertEqual(await self._balance(telegram_id), ledger_sum)
        summary = await self.repo.get_package_summary(telegram_id)
        self.assertEqual(
            sum(p.remaining for p in summary.packages) + summary.unallocated,
            summary.balance,
        )

    async def _notifications(self, telegram_id, kind=None):
        items = await self.repo.list_notifications(telegram_id, limit=100)
        return [item for item in items if kind is None or item.kind == kind]

    # --- отмена занятия студией ------------------------------------------

    async def test_studio_cancellation_refunds_every_participant_once(self):
        slot = await self._slot()
        for telegram_id in (101, 102, 103):
            await self._user(telegram_id, credits=2)
            await self.repo.book_class_slot(slot.id, telegram_id)
        # Участник 103 отменил запись сам — ему возврат уже был.
        await self.repo.cancel_class_slot_booking(slot.id, 103)

        result = await self.repo.cancel_class_slot(slot.id, ADMIN_ID, "болезнь")

        self.assertFalse(result.already_cancelled)
        self.assertEqual(result.slot.status, "cancelled")
        self.assertEqual(result.slot.cancel_reason, "болезнь")
        self.assertEqual({r.telegram_id for r in result.refunds}, {101, 102})
        for telegram_id in (101, 102, 103):
            self.assertEqual(await self._balance(telegram_id), 2)
            await self._assert_consistent(telegram_id)
        rows = await self._fetch(
            "SELECT telegram_id, actor_telegram_id, reason, booking_id "
            "FROM lesson_credit_ledger WHERE entry_type = 'slot_cancellation' "
            "ORDER BY telegram_id"
        )
        self.assertEqual([r["telegram_id"] for r in rows], [101, 102])
        self.assertTrue(all(r["actor_telegram_id"] == ADMIN_ID for r in rows))
        self.assertTrue(all(r["booking_id"] is not None for r in rows))

        notices = await self._notifications(101, "slot_cancelled")
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].payload["balance"], 2)
        self.assertEqual(await self._notifications(103, "slot_cancelled"), [])

        bookings = {
            b.slot_id: b for b in await self.repo.list_bookings_for_telegram_id(101)
        }
        self.assertEqual(bookings[slot.id].cancelled_by, "studio")
        own = await self.repo.list_bookings_for_telegram_id(103)
        self.assertEqual(own[0].cancelled_by, "user")

        # Повтор: ничего не меняется, второй возврат не начисляется.
        again = await self.repo.cancel_class_slot(slot.id, ADMIN_ID, "повтор")
        self.assertTrue(again.already_cancelled)
        self.assertEqual(again.refunds, ())
        self.assertEqual(await self._balance(101), 2)
        self.assertEqual(
            await self._val(
                "SELECT COUNT(*) FROM lesson_credit_ledger "
                "WHERE entry_type = 'slot_cancellation'"
            ),
            2,
        )

    async def test_concurrent_studio_cancellations_refund_only_once(self):
        slot = await self._slot()
        for telegram_id in range(201, 206):
            await self._user(telegram_id, credits=1)
            await self.repo.book_class_slot(slot.id, telegram_id)

        results = await asyncio.gather(
            *(self.repo.cancel_class_slot(slot.id, ADMIN_ID) for _ in range(4))
        )

        self.assertEqual(sum(not r.already_cancelled for r in results), 1)
        for telegram_id in range(201, 206):
            self.assertEqual(await self._balance(telegram_id), 1)
            await self._assert_consistent(telegram_id)
        self.assertEqual(
            await self._val(
                "SELECT COUNT(*) FROM user_notifications WHERE kind = 'slot_cancelled'"
            ),
            5,
        )

    async def test_studio_and_user_cancellation_race_refunds_once(self):
        slot = await self._slot()
        await self._user(301, credits=1)
        await self.repo.book_class_slot(slot.id, 301)

        results = await asyncio.gather(
            self.repo.cancel_class_slot(slot.id, ADMIN_ID),
            self.repo.cancel_class_slot_booking(slot.id, 301),
            return_exceptions=True,
        )

        self.assertFalse(any(isinstance(r, Exception) for r in results), results)
        self.assertEqual(await self._balance(301), 1)
        await self._assert_consistent(301)

    async def test_cancelled_slot_is_terminal(self):
        slot = await self._slot()
        await self._user(401, credits=1)
        await self.repo.cancel_class_slot(slot.id, ADMIN_ID)

        with self.assertRaises(SlotUnavailableError):
            await self.repo.book_class_slot(slot.id, 401)
        with self.assertRaises(ValueError):
            await self.repo.update_class_slot_capacity(slot.id, 9, ADMIN_ID)
        with self.assertRaises(SlotStateError):
            await self.repo.reschedule_class_slot(
                slot.id, datetime.now(timezone.utc) + timedelta(days=5), ADMIN_ID
            )
        self.assertFalse(await self.repo.reopen_class_slot(slot.id, ADMIN_ID))
        self.assertFalse(await self.repo.close_class_slot(slot.id, ADMIN_ID))
        public = await self.repo.list_class_slots()
        self.assertNotIn(slot.id, [s.id for s in public])
        admin_view = await self.repo.list_class_slots(include_cancelled=True)
        self.assertIn(slot.id, [s.id for s in admin_view])

    async def test_started_slot_cannot_be_cancelled_or_moved(self):
        slot = await self._slot()
        await self._sql(
            "UPDATE lesson_slots SET starts_at = NOW() - INTERVAL '1 minute' "
            "WHERE id = $1",
            slot.id,
        )
        with self.assertRaises(SlotStateError):
            await self.repo.cancel_class_slot(slot.id, ADMIN_ID)
        with self.assertRaises(SlotStateError):
            await self.repo.reschedule_class_slot(
                slot.id, datetime.now(timezone.utc) + timedelta(days=1), ADMIN_ID
            )

    async def test_studio_cancellation_returns_lesson_to_same_package(self):
        await self._user(501)
        first = await self._paid(501, lessons=2)
        second = await self._paid(501, lessons=4)
        slot = await self._slot()

        booking = await self.repo.book_class_slot(slot.id, 501)
        self.assertEqual(booking.source.id, first)
        self.assertEqual(booking.source.remaining, 1)

        await self.repo.cancel_class_slot(slot.id, ADMIN_ID)

        summary = await self.repo.get_package_summary(501)
        remaining = {p.id: p.remaining for p in summary.packages}
        self.assertEqual(remaining, {first: 2, second: 4})
        await self._assert_consistent(501)

    # --- перенос ---------------------------------------------------------

    async def test_reschedule_keeps_bookings_and_balance_and_notifies(self):
        slot = await self._slot(days=3)
        await self._user(601, credits=2)
        await self.repo.book_class_slot(slot.id, 601)
        new_time = datetime.now(timezone.utc) + timedelta(hours=10)

        result = await self.repo.reschedule_class_slot(
            slot.id, new_time, ADMIN_ID, "замена зала"
        )

        self.assertEqual(result.participants, (601,))
        self.assertEqual(result.slot.starts_at, new_time)
        self.assertEqual(result.slot.booked_count, 1)
        self.assertEqual(await self._balance(601), 1)
        notice = (await self._notifications(601, "slot_rescheduled"))[0]
        self.assertEqual(
            datetime.fromisoformat(notice.payload["old_starts_at"]), slot.starts_at
        )
        self.assertEqual(
            datetime.fromisoformat(notice.payload["new_starts_at"]), new_time
        )
        events = await self.repo.list_slot_events(slot.id)
        self.assertEqual(events[0].event_type, "rescheduled")
        booking = (await self.repo.list_bookings_for_telegram_id(601))[0]
        self.assertEqual(booking.previous_starts_at, slot.starts_at)
        self.assertEqual(booking.cancellation_deadline, new_time)

        # До нового начала 10 часов, но занятие перенесла студия — отмена
        # без ограничения «за 24 часа».
        self.assertTrue(await self.repo.cancel_class_slot_booking(slot.id, 601))
        self.assertEqual(await self._balance(601), 2)
        await self._assert_consistent(601)

    async def test_booking_after_reschedule_keeps_24_hour_rule(self):
        slot = await self._slot(days=3)
        await self.repo.reschedule_class_slot(
            slot.id, datetime.now(timezone.utc) + timedelta(hours=10), ADMIN_ID
        )
        await self._user(602, credits=1)
        await self.repo.book_class_slot(slot.id, 602)

        with self.assertRaises(CancellationWindowExpiredError):
            await self.repo.cancel_class_slot_booking(slot.id, 602)

    async def test_reschedule_validation(self):
        slot = await self._slot()
        with self.assertRaises(ValueError):
            await self.repo.reschedule_class_slot(slot.id, slot.starts_at, ADMIN_ID)
        with self.assertRaises(ValueError):
            await self.repo.reschedule_class_slot(
                slot.id, datetime.now(timezone.utc) - timedelta(hours=1), ADMIN_ID
            )
        with self.assertRaises(LookupError):
            await self.repo.reschedule_class_slot(
                999999, datetime.now(timezone.utc) + timedelta(days=1), ADMIN_ID
            )

    async def test_reschedule_races_with_booking_and_cancellation(self):
        slot = await self._slot(days=3, capacity=1)
        await self._user(701, credits=1)
        await self._user(702, credits=1)
        await self._user(703, credits=1)
        await self.repo.book_class_slot(slot.id, 703)
        await self.repo.cancel_class_slot_booking(slot.id, 703)  # место свободно

        new_time = datetime.now(timezone.utc) + timedelta(days=4)
        results = await asyncio.gather(
            self.repo.reschedule_class_slot(slot.id, new_time, ADMIN_ID),
            self.repo.book_class_slot(slot.id, 701),
            self.repo.book_class_slot(slot.id, 702),
            return_exceptions=True,
        )

        booked = [r for r in results[1:] if not isinstance(r, Exception)]
        self.assertEqual(len(booked), 1)
        self.assertIsInstance(
            [r for r in results[1:] if isinstance(r, Exception)][0],
            SlotUnavailableError,
        )
        current = await self.repo.get_class_slot(slot.id)
        self.assertEqual(current.starts_at, new_time)
        self.assertEqual(current.booked_count, 1)
        for telegram_id in (701, 702, 703):
            await self._assert_consistent(telegram_id)

    # --- абонементы ------------------------------------------------------

    async def test_bookings_use_unallocated_credits_then_oldest_package(self):
        await self._user(801, credits=1)  # ручное начисление вне абонемента
        payment_id = await self._paid(801, lessons=2)
        slots = [await self._slot(days=d) for d in (2, 3, 4)]

        first = await self.repo.book_class_slot(slots[0].id, 801)
        second = await self.repo.book_class_slot(slots[1].id, 801)

        self.assertIsNone(first.source)
        self.assertEqual(second.source.id, payment_id)
        self.assertEqual(second.source.remaining, 1)
        summary = await self.repo.get_package_summary(801)
        self.assertEqual(summary.balance, 1)
        self.assertEqual(summary.unallocated, 0)
        self.assertEqual(summary.active_packages[0].remaining, 1)

        # Отмена возвращает занятие в тот же абонемент.
        self.assertTrue(await self.repo.cancel_class_slot_booking(slots[1].id, 801))
        summary = await self.repo.get_package_summary(801)
        self.assertEqual(summary.active_packages[0].remaining, 2)
        await self._assert_consistent(801)

    async def test_parallel_bookings_of_one_user_keep_package_totals(self):
        await self._user(802, credits=1)
        await self._paid(802, lessons=1)
        slots = [await self._slot(days=d) for d in (2, 3)]

        await asyncio.gather(*(self.repo.book_class_slot(s.id, 802) for s in slots))

        summary = await self.repo.get_package_summary(802)
        self.assertEqual(summary.balance, 0)
        self.assertEqual(sum(p.remaining for p in summary.packages), 0)
        await self._assert_consistent(802)

    async def test_used_package_lesson_blocks_refund_until_booking_cancelled(self):
        await self._user(803)
        payment_id = await self._paid(803, lessons=4)
        await self.repo.adjust_lesson_credits(
            803, 4, "компенсация", ADMIN_ID, uuid.uuid4()
        )
        slot = await self._slot()
        # Ручные занятия вне абонемента расходуются первыми — израсходуем их.
        for days in (2, 3, 4, 5):
            await self.repo.book_class_slot((await self._slot(days=days)).id, 803)
        await self.repo.book_class_slot(slot.id, 803)  # 1 занятие абонемента

        with self.assertRaises(ValueError):
            await self.repo.prepare_lesson_refund(payment_id, ADMIN_ID, "возврат")

        await self.repo.cancel_class_slot_booking(slot.id, 803)
        refund = await self.repo.prepare_lesson_refund(payment_id, ADMIN_ID, "возврат")
        self.assertEqual(refund.lessons, 4)
        await self._assert_consistent(803)

    async def test_grant_package_is_audited_idempotent_and_listed(self):
        await self._user(901)
        key = uuid.uuid4()

        result = await self.repo.grant_lesson_package(
            901,
            "pack_4",
            "Абонемент на 4 занятия",
            4,
            "оплата наличными",
            ADMIN_ID,
            key,
        )
        repeated = await self.repo.grant_lesson_package(
            901,
            "pack_4",
            "Абонемент на 4 занятия",
            4,
            "оплата наличными",
            ADMIN_ID,
            key,
        )

        self.assertTrue(result.applied)
        self.assertFalse(repeated.applied)
        self.assertEqual(repeated.grant.id, result.grant.id)
        self.assertEqual(await self._balance(901), 4)
        row = (
            await self._fetch(
                "SELECT entry_type, actor_telegram_id, reason, grant_id "
                "FROM lesson_credit_ledger WHERE telegram_id = 901"
            )
        )[0]
        self.assertEqual(
            tuple(row), ("package_grant", ADMIN_ID, "оплата наличными", result.grant.id)
        )
        summary = await self.repo.get_package_summary(901)
        self.assertEqual(summary.active_packages[0].kind, "grant")
        self.assertEqual(summary.active_packages[0].remaining, 4)
        self.assertEqual(len(await self._notifications(901, "package_granted")), 1)

        with self.assertRaises(PermissionError):
            await self.repo.grant_lesson_package(
                901, "single", "Разовое", 1, "без прав", 901, uuid.uuid4()
            )
        with self.assertRaises(LookupError):
            await self.repo.grant_lesson_package(
                424242, "single", "Разовое", 1, "нет профиля", ADMIN_ID, uuid.uuid4()
            )

    async def test_revoke_grant_removes_only_unused_lessons(self):
        await self._user(902)
        result = await self.repo.grant_lesson_package(
            902, "pack_4", "Абонемент", 4, "выдача", ADMIN_ID, uuid.uuid4()
        )
        await self.repo.book_class_slot((await self._slot()).id, 902)

        revoked = await self.repo.revoke_lesson_package_grant(
            result.grant.id, "ошибка выдачи", ADMIN_ID
        )
        again = await self.repo.revoke_lesson_package_grant(
            result.grant.id, "повтор", ADMIN_ID
        )

        self.assertEqual(revoked.revoked_lessons, 3)
        self.assertFalse(again.applied)
        self.assertEqual(await self._balance(902), 0)
        summary = await self.repo.get_package_summary(902)
        self.assertEqual(summary.packages[0].status, "revoked")
        await self._assert_consistent(902)

    # --- история ---------------------------------------------------------

    async def test_credit_history_shows_class_and_package_with_cursor(self):
        await self._user(1001)
        await self._paid(1001, lessons=4)
        slot = await self._slot()
        await self.repo.book_class_slot(slot.id, 1001)
        await self.repo.cancel_class_slot_booking(slot.id, 1001)

        history = await self.repo.list_credit_history(1001, limit=2)

        self.assertEqual([e.entry_type for e in history], ["adjustment", "lesson_use"])
        self.assertEqual(history[1].class_key, "beginner")
        self.assertEqual(history[1].package_title, "Абонемент на 4 занятия")
        older = await self.repo.list_credit_history(1001, before_id=history[-1].id)
        self.assertEqual([e.entry_type for e in older], ["purchase"])
        audit = await self.repo.list_audit_events()
        self.assertIn("created", [event.action for event in audit])

    # --- уведомления -----------------------------------------------------

    async def test_low_balance_notice_once_per_top_up_cycle(self):
        await self._user(1101)
        await self._paid(1101, lessons=2)
        slots = [await self._slot(days=d) for d in (2, 3, 4)]

        await self.repo.book_class_slot(slots[0].id, 1101)  # осталось 1
        await self.repo.book_class_slot(slots[1].id, 1101)  # осталось 0
        self.assertEqual(len(await self._notifications(1101, "low_balance")), 1)

        await self._paid(1101, lessons=1)  # новый цикл
        await self.repo.book_class_slot(slots[2].id, 1101)
        self.assertEqual(len(await self._notifications(1101, "low_balance")), 2)

        await self.repo.update_notification_settings(1101, low_balance=False)
        settings = await self.repo.get_notification_settings(1101)
        self.assertFalse(settings.low_balance)
        self.assertTrue(settings.reminders)

    async def test_outbox_claim_retry_send_expire_and_dedupe(self):
        await self._user(1201)
        self.assertTrue(
            await self.repo.enqueue_notification(1201, "credits_adjusted", {}, "k:1")
        )
        self.assertFalse(
            await self.repo.enqueue_notification(1201, "credits_adjusted", {}, "k:1")
        )
        await self.repo.enqueue_notification(
            1201,
            "lesson_reminder",
            {},
            "k:2",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )

        first, second = await asyncio.gather(
            self.repo.claim_due_notifications(limit=10),
            self.repo.claim_due_notifications(limit=10),
        )
        claimed = list(first) + list(second)
        self.assertEqual(len(claimed), 1)
        notification = claimed[0]
        self.assertEqual(notification.attempts, 1)

        await self.repo.mark_notification_retry(
            notification.id, "network", datetime.now(timezone.utc)
        )
        retried = await self.repo.claim_due_notifications()
        self.assertEqual([n.id for n in retried], [notification.id])
        self.assertEqual(retried[0].attempts, 2)
        await self.repo.mark_notification_sent(notification.id)
        self.assertEqual(await self.repo.claim_due_notifications(), [])

        statuses = {
            row["dedupe_key"]: row["delivery_status"]
            for row in await self._fetch(
                "SELECT dedupe_key, delivery_status FROM user_notifications"
            )
        }
        self.assertEqual(statuses, {"k:1": "sent", "k:2": "skipped"})
        self.assertEqual(await self.repo.count_unread_notifications(1201), 2)
        self.assertEqual(
            await self.repo.mark_notifications_read(1201, [notification.id]), 1
        )
        self.assertEqual(await self.repo.mark_notifications_read(1201), 1)
        self.assertEqual(await self.repo.count_unread_notifications(1201), 0)

    async def test_reminders_respect_window_settings_and_reschedule(self):
        lead = timedelta(hours=3)
        early = await self._slot(days=1)
        await self._user(1301, credits=2)
        await self._user(1302, credits=1)
        await self.repo.book_class_slot(early.id, 1301)
        await self.repo.book_class_slot(early.id, 1302)
        await self.repo.update_notification_settings(1302, reminders=False)
        # Занятие через 2 часа; записались заранее.
        await self._sql(
            "UPDATE lesson_slots SET starts_at = NOW() + INTERVAL '2 hours' "
            "WHERE id = $1",
            early.id,
        )
        await self._sql(
            "UPDATE lesson_bookings SET booked_at = NOW() - INTERVAL '1 day'"
        )
        late = await self._slot(days=1)
        await self._sql(
            "UPDATE lesson_slots SET starts_at = NOW() + INTERVAL '1 hour' "
            "WHERE id = $1",
            late.id,
        )
        await self.repo.book_class_slot(late.id, 1301)  # записался внутри окна

        self.assertEqual(await self.repo.enqueue_due_reminders(lead), 1)
        self.assertEqual(await self.repo.enqueue_due_reminders(lead), 0)
        reminder = (await self._notifications(1301, "lesson_reminder"))[0]
        self.assertEqual(reminder.payload["slot_id"], early.id)
        starts_at = datetime.fromisoformat(reminder.payload["starts_at"])
        booking_id = reminder.payload["booking_id"]
        self.assertTrue(await self.repo.is_reminder_current(booking_id, starts_at))

        await self.repo.reschedule_class_slot(
            early.id, datetime.now(timezone.utc) + timedelta(days=2), ADMIN_ID
        )
        self.assertFalse(await self.repo.is_reminder_current(booking_id, starts_at))

    # --- вход на сайт через бота -----------------------------------------

    async def test_connect_login_is_confirmed_and_consumed_once(self):
        await self._user(1401)
        expires = datetime.now(timezone.utc) + timedelta(minutes=10)
        await self.repo.create_telegram_connect_request(
            "token-1", "secret-1", "login", None, expires
        )
        pending, consumed = await self.repo.consume_telegram_connect_request("secret-1")
        self.assertEqual((pending.status, consumed), ("pending", False))

        confirmed = await self.repo.confirm_telegram_connect_request("token-1", 1401)
        self.assertEqual(confirmed.telegram_id, 1401)
        with self.assertRaises(TelegramConnectError):
            await self.repo.confirm_telegram_connect_request("token-1", 1401)

        first, second = await asyncio.gather(
            self.repo.consume_telegram_connect_request("secret-1"),
            self.repo.consume_telegram_connect_request("secret-1"),
        )
        self.assertEqual(sorted([first[1], second[1]]), [False, True])
        self.assertEqual(
            await self.repo.consume_telegram_connect_request("wrong"), (None, False)
        )

        await self.repo.create_telegram_connect_request(
            "token-old",
            "secret-old",
            "login",
            None,
            datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        with self.assertRaises(TelegramConnectError):
            await self.repo.confirm_telegram_connect_request("token-old", 1401)

    async def test_connect_link_merges_telegram_only_account(self):
        await self._user(1501)
        shell = await self.repo.get_or_create_web_user_from_telegram(1501, "Анна")
        account = await self.repo.create_web_user("anna@example.org", "hash", "Анна")
        await self.repo.create_telegram_connect_request(
            "token-link",
            "secret-link",
            "link",
            account.id,
            datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        request = await self.repo.get_telegram_connect_request("token-link")
        self.assertEqual(request.web_user_email, "anna@example.org")

        await self.repo.confirm_telegram_connect_request("token-link", 1501)

        self.assertIsNone(await self.repo.get_web_user_by_id(shell.id))
        linked = await self.repo.get_web_user_by_telegram_id(1501)
        self.assertEqual(linked.id, account.id)

        other = await self.repo.create_web_user("other@example.org", "hash", "Иван")
        with self.assertRaises(TelegramAlreadyLinkedError):
            await self.repo.link_telegram_to_web_user(other.id, 1501)

    # --- поддержка -------------------------------------------------------

    async def test_support_thread_and_reopen(self):
        await self._user(1601)
        ticket_id, _ = await self.repo.create_support_message(1601, "Вопрос")
        await self.repo.reply_support_ticket(ticket_id, ADMIN_ID, "Ответ")
        await self.repo.close_support_ticket(ticket_id, ADMIN_ID)

        thread = await self.repo.get_support_ticket_thread(ticket_id, 1601)
        self.assertEqual(
            [m.sender_role for m in thread.messages], ["user", "admin", "admin"]
        )
        self.assertIsNone(await self.repo.get_support_ticket_thread(ticket_id, 999))

        self.assertEqual(
            await self.repo.reopen_support_ticket(ticket_id, ADMIN_ID), 1601
        )
        with self.assertRaises(SupportTicketStateError):
            await self.repo.reopen_support_ticket(ticket_id, ADMIN_ID)
        await self.repo.close_support_ticket(ticket_id, ADMIN_ID)
        await self.repo.create_support_message(1601, "Новый вопрос")
        with self.assertRaises(SupportTicketStateError):
            await self.repo.reopen_support_ticket(ticket_id, ADMIN_ID)

    async def test_participants_list(self):
        slot = await self._slot()
        await self._user(1701, credits=1)
        await self._user(1702, credits=1)
        await self.repo.book_class_slot(slot.id, 1701)
        await self.repo.book_class_slot(slot.id, 1702)
        await self.repo.cancel_class_slot_booking(slot.id, 1702)

        active = await self.repo.list_slot_participants(slot.id)
        everyone = await self.repo.list_slot_participants(
            slot.id, include_cancelled=True
        )

        self.assertEqual([p.telegram_id for p in active], [1701])
        self.assertEqual({p.telegram_id for p in everyone}, {1701, 1702})
        with self.assertRaises(BookingNotFoundError):
            await self.repo.cancel_class_slot_booking(slot.id, 4242)


if __name__ == "__main__":
    unittest.main()
