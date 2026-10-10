"""Подписи кнопок и reply-клавиатуры клиентской части бота.

Навигация:
- главное меню (reply) — шесть разделов, всегда под рукой:
  📅 Расписание, 💃 Мои занятия, 🎟 Абонементы, 💳 Покупки, 👤 Профиль,
  💬 Поддержка;
- внутри разделов — inline-экраны в одном сообщении, «⬅️ Назад» возвращает
  на предыдущий экран; редкие действия (история, уведомления, «О студии»)
  — inline-кнопками внутри разделов, а не в главном меню;
- ввод текста (имя, телефон, обращение) — reply-клавиатура
  [❌ Отмена] (вернуться туда, откуда начат ввод) и [🏠 Главное меню].
"""

from typing import Optional

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from ..admin import ADMIN_MENU, BOT_STATUS
from ..common import CANCEL, MAIN_MENU, keyboard


BOOK = "📅 Расписание"
MY_CLASSES = "💃 Мои занятия"
PACKAGES = "🎟 Абонементы"
PURCHASES = "💳 Покупки"
PROFILE = "👤 Профиль"
HELP = "💬 Поддержка"
# «О студии» больше не в главном меню (кнопка в «Расписании» и «Покупках»,
# команда /about), но старая кнопка по-прежнему открывает раздел.
ABOUT = "💃 О студии"
PROFILE_NAME = "✏️ Изменить имя"
PROFILE_PHONE = "📱 Изменить телефон"
SEND_PHONE = "📲 Отправить мой номер"

# Подписи прежних версий меню. Reply-клавиатура остаётся у пользователя до
# тех пор, пока бот не пришлёт новую, поэтому после обновления люди ещё
# какое-то время нажимают старые кнопки — они ведут в новые разделы.
LEGACY_TO_SECTION = {
    "👤 Учетная запись": PROFILE,
    "📋 Показать мои данные": PROFILE,
    "📱 Изменить номер телефона": PROFILE_PHONE,
    "💃 Запись на занятия": BOOK,
    "🗓️ Записаться на занятие": BOOK,
    "🗓 Записаться": BOOK,
    "⬅️ Назад к выбору занятия": BOOK,
    "📖 Мои занятия": MY_CLASSES,
    "💳 Абонементы": PACKAGES,
    "💳 Покупка занятий": PURCHASES,
    "⬅️ Назад к выбору пакета": PURCHASES,
    "🆘 Обратиться в поддержку": HELP,
    "💬 Помощь": HELP,
    "📚 О студии": ABOUT,
    "⬅️ Назад к занятиям": MAIN_MENU,
}


def main_menu_keyboard(role: Optional[str] = None) -> ReplyKeyboardMarkup:
    """Главное меню; сотрудникам (``role``) — ещё «Админ-меню», а
    администратору — и «Состояние бота»."""
    rows = [[BOOK, MY_CLASSES], [PACKAGES, PURCHASES], [PROFILE, HELP]]
    if role == "admin":
        rows.append([ADMIN_MENU, BOT_STATUS])
    elif role is not None:
        rows.append([ADMIN_MENU])
    return keyboard(*rows)


def profile_keyboard() -> ReplyKeyboardMarkup:
    return keyboard([PROFILE_NAME, PROFILE_PHONE], [MAIN_MENU])


def input_keyboard() -> ReplyKeyboardMarkup:
    return keyboard([CANCEL], [MAIN_MENU])


def phone_request_keyboard() -> ReplyKeyboardMarkup:
    return keyboard(
        [KeyboardButton(text=SEND_PHONE, request_contact=True)],
        [CANCEL],
        [MAIN_MENU],
    )


__all__ = [
    "ABOUT",
    "BOOK",
    "CANCEL",
    "HELP",
    "LEGACY_TO_SECTION",
    "MAIN_MENU",
    "MY_CLASSES",
    "PACKAGES",
    "PROFILE",
    "PROFILE_NAME",
    "PROFILE_PHONE",
    "PURCHASES",
    "SEND_PHONE",
    "input_keyboard",
    "main_menu_keyboard",
    "phone_request_keyboard",
    "profile_keyboard",
]
