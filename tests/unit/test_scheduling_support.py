import unittest
from datetime import datetime, timedelta, timezone

from flamenco_bot.database.repository import (
    BookingCooldownError,
    BookingNotFoundError,
    CancellationWindowExpiredError,
    InsufficientLessonCreditsError,
    SlotUnavailableError,
)
from flamenco_bot.runtime.security import SupportRateLimiter
from tests.pg import PostgresTestCase, set_credits, sql


async def _grant_credits(repository, telegram_id: int, credits: int) -> None:
    """Выставляет баланс занятий — бронирование их списывает."""
    await set_credits(repository, telegram_id, credits)


class SchedulingAndSupportTests(PostgresTestCase):
    async def test_class_booking_is_immediate_idempotent_and_capacity_limited(self):
        repository = self.repository
        await repository.get_or_create_profile(1001, "Анна", False)
        await repository.get_or_create_profile(1002, "Мария", False)
        await _grant_credits(repository, 1001, 1)
        await _grant_credits(repository, 1002, 1)
        slot = await repository.create_class_slot(
            "beginner",
            datetime.now(timezone.utc) + timedelta(days=1),
            1,
            77,
        )

        first = await repository.book_class_slot(slot.id, 1001)
        repeated = await repository.book_class_slot(slot.id, 1001)
        self.assertFalse(first.already_booked)
        self.assertTrue(repeated.already_booked)
        self.assertEqual(first.id, repeated.id)
        self.assertEqual(
            (await repository.list_class_slots())[0].booked_count,
            1,
        )
        with self.assertRaises(SlotUnavailableError):
            await repository.book_class_slot(slot.id, 1002)

    async def test_slot_capacity_cannot_be_lowered_below_confirmed_bookings(self):
        repository = self.repository
        await repository.get_or_create_profile(1001, "Анна", False)
        await repository.get_or_create_profile(1002, "Мария", False)
        await _grant_credits(repository, 1001, 1)
        await _grant_credits(repository, 1002, 1)
        slot = await repository.create_class_slot(
            "individual",
            datetime.now(timezone.utc) + timedelta(days=1),
            2,
            77,
        )
        await repository.book_class_slot(slot.id, 1001)
        await repository.book_class_slot(slot.id, 1002)

        with self.assertRaisesRegex(ValueError, "ниже числа"):
            await repository.update_class_slot_capacity(slot.id, 1)

        self.assertTrue(await repository.update_class_slot_capacity(slot.id, 2))
        self.assertTrue(await repository.close_class_slot(slot.id))
        with self.assertRaises(SlotUnavailableError):
            await repository.book_class_slot(slot.id, 1001)

    async def test_support_ticket_can_be_replied_to_closed_and_reopened(self):
        repository = self.repository
        await repository.get_or_create_profile(1001, "Анна", False)

        ticket_id, created = await repository.create_support_message(1001, "Помогите")
        same_ticket_id, created_again = await repository.create_support_message(
            1001, "Дополнение"
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(ticket_id, same_ticket_id)
        self.assertEqual(
            (await repository.list_open_support_tickets())[0].last_message,
            "Дополнение",
        )
        self.assertEqual(
            await repository.reply_support_ticket(ticket_id, 77, "Проверяем"),
            1001,
        )
        self.assertEqual(
            (await repository.list_open_support_tickets())[0].last_message,
            "Проверяем",
        )
        self.assertEqual(await repository.close_support_ticket(ticket_id, 77), 1001)
        self.assertEqual(await repository.list_open_support_tickets(), [])
        reopened_id, reopened = await repository.create_support_message(
            1001, "Новый вопрос"
        )
        self.assertTrue(reopened)
        self.assertNotEqual(reopened_id, ticket_id)

    async def test_support_messages_are_validated(self):
        repository = self.repository
        with self.assertRaisesRegex(ValueError, "1 до 2000"):
            await repository.create_support_message(1001, "  ")
        with self.assertRaisesRegex(ValueError, "1 до 2000"):
            await repository.create_support_message(1001, "x" * 2001)

    async def test_list_bookings_for_telegram_id_excludes_other_users(self):
        repository = self.repository
        await repository.get_or_create_profile(1001, "Анна", False)
        await repository.get_or_create_profile(1002, "Мария", False)
        await _grant_credits(repository, 1001, 1)
        await _grant_credits(repository, 1002, 1)
        slot = await repository.create_class_slot(
            "beginner",
            datetime.now(timezone.utc) + timedelta(days=1),
            2,
            77,
        )
        await repository.book_class_slot(slot.id, 1001)
        await repository.book_class_slot(slot.id, 1002)

        bookings = await repository.list_bookings_for_telegram_id(1001)
        self.assertEqual(len(bookings), 1)
        self.assertEqual(bookings[0].slot_id, slot.id)
        self.assertEqual(bookings[0].class_key, "beginner")
        self.assertEqual(bookings[0].booking_status, "confirmed")
        self.assertEqual(bookings[0].slot_status, "open")

    async def test_list_support_tickets_for_telegram_id_includes_closed_tickets(self):
        repository = self.repository
        await repository.get_or_create_profile(1001, "Анна", False)
        await repository.get_or_create_profile(1002, "Мария", False)
        ticket_id, _ = await repository.create_support_message(1001, "Помогите")
        await repository.close_support_ticket(ticket_id, 77)
        await repository.create_support_message(1002, "Другой вопрос")

        tickets = await repository.list_support_tickets_for_telegram_id(1001)
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].id, ticket_id)
        self.assertEqual(tickets[0].status, "closed")


