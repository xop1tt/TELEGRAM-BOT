import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from flamenco_bot.database.repository import (
    CreditAdjustment,
    CreditBalanceMismatch,
    CreditReconciliation,
)
from flamenco_bot.services import CreditService


class CreditServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = AsyncMock()
        self.service = CreditService(self.repository, max_adjustment=10)
        self.key = uuid.uuid4()

    def test_limit_must_be_positive(self):
        with self.assertRaises(ValueError):
            CreditService(self.repository, 0)

    def test_validate_normalizes_reason_and_rejects_bad_input(self):
        self.assertEqual(self.service.validate_adjustment(2, "  наличные "), "наличные")
        for delta, reason in (
            (0, "причина"),
            (11, "причина"),
            (-11, "причина"),
            (1, ""),
            (1, "   "),
            (1, "я" * 301),
        ):
            with self.subTest(delta=delta, reason=reason[:5]):
                with self.assertRaises(ValueError):
                    self.service.validate_adjustment(delta, reason)
        self.assertEqual(self.service.validate_adjustment(-10, "x"), "x")

    async def test_adjust_passes_normalized_values_to_repository(self):
        adjustment = CreditAdjustment(5, 100, -2, 3, True)
        self.repository.adjust_lesson_credits.return_value = adjustment

        result = await self.service.adjust(100, -2, " ошибка ", 9001, self.key)

        self.assertIs(result, adjustment)
        self.repository.adjust_lesson_credits.assert_awaited_once_with(
            100, -2, "ошибка", 9001, self.key
        )

    async def test_invalid_adjustment_never_reaches_repository(self):
        with self.assertRaises(ValueError):
            await self.service.adjust(100, 11, "причина", 9001, self.key)
        self.repository.adjust_lesson_credits.assert_not_awaited()

    async def test_reconcile_returns_repository_report(self):
        report = CreditReconciliation(
            checked_users=3,
            mismatched_users=1,
            mismatches=[
                CreditBalanceMismatch(
                    100, "Анна", 5, 3, 2, datetime(2026, 1, 1, tzinfo=timezone.utc)
                )
            ],
        )
        self.repository.get_credit_reconciliation.return_value = report

        self.assertIs(await self.service.reconcile(20), report)
        self.assertEqual(report.mismatches[0].difference, 2)
        self.repository.get_credit_reconciliation.assert_awaited_once_with(20)


if __name__ == "__main__":
    unittest.main()
