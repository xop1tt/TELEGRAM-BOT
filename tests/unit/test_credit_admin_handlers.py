import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from flamenco_bot.config import Config
from flamenco_bot.database.repository import (
    CreditAdjustment,
    CreditBalanceMismatch,
    CreditReconciliation,
    InsufficientLessonCreditsError,
)
from flamenco_bot.handlers.admin import (
    audit_credit_balances,
    cancel_credit_adjustment,
    confirm_credit_adjustment,
    require_credit_adjustment_confirmation,
    prepare_credit_adjustment,
)
from flamenco_bot.handlers.states import AdminForm
from flamenco_bot.keyboards.admin import (
    CREDITS_ADJUST_CANCEL,
    CREDITS_ADJUST_CONFIRM,
    credit_adjustment_confirmation_keyboard,
)
from tests.support import FakeMessage, FakeRepository, FakeState

ADMIN_ID = 1001
TARGET_ID = 2002


class CreditAdjustmentHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.message = FakeMessage(telegram_id=ADMIN_ID)
        self.state = FakeState()
        self.repository = FakeRepository()
        self.admin = replace(self.repository.profile, is_admin=True, role="admin")
        self.target = replace(
            self.repository.profile,
            telegram_id=TARGET_ID,
            user_name="Мария",
            lesson_credits=3,
        )

        async def get_profile(telegram_id):
            return {ADMIN_ID: self.admin, TARGET_ID: self.target}.get(telegram_id)

        self.repository.get_profile = AsyncMock(side_effect=get_profile)
        self.repository.adjust_lesson_credits = AsyncMock(
            return_value=CreditAdjustment(7, TARGET_ID, 2, 5, True)
        )
        self.repository.get_credit_reconciliation = AsyncMock()
        log_patch = patch("flamenco_bot.handlers.admin.actions_logger")
        self.actions_logger = log_patch.start().return_value
        self.addCleanup(log_patch.stop)

    async def _start(self, text):
        """Как кнопка «± Баланс»: участник и «+N причина» уже разобраны."""
        _, telegram_id, delta, reason = text.split(maxsplit=3)
        self.message.text = "{} {}".format(delta, reason)
        await prepare_credit_adjustment(
            self.message,
            self.state,
            self.repository,
            int(telegram_id),
            int(delta),
            reason,
        )

    async def _confirm(self):
        self.message.text = CREDITS_ADJUST_CONFIRM
        await confirm_credit_adjustment(self.message, self.state, self.repository)

    @property
    def reply(self):
        return self.message.last_answer.args[0]

    async def test_amount_over_configured_limit_is_rejected(self):
        limit = Config.CREDIT_ADJUSTMENT_MAX_DELTA
        await self._start("/credits_adjust 2002 +{} причина".format(limit + 1))

        self.assertIn(str(limit), self.reply)
        self.assertIsNone(self.state.current_state)

    async def test_unknown_participant_and_overdraft_are_rejected_before_preview(self):
        await self._start("/credits_adjust 999 +1 причина")
        self.assertIn("не найден", self.reply)

        await self._start("/credits_adjust 2002 -4 причина")
        self.assertIn("отрицательным", self.reply)
        self.assertIsNone(self.state.current_state)
        self.repository.adjust_lesson_credits.assert_not_awaited()

    async def test_preview_requires_confirmation_before_changing_balance(self):
        await self._start("/credits_adjust 2002 +2 оплата наличными")

        self.assertEqual(
            self.state.current_state,
            AdminForm.waiting_for_credit_adjustment_confirmation.state,
        )
        self.assertIn("Мария", self.reply)
        self.assertIn("+2", self.reply)
        self.assertIn("3 → 5", self.reply)
        self.assertIn("оплата наличными", self.reply)
        self.assertEqual(
            self.message.last_answer.kwargs["reply_markup"],
            credit_adjustment_confirmation_keyboard(),
        )
        self.repository.adjust_lesson_credits.assert_not_awaited()

    async def test_confirmation_applies_with_admin_reason_key_and_audit(self):
        await self._start("/credits_adjust 2002 +2 оплата наличными")
        key = uuid.UUID(self.state.data["credit_adjustment"]["key"])

        await self._confirm()

        self.repository.adjust_lesson_credits.assert_awaited_once_with(
            TARGET_ID, 2, "оплата наличными", ADMIN_ID, key
        )
        self.assertIsNone(self.state.current_state)
        self.assertIn("теперь 5", self.reply)
        audit = self.actions_logger.warning.call_args
        self.assertIn("action=adjust_credits", audit.args[0])
        self.assertEqual(audit.args[1:4], (ADMIN_ID, TARGET_ID, 2))
        # Причина не попадает в файловый журнал (она есть в ledger).
        self.assertNotIn("оплата наличными", str(audit))

    async def test_negative_adjustment_goes_through_the_same_confirmation(self):
        await self._start("/credits_adjust 2002 -1 ошибка начисления")
        self.assertIn("3 → 2", self.reply)
        self.repository.adjust_lesson_credits.return_value = CreditAdjustment(
            8, TARGET_ID, -1, 2, True
        )

        await self._confirm()

        args = self.repository.adjust_lesson_credits.await_args.args
        self.assertEqual(args[:4], (TARGET_ID, -1, "ошибка начисления", ADMIN_ID))
        self.assertIn("-1", self.reply)

    async def test_confirm_without_pending_adjustment_does_nothing(self):
        await self._confirm()

        self.assertIn("Нет корректировки", self.reply)
        self.repository.adjust_lesson_credits.assert_not_awaited()

    async def test_repeated_confirm_tap_cannot_apply_twice(self):
        await self._start("/credits_adjust 2002 +2 причина")
        await self._confirm()
        await self._confirm()

        self.repository.adjust_lesson_credits.assert_awaited_once()
        self.assertIn("Нет корректировки", self.reply)

    async def test_expired_confirmation_is_dropped(self):
        await self._start("/credits_adjust 2002 +2 причина")
        self.state.data["credit_adjustment"]["expires_at"] = 0

        await self._confirm()

        self.assertIn("истекло", self.reply)
        self.assertIsNone(self.state.current_state)
        self.repository.adjust_lesson_credits.assert_not_awaited()

    async def test_cancel_discards_the_pending_adjustment(self):
        await self._start("/credits_adjust 2002 +2 причина")
        self.message.text = CREDITS_ADJUST_CANCEL
        await cancel_credit_adjustment(self.message, self.state, self.repository)

        self.assertIsNone(self.state.current_state)
        self.assertIn("баланс не изменён", self.reply)
        await self._confirm()
        self.repository.adjust_lesson_credits.assert_not_awaited()

    async def test_other_text_during_confirmation_only_repeats_the_prompt(self):
        await self._start("/credits_adjust 2002 +2 причина")
        self.message.text = "да"
        await require_credit_adjustment_confirmation(
            self.message, self.state, self.repository
        )

        self.assertIn("Нажмите", self.reply)
        self.repository.adjust_lesson_credits.assert_not_awaited()
        self.assertIsNotNone(self.state.current_state)

    async def test_overdraft_after_preview_is_reported_and_clears_state(self):
        await self._start("/credits_adjust 2002 -2 причина")
        self.repository.adjust_lesson_credits.side_effect = (
            InsufficientLessonCreditsError("недостаточно занятий")
        )

        await self._confirm()

        self.assertIn("Баланс не изменён", self.reply)
        self.assertIsNone(self.state.current_state)

    async def test_failed_attempt_keeps_confirmation_and_retries_with_same_key(self):
        await self._start("/credits_adjust 2002 +2 причина")
        self.repository.adjust_lesson_credits.side_effect = [
            ConnectionError("db down"),
            CreditAdjustment(7, TARGET_ID, 2, 5, True),
        ]

        with self.assertRaises(ConnectionError):
            await self._confirm()
        self.assertIsNotNone(self.state.current_state)
        await self._confirm()

        keys = {
            call.args[4]
            for call in self.repository.adjust_lesson_credits.await_args_list
        }
        self.assertEqual(len(keys), 1)
        self.assertIsNone(self.state.current_state)

    async def test_already_applied_adjustment_is_reported_without_new_audit(self):
        await self._start("/credits_adjust 2002 +2 причина")
        self.repository.adjust_lesson_credits.return_value = CreditAdjustment(
            7, TARGET_ID, 2, 5, False
        )

        await self._confirm()

        self.assertIn("уже была применена", self.reply)


class CreditAuditHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.message = FakeMessage(telegram_id=ADMIN_ID, text="/credits_audit")
        self.repository = FakeRepository()
        admin = replace(self.repository.profile, is_admin=True, role="admin")
        self.repository.get_profile = AsyncMock(return_value=admin)
        self.repository.get_credit_reconciliation = AsyncMock()
        log_patch = patch("flamenco_bot.handlers.admin.actions_logger")
        log_patch.start()
        self.addCleanup(log_patch.stop)

    async def test_clean_report(self):
        self.repository.get_credit_reconciliation.return_value = CreditReconciliation(
            checked_users=12, mismatched_users=0, mismatches=[]
        )
        await audit_credit_balances(self.message, self.repository)

        self.assertIn("расхождений нет", self.message.last_answer.args[0])
        self.assertIn("12", self.message.last_answer.args[0])

    async def test_report_lists_mismatches_and_does_not_repair(self):
        self.repository.get_credit_reconciliation.return_value = CreditReconciliation(
            checked_users=12,
            mismatched_users=2,
            mismatches=[
                CreditBalanceMismatch(
                    100, "Анна", 9, 4, 1, datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
                ),
                CreditBalanceMismatch(101, "Борис", 2, 0, 0, None),
            ],
        )
        await audit_credit_balances(self.message, self.repository)

        text = self.message.last_answer.args[0]
        self.assertIn("у 2 из 12", text)
        self.assertIn("100 | Анна | баланс 9 | ledger 4 | разница +5 | записей 1", text)
        self.assertIn("101 | Борис | баланс 2 | ledger 0 | разница +2", text)
        self.assertIn("в ledger нет записей", text)
        self.assertIn("автоматически не исправляется", text)

    async def test_non_admin_cannot_run_audit(self):
        self.message.from_user.id = 5
        self.repository.get_profile = AsyncMock(
            return_value=replace(self.repository.profile, telegram_id=5)
        )
        await audit_credit_balances(self.message, self.repository)

        self.assertIn("только администратору", self.message.last_answer.args[0])
        self.repository.get_credit_reconciliation.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
