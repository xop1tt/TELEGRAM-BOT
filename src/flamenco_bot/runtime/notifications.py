"""Фоновая отправка уведомлений участникам из outbox ``user_notifications``.

Уведомление попадает в outbox в той же транзакции, что и событие (запись,
отмена, перенос, возврат), — отправка выполняется здесь, уже после commit.
Сбой Telegram не откатывает бизнес-операцию, а повторная отправка не
повторяет финансовую операцию: повторяется только сообщение.

Задачу запускают и бот, и веб-API (у обоих есть объект Bot), как и сверку
платежей: итерацию выполняет тот, кто взял advisory-блокировку
``NOTIFICATION_LOCK_ID``. Дублей не будет и без блокировки — строки
забираются ``FOR UPDATE SKIP LOCKED`` со статусом 'sending' и сроком аренды.
Доставка «как минимум один раз»: если процесс упадёт между отправкой и
отметкой, сообщение после срока аренды уйдёт повторно — это допустимо для
уведомления и не трогает баланс.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..database.studio_models import UserNotification
from ..studio_rules import rules
from ..notification_texts import NotificationMessage, render_notification


# Рядом с MIGRATION_LOCK_ID (715_203_401) и RECONCILIATION_LOCK_ID (…402).
NOTIFICATION_LOCK_ID = 715_203_403
DISPATCH_INTERVAL_SECONDS = 10.0
REMINDER_SCAN_INTERVAL_SECONDS = 300.0
BATCH_SIZE = 20
MAX_ATTEMPTS = 6
MAX_BACKOFF = timedelta(hours=1)


def notification_markup(
    message: NotificationMessage,
) -> Optional[InlineKeyboardMarkup]:
    if not message.actions:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=data)
                for label, data in message.actions
            ]
        ]
    )


def retry_delay(attempts: int) -> timedelta:
    """30 с, 1 мин, 2 мин, … — не больше часа."""
    return min(timedelta(seconds=30 * 2 ** max(0, attempts - 1)), MAX_BACKOFF)


async def _is_outdated(repository: Any, notification: UserNotification) -> bool:
    """Напоминание о занятии, которое перенесли, отменили или отменили запись;
    уведомление об обслуживании, которое уже не соответствует его состоянию
    (например, предупреждение о перенесённых или отменённых работах)."""
    if notification.kind.startswith("maintenance_"):
        return not await repository.is_maintenance_notification_current(
            notification.kind, notification.payload
        )
    if notification.kind != "lesson_reminder":
        return False
    payload = notification.payload
    try:
        starts_at = datetime.fromisoformat(str(payload["starts_at"]))
        booking_id = int(payload["booking_id"])
    except (KeyError, TypeError, ValueError):
        return True
    return not await repository.is_reminder_current(booking_id, starts_at)


async def deliver_notification(
    repository: Any,
    bot: Any,
    notification: UserNotification,
    logger: logging.Logger,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> str:
    """Отправляет одно уведомление и сохраняет итог; возвращает статус."""
    if await _is_outdated(repository, notification):
        await repository.mark_notification_finished(
            notification.id, "skipped", "outdated"
        )
        return "skipped"
    message = render_notification(notification.kind, notification.payload)
    try:
        await bot.send_message(
            notification.telegram_id,
            message.text,
            reply_markup=notification_markup(message),
        )
    except TelegramRetryAfter as error:
        await repository.mark_notification_retry(
            notification.id,
            "retry_after",
            clock() + timedelta(seconds=max(1, int(error.retry_after))),
        )
        return "retry"
    except (
        TelegramForbiddenError,
        TelegramBadRequest,
        TelegramNotFound,
        TelegramUnauthorizedError,
    ) as error:
        # Пользователь заблокировал бота, чат не найден, сообщение
        # отклонено — повтор не поможет.
        logger.warning(
            "Notification not deliverable notification_id=%s telegram_id=%s "
            "error_type=%s",
            notification.id,
            notification.telegram_id,
            type(error).__name__,
        )
        await repository.mark_notification_finished(
            notification.id, "failed", type(error).__name__
        )
        return "failed"
    except (TelegramAPIError, OSError, asyncio.TimeoutError) as error:
        if notification.attempts >= MAX_ATTEMPTS:
            logger.error(
                "Notification delivery gave up notification_id=%s attempts=%s",
                notification.id,
                notification.attempts,
            )
            await repository.mark_notification_finished(
                notification.id, "failed", type(error).__name__
            )
            return "failed"
        await repository.mark_notification_retry(
            notification.id,
            type(error).__name__,
            clock() + retry_delay(notification.attempts),
        )
        return "retry"
    await repository.mark_notification_sent(notification.id)
    return "sent"


async def dispatch_due_notifications(
    repository: Any,
    bot: Any,
    logger: logging.Logger,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Одна пачка уведомлений; возвращает число отправленных."""
    sent = 0
    for notification in await repository.claim_due_notifications(limit=batch_size):
        try:
            status = await deliver_notification(repository, bot, notification, logger)
        except Exception:
            # Например, БД недоступна при отметке результата: уведомление
            # вернётся в очередь по истечении аренды.
            logger.exception(
                "Notification delivery failed notification_id=%s", notification.id
            )
            continue
        sent += status == "sent"
    if sent:
        logger.info("Notifications delivered count=%s", sent)
    return sent


