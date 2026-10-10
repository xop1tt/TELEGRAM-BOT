"""Покупка занятий, сверка оплаты и возвраты через ЮKassa.

Сервис не зависит от интерфейса: Telegram-бот и будущий Web API вызывают
одни и те же методы и сами превращают результат в ответ пользователю.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlsplit

from ..database.repository import LessonPayment, PaymentAttemptUnresolved
from ..payments import LessonPackage, PaymentProviderError


logger = logging.getLogger("bot.services.payments")


class CheckoutUnavailableError(RuntimeError):
    """ЮKassa не настроена или нет постоянного хранилища платежей."""


class CheckoutFailedError(RuntimeError):
    """Создание платежа не подтверждено; повторная оплата небезопасна."""


@dataclass(frozen=True)
class CheckoutResult:
    payment: LessonPayment
    amount_minor: int
    package_key: str
    # None, если провайдер не вернул безопасную HTTPS-ссылку.
    confirmation_url: Optional[str]


class PaymentCheckStatus(Enum):
    NOT_FOUND = "not_found"
    ALREADY_SUCCEEDED = "already_succeeded"
    CANCELED = "canceled"
    REFUND_PENDING = "refund_pending"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    MISMATCH = "mismatch"
    CONFIRMED = "confirmed"
    ALREADY_PROCESSED = "already_processed"
    STATUS_CHANGED = "status_changed"
    PROVIDER_CANCELED = "provider_canceled"
    PENDING = "pending"


@dataclass(frozen=True)
class PaymentCheckResult:
    status: PaymentCheckStatus
    payment: Optional[LessonPayment] = None
    credits: Optional[int] = None


class RefundStatus(Enum):
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    MISMATCH = "mismatch"
    COMPLETED = "completed"
    ALREADY_PROCESSED = "already_processed"
    PENDING = "pending"
    RELEASED = "released"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class RefundResult:
    status: RefundStatus
    lessons: Optional[int] = None
    provider_refund_id: Optional[str] = None
    provider_status: Optional[str] = None
    error_message: Optional[str] = None


def safe_confirmation_url(url: Optional[str]) -> Optional[str]:
    if url is None:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return url


class PaymentService:
    def __init__(self, repository: Any, gateway: Any) -> None:
        self.repository = repository
        self.gateway = gateway

    @property
    def checkout_available(self) -> bool:
        return bool(self.gateway.is_configured)

    async def start_checkout(
        self,
        telegram_id: int,
        package: LessonPackage,
    ) -> CheckoutResult:
        if not self.checkout_available:
            raise CheckoutUnavailableError("Оплата недоступна")
        try:
            attempt = await self.repository.begin_lesson_payment_attempt(
                telegram_id=telegram_id,
                package_key=package.key,
                package_title=package.title,
                lessons=package.lessons,
                amount_minor=package.price_rub * 100,
            )
            provider_payment = await self.gateway.create_payment(
                amount_minor=attempt.amount_minor,
                description="Фламенко: {}".format(attempt.package_title),
                telegram_id=telegram_id,
                package_key=attempt.package_key,
                idempotence_key=str(attempt.idempotence_key),
            )
        except (PaymentProviderError, PaymentAttemptUnresolved, ValueError) as error:
            logger.error(
                "Failed to create YooKassa payment telegram_id=%s error_type=%s",
                telegram_id,
                type(error).__name__,
            )
            raise CheckoutFailedError("Создание платежа не подтверждено") from error

        payment = await self.repository.create_lesson_payment(
            telegram_id=telegram_id,
            package_key=attempt.package_key,
            package_title=attempt.package_title,
            lessons=attempt.lessons,
            amount_minor=attempt.amount_minor,
            provider_payment_id=provider_payment.payment_id,
            confirmation_url=provider_payment.confirmation_url or "",
            idempotence_key=attempt.idempotence_key,
        )
        confirmation_url = safe_confirmation_url(provider_payment.confirmation_url)
        if confirmation_url is None:
            logger.error("YooKassa confirmation URL invalid payment_id=%s", payment.id)
        else:
            logger.info(
                "Lesson payment created telegram_id=%s payment_id=%s package=%s",
                telegram_id,
                payment.id,
                attempt.package_key,
            )
        return CheckoutResult(
            payment=payment,
            amount_minor=attempt.amount_minor,
            package_key=attempt.package_key,
            confirmation_url=confirmation_url,
        )

    async def check_payment(
        self,
        payment_id: int,
        telegram_id: int,
    ) -> PaymentCheckResult:
        """Сверяет платёж с ЮKassa и начисляет занятия ровно один раз."""
        payment = await self.repository.get_lesson_payment(payment_id, telegram_id)
        if payment is None:
            logger.warning(
                "Lesson payment check failed reason=not_found payment_id=%s "
                "actor_id=%s",
                payment_id,
                telegram_id,
            )
            return PaymentCheckResult(PaymentCheckStatus.NOT_FOUND)
        if payment.status == "succeeded":
            return PaymentCheckResult(
                PaymentCheckStatus.ALREADY_SUCCEEDED,
                payment,
                await self.repository.get_lesson_credits(telegram_id),
            )
        if payment.status == "canceled":
            return PaymentCheckResult(PaymentCheckStatus.CANCELED, payment)
        if payment.status == "refund_pending":
            return PaymentCheckResult(PaymentCheckStatus.REFUND_PENDING, payment)

        try:
            provider_payment = await self.gateway.get_payment(
                payment.provider_payment_id
            )
        except PaymentProviderError as error:
            logger.error(
                "Failed to check YooKassa payment payment_id=%s error=%s",
                payment.id,
                error,
            )
            return PaymentCheckResult(PaymentCheckStatus.PROVIDER_UNAVAILABLE, payment)

        if (
            provider_payment.payment_id != payment.provider_payment_id
            or provider_payment.amount_minor != payment.amount_minor
            or provider_payment.currency != "RUB"
            or provider_payment.metadata.get("telegram_id") != str(telegram_id)
            or provider_payment.metadata.get("package_key") != payment.package_key
        ):
            logger.error("YooKassa payment details mismatch payment_id=%s", payment.id)
            return PaymentCheckResult(PaymentCheckStatus.MISMATCH, payment)

        if provider_payment.status == "succeeded":
            completed = await self.repository.complete_lesson_payment(
                payment.id,
                telegram_id,
            )
            if not completed:
                latest_payment = await self.repository.get_lesson_payment(
                    payment.id,
                    telegram_id,
                )
                if latest_payment is not None and latest_payment.status == "succeeded":
                    return PaymentCheckResult(
                        PaymentCheckStatus.ALREADY_PROCESSED,
                        latest_payment,
                        await self.repository.get_lesson_credits(telegram_id),
                    )
                return PaymentCheckResult(
                    PaymentCheckStatus.STATUS_CHANGED,
                    latest_payment or payment,
                )
            logger.info("Lesson payment confirmed payment_id=%s", payment.id)
            return PaymentCheckResult(
                PaymentCheckStatus.CONFIRMED,
                payment,
                await self.repository.get_lesson_credits(telegram_id),
            )
        if provider_payment.status == "canceled":
            canceled = await self.repository.cancel_lesson_payment(
                payment.id,
                telegram_id,
            )
            logger.info(
                "Lesson payment canceled payment_id=%s actor_id=%s updated=%s",
                payment.id,
                telegram_id,
                canceled,
            )
            return PaymentCheckResult(PaymentCheckStatus.PROVIDER_CANCELED, payment)
        return PaymentCheckResult(PaymentCheckStatus.PENDING, payment)

    async def process_refund(
        self,
        payment_id: int,
        admin_id: int,
        reason: Optional[str] = None,
    ) -> RefundResult:
        """Создаёт возврат или сверяет уже начатый; повторный вызов безопасен."""
        try:
            refund = await self.repository.prepare_lesson_refund(
                payment_id,
                admin_id,
                reason or "Административный возврат",
            )
        except ValueError as error:
            return RefundResult(RefundStatus.INVALID, error_message=str(error))
        if refund is None:
            return RefundResult(RefundStatus.UNAVAILABLE)

        try:
            provider_refund = (
                await self.gateway.get_refund(refund.provider_refund_id)
                if refund.provider_refund_id
                else await self.gateway.create_refund(
                    payment_id=refund.provider_payment_id,
                    amount_minor=refund.amount_minor,
                    description=refund.reason,
                    idempotence_key=str(refund.idempotence_key),
                )
            )
        except PaymentProviderError as error:
            logger.error(
                "Refund provider check failed payment_id=%s error_type=%s",
                payment_id,
                type(error).__name__,
            )
            return RefundResult(RefundStatus.PROVIDER_UNAVAILABLE, refund.lessons)

        if (
            provider_refund.payment_id != refund.provider_payment_id
            or provider_refund.amount_minor != refund.amount_minor
            or provider_refund.currency != "RUB"
        ):
            logger.error("Refund details mismatch payment_id=%s", payment_id)
            return RefundResult(RefundStatus.MISMATCH, refund.lessons)

        await self.repository.record_provider_refund(
            payment_id, provider_refund.refund_id
        )
        if provider_refund.status == "succeeded":
            completed = await self.repository.complete_lesson_refund(payment_id)
            if not completed:
                return RefundResult(
                    RefundStatus.ALREADY_PROCESSED,
                    refund.lessons,
                    provider_refund.refund_id,
                    provider_refund.status,
                )
            logger.warning(
                "Admin refund completed payment_id=%s admin_id=%s refund_id=%s",
                payment_id,
                admin_id,
                provider_refund.refund_id,
            )
            return RefundResult(
                RefundStatus.COMPLETED,
                refund.lessons,
                provider_refund.refund_id,
                provider_refund.status,
            )
        if provider_refund.status == "pending":
            return RefundResult(
                RefundStatus.PENDING,
                refund.lessons,
                provider_refund.refund_id,
                provider_refund.status,
            )
        if provider_refund.status == "canceled":
            released = await self.repository.release_lesson_refund(payment_id)
            if released:
                logger.warning(
                    "Provider canceled lesson refund payment_id=%s refund_id=%s",
                    payment_id,
                    provider_refund.refund_id,
                )
                return RefundResult(
                    RefundStatus.RELEASED,
                    refund.lessons,
                    provider_refund.refund_id,
                    provider_refund.status,
                )
        return RefundResult(
            RefundStatus.UNRESOLVED,
            refund.lessons,
            provider_refund.refund_id,
            provider_refund.status,
        )
