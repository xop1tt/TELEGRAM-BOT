"""История занятий и движений баланса для участника и администратора.

Финансовая история — это ledger (``lesson_credit_ledger``), отдельной
таблицы истории нет. Здесь строки ledger лишь получают понятный тип
операции и подпись — одни и те же для бота и сайта.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from ..database.repository import UserBooking
from ..database.studio_models import LedgerEntry


# Тип операции -> подпись. Один источник для бота и /api/history.
OPERATION_LABELS = {
    "purchase": "Покупка абонемента",
    "lesson_use": "Запись на занятие",
    "booking_refund": "Возврат: отмена записи",
    "studio_cancellation": "Возврат: занятие отменено студией",
    "manual_credit": "Начисление студией",
    "manual_debit": "Списание студией",
    "package_grant": "Абонемент от студии",
    "package_revoke": "Отзыв абонемента студией",
    "payment_refund_pending": "Возврат оплаты в обработке",
    "payment_refund": "Возврат оплаты",
    "payment_refund_cancelled": "Возврат оплаты отменён",
    "lesson_debit": "Списание занятия",
    "expiration": "Сгорание: истёк срок абонемента",
    "lesson_credit": "Начисление занятия",
}

# Причину видит участник только у операций, где её пишет администратор
# специально для этого (админ-интерфейс предупреждает об этом).
_USER_VISIBLE_REASON = frozenset(
    {
        "manual_credit",
        "manual_debit",
        "package_grant",
        "package_revoke",
        "studio_cancellation",
        "expiration",
    }
)


def classify_entry(entry: LedgerEntry) -> str:
    entry_type = entry.entry_type
    if entry_type == "lesson_use":
        return "lesson_use" if entry.booking_id is not None else "lesson_debit"
    if entry_type == "adjustment":
        # С миграции 010 'adjustment' — только возврат при отмене записи
        # участником; более старые строки без брони — просто начисление.
        if entry.delta > 0:
            return "booking_refund" if entry.booking_id is not None else "lesson_credit"
        return "lesson_debit"
    if entry_type == "admin_adjustment":
        return "manual_credit" if entry.delta > 0 else "manual_debit"
    if entry_type == "slot_cancellation":
        return "studio_cancellation"
    if entry_type == "refund_reservation":
        return "payment_refund_pending"
    if entry_type == "refund":
        return "payment_refund"
    if entry_type == "refund_release":
        return "payment_refund_cancelled"
    if entry_type in {"purchase", "package_grant", "package_revoke", "expiration"}:
        return entry_type
    return "lesson_credit" if entry.delta > 0 else "lesson_debit"


@dataclass(frozen=True)
class HistoryOperation:
    entry: LedgerEntry
    operation: str
    label: str
    # Причина для участника (None, если её не показывают); для
    # администратора — ``entry.reason``.
    visible_reason: Optional[str]


def describe_entry(entry: LedgerEntry) -> HistoryOperation:
    operation = classify_entry(entry)
    return HistoryOperation(
        entry=entry,
        operation=operation,
        label=OPERATION_LABELS[operation],
        visible_reason=entry.reason if operation in _USER_VISIBLE_REASON else None,
    )


def booking_status(booking: UserBooking, now: Optional[datetime] = None) -> str:
    """Статус записи для истории: upcoming, attended, cancelled_by_user,
    cancelled_by_studio. «Прошло» — прошедшая неотменённая запись: факт
    присутствия бот не отмечает."""
    current = now or datetime.now(timezone.utc)
    if booking.booking_status == "cancelled":
        return (
            "cancelled_by_studio"
            if booking.cancelled_by == "studio"
            else "cancelled_by_user"
        )
    if booking.slot_status == "cancelled":
        return "cancelled_by_studio"
    return "upcoming" if booking.starts_at > current else "attended"


BOOKING_STATUS_LABELS = {
    "upcoming": "Записан",
    "attended": "Занятие прошло",
    "cancelled_by_user": "Отменено вами",
    "cancelled_by_studio": "Отменено студией",
}


class HistoryService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    async def operations(
        self,
        telegram_id: Optional[int],
        limit: int = 10,
        before_id: Optional[int] = None,
    ) -> Sequence[HistoryOperation]:
        entries = await self.repository.list_credit_history(
            telegram_id, limit=limit, before_id=before_id
        )
        return [describe_entry(entry) for entry in entries]

    async def bookings(
        self,
        telegram_id: int,
        limit: int = 10,
        offset: int = 0,
    ) -> Sequence[UserBooking]:
        return await self.repository.list_bookings_for_telegram_id(
            telegram_id, limit=limit, offset=offset
        )
