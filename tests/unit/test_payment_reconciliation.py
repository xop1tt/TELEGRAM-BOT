"""Фоновая сверка "забытых" pending-платежей (без вебхука ЮKassa)."""

import asyncio
import logging
import unittest
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from flamenco_bot.runtime.payment_reconciliation import (
    RECONCILIATION_LOCK_ID,
    reconcile_pending_payments,
    start_reconciliation_task,
)
from flamenco_bot.services.payments import PaymentCheckResult, PaymentCheckStatus
from tests.pg import PostgresTestCase


@dataclass(frozen=True)
class _Record:
    id: int
    telegram_id: int


def _lock(acquired=True, calls=None):
    """Подмена ``repository.try_advisory_lock`` для AsyncMock-репозитория."""

    @asynccontextmanager
    async def try_advisory_lock(lock_id):
        if calls is not None:
            calls.append(lock_id)
        yield acquired

    return try_advisory_lock


class ReconcilePendingPaymentsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.logger = logging.getLogger("test.payment_reconciliation")
        self.repository = AsyncMock()
        self.lock_calls = []
        self.repository.try_advisory_lock = _lock(calls=self.lock_calls)
        self.payment_service = AsyncMock()

    async def _run_one_tick(self, **kwargs):
        """Запускает цикл и останавливает его после первой итерации."""

        class _StopAfterOneTick(Exception):
            pass

        ticks = {"count": 0}

        async def sleep(_seconds):
            ticks["count"] += 1
            if ticks["count"] > 1:
                raise _StopAfterOneTick

        with self.assertRaises(_StopAfterOneTick):
            await reconcile_pending_payments(
                self.payment_service,
                self.repository,
                self.logger,
                sleep=sleep,
                clock=lambda: datetime(2025, 1, 1, tzinfo=timezone.utc),
                **kwargs,
            )

    async def test_checks_every_pending_payment_returned_by_the_repository(self):
        self.repository.list_pending_lesson_payments_older_than.return_value = [
            _Record(id=1, telegram_id=100),
            _Record(id=2, telegram_id=200),
        ]
        self.payment_service.check_payment.return_value = PaymentCheckResult(
            PaymentCheckStatus.PENDING
        )

        await self._run_one_tick()

        self.assertEqual(self.payment_service.check_payment.await_count, 2)
        self.payment_service.check_payment.assert_any_await(1, 100)
        self.payment_service.check_payment.assert_any_await(2, 200)

    async def test_one_failing_check_does_not_stop_the_rest(self):
        self.repository.list_pending_lesson_payments_older_than.return_value = [
            _Record(id=1, telegram_id=100),
            _Record(id=2, telegram_id=200),
        ]
        self.payment_service.check_payment.side_effect = [
            RuntimeError("ЮKassa недоступна"),
            PaymentCheckResult(PaymentCheckStatus.CONFIRMED),
        ]

        await self._run_one_tick()

        self.assertEqual(self.payment_service.check_payment.await_count, 2)

    async def test_user_is_notified_only_when_sweep_credits_lessons(self):
        self.repository.list_pending_lesson_payments_older_than.return_value = [
            _Record(id=1, telegram_id=100),
            _Record(id=2, telegram_id=200),
            _Record(id=3, telegram_id=300),
        ]
        confirmed = PaymentCheckResult(PaymentCheckStatus.CONFIRMED, credits=4)
        self.payment_service.check_payment.side_effect = [
            PaymentCheckResult(PaymentCheckStatus.PENDING),
            confirmed,
            PaymentCheckResult(PaymentCheckStatus.ALREADY_SUCCEEDED, credits=4),
        ]
        on_confirmed = AsyncMock()

        await self._run_one_tick(on_confirmed=on_confirmed)

        on_confirmed.assert_awaited_once_with(200, confirmed)

    async def test_notification_failure_does_not_stop_the_sweep(self):
        self.repository.list_pending_lesson_payments_older_than.return_value = [
            _Record(id=1, telegram_id=100),
            _Record(id=2, telegram_id=200),
        ]
        self.payment_service.check_payment.return_value = PaymentCheckResult(
            PaymentCheckStatus.CONFIRMED
        )
        on_confirmed = AsyncMock(side_effect=RuntimeError("bot blocked"))

        await self._run_one_tick(on_confirmed=on_confirmed)

        self.assertEqual(self.payment_service.check_payment.await_count, 2)
        self.assertEqual(on_confirmed.await_count, 2)

    async def test_listing_failure_is_logged_and_does_not_crash_the_loop(self):
        self.repository.list_pending_lesson_payments_older_than.side_effect = (
            RuntimeError("БД недоступна")
        )

        await self._run_one_tick()

        self.payment_service.check_payment.assert_not_awaited()

    async def test_iteration_runs_under_reconciliation_lock(self):
        self.repository.list_pending_lesson_payments_older_than.return_value = []

        await self._run_one_tick()

        self.assertEqual(self.lock_calls, [RECONCILIATION_LOCK_ID])

    async def test_iteration_is_skipped_when_another_process_holds_the_lock(self):
        """Бот и API запускают сверку оба — итерацию делает только один."""
        self.repository.try_advisory_lock = _lock(acquired=False)
        self.repository.list_pending_lesson_payments_older_than.return_value = [
            _Record(id=1, telegram_id=100),
        ]

        await self._run_one_tick()

        self.repository.list_pending_lesson_payments_older_than.assert_not_awaited()
        self.payment_service.check_payment.assert_not_awaited()

    async def test_lock_failure_is_logged_and_does_not_crash_the_loop(self):
        @asynccontextmanager
        async def unavailable(lock_id):
            raise ConnectionRefusedError("БД недоступна")
            yield  # pragma: no cover

        self.repository.try_advisory_lock = unavailable

        await self._run_one_tick()

        self.payment_service.check_payment.assert_not_awaited()

    async def test_invalid_intervals_are_rejected(self):
        with self.assertRaises(ValueError):
            await reconcile_pending_payments(
                self.payment_service,
                self.repository,
                self.logger,
                interval_seconds=0,
            )


class StartReconciliationTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_task_is_started_when_checkout_is_unavailable(self):
        payment_service = AsyncMock()
        type(payment_service).checkout_available = False

        task = start_reconciliation_task(
            payment_service,
            repository=AsyncMock(),
            logger=logging.getLogger("test.payment_reconciliation"),
        )

        self.assertIsNone(task)

    async def test_task_is_started_when_checkout_is_available(self):
        payment_service = AsyncMock()
        type(payment_service).checkout_available = True
        repository = AsyncMock()
        repository.list_pending_lesson_payments_older_than.return_value = []

        task = start_reconciliation_task(
            payment_service,
            repository,
            logging.getLogger("test.payment_reconciliation"),
        )
        try:
            self.assertIsNotNone(task)
        finally:
            task.cancel()
            with self.assertRaises(BaseException):
                await task


class ApiLifespanReconciliationTests(PostgresTestCase):
    """Сверка платежей идёт и в процессе веб-API — без работающего бота."""

    async def test_api_starts_reconciliation_and_cancels_it_on_shutdown(self):
        from flamenco_bot.api import app as app_module

        started = {}

        def fake_start(payment_service, repository, logger, on_confirmed=None):
            started["service"] = payment_service
            started["on_confirmed"] = on_confirmed
            started["task"] = asyncio.create_task(asyncio.Event().wait())
            return started["task"]

        config = app_module.WebConfig
        with (
            patch.object(app_module, "start_reconciliation_task", fake_start),
            patch.object(app_module, "is_database_configured", return_value=True),
            patch.object(
                app_module.PostgresRepository,
                "connect",
                AsyncMock(return_value=self.repository),
            ),
            patch.object(config, "ENV", "development"),
        ):
            application = app_module.create_app()
            async with app_module.lifespan(application):
                self.assertIs(application.state.reconciliation_task, started["task"])
                # Сверке нужен только статус платежа — return_url бота, а не
                # WEB_YOOKASSA_RETURN_URL, который может быть ещё не задан.
                self.assertEqual(
                    started["service"].gateway.return_url,
                    config.YOOKASSA_RETURN_URL,
                )
                self.assertIsNotNone(started["on_confirmed"])

        self.assertTrue(started["task"].cancelled())


if __name__ == "__main__":
    unittest.main()