class BookingCreditsAndCancellationTests(PostgresTestCase):
    """Бронирование теперь списывает lesson_credit; отмена — возвращает его.

    Правило подтверждено владельцем продукта при аудите (не придумано в
    коде): 1 credit за запись, отмена не позднее 24ч до начала, 12ч
    кулдаун на повторную запись тем же пользователем после отмены.
    """

    async def _repository_with_slot(self, capacity=1, starts_in=timedelta(days=2)):
        repository = self.repository
        await repository.get_or_create_profile(1001, "Анна", False)
        slot = await repository.create_class_slot(
            "beginner",
            datetime.now(timezone.utc) + starts_in,
            capacity,
            77,
        )
        return repository, slot

    async def test_booking_without_credits_is_rejected(self):
        repository, slot = await self._repository_with_slot()

        with self.assertRaises(InsufficientLessonCreditsError):
            await repository.book_class_slot(slot.id, 1001)
        self.assertEqual(await repository.get_lesson_credits(1001), 0)

    async def test_booking_debits_exactly_one_credit(self):
        repository, slot = await self._repository_with_slot()
        await _grant_credits(repository, 1001, 2)

        await repository.book_class_slot(slot.id, 1001)

        self.assertEqual(await repository.get_lesson_credits(1001), 1)

    async def test_duplicate_booking_does_not_debit_twice(self):
        repository, slot = await self._repository_with_slot()
        await _grant_credits(repository, 1001, 2)

        await repository.book_class_slot(slot.id, 1001)
        repeated = await repository.book_class_slot(slot.id, 1001)

        self.assertTrue(repeated.already_booked)
        self.assertEqual(await repository.get_lesson_credits(1001), 1)

    async def test_cancellation_refunds_the_credit_and_frees_the_seat(self):
        repository, slot = await self._repository_with_slot()
        await _grant_credits(repository, 1001, 1)
        await repository.book_class_slot(slot.id, 1001)

        cancelled = await repository.cancel_class_slot_booking(slot.id, 1001)

        self.assertTrue(cancelled)
        self.assertEqual(await repository.get_lesson_credits(1001), 1)
        self.assertEqual(
            (await repository.list_class_slots())[0].booked_count,
            0,
        )

    async def test_cancelling_twice_is_idempotent_and_does_not_double_refund(self):
        repository, slot = await self._repository_with_slot()
        await _grant_credits(repository, 1001, 1)
        await repository.book_class_slot(slot.id, 1001)

        first = await repository.cancel_class_slot_booking(slot.id, 1001)
        second = await repository.cancel_class_slot_booking(slot.id, 1001)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(await repository.get_lesson_credits(1001), 1)

    async def test_cancelling_a_booking_that_does_not_exist_raises(self):
        repository, slot = await self._repository_with_slot()

        with self.assertRaises(BookingNotFoundError):
            await repository.cancel_class_slot_booking(slot.id, 1001)

    async def test_cancellation_within_24h_of_start_is_rejected(self):
        repository, slot = await self._repository_with_slot(
            starts_in=timedelta(hours=1)
        )
        await _grant_credits(repository, 1001, 1)
        await repository.book_class_slot(slot.id, 1001)

        with self.assertRaises(CancellationWindowExpiredError):
            await repository.cancel_class_slot_booking(slot.id, 1001)
        # Кредит не должен уходить, если отмена отклонена.
        self.assertEqual(await repository.get_lesson_credits(1001), 0)

    async def test_rebooking_right_after_cancellation_hits_cooldown(self):
        repository, slot = await self._repository_with_slot()
        await _grant_credits(repository, 1001, 2)
        await repository.book_class_slot(slot.id, 1001)
        await repository.cancel_class_slot_booking(slot.id, 1001)

        with self.assertRaises(BookingCooldownError):
            await repository.book_class_slot(slot.id, 1001)
        # Кредит, возвращённый при отмене, не тронут — попытка не прошла.
        self.assertEqual(await repository.get_lesson_credits(1001), 2)

    async def test_rebooking_after_cooldown_expires_succeeds_and_debits_again(self):
        repository, slot = await self._repository_with_slot()
        await _grant_credits(repository, 1001, 2)
        await repository.book_class_slot(slot.id, 1001)
        await repository.cancel_class_slot_booking(slot.id, 1001)
        # Симулируем, что кулдаун истёк.
        await sql(
            repository,
            "UPDATE lesson_bookings SET updated_at = $3 "
            "WHERE slot_id = $1 AND telegram_id = $2",
            slot.id,
            1001,
            datetime.now(timezone.utc) - timedelta(hours=13),
        )

        rebooked = await repository.book_class_slot(slot.id, 1001)

        self.assertFalse(rebooked.already_booked)
        self.assertEqual(await repository.get_lesson_credits(1001), 1)

    async def test_cooldown_only_blocks_the_same_user_not_others(self):
        repository, slot = await self._repository_with_slot(capacity=2)
        await repository.get_or_create_profile(1002, "Мария", False)
        await _grant_credits(repository, 1001, 2)
        await _grant_credits(repository, 1002, 1)
        await repository.book_class_slot(slot.id, 1001)
        await repository.cancel_class_slot_booking(slot.id, 1001)

        other_user_booking = await repository.book_class_slot(slot.id, 1002)

        self.assertFalse(other_user_booking.already_booked)

    async def test_my_bookings_reflects_cancelled_status(self):
        repository, slot = await self._repository_with_slot()
        await _grant_credits(repository, 1001, 1)
        await repository.book_class_slot(slot.id, 1001)
        await repository.cancel_class_slot_booking(slot.id, 1001)

        bookings = await repository.list_bookings_for_telegram_id(1001)

        self.assertEqual(len(bookings), 1)
        self.assertEqual(bookings[0].booking_status, "cancelled")


class SupportRateLimiterTests(unittest.TestCase):
    def test_support_messages_are_limited_per_user_and_expire(self):
        current_time = [0.0]
        limiter = SupportRateLimiter(
            max_messages=2,
            window_seconds=10,
            max_tracked_users=2,
            clock=lambda: current_time[0],
        )
        self.assertTrue(limiter.allow(1))
        self.assertTrue(limiter.allow(1))
        self.assertFalse(limiter.allow(1))
        self.assertTrue(limiter.allow(2))
        self.assertTrue(limiter.allow(3))
        self.assertLessEqual(len(limiter._events), 2)

        current_time[0] = 11
        self.assertTrue(limiter.allow(1))
