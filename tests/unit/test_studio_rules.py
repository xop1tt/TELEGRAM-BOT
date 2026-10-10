"""Чистые правила: остатки абонементов, источник списания, окно отмены,
ввод времени администратором, тексты уведомлений, типы операций истории."""

import unittest
from datetime import datetime, timedelta, timezone

from flamenco_bot.database.repository import (
    CancellationWindowExpiredError,
    UserBooking,
    booking_cancellation_deadline,
    check_cancellation_window,
)
from flamenco_bot.database.studio_models import (
    NOTIFICATION_KINDS,
    LedgerEntry,
    effective_remaining,
    low_balance_dedupe_key,
    normalize_reason,
    pick_credit_source_index,
    reminder_dedupe_key,
)
from flamenco_bot.notification_texts import render_notification
from flamenco_bot.services.history import (
    OPERATION_LABELS,
    booking_status,
    classify_entry,
    describe_entry,
)
from flamenco_bot.studio_time import parse_admin_datetime, to_studio_time


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def entry(entry_type, delta, booking_id=None, reason=None):
    return LedgerEntry(
        id=1,
        telegram_id=1001,
        entry_type=entry_type,
        delta=delta,
        created_at=NOW,
        reason=reason,
        booking_id=booking_id,
    )


class PackageRulesTests(unittest.TestCase):
    def test_packages_are_spent_before_unexpiring_credits(self):
        # Бессрочные занятия вне абонементов — в последнюю очередь.
        self.assertEqual(effective_remaining(5, [2, 2]), ([2, 2], 1))
        self.assertEqual(pick_credit_source_index(5, [2, 2]), 0)
        self.assertIsNone(pick_credit_source_index(1, [0, 0]))

    def test_oldest_package_is_next_when_no_unallocated(self):
        self.assertEqual(pick_credit_source_index(4, [2, 2]), 0)
        self.assertEqual(pick_credit_source_index(2, [0, 2]), 1)

    def test_manual_debit_reduces_oldest_packages_first(self):
        # Баланс 3, а по абонементам 2+2: недостача 1 снимается с раннего.
        self.assertEqual(effective_remaining(3, [2, 2]), ([1, 2], 0))
        self.assertEqual(pick_credit_source_index(3, [2, 2]), 0)
        self.assertEqual(effective_remaining(1, [2, 2]), ([0, 1], 0))
        self.assertEqual(pick_credit_source_index(1, [2, 2]), 1)

    def test_no_credits_no_source(self):
        self.assertEqual(effective_remaining(0, [2]), ([0], 0))
        self.assertIsNone(pick_credit_source_index(0, [2]))
        self.assertIsNone(pick_credit_source_index(0, []))

    def test_totals_always_match_balance(self):
        for balance in range(0, 8):
            for raw in ([], [1], [3, 1], [2, 2, 2], [-1, 3]):
                effective, unallocated = effective_remaining(balance, raw)
                self.assertEqual(sum(effective) + unallocated, max(balance, 0))
                self.assertTrue(all(value >= 0 for value in effective))

    def test_dedupe_keys(self):
        self.assertEqual(low_balance_dedupe_key(7, 42), "low_balance:7:42")
        moment = datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc)
        self.assertNotEqual(
            reminder_dedupe_key(5, moment),
            reminder_dedupe_key(5, moment + timedelta(hours=1)),
        )

    def test_reason_validation(self):
        self.assertIsNone(normalize_reason("  ", required=False))
        self.assertEqual(normalize_reason(" наличные ", required=True), "наличные")
        with self.assertRaises(ValueError):
            normalize_reason("", required=True)
        with self.assertRaises(ValueError):
            normalize_reason("x" * 301, required=False)