async def run_notification_worker(
    repository: Any,
    bot: Any,
    logger: logging.Logger,
    reminder_lead: Optional[timedelta],
    interval_seconds: float = DISPATCH_INTERVAL_SECONDS,
    reminder_interval_seconds: float = REMINDER_SCAN_INTERVAL_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = lambda: asyncio.get_running_loop().time(),
) -> None:
    if interval_seconds <= 0 or reminder_interval_seconds <= 0:
        raise ValueError("Интервалы отправки уведомлений должны быть положительными")
    last_reminder_scan: Optional[float] = None
    while True:
        await sleep(interval_seconds)
        try:
            async with repository.try_advisory_lock(NOTIFICATION_LOCK_ID) as locked:
                if not locked:
                    continue
                now = monotonic()
                if reminder_lead is not None and (
                    last_reminder_scan is None
                    or now - last_reminder_scan >= reminder_interval_seconds
                ):
                    created = await repository.enqueue_due_reminders(reminder_lead)
                    if created:
                        logger.info("Lesson reminders queued count=%s", created)
                if last_reminder_scan is None or (
                    now - last_reminder_scan >= reminder_interval_seconds
                ):
                    last_reminder_scan = now
                    try:
                        await process_credit_expiry(repository, logger)
                    except Exception:
                        # Сбой сгорания не должен останавливать отправку.
                        logger.exception("Credit expiry processing failed")
                try:
                    await process_maintenance(repository, logger)
                except Exception:
                    # Сбой постановки уведомлений об обслуживании не должен
                    # останавливать остальную отправку (а включение
                    # обслуживания от неё и так не зависит — см.
                    # maintenance.py).
                    logger.exception("Maintenance processing failed")
                await dispatch_due_notifications(repository, bot, logger)
        except Exception:
            logger.exception("Notification worker iteration failed")


async def process_maintenance(repository: Any, logger: logging.Logger) -> dict:
    """Автостарт обслуживания и постановка недостающих уведомлений о нём.

    Каждую итерацию (раз в несколько секунд): предупреждения за 10 минут и
    начало работ не должны ждать пятиминутного цикла напоминаний. Шаг
    идемпотентен — повтор после сбоя или рестарта досылает только
    недостающее (``site_maintenance_dispatches``).
    """
    summary = await repository.process_maintenance()
    if summary["started"] or summary["queued"] or summary["skipped"]:
        logger.info(
            "Maintenance processed started=%s queued=%s skipped_late=%s",
            summary["started"],
            summary["queued"],
            summary["skipped"],
        )
    return summary


async def process_credit_expiry(repository: Any, logger: logging.Logger) -> None:
    """Сгорание истёкших абонементов и предупреждения о скором сгорании.

    Тем же циклом, что напоминания (раз в несколько минут): уведомления
    ставятся в outbox в транзакции сгорания и уходят обычной отправкой.
    """
    burned = await repository.expire_due_lots()
    if burned:
        logger.info("Expired lesson credits burned lessons=%s", burned)
    warned = await repository.enqueue_expiry_warnings(rules().expiry_warning_days)
    if warned:
        logger.info("Package expiry warnings queued count=%s", warned)


def start_notification_worker(
    repository: Any,
    bot: Any,
    logger: logging.Logger,
    reminder_hours: int,
) -> "asyncio.Task[None]":
    lead = timedelta(hours=reminder_hours) if reminder_hours > 0 else None
    return asyncio.create_task(run_notification_worker(repository, bot, logger, lead))
