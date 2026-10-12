"""«Покупки»: неудачные и редкие исходы оплаты и проверки платежа.

Счастливый путь (оплатить → проверить → зачислено) покрыт в test_handlers.py;
здесь — всё остальное: некорректные кнопки, смена цены, отказ провайдера, счёт
без безопасной ссылки, каждый статус проверки. Главное правило: при любом отказе
деньги/занятия не создаются повторно и пользователь получает понятный ответ.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Пакет обработчиков импортируется первым: прямой импорт purchases.py без него
# упирается в циклическую зависимость handlers ↔ keyboards.user.purchases
# (остальные тесты импортируют flamenco_bot.handlers раньше).
import flamenco_bot.handlers  # noqa: F401
from flamenco_bot.keyboards.user.purchases import (
    check_lesson_payment,
    pay_for_package,
    show_bill,
    show_package,
)
from flamenco_bot.services import CheckoutFailedError, PaymentCheckStatus
from flamenco_bot.services.payments import CheckoutResult, PaymentCheckResult
from tests.support import (
    FakeCallback,
    FakeRepository,
    callback_data,
    inline_buttons,
)

PAYMENT_KWARGS = dict(
    id=8,
    telegram_id=1001,
    package_key="single",
    lessons=1,
    amount_minor=100000,
    provider_payment_id="p",
    confirmation_url="https://pay.example.test/c",
)


def payment(status="pending", url="https://pay.example.test/c"):
    from flamenco_bot.database.repository import LessonPayment

    return LessonPayment(
        **{**PAYMENT_KWARGS, "confirmation_url": url, "status": status}
    )


class PackageCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_package_buttons_are_rejected(self):
        for data in ("pack:single", "pack:nope:0", "pack:single:x", "pack:a:b:c"):
            with self.subTest(data=data):
                callback = FakeCallback(data)
                await show_package(callback)
                self.assertEqual(
                    callback.answer.await_args.args[0], "Абонемент не найден."
                )
                self.assertTrue(callback.answer.await_args.kwargs["show_alert"])

    async def test_known_package_shows_its_price_button(self):
        callback = FakeCallback("pack:single:5")
        await show_package(callback)
        _, markup = callback.screen
        self.assertTrue(any(d.startswith("pay:single:") for d in callback_data(markup)))

    async def test_malformed_pay_buttons_never_reach_the_provider(self):
        repository = FakeRepository()
        gateway = SimpleNamespace(is_configured=True, create_payment=AsyncMock())
        for data in (
            "pay:single",
            "pay:nope:1000:0",
            "pay:single:abc:0",
            "pay:single:1000:x",
        ):
            with self.subTest(data=data):
                callback = FakeCallback(data)
                await pay_for_package(callback, repository, gateway)
                self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        gateway.create_payment.assert_not_awaited()
        repository.begin_lesson_payment_attempt.assert_not_awaited()


class CheckoutOutcomeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = FakeRepository()
        self.gateway = SimpleNamespace(is_configured=True)

    async def _pay(self, checkout=None, error=None):
        callback = FakeCallback("pay:single:1000:0")
        with patch(
            "flamenco_bot.keyboards.user.purchases.PaymentService.start_checkout",
            AsyncMock(return_value=checkout, side_effect=error),
        ):
            await pay_for_package(callback, self.repository, self.gateway)
        return callback

    async def test_provider_failure_tells_not_to_pay_again(self):
        callback = await self._pay(error=CheckoutFailedError("timeout"))
        text, markup = callback.screen
        self.assertIn("Не оплачивайте повторно", text)
        self.assertIsNone(markup)

    async def test_missing_safe_link_registers_payment_but_asks_not_to_retry(self):
        checkout = CheckoutResult(payment(), 100000, "single", None)
        callback = await self._pay(checkout=checkout)
        text, markup = callback.screen
        self.assertIn("Платёж №8 зарегистрирован", text)
        self.assertIn("Не создавайте новый платёж", text)
        self.assertIsNone(markup)

    async def test_unconfigured_gateway_creates_no_profile_or_payment(self):
        self.gateway.is_configured = False
        callback = FakeCallback("pay:single:1000:0")
        await pay_for_package(callback, self.repository, self.gateway)
        self.assertIn("Оплата недоступна", callback.screen[0])
        self.repository.get_or_create_profile.assert_not_awaited()
        self.repository.begin_lesson_payment_attempt.assert_not_awaited()


class BillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = FakeRepository()

    async def test_bad_callback_and_foreign_or_closed_bill(self):
        for data in ("bill:8", "bill:x:0", "bill:8:x"):
            callback = FakeCallback(data)
            await show_bill(callback, self.repository)
            self.assertEqual(callback.answer.await_args.args[0], "Счёт не найден.")

        # Чужой счёт (репозиторий не нашёл) и уже оплаченный выглядят одинаково.
        for found in (None, payment("succeeded"), payment("canceled")):
            self.repository.get_lesson_payment.return_value = found
            callback = FakeCallback("bill:8:0")
            await show_bill(callback, self.repository)
            self.assertIn("уже оплачен или закрыт", callback.answer.await_args.args[0])
        self.repository.get_lesson_payment.assert_awaited_with(8, 1001)

    async def test_open_bill_offers_payment_link_and_check(self):
        self.repository.get_lesson_payment.return_value = payment()
        callback = FakeCallback("bill:8:3")
        await show_bill(callback, self.repository)
        text, markup = callback.screen
        buttons = inline_buttons(markup)
        self.assertEqual(buttons[0].url, "https://pay.example.test/c")
        self.assertIn("lesson_payment_check:8:3", callback_data(markup))
        self.assertIn("Проверить оплату", text)

    async def test_unsafe_confirmation_link_is_not_offered(self):
        for url in (
            "http://insecure.example/pay",
            "javascript:alert(1)",
            "",
            "https://u:p@host/x",
        ):
            self.repository.get_lesson_payment.return_value = payment(url=url)
            callback = FakeCallback("bill:8:0")
            await show_bill(callback, self.repository)
            _, markup = callback.screen
            self.assertFalse([b for b in inline_buttons(markup) if b.url], url)
            self.assertIn("lesson_payment_check:8:0", callback_data(markup))


class PaymentCheckStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = FakeRepository()
        self.gateway = SimpleNamespace(is_configured=True)

    async def _check(
        self, status, payment_obj=None, data="lesson_payment_check:8:0", credits=None
    ):
        callback = FakeCallback(data)
        result = PaymentCheckResult(status, payment_obj, credits)
        with patch(
            "flamenco_bot.keyboards.user.purchases.PaymentService.check_payment",
            AsyncMock(return_value=result),
        ):
            await check_lesson_payment(callback, self.repository, self.gateway)
        return callback

    async def test_refusals_show_alert_and_send_no_confirmation_message(self):
        expectations = {
            PaymentCheckStatus.NOT_FOUND: "Платёж не найден",
            PaymentCheckStatus.CANCELED: "Платёж отменён",
            PaymentCheckStatus.PROVIDER_CANCELED: "Платёж отменён",
            PaymentCheckStatus.REFUND_PENDING: "Возврат уже обрабатывается",
            PaymentCheckStatus.PROVIDER_UNAVAILABLE: "Не удалось проверить платёж",
            PaymentCheckStatus.MISMATCH: "Данные платежа не совпали",
            PaymentCheckStatus.PENDING: "Оплата ещё не подтверждена",
        }
        self.assertEqual(
            set(expectations)
            | {
                PaymentCheckStatus.CONFIRMED,
                PaymentCheckStatus.ALREADY_SUCCEEDED,
                PaymentCheckStatus.ALREADY_PROCESSED,
                PaymentCheckStatus.STATUS_CHANGED,
            },
            set(PaymentCheckStatus),
        )
        for status, fragment in expectations.items():
            with self.subTest(status=status):
                callback = await self._check(status)
                self.assertIn(fragment, callback.answer.await_args.args[0])
                self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
                callback.message.answer.assert_not_awaited()

    async def test_status_changed_asks_to_retry_without_alert(self):
        callback = await self._check(PaymentCheckStatus.STATUS_CHANGED)
        self.assertIn("Статус платежа изменился", callback.answer.await_args.args[0])
        self.assertIn("ещё раз", callback.message.answer.await_args.args[0])

    async def test_confirmed_credits_are_reported_once_with_balance(self):
        callback = await self._check(
            PaymentCheckStatus.CONFIRMED, payment("succeeded"), credits=4
        )
        text = callback.message.answer.await_args.args[0]
        self.assertIn("Оплата прошла. Зачислено 1 занятие", text)
        self.assertIn("Баланс: 4", text)
        self.assertEqual(callback.answer.await_args.args[0], "Оплата подтверждена.")

    async def test_repeated_check_does_not_claim_a_new_credit(self):
        for status in (
            PaymentCheckStatus.ALREADY_SUCCEEDED,
            PaymentCheckStatus.ALREADY_PROCESSED,
        ):
            callback = await self._check(status, payment("succeeded"), credits=2)
            text = callback.message.answer.await_args.args[0]
            self.assertIn("Оплата уже подтверждена", text)
            self.assertNotIn("Зачислено", text)

    async def test_missing_balance_is_read_from_the_repository(self):
        self.repository.get_lesson_credits.return_value = 7
        callback = await self._check(
            PaymentCheckStatus.ALREADY_PROCESSED, None, credits=None
        )
        self.assertIn("Баланс: 7", callback.message.answer.await_args.args[0])

    async def test_unavailable_slot_after_payment_gets_a_notice(self):
        callback = await self._check(
            PaymentCheckStatus.CONFIRMED,
            payment("succeeded"),
            "lesson_payment_check:8:999",
            1,
        )
        text = callback.message.answer.await_args.args[0]
        self.assertIn("Выбранное занятие уже недоступно", text)

    async def test_malformed_check_buttons(self):
        for data in (
            "lesson_payment_check:x",
            "lesson_payment_check:",
            "lesson_payment_check:1:2:3",
        ):
            with self.subTest(data=data):
                callback = await self._check(
                    PaymentCheckStatus.CONFIRMED, payment(), data
                )
                self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
                callback.message.answer.assert_not_awaited()

    async def test_callback_without_user_or_message_is_refused(self):
        callback = FakeCallback("lesson_payment_check:8:0")
        callback.message = None
        with patch(
            "flamenco_bot.keyboards.user.purchases.PaymentService.check_payment",
            AsyncMock(),
        ) as check:
            await check_lesson_payment(callback, self.repository, self.gateway)
        check.assert_not_awaited()
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])


if __name__ == "__main__":
    unittest.main()
