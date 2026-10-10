"""Админка сайта: карточка участника и финансы — ``/api/admin/*``.

То же, что карточка клиента и «💰 Финансы» в админке бота: профиль,
абонементы, занятия, покупки, операции, обращения; выдача и отзыв
абонемента, корректировка баланса, правка имени и телефона; возвраты
оплаты, сверка платежа с ЮKassa, журнал операций, журнал действий и сверка
баланса с ledger. Только администратору (``require_admin``); логика — в тех
же сервисах, что у бота, здесь только HTTP-обёртка.
"""

import logging
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from ...config import Config
from ...database.repository import (
    BookingCooldownError,
    BookingNotFoundError,
    CancellationWindowExpiredError,
    InsufficientLessonCreditsError,
    SlotUnavailableError,
)
from ...database.studio_models import PackageGrantNotFoundError
from ...services import (
    AdminNotifier,
    BookingService,
    CreditService,
    HistoryService,
    InvalidUserNameError,
    PackageService,
    PaymentCheckStatus,
    PaymentService,
    ProfileService,
    RefundStatus,
    normalize_user_name,
)
from ...services.audit import audit_label
from ...services.profile import normalize_phone
from ..dependencies import get_repository, require_admin, require_admin_actor
from ..schemas import (
    MAX_DB_ID,
    AdminAuditEventResponse,
    AdminAuditPage,
    AdminClientCardResponse,
    AdminClientProfileRequest,
    AdminCreditAdjustmentRequest,
    AdminGrantRequest,
    AdminLedgerOperationResponse,
    AdminLedgerPage,
    AdminOperationResultResponse,
    AdminPaymentResponse,
    AdminPaymentsPage,
    AdminReconciliationResponse,
    AdminRefundRequest,
    AdminRevokeRequest,
    BookingHistoryPage,
    BookingRequest,
    BookingResponse,
    PackageSummaryResponse,
    ProfileResponse,
    SupportTicketResponse,
    UserBookingResponse,
)


logger = logging.getLogger("bot.api.admin")
router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

PAYMENT_STATUSES = "^(pending|succeeded|canceled|refund_pending|refunded)$"


def _db_id() -> Any:
    return Path(gt=0, le=MAX_DB_ID)


def _admin_payment_service(repository: Any, request_state: Any) -> PaymentService:
    # Возврат и сверка не возвращают пользователя на сайт: шлюз сверки (с
    # return_url бота) работает и без WEB_YOOKASSA_RETURN_URL.
    gateway = getattr(request_state, "admin_payment_gateway", None) or getattr(
        request_state, "payment_gateway"
    )
    return PaymentService(repository, gateway)


async def _profile_or_404(repository: Any, telegram_id: int) -> Any:
    profile = await repository.get_profile(telegram_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Участник не найден")
    return profile


# ---------- карточка участника ----------


@router.get("/clients/{telegram_id}", response_model=AdminClientCardResponse)
async def client_card(
    telegram_id: int = _db_id(),
    repository: Any = Depends(get_repository),
) -> AdminClientCardResponse:
    try:
        overview = await ProfileService(repository).overview(
            telegram_id, upcoming_limit=10
        )
    except LookupError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Участник не найден") from error
    web_user = await repository.get_web_user_by_telegram_id(telegram_id)
    return AdminClientCardResponse(
        profile=ProfileResponse.from_record(overview.profile),
        role=overview.profile.role,
        email=web_user.email if web_user else None,
        has_web_account=web_user is not None,
        packages=PackageSummaryResponse.from_summary(overview.packages),
        upcoming=[UserBookingResponse.from_record(b) for b in overview.upcoming],
    )


@router.get("/clients/{telegram_id}/bookings", response_model=BookingHistoryPage)
async def client_bookings(
    telegram_id: int = _db_id(),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=10_000),
    repository: Any = Depends(get_repository),
) -> BookingHistoryPage:
    await _profile_or_404(repository, telegram_id)
    bookings = list(
        await HistoryService(repository).bookings(
            telegram_id, limit=limit + 1, offset=offset
        )
    )
    has_more = len(bookings) > limit
    return BookingHistoryPage(
        items=[UserBookingResponse.from_record(item) for item in bookings[:limit]],
        next_offset=offset + limit if has_more else None,
    )


