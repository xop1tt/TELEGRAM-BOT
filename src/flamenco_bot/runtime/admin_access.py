import logging
from typing import Any, Optional

from aiogram.types import Message


logger = logging.getLogger("bot.admin_access")


async def get_admin_id(message: Message, repository: Any) -> Optional[int]:
    sender = message.from_user
    if sender is None or message.chat.type != "private":
        logger.debug(
            "Admin access denied reason=%s telegram_id=%s",
            "missing_sender" if sender is None else "non_private_chat",
            sender.id if sender else None,
        )
        return None

    profile = await repository.get_profile(sender.id)
    if profile is None or profile.telegram_id != sender.id or not profile.is_admin:
        logger.debug(
            "Admin access denied reason=%s telegram_id=%s",
            "not_registered_or_not_admin",
            sender.id,
        )
        return None
    logger.debug("Admin access granted telegram_id=%s", sender.id)
    return sender.id


async def get_staff_role(message: Message, repository: Any) -> Optional[str]:
    """Роль отправителя (admin/teacher) в личном чате, иначе ``None``."""
    sender = message.from_user
    if sender is None or message.chat.type != "private":
        return None
    profile = await repository.get_profile(sender.id)
    if profile is None or profile.telegram_id != sender.id:
        return None
    return profile.role


async def get_staff_id(
    message: Message, repository: Any, slot_id: Optional[int] = None
) -> Optional[int]:
    """Telegram ID сотрудника (администратор или преподаватель).

    С ``slot_id`` преподаватель проходит только для своего занятия.
    """
    role = await get_staff_role(message, repository)
    if role is None:
        return None
    sender_id = message.from_user.id
    if (
        slot_id is not None
        and role != "admin"
        and not await repository.is_slot_teacher(slot_id, sender_id)
    ):
        return None
    return sender_id
