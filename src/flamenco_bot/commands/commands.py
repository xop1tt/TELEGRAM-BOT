import logging
from typing import Any

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault


# Меню команд Telegram видят все — поэтому по умолчанию только клиентские
# команды; администраторы получают полный список в своём чате
# (BotCommandScopeChat, см. main.py).
def get_client_commands() -> list[BotCommand]:
    return [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="schedule", description="Расписание и запись"),
        BotCommand(command="lessons", description="Мои занятия"),
        BotCommand(command="packages", description="Мои абонементы"),
        BotCommand(command="buy", description="Купить абонемент"),
        BotCommand(command="account", description="Профиль"),
        BotCommand(command="about", description="О студии и цены"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="cancel", description="Отменить ввод"),
    ]


def get_admin_commands() -> list[BotCommand]:
    return [
        BotCommand(command="admin", description="Админ-меню"),
        BotCommand(command="slots", description="Расписание занятий"),
        BotCommand(command="slot", description="Карточка занятия: /slot ID"),
        BotCommand(command="client", description="Карточка участника: /client ID"),
        BotCommand(command="support_tickets", description="Обращения поддержки"),
        BotCommand(command="credits_audit", description="Сверка баланса с ledger"),
        BotCommand(command="ledger", description="Последние операции с балансом"),
        BotCommand(command="audit", description="Журнал действий"),
    ]


def get_bot_commands() -> list[BotCommand]:
    """Полный список (клиентские + админские) — для чатов администраторов."""
    return get_client_commands() + get_admin_commands()


async def register_commands(bot: Any, repository: Any, logger: logging.Logger) -> None:
    """Клиентам — клиентские команды, администраторам — полный список.

    Список администраторов берётся из БД при запуске: администратор,
    назначенный позже, увидит свои команды после перезапуска бота (сами
    команды и так проверяют права при вызове).
    """
    await bot.set_my_commands(get_client_commands(), scope=BotCommandScopeDefault())
    for admin_id in await repository.list_admin_ids():
        try:
            await bot.set_my_commands(
                get_bot_commands(), scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except TelegramAPIError as error:
            logger.warning(
                "Admin command menu not set admin_id=%s error_type=%s",
                admin_id,
                type(error).__name__,
            )
