import logging
from typing import Any

from aiogram import Router
from aiogram.types import Message

from ..runtime.admin_access import get_staff_role
from ..keyboards.user import main_menu_keyboard


logger = logging.getLogger("bot.handlers.fallback")
router = Router(name="fallback")


@router.message()
async def fallback_message(message: Message, repository: Any) -> None:
    await message.answer(
        "Не понял сообщение. Выберите раздел в меню ниже или отправьте /help.\n"
        "Чтобы написать в студию, нажмите «💬 Поддержка».",
        reply_markup=main_menu_keyboard(role=await get_staff_role(message, repository)),
    )
    logger.info(
        "Handled fallback telegram_id=%s",
        message.from_user.id if message.from_user else None,
    )
