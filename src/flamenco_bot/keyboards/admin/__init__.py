from aiogram.types import ReplyKeyboardMarkup

from ..common import CANCEL, MAIN_MENU, keyboard


ADMIN_MENU = "🛠 Админ-меню"
BOT_STATUS = "📊 Состояние бота"
CREDITS_ADJUST_CONFIRM = "✅ Применить корректировку"
CREDITS_ADJUST_CANCEL = "↩️ Не применять"
ADMIN_SCHEDULE = "🗓 Расписание занятий"
ADMIN_CLIENTS = "👥 Клиенты"
ADMIN_FINANCE = "💰 Финансы"
SUPPORT_TICKETS = "📨 Обращения поддержки"


def admin_menu_keyboard(role: str = "admin") -> ReplyKeyboardMarkup:
    """Администратору — четыре раздела, преподавателю — свои занятия и
    поддержка; действия внутри — inline-кнопками в карточках."""
    if role != "admin":
        return keyboard([ADMIN_SCHEDULE, SUPPORT_TICKETS], [MAIN_MENU])
    return keyboard(
        [ADMIN_SCHEDULE, ADMIN_CLIENTS],
        [SUPPORT_TICKETS, ADMIN_FINANCE],
        [MAIN_MENU],
    )


def admin_action_keyboard() -> ReplyKeyboardMarkup:
    return keyboard([ADMIN_MENU], [MAIN_MENU])


def admin_input_keyboard() -> ReplyKeyboardMarkup:
    """Пока администратор вводит текст в сценарии: отмена или выход."""
    return keyboard([CANCEL], [MAIN_MENU])


def credit_adjustment_confirmation_keyboard() -> ReplyKeyboardMarkup:
    return keyboard(
        [CREDITS_ADJUST_CONFIRM],
        [CREDITS_ADJUST_CANCEL],
        [MAIN_MENU],
    )
