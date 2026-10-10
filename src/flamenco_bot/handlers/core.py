import logging
from typing import Any

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ..runtime.admin_access import get_admin_id
from ..keyboards.user import main_menu_keyboard
from ..keyboards.user.account import open_profile
from ..keyboards.user.main_menu import (
    account_summary,
    ensure_profile,
    open_booking,
    open_my_classes,
    open_packages,
    open_purchases,
    show_about,
)
from ..keyboards.user.navigation import cancel_current_action


logger = logging.getLogger("bot.handlers.core")
router = Router(name="core_commands")

CLIENT_HELP = (
    "Как пользоваться ботом\n\n"
    "📅 Расписание — ближайшие занятия, выбор времени и запись\n"
    "💃 Мои занятия — ваши записи, отмена и история\n"
    "🎟 Абонементы — остатки занятий по абонементам\n"
    "💳 Покупки — купить абонемент, счета и история покупок\n"
    "👤 Профиль — данные, баланс, история операций, уведомления\n"
    "💬 Поддержка — написать в студию\n\n"
    "/start — главное меню, /about — о студии и ценах, /cancel — отменить ввод."
)

ADMIN_HELP = (
    "Администратору — всё в «🛠 Админ-меню»: расписание (создать, перенести, "
    "отменить, открыть/закрыть, участники), клиенты (профиль, абонементы, "
    "выдача, корректировка), поддержка, финансы.\n\n"
    "Команды:\n"
    "/admin — админ-меню\n"
    "/slots — расписание занятий\n"
    "/slot ID — карточка занятия\n"
    "/client ID — карточка участника\n"
    "/support_tickets — обращения поддержки\n"
    "/credits_audit — сверка баланса с ledger\n"
    "/ledger [ID] — последние операции с балансом\n"
    "/audit — журнал действий\n"
    "/refund ID причина, /refund_check ID — возврат оплаты"
)


@router.message(CommandStart())
async def start_command(
    message: Message,
    repository: Any,
    state: FSMContext,
) -> None:
    await state.clear()
    profile = await ensure_profile(message, repository)
    summary = await account_summary(repository, profile.telegram_id)
    await message.answer(
        "¡Hola, {}! Это бот студии фламенко Mirada Studio.\n"
        "Здесь можно записаться на занятие, купить абонемент и посмотреть "
        "свои записи. Направления, цены и правила — /about.\n\n"
        "{}".format(profile.user_name, summary),
        reply_markup=main_menu_keyboard(is_admin=profile.is_admin),
    )
    logger.info("Handled /start telegram_id=%s", profile.telegram_id)


@router.message(Command("help"))
async def help_command(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    await state.clear()
    is_admin = await get_admin_id(message, repository) is not None
    await message.answer(
        CLIENT_HELP + ("\n\n" + ADMIN_HELP if is_admin else ""),
        reply_markup=main_menu_keyboard(is_admin=is_admin),
    )
    sender = message.from_user
    logger.info("Handled /help telegram_id=%s", sender.id if sender else None)


@router.message(Command("account"))
async def account_command(
    message: Message,
    repository: Any,
    state: FSMContext,
) -> None:
    await open_profile(message, state, repository)


@router.message(Command("lessons"))
async def lessons_command(
    message: Message,
    repository: Any,
    state: FSMContext,
) -> None:
    await open_my_classes(message, state, repository)


@router.message(Command("schedule"))
async def schedule_command(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    await open_booking(message, state, repository)


@router.message(Command("buy"))
async def buy_command(
    message: Message,
    repository: Any,
    state: FSMContext,
) -> None:
    await open_purchases(message, state, repository)


@router.message(Command("packages"))
async def packages_command(
    message: Message,
    repository: Any,
    state: FSMContext,
) -> None:
    await open_packages(message, state, repository)


@router.message(Command("about"))
async def about_command(message: Message, state: FSMContext) -> None:
    await show_about(message, state)


@router.message(Command("cancel"))
async def cancel_command(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    await cancel_current_action(message, state, repository)
    logger.info(
        "Handled /cancel telegram_id=%s",
        message.from_user.id if message.from_user else None,
    )