class CancellationWindowTests(unittest.TestCase):
    def test_regular_booking_keeps_24_hour_rule(self):
        starts_at = NOW + timedelta(hours=30)
        self.assertEqual(
            booking_cancellation_deadline(starts_at, NOW, None),
            starts_at - timedelta(hours=24),
        )
        check_cancellation_window(starts_at, NOW, None, now=NOW)
        with self.assertRaises(CancellationWindowExpiredError):
            check_cancellation_window(
                starts_at, NOW, None, now=starts_at - timedelta(hours=23)
            )

    def test_booking_made_before_reschedule_cancels_until_start(self):
        booked_at = NOW - timedelta(days=2)
        rescheduled_at = NOW - timedelta(hours=1)
        starts_at = NOW + timedelta(hours=2)
        self.assertEqual(
            booking_cancellation_deadline(starts_at, booked_at, rescheduled_at),
            starts_at,
        )
        check_cancellation_window(starts_at, booked_at, rescheduled_at, now=NOW)
        with self.assertRaises(CancellationWindowExpiredError) as caught:
            check_cancellation_window(
                starts_at, booked_at, rescheduled_at, now=starts_at
            )
        self.assertIn("началось", str(caught.exception))

    def test_booking_after_reschedule_uses_regular_rule(self):
        rescheduled_at = NOW - timedelta(hours=3)
        booked_at = NOW - timedelta(hours=1)
        starts_at = NOW + timedelta(hours=5)
        with self.assertRaises(CancellationWindowExpiredError):
            check_cancellation_window(starts_at, booked_at, rescheduled_at, now=NOW)

    def test_user_booking_can_cancel_hint(self):
        booking = UserBooking(
            id=1,
            slot_id=2,
            class_key="beginner",
            starts_at=NOW + timedelta(hours=3),
            booking_status="confirmed",
            slot_status="open",
            booked_at=NOW - timedelta(days=1),
            rescheduled_at=NOW - timedelta(hours=1),
        )
        self.assertTrue(booking.can_cancel(NOW))
        cancelled_slot = UserBooking(**{**booking.__dict__, "slot_status": "cancelled"})
        self.assertFalse(cancelled_slot.can_cancel(NOW))


class AdminDatetimeTests(unittest.TestCase):
    def test_short_date_resolves_to_nearest_future(self):
        now = datetime(2026, 12, 20, 9, 0, tzinfo=timezone.utc)
        parsed = parse_admin_datetime("05.01 19:00", now=now)
        local = to_studio_time(parsed)
        self.assertEqual((local.year, local.month, local.day), (2027, 1, 5))
        self.assertEqual((local.hour, local.minute), (19, 0))

    def test_full_formats_and_iso(self):
        for text in ("14.10.2026 19:00", "2026-10-14 19:00", "14.10.26 19:00"):
            local = to_studio_time(parse_admin_datetime(text))
            self.assertEqual((local.month, local.day, local.hour), (10, 14, 19))
        iso = parse_admin_datetime("2026-10-14T19:00+04:00")
        self.assertEqual(iso.utcoffset(), timedelta(hours=4))

    def test_garbage_is_rejected(self):
        for text in ("", "завтра", "32.13 25:00"):
            with self.assertRaises(ValueError):
                parse_admin_datetime(text)


class NotificationTextTests(unittest.TestCase):
    def test_every_kind_renders_title_body_and_actions(self):
        payloads = {
            "booking_confirmed": {
                "class_key": "beginner",
                "starts_at": "2026-10-08T16:00:00+00:00",
                "balance": 3,
                "source_title": "Абонемент на 4 занятия",
            },
            "booking_cancelled": {
                "class_key": "beginner",
                "starts_at": "2026-10-08T16:00:00+00:00",
                "balance": 4,
            },
            "slot_cancelled": {
                "class_key": "intermediate",
                "starts_at": "2026-10-08T16:00:00+00:00",
                "reason": "болезнь преподавателя",
                "refunded": True,
                "balance": 2,
            },
            "slot_rescheduled": {
                "class_key": "beginner",
                "old_starts_at": "2026-10-08T16:00:00+00:00",
                "new_starts_at": "2026-10-09T17:00:00+00:00",
            },
            "lesson_reminder": {
                "class_key": "individual",
                "starts_at": "2026-10-06T15:00:00+00:00",
            },
            "low_balance": {"balance": 1},
            "package_granted": {"title": "Абонемент", "lessons": 4, "balance": 4},
            "package_expiring": {
                "title": "Абонемент на 4 занятия",
                "lessons": 2,
                "expires_at": "2026-10-20T16:00:00+00:00",
            },
            "credits_expired": {
                "title": "Абонемент на 4 занятия",
                "lessons": 2,
                "balance": 0,
            },
            "package_revoked": {
                "title": "Абонемент",
                "revoked_lessons": 2,
                "balance": 0,
                "reason": "ошибка",
            },
            "credits_adjusted": {"delta": -1, "balance": 2, "reason": "ошибка"},
        }
        self.assertEqual(set(payloads), set(NOTIFICATION_KINDS))
        for kind, payload in payloads.items():
            with self.subTest(kind=kind):
                message = render_notification(kind, payload, now=NOW)
                self.assertTrue(message.title)
                self.assertTrue(message.body)
                self.assertTrue(message.actions)
                for _, data in message.actions:
                    self.assertLessEqual(len(data.encode("utf-8")), 64)

    def test_texts_use_studio_time_and_key_facts(self):
        rescheduled = render_notification(
            "slot_rescheduled",
            {
                "class_key": "beginner",
                "old_starts_at": "2026-10-08T16:00:00+00:00",
                "new_starts_at": "2026-10-09T17:00:00+00:00",
            },
        )
        # Europe/Moscow: 16:00 UTC -> 19:00, 17:00 UTC -> 20:00.
        self.assertIn("Было: Чт 08.10 · 19:00", rescheduled.body)
        self.assertIn("Стало: Пт 09.10 · 20:00", rescheduled.body)
        cancelled = render_notification(
            "slot_cancelled",
            {
                "class_key": "beginner",
                "starts_at": "2026-10-08T16:00:00+00:00",
                "reason": "болезнь",
                "refunded": True,
                "balance": 2,
            },
        )
        self.assertIn("Причина: болезнь.", cancelled.body)
        self.assertIn("вернулось на баланс", cancelled.body)
        self.assertIn("Баланс: 2 занятия.", cancelled.body)
        reminder = render_notification(
            "lesson_reminder",
            {"class_key": "beginner", "starts_at": "2026-10-06T15:00:00+00:00"},
            now=NOW,
        )
        self.assertIn("Сегодня в 18:00", reminder.body)

    def test_unknown_kind_does_not_crash(self):
        self.assertTrue(render_notification("something_new", {}).title)


