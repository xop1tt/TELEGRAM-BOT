"""Отправка уведомлений из outbox: после commit, с повторами и без дублей."""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage

from flamenco_bot.runtime.notifications import (
    MAX_ATTEMPTS,
    NOTIFICATION_LOCK_ID,
    deliver_notification,
    dispatch_due_notifications,
    retry_delay,
    run_notification_worker,
)
from tests.pg import PostgresTestCase, fetchrow, fetchval, sql


LOGGER = SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
    exception=lambda *a, **k: None,
)
METHOD = SendMessage(chat_id=1, text="x")


class StopLoop(Exception):
    pass


class NotificationDispatcherTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.repository.get_or_create_profile(1001, "Анна")
        self.bot = SimpleNamespace(send_message=AsyncMock())

    async def _queue(self, kind="credits_adjusted", payload=None, key="k:1"):
        await self.repository.enqueue_notification(
            1001, kind, payload or {"delta": 1, "balance": 2}, key
        )
        return (await self.repository.claim_due_notifications())[0]

    async def _status(self, notification_id):
        return await fetchval(
            self.repository,
            "SELECT delivery_status FROM user_notifications WHERE id = $1",
            notification_id,
        )

    async def test_sent_notification_is_rendered_with_buttons(self):
        notification = await self._queue()

        status = await deliver_notification(
            self.repository, self.bot, notification, LOGGER
        )

        self.assertEqual(status, "sent")
        self.assertEqual(await self._status(notification.id), "sent")
        args, kwargs = self.bot.send_message.await_args
        self.assertEqual(args[0], 1001)
        self.assertIn("Баланс изменён студией", args[1])
        self.assertIsNotNone(kwargs["reply_markup"])
        # Отправленное уведомление больше не забирается.
        self.assertEqual(await self.repository.claim_due_notifications(), [])

    async def test_blocked_bot_marks_failed_without_retry(self):
        notification = await self._queue()
        self.bot.send_message.side_effect = TelegramForbiddenError(
            METHOD, "bot was blocked by the user"
        )

        status = await deliver_notification(
            self.repository, self.bot, notification, LOGGER
        )

        self.assertEqual(status, "failed")
        self.assertEqual(await self._status(notification.id), "failed")

    async def test_rate_limit_and_network_errors_are_retried_later(self):
        notification = await self._queue()
        self.bot.send_message.side_effect = TelegramRetryAfter(METHOD, "slow", 30)
        now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

        status = await deliver_notification(
            self.repository, self.bot, notification, LOGGER, clock=lambda: now
        )

        self.assertEqual(status, "retry")
        data = await fetchrow(
            self.repository,
            "SELECT delivery_status, next_attempt_at FROM user_notifications "
            "WHERE id = $1",
            notification.id,
        )
        self.assertEqual(data["delivery_status"], "pending")
        self.assertEqual(data["next_attempt_at"], now + timedelta(seconds=30))

        await sql(
            self.repository,
            "UPDATE user_notifications SET next_attempt_at = NOW() WHERE id = $1",
            notification.id,
        )
        notification = (await self.repository.claim_due_notifications())[0]
        self.bot.send_message.side_effect = TelegramNetworkError(METHOD, "timeout")
        self.assertEqual(
            await deliver_notification(self.repository, self.bot, notification, LOGGER),
            "retry",
        )

    async def test_gives_up_after_max_attempts(self):
        notification = await self._queue()
        self.bot.send_message.side_effect = TelegramNetworkError(METHOD, "timeout")
        exhausted = type(notification)(
            **{**notification.__dict__, "attempts": MAX_ATTEMPTS}
        )

        status = await deliver_notification(
            self.repository, self.bot, exhausted, LOGGER
        )

        self.assertEqual(status, "failed")

    async def test_outdated_reminder_is_skipped_not_sent(self):
        slot = await self.repository.create_class_slot(
            "beginner", datetime.now(timezone.utc) + timedelta(days=2), 5, 77
        )
        notification = await self._queue(
            "lesson_reminder",
            {
                "slot_id": slot.id,
                "booking_id": 999,
                "class_key": "beginner",
                "starts_at": slot.starts_at.isoformat(),
            },
            key="reminder:999",
        )

        status = await deliver_notification(
            self.repository, self.bot, notification, LOGGER
        )

        self.assertEqual(status, "skipped")
        self.bot.send_message.assert_not_awaited()

    async def test_dispatch_batch_counts_sent_and_survives_failures(self):
        await self.repository.enqueue_notification(1001, "low_balance", {}, "a")
        await self.repository.enqueue_notification(1001, "low_balance", {}, "b")
        self.bot.send_message.side_effect = [None, RuntimeError("unexpected")]

        sent = await dispatch_due_notifications(self.repository, self.bot, LOGGER)

        self.assertEqual(sent, 1)

    async def test_worker_skips_iteration_when_other_process_holds_lock(self):
        await self.repository.enqueue_notification(1001, "low_balance", {}, "a")
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 1:
                raise StopLoop

        async with self.repository.try_advisory_lock(NOTIFICATION_LOCK_ID):
            with self.assertRaises(StopLoop):
                await run_notification_worker(
                    self.repository, self.bot, LOGGER, timedelta(hours=3), sleep=sleep
                )
        self.bot.send_message.assert_not_awaited()

        sleeps.clear()
        with self.assertRaises(StopLoop):
            await run_notification_worker(
                self.repository, self.bot, LOGGER, timedelta(hours=3), sleep=sleep
            )
        self.bot.send_message.assert_awaited_once()

    def test_retry_delay_grows_and_is_capped(self):
        self.assertEqual(retry_delay(1), timedelta(seconds=30))
        self.assertEqual(retry_delay(2), timedelta(seconds=60))
        self.assertEqual(retry_delay(20), timedelta(hours=1))


if __name__ == "__main__":
    unittest.main()
