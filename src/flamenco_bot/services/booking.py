"""Запись на занятия.

Атомарность (вместимость, дубли, параллельные нажатия) обеспечивает
репозиторий: PostgreSQL блокирует строку слота в транзакции. Сервис не
дублирует эти проверки, а добавляет общие для всех интерфейсов побочные
эффекты.
"""

import logging
from typing import Any

from ..class_catalog import CLASS_LABELS
from ..database.repository import ClassBooking
from ..studio_time import format_studio_datetime
from .notifications import AdminNotifier, NotificationReport


logger = logging.getLogger("bot.services.booking")


class BookingService:
    def __init__(self, repository: Any, notifier: AdminNotifier) -> None:
        self.repository = repository
        self.notifier = notifier

    async def book(
        self,
        slot_id: int,
        telegram_id: int,
        notify_user: bool = False,
    ) -> ClassBooking:
        """Подтверждает место и списывает 1 lesson_credit.

        ``SlotUnavailableError`` — слот закрыт/заполнен/в прошлом.
        ``InsufficientLessonCreditsError`` — на балансе нет занятий.
        ``BookingCooldownError`` — слот отменён этим же пользователем
        недавно, повторная запись пока недоступна.

        ``notify_user`` — поставить участнику уведомление о записи (запись
        с сайта); бот показывает результат в том же чате и не дублирует его.
        """
        booking = await self.repository.book_class_slot(
            slot_id, telegram_id, notify_user=notify_user
        )
        logger.info(
            "Class booking confirmed slot_id=%s telegram_id=%s duplicate=%s",
            slot_id,
            telegram_id,
            booking.already_booked,
        )
        return booking

    async def cancel(
        self,
        slot_id: int,
        telegram_id: int,
        notify_user: bool = False,
    ) -> bool:
        """Отменяет запись и возвращает кредит.

        ``False``, если запись уже была отменена ранее (идемпотентно).
        ``BookingNotFoundError``/``CancellationWindowExpiredError`` —
        см. ``PostgresRepository.cancel_class_slot_booking``.
        """
        cancelled = await self.repository.cancel_class_slot_booking(
            slot_id, telegram_id, notify_user=notify_user
        )
        logger.info(
            "Class booking cancel slot_id=%s telegram_id=%s cancelled=%s",
            slot_id,
            telegram_id,
            cancelled,
        )
        return cancelled

    async def admin_book(
        self, slot_id: int, telegram_id: int, admin_telegram_id: int
    ) -> ClassBooking:
        """Запись участника администратором — по тем же правилам, что
        самозапись (открыто, есть место, пауза после отмены, занятие на
        балансе): решение владельца студии. Участник получает уведомление,
        действие — в журнале."""
        booking = await self.repository.book_class_slot(
            slot_id, telegram_id, notify_user=True
        )
        if not booking.already_booked:
            await self.repository.record_staff_action(
                admin_telegram_id,
                "booking_created",
                telegram_id,
                {"slot_id": slot_id},
            )
        return booking

    async def admin_cancel(
        self, slot_id: int, telegram_id: int, admin_telegram_id: int
    ) -> bool:
        """Отмена записи администратором: всегда с возвратом занятия, даже
        позже окна отмены (до начала занятия)."""
        cancelled = await self.repository.cancel_class_slot_booking(
            slot_id,
            telegram_id,
            notify_user=True,
            admin_telegram_id=admin_telegram_id,
        )
        if cancelled:
            await self.repository.record_staff_action(
                admin_telegram_id,
                "booking_cancelled",
                telegram_id,
                {"slot_id": slot_id},
            )
        return cancelled

    async def join_waitlist(self, slot_id: int, telegram_id: int) -> Any:
        return await self.repository.join_waitlist(slot_id, telegram_id)

    async def leave_waitlist(self, slot_id: int, telegram_id: int) -> bool:
        return await self.repository.leave_waitlist(slot_id, telegram_id)

    async def notify_admins_about_booking(
        self,
        booking: ClassBooking,
        participant_name: str,
    ) -> NotificationReport:
        report = await self.notifier.notify(
            "Новая запись №{}: {} — {}, участник {} (ID {}).".format(
                booking.id,
                CLASS_LABELS[booking.class_key],
                format_studio_datetime(booking.starts_at),
                participant_name,
                booking.telegram_id,
            ),
            event="class_booking",
        )
        if not report.complete:
            logger.error(
                "Class booking confirmed without notifying all admins "
                "booking_id=%s recipients=%s delivered=%s",
                booking.id,
                report.recipients,
                report.delivered,
            )
        return report