class HistoryClassificationTests(unittest.TestCase):
    def test_operation_types(self):
        cases = {
            ("purchase", 4, None): "purchase",
            ("lesson_use", -1, 7): "lesson_use",
            ("lesson_use", -1, None): "lesson_debit",
            ("adjustment", 1, 7): "booking_refund",
            ("admin_adjustment", 2, None): "manual_credit",
            ("admin_adjustment", -2, None): "manual_debit",
            ("slot_cancellation", 1, 7): "studio_cancellation",
            ("package_grant", 4, None): "package_grant",
            ("package_revoke", -2, None): "package_revoke",
            ("refund_reservation", -4, None): "payment_refund_pending",
            ("refund", -4, None): "payment_refund",
            ("refund_release", 4, None): "payment_refund_cancelled",
        }
        for (entry_type, delta, booking_id), expected in cases.items():
            with self.subTest(entry_type=entry_type, delta=delta):
                self.assertEqual(
                    classify_entry(entry(entry_type, delta, booking_id)), expected
                )
                self.assertIn(expected, OPERATION_LABELS)

    def test_reason_is_shown_only_for_studio_operations(self):
        manual = describe_entry(entry("admin_adjustment", 1, reason="наличные"))
        self.assertEqual(manual.visible_reason, "наличные")
        hidden = describe_entry(entry("lesson_use", -1, 3, reason="служебное"))
        self.assertIsNone(hidden.visible_reason)

    def test_booking_status_labels(self):
        base = dict(
            id=1,
            slot_id=2,
            class_key="beginner",
            booking_status="confirmed",
            slot_status="open",
        )
        future = UserBooking(starts_at=NOW + timedelta(days=1), **base)
        past = UserBooking(starts_at=NOW - timedelta(days=1), **base)
        self.assertEqual(booking_status(future, NOW), "upcoming")
        self.assertEqual(booking_status(past, NOW), "attended")
        by_studio = UserBooking(
            starts_at=NOW,
            **{**base, "booking_status": "cancelled"},
            cancelled_by="studio",
        )
        self.assertEqual(booking_status(by_studio, NOW), "cancelled_by_studio")
        by_user = UserBooking(
            starts_at=NOW,
            **{**base, "booking_status": "cancelled"},
            cancelled_by="user",
        )
        self.assertEqual(booking_status(by_user, NOW), "cancelled_by_user")


if __name__ == "__main__":
    unittest.main()


class MessageLengthTests(unittest.TestCase):
    def test_long_screens_are_trimmed_to_telegram_limit(self):
        from flamenco_bot.keyboards.user.screens import MAX_MESSAGE_LENGTH, fit_message

        self.assertEqual(fit_message("коротко"), "коротко")
        trimmed = fit_message("а" * 10_000)
        self.assertLessEqual(len(trimmed), MAX_MESSAGE_LENGTH)
        self.assertTrue(trimmed.endswith("…"))