@router.get(
    "/clients/{telegram_id}/tickets", response_model=List[SupportTicketResponse]
)
async def client_tickets(
    telegram_id: int = _db_id(),
    repository: Any = Depends(get_repository),
) -> List[SupportTicketResponse]:
    await _profile_or_404(repository, telegram_id)
    tickets = await repository.list_support_tickets_for_telegram_id(telegram_id)
    return [SupportTicketResponse.from_record(ticket) for ticket in tickets]


@router.patch("/clients/{telegram_id}/profile", response_model=ProfileResponse)
async def update_client_profile(
    payload: AdminClientProfileRequest,
    telegram_id: int = _db_id(),
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> ProfileResponse:
    try:
        user_name = (
            normalize_user_name(payload.user_name)
            if payload.user_name is not None
            else None
        )
        phone = normalize_phone(payload.phone) if payload.phone is not None else None
    except (InvalidUserNameError, ValueError) as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from error
    if user_name is None and phone is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Нечего менять")
    if not await repository.admin_update_profile(
        telegram_id, admin_id, user_name=user_name, phone=phone
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Участник не найден")
    return ProfileResponse.from_record(await _profile_or_404(repository, telegram_id))


# ---------- абонементы и баланс ----------


@router.post(
    "/clients/{telegram_id}/grants", response_model=AdminOperationResultResponse
)
async def grant_package(
    payload: AdminGrantRequest,
    telegram_id: int = _db_id(),
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> AdminOperationResultResponse:
    """Выдача абонемента из каталога без оплаты; повтор с тем же
    ``idempotence_key`` ничего не начисляет."""
    try:
        result = await PackageService(repository).grant(
            telegram_id,
            payload.package_key,
            payload.reason,
            admin_id,
            payload.idempotence_key,
        )
    except LookupError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except (ValueError, PermissionError) as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from error
    return AdminOperationResultResponse(applied=result.applied, balance=result.balance)


@router.post("/grants/{grant_id}/revoke", response_model=AdminOperationResultResponse)
async def revoke_grant(
    payload: AdminRevokeRequest,
    grant_id: int = _db_id(),
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> AdminOperationResultResponse:
    """Списываются только неиспользованные занятия выдачи."""
    try:
        result = await PackageService(repository).revoke(
            grant_id, payload.reason, admin_id
        )
    except PackageGrantNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except (ValueError, PermissionError) as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from error
    return AdminOperationResultResponse(
        applied=result.applied,
        balance=result.balance,
        lessons=result.revoked_lessons,
    )


@router.post(
    "/clients/{telegram_id}/credits", response_model=AdminOperationResultResponse
)
async def adjust_credits(
    payload: AdminCreditAdjustmentRequest,
    telegram_id: int = _db_id(),
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> AdminOperationResultResponse:
    """Ручная корректировка: начисление — партия со сроком ручных
    начислений, списание не уводит баланс ниже нуля; повтор с тем же
    ключом идемпотентен. Участник получает уведомление с причиной."""
    service = CreditService(repository, Config.CREDIT_ADJUSTMENT_MAX_DELTA)
    try:
        result = await service.adjust(
            telegram_id,
            payload.delta,
            payload.reason,
            admin_id,
            payload.idempotence_key,
        )
    except LookupError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except InsufficientLessonCreditsError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except (ValueError, PermissionError) as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from error
    return AdminOperationResultResponse(applied=result.applied, balance=result.balance)


# ---------- запись за участника ----------


@router.post(
    "/clients/{telegram_id}/bookings",
    response_model=BookingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def book_for_client(
    payload: BookingRequest,
    request: Request,
    telegram_id: int = _db_id(),
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> BookingResponse:
    """Запись участника администратором — по тем же правилам, что
    самозапись; с баланса списывается занятие, участник получает
    уведомление."""
    await _profile_or_404(repository, telegram_id)
    service = BookingService(
        repository, AdminNotifier(request.app.state.bot, repository)
    )
    try:
        booking = await service.admin_book(payload.slot_id, telegram_id, admin_id)
    except (
        SlotUnavailableError,
        InsufficientLessonCreditsError,
        BookingCooldownError,
    ) as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return BookingResponse.from_booking(booking)


@router.post(
    "/clients/{telegram_id}/bookings/{slot_id}/cancel",
    response_model=AdminOperationResultResponse,
)
async def cancel_for_client(
    request: Request,
    telegram_id: int = _db_id(),
    slot_id: int = _db_id(),
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> AdminOperationResultResponse:
    """Отмена записи администратором: занятие возвращается всегда, даже
    позже окна отмены (но до начала занятия)."""
    service = BookingService(
        repository, AdminNotifier(request.app.state.bot, repository)
    )
    try:
        cancelled = await service.admin_cancel(slot_id, telegram_id, admin_id)
    except BookingNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except CancellationWindowExpiredError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return AdminOperationResultResponse(
        applied=cancelled,
        balance=await repository.get_lesson_credits(telegram_id),
        message="Запись отменена, занятие вернулось на баланс"
        if cancelled
        else "Запись уже была отменена",
    )


# ---------- платежи ----------


def _payment_response(payment: Any) -> AdminPaymentResponse:
    return AdminPaymentResponse.from_record(payment)


@router.get("/payments", response_model=AdminPaymentsPage)
async def list_payments(
    payment_status: Optional[str] = Query(
        default=None, alias="status", pattern=PAYMENT_STATUSES
    ),
    telegram_id: Optional[int] = Query(default=None, gt=0, le=MAX_DB_ID),
    before_id: Optional[int] = Query(default=None, gt=0, le=MAX_DB_ID),
    limit: int = Query(default=50, ge=1, le=200),
    repository: Any = Depends(get_repository),
) -> AdminPaymentsPage:
    payments = list(
        await repository.list_admin_payments(
            limit + 1,
            status=payment_status,
            telegram_id=telegram_id,
            before_id=before_id,
        )
    )
    has_more = len(payments) > limit
    payments = payments[:limit]
    return AdminPaymentsPage(
        items=[_payment_response(payment) for payment in payments],
        next_before_id=payments[-1].id if has_more and payments else None,
    )


async def _payment_or_404(repository: Any, payment_id: int) -> Any:
    payment = await repository.get_admin_payment(payment_id)
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Платёж не найден")
    return payment


REFUND_MESSAGES = {
    RefundStatus.UNAVAILABLE: "Возврат недоступен: платёж не в статусе успешной оплаты",
    RefundStatus.PROVIDER_UNAVAILABLE: (
        "ЮKassa не подтвердила результат. Занятия зарезервированы — повторите "
        "сверку возврата позже"
    ),
    RefundStatus.MISMATCH: (
        "Данные возврата не совпали с ЮKassa. Занятия зарезервированы — нужна "
        "ручная сверка"
    ),
    RefundStatus.COMPLETED: "Возврат подтверждён, неиспользованные занятия списаны",
    RefundStatus.ALREADY_PROCESSED: "Возврат уже был обработан",
    RefundStatus.PENDING: "ЮKassa приняла возврат, он обрабатывается — сверьте позже",
    RefundStatus.RELEASED: "ЮKassa отменила возврат, занятия вернулись на баланс",
    RefundStatus.UNRESOLVED: (
        "ЮKassa вернула неожиданный статус. Занятия зарезервированы — нужна сверка"
    ),
}


async def _refund(
    request: Request, repository: Any, payment_id: int, admin_id: int, reason: str
) -> AdminOperationResultResponse:
    await _payment_or_404(repository, payment_id)
    service = _admin_payment_service(repository, request.app.state)
    result = await service.process_refund(payment_id, admin_id, reason)
    if result.status is RefundStatus.INVALID:
        raise HTTPException(status.HTTP_409_CONFLICT, result.error_message)
    if result.status is RefundStatus.UNAVAILABLE:
        raise HTTPException(status.HTTP_409_CONFLICT, REFUND_MESSAGES[result.status])
    logger.warning(
        "Admin refund via site payment_id=%s admin_id=%s status=%s",
        payment_id,
        admin_id,
        result.status.value,
    )
    return AdminOperationResultResponse(
        applied=result.status is RefundStatus.COMPLETED,
        lessons=result.lessons,
        status=result.status.value,
        message=REFUND_MESSAGES.get(result.status, result.status.value),
    )


@router.post(
    "/payments/{payment_id}/refund", response_model=AdminOperationResultResponse
)
async def refund_payment(
    payload: AdminRefundRequest,
    request: Request,
    payment_id: int = _db_id(),
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> AdminOperationResultResponse:
    """Возврат успешной оплаты через ЮKassa — только если все занятия этого
    платежа ещё не использованы; повтор безопасен (сверяет начатый)."""
    return await _refund(request, repository, payment_id, admin_id, payload.reason)


@router.post(
    "/payments/{payment_id}/refund-check",
    response_model=AdminOperationResultResponse,
)
async def check_refund(
    request: Request,
    payment_id: int = _db_id(),
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> AdminOperationResultResponse:
    """Повторная сверка начатого возврата с ЮKassa (то же, что /refund_check)."""
    payment = await _payment_or_404(repository, payment_id)
    if payment.status != "refund_pending":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "У этого платежа нет возврата в обработке"
        )
    return await _refund(
        request, repository, payment_id, admin_id, payment.refund_reason or ""
    )


@router.post(
    "/payments/{payment_id}/check", response_model=AdminOperationResultResponse
)
async def check_payment(
    request: Request,
    payment_id: int = _db_id(),
    repository: Any = Depends(get_repository),
) -> AdminOperationResultResponse:
    """Сверка ожидающего платежа с ЮKassa: если он оплачен, занятия
    начисляются (ровно один раз — как у фоновой сверки)."""
    payment = await _payment_or_404(repository, payment_id)
    service = _admin_payment_service(repository, request.app.state)
    result = await service.check_payment(payment_id, payment.telegram_id)
    messages = {
        PaymentCheckStatus.CONFIRMED: "Оплата подтверждена, занятия начислены",
        PaymentCheckStatus.ALREADY_SUCCEEDED: "Платёж уже был оплачен",
        PaymentCheckStatus.ALREADY_PROCESSED: "Платёж уже обработан",
        PaymentCheckStatus.PENDING: "ЮKassa: оплата ещё не завершена",
        PaymentCheckStatus.PROVIDER_CANCELED: "ЮKassa: платёж отменён",
        PaymentCheckStatus.CANCELED: "Платёж отменён",
        PaymentCheckStatus.REFUND_PENDING: "По платежу идёт возврат",
        PaymentCheckStatus.PROVIDER_UNAVAILABLE: "ЮKassa недоступна, повторите позже",
        PaymentCheckStatus.MISMATCH: "Данные платежа не совпали с ЮKassa",
        PaymentCheckStatus.STATUS_CHANGED: "Статус платежа изменился",
    }
    return AdminOperationResultResponse(
        applied=result.status is PaymentCheckStatus.CONFIRMED,
        status=result.status.value,
        message=messages.get(result.status, "Статус: {}".format(result.status.value)),
    )


@router.get("/ledger", response_model=AdminLedgerPage)
async def ledger(
    telegram_id: Optional[int] = Query(default=None, gt=0, le=MAX_DB_ID),
    before_id: Optional[int] = Query(default=None, gt=0, le=MAX_DB_ID),
    limit: int = Query(default=30, ge=1, le=100),
    repository: Any = Depends(get_repository),
) -> AdminLedgerPage:
    operations = list(
        await HistoryService(repository).operations(
            telegram_id, limit=limit + 1, before_id=before_id
        )
    )
    has_more = len(operations) > limit
    operations = operations[:limit]
    return AdminLedgerPage(
        items=[AdminLedgerOperationResponse.from_operation(op) for op in operations],
        next_before_id=operations[-1].entry.id if has_more and operations else None,
    )


@router.get("/audit", response_model=AdminAuditPage)
async def audit(
    before: Optional[datetime] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    repository: Any = Depends(get_repository),
) -> AdminAuditPage:
    events = list(await repository.list_audit_events(limit=limit + 1, before=before))
    has_more = len(events) > limit
    events = events[:limit]
    return AdminAuditPage(
        items=[
            AdminAuditEventResponse(**vars(event), label=audit_label(event.action))
            for event in events
        ],
        next_before=events[-1].created_at if has_more and events else None,
    )


@router.get("/reconciliation", response_model=AdminReconciliationResponse)
async def reconciliation(
    repository: Any = Depends(get_repository),
) -> AdminReconciliationResponse:
    """Сверка баланса с суммой ledger: показывает расхождения, ничего не чинит."""
    result = await CreditService(
        repository, Config.CREDIT_ADJUSTMENT_MAX_DELTA
    ).reconcile(50)
    return AdminReconciliationResponse.from_result(result)


__all__ = ["router"]
