"""Обращения пользователей в поддержку."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from aiogram.exceptions import TelegramAPIError

from ..database.repository import MAX_SUPPORT_MESSAGE_LENGTH
from ..database.studio_models import SupportTicketThread
from ..runtime.security import SupportRateLimiter
from .notifications import AdminNotifier, NotificationReport


logger = logging.getLogger("bot.services.support")


class SupportMessageInvalidError(ValueError):
    pass


class SupportRateLimitedError(RuntimeError):
    pass


@dataclass(frozen=True)
class SupportSubmission:
    ticket_id: int
    created: bool
    notifications: NotificationReport


class SupportService:
    def __init__(
        self,
        repository: Any,
        limiter: SupportRateLimiter,
        notifier: AdminNotifier,
    ) -> None:
        self.repository = repository
        self.limiter = limiter
        self.notifier = notifier

    async def submit_message(
        self,
        telegram_id: int,
        user_name: str,
        body: str,
    ) -> SupportSubmission:
        body = body.strip()
        if not body or len(body) > MAX_SUPPORT_MESSAGE_LENGTH:
            raise SupportMessageInvalidError(
                "Сообщение должно содержать от 1 до {} символов.".format(
                    MAX_SUPPORT_MESSAGE_LENGTH
                )
            )
        if not self.limiter.allow(telegram_id):
            logger.warning("Support rate limit exceeded telegram_id=%s", telegram_id)
            raise SupportRateLimitedError("Слишком много сообщений")

        await self.repository.get_or_create_profile(
            telegram_id=telegram_id,
            user_name=user_name,
        )
        ticket_id, created = await self.repository.create_support_message(
            telegram_id, body
        )
        report = await self.notifier.notify(
            "Обращение №{} от {} ({}):\n{}".format(
                ticket_id,
                user_name,
                telegram_id,
                body,
            ),
            event="support_ticket",
        )
        if not report.complete:
            logger.error(
                "Support ticket saved without notifying all admins ticket_id=%s "
                "recipients=%s delivered=%s",
                ticket_id,
                report.recipients,
                report.delivered,
            )
        logger.info(
            "Support message saved ticket_id=%s telegram_id=%s new_ticket=%s "
            "admin_notifications=%s",
            ticket_id,
            telegram_id,
            created,
            report.delivered,
        )
        return SupportSubmission(ticket_id, created, report)

    async def thread(
        self,
        ticket_id: int,
        telegram_id: Optional[int] = None,
    ) -> Optional[SupportTicketThread]:
        """Обращение с перепиской; ``telegram_id`` — только своё обращение."""
        return await self.repository.get_support_ticket_thread(ticket_id, telegram_id)

    async def reopen(self, ticket_id: int, admin_telegram_id: int) -> Optional[int]:
        """``SupportTicketStateError`` — уже открыто или у участника есть
        другое открытое обращение."""
        user_id = await self.repository.reopen_support_ticket(
            ticket_id, admin_telegram_id
        )
        if user_id is not None:
            logger.info(
                "Support ticket reopened ticket_id=%s admin_id=%s",
                ticket_id,
                admin_telegram_id,
            )
        return user_id


class SupportReplyStatus(Enum):
    DELIVERED = "delivered"
    # Ответ сохранён в обращении, но Telegram не доставил его участнику
    # (например, участник заблокировал бота).
    SAVED_NOT_DELIVERED = "saved_not_delivered"
    NOT_FOUND = "not_found"


async def deliver_support_reply(
    bot: Any,
    repository: Any,
    ticket_id: int,
    admin_id: int,
    body: str,
    reply_markup: Any = None,
) -> SupportReplyStatus:
    """Сохраняет ответ администратора и отправляет его участнику в Telegram.

    Общий шаг для бота (/support_reply, админ-панель) и админки сайта.
    """
    user_id = await repository.reply_support_ticket(ticket_id, admin_id, body)
    if user_id is None:
        return SupportReplyStatus.NOT_FOUND
    try:
        await bot.send_message(
            user_id,
            "Ответ службы поддержки по обращению №{}:\n{}".format(ticket_id, body),
            reply_markup=reply_markup,
        )
    except TelegramAPIError as error:
        logger.exception(
            "Support reply delivery failed ticket_id=%s user_id=%s error_type=%s",
            ticket_id,
            user_id,
            type(error).__name__,
        )
        return SupportReplyStatus.SAVED_NOT_DELIVERED
    logger.info(
        "Support reply delivered ticket_id=%s admin_id=%s user_id=%s",
        ticket_id,
        admin_id,
        user_id,
    )
    return SupportReplyStatus.DELIVERED


async def close_ticket_and_notify(
    bot: Any, repository: Any, ticket_id: int, admin_id: int
) -> bool:
    """Закрывает обращение и сообщает участнику; ``False`` — не найдено."""
    user_id = await repository.close_support_ticket(ticket_id, admin_id)
    if user_id is None:
        return False
    try:
        await bot.send_message(
            user_id,
            "Обращение №{} закрыто службой поддержки.".format(ticket_id),
        )
    except TelegramAPIError as error:
        logger.warning(
            "Support close notification failed ticket_id=%s user_id=%s error_type=%s",
            ticket_id,
            user_id,
            type(error).__name__,
        )
    logger.info(
        "Support ticket closed ticket_id=%s admin_id=%s user_id=%s",
        ticket_id,
        admin_id,
        user_id,
    )
    return True
