"""Inline-экраны разделов «Записаться», «Мои занятия», «Абонементы».

Экран — один текст + inline-клавиатура. При переходе внутри раздела бот
редактирует то же сообщение (не засыпает чат новыми), при входе из главного
меню — присылает новое.

callback_data (≤ 64 байт). Выбранное занятие (slot_id) передаётся по
цепочке «нет баланса → абонемент → оплата → проверка оплаты» прямо в
callback_data: так оно не теряется при сбросе FSM, переходе по меню или
перезапуске бота. 0 — занятие не выбрано.

  slots:<filter>[:<week>]              занятия на 7 дней (all или ключ направления;
                                       week — сдвиг на 7 дней вперёд, 0 — ближайшие)
  book:<class_key>:<slot_id>           записаться (список, после оплаты, старые)
  bookok:<class_key>:<slot_id>         подтверждённая запись (меньше 24 ч до начала)
  my                                   «Мои занятия»
  cancel_booking:<slot_id>             запрос отмены → экран подтверждения
  cancel_ok:<slot_id>                  подтверждённая отмена
  packs:<slot_id>                      «Покупки»: каталог абонементов и счета
  pack:<package_key>:<slot_id>         абонемент и кнопка «Оплатить N ₽»
  pay:<package_key>:<price>:<slot_id>  создать платёж (цена сверяется с каталогом)
  bill:<payment_id>:<slot_id>          неоплаченный счёт: ссылка и проверка
  lesson_payment_check:<payment_id>[:<slot_id>]  проверить оплату
  about                                «О студии»: направления, цены, правила

Экраны личного кабинета (абонементы, история, уведомления, профиль,
обращения) — в ``cabinet.py``.
"""

import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Optional, Sequence

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from ...config import Config
from ...class_catalog import (
    ACTIVE_CLASS_KEYS,
    CLASS_LABELS,
    CLASS_SHORT_LABELS,
)
from ...database.repository import ClassSlot, LessonPaymentHistoryItem, UserBooking
from ...payments import LessonPackage, PURCHASE_PACKAGES
from ...presentation import (
    PLACE_FORMS,
    balance_line,
    deadline_text,
    format_class_time,
    format_price,
    lessons_count,
    plural,
)
from ...services.profile import upcoming_bookings
from ...studio_time import to_studio_time


logger = logging.getLogger("bot.handlers.screens")

# Расписание показывает 7 дней; кнопками листается вперёд и назад.
SCHEDULE_DAYS = 7
MAX_SCHEDULE_WEEKS = 12
MAX_SLOTS_ON_SCREEN = 60


def is_slot_filter(key: str) -> bool:
    """«all» или действующее направление (каталог в базе, миграция 017)."""
    return key == "all" or key in ACTIVE_CLASS_KEYS


BACK = "⬅️ Назад"
BOOK_BUTTON = "📅 Записаться"
MY_CLASSES_BUTTON = "💃 Мои занятия"
ABOUT_BUTTON = "ℹ️ О студии и цены"


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def markup(*rows: Sequence[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows if row])


# Лимит Telegram на текст сообщения — 4096 символов.
MAX_MESSAGE_LENGTH = 4096


def fit_message(text: str) -> str:
    """Длинный экран (переписка, журнал) обрезается, а не роняет обработчик."""
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    return text[: MAX_MESSAGE_LENGTH - 2].rstrip() + "\n…"


async def show(
    callback: CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup],
) -> None:
    """Показывает экран в сообщении с нажатой кнопкой, иначе — новым."""
    text = fit_message(text)
    message = callback.message
    edit = getattr(message, "edit_text", None)
    if edit is not None:
        try:
            await edit(text, reply_markup=reply_markup)
            return
        except TelegramBadRequest as error:
            if "message is not modified" in str(error):
                return
            logger.info("Screen edit failed, sending new message: %s", error)
    bot = callback.bot
    if bot is None:
        raise RuntimeError("Telegram bot is unavailable for callback handling")
    await bot.send_message(callback.from_user.id, text, reply_markup=reply_markup)


def parse_int(value: str) -> Optional[int]:
    return int(value) if value.isdigit() else None


# ---------- Занятия ----------


def is_bookable(slot: ClassSlot) -> bool:
    return slot.status == "open" and slot.remaining > 0


def slot_button_text(slot: ClassSlot) -> str:
    return "{} · {} · {} {}".format(
        format_class_time(slot.starts_at),
        CLASS_SHORT_LABELS[slot.class_key],
        slot.remaining,
        plural(slot.remaining, PLACE_FORMS),
    )


async def find_slot(repository: Any, slot_id: int) -> Optional[ClassSlot]:
    """Предстоящее занятие по id (в том числе закрытое или заполненное)."""
    for slot in await repository.list_class_slots(limit=100):
        if slot.id == slot_id:
            return slot
    return None


def waitlist_button_text(slot: ClassSlot) -> str:
    return "🔔 {} · {} · мест нет".format(
        format_class_time(slot.starts_at), CLASS_SHORT_LABELS[slot.class_key]
    )


def _studio_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=Config.STUDIO_TIMEZONE)


async def slots_screen(
    repository: Any,
    telegram_id: int,
    filter_key: str = "all",
    notice: str = "",
    week: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """Занятия на 7 дней: ``week`` 0 — от сегодня, 1 — следующие 7 дней и т. д."""
    week = min(max(week, 0), MAX_SCHEDULE_WEEKS)
    today = to_studio_time(datetime.now(Config.STUDIO_TIMEZONE)).date()
    first_day = today + timedelta(days=SCHEDULE_DAYS * week)
    last_day = first_day + timedelta(days=SCHEDULE_DAYS - 1)
    # Открытые занятия: со свободными местами — запись, заполненные — лист
    # ожидания (миграция 019).
    slots = [
        slot
        for slot in await repository.list_class_slots(
            class_key=None if filter_key == "all" else filter_key,
            limit=100,
            starts_after=(
                _studio_midnight(first_day) - timedelta(seconds=1) if week else None
            ),
            starts_before=_studio_midnight(last_day + timedelta(days=1)),
        )
        if slot.status == "open"
    ][:MAX_SLOTS_ON_SCREEN]
    bookable = [slot for slot in slots if is_bookable(slot)]
    credits = await repository.get_lesson_credits(telegram_id)

    title = "{}: {} – {}".format(
        "Занятия" if week else "Ближайшие занятия",
        first_day.strftime("%d.%m"),
        last_day.strftime("%d.%m"),
    )
    if filter_key != "all":
        title += " — {}".format(CLASS_LABELS[filter_key])
    lines = [notice] if notice else []
    lines.append(title)
    if bookable:
        lines.append(
            "Выберите занятие, чтобы записаться. {}.".format(balance_line(credits))
        )
        if credits == 0:
            lines.append("Если баланса не хватит, бот предложит абонемент.")
    if len(bookable) < len(slots):
        lines.append(
            "🔔 — мест нет: встаньте в лист ожидания, и мы напишем, когда место "
            "освободится."
        )
    if not slots:
        lines.append(
            "На эти дни свободных занятий{} нет. Загляните на другую неделю "
            "или напишите нам в «💬 Поддержка».".format(
                " по этому направлению" if filter_key != "all" else ""
            )
        )

    def filter_button(key: str) -> InlineKeyboardButton:
        label = "Все направления" if key == "all" else CLASS_SHORT_LABELS[key]
        prefix = "✓ " if key == filter_key else ""
        return button(prefix + label, "slots:{}:{}".format(key, week))

    keys = ["all", *ACTIVE_CLASS_KEYS]
    rows = [
        [filter_button(key) for key in keys[index : index + 2]]
        for index in range(0, len(keys), 2)
    ]
    rows.extend(
        [
            button(slot_button_text(slot), "book:{}:{}".format(slot.class_key, slot.id))
            if is_bookable(slot)
            else button(waitlist_button_text(slot), "wl:{}".format(slot.id))
        ]
        for slot in slots
    )
    navigation = []
    if week > 0:
        navigation.append(button("⬅️ Назад", "slots:{}:{}".format(filter_key, week - 1)))
    if week < MAX_SCHEDULE_WEEKS:
        navigation.append(
            button("Вперёд ➡️", "slots:{}:{}".format(filter_key, week + 1))
        )
    rows.append(navigation)
    rows.append([button(MY_CLASSES_BUTTON, "my"), button(ABOUT_BUTTON, "about")])
    return "\n".join(lines), markup(*rows)


# ---------- Мои занятия ----------


def booking_line(booking: UserBooking, now: Optional[datetime] = None) -> str:
    line = "• {} — {}".format(
        format_class_time(booking.starts_at, now), CLASS_LABELS[booking.class_key]
    )
    if booking.previous_starts_at is not None:
        line += " (перенесено, было {})".format(
            format_class_time(booking.previous_starts_at, now)
        )
    if not booking.can_cancel(now):
        line += " (отмена уже недоступна)"
    return line


async def my_classes_screen(
    repository: Any,
    telegram_id: int,
    notice: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    upcoming = upcoming_bookings(
        await repository.list_bookings_for_telegram_id(telegram_id)
    )
    credits = await repository.get_lesson_credits(telegram_id)
    waitlist = await repository.list_user_waitlist(telegram_id)
    lines = [notice, ""] if notice else []
    lines.extend(["Мои занятия", balance_line(credits) + "."])
    history_row = [button("📚 История занятий", "hist:c:0")]
    waitlist_rows = [
        [
            button(
                "Выйти из очереди: {} · {}".format(
                    format_class_time(entry.slot.starts_at),
                    CLASS_SHORT_LABELS[entry.slot.class_key],
                ),
                "wlx:{}".format(entry.slot.id),
            )
        ]
        for entry in waitlist
    ]
    if waitlist:
        lines.extend(["", "🔔 Лист ожидания:"])
        lines.extend(
            "• {} — {}".format(
                format_class_time(entry.slot.starts_at),
                CLASS_LABELS[entry.slot.class_key],
            )
            for entry in waitlist
        )
    if not upcoming:
        lines.extend(["", "Предстоящих занятий пока нет."])
        return "\n".join(lines), markup(
            *waitlist_rows,
            [button(BOOK_BUTTON, "slots:all")],
            [button("💳 Купить абонемент", "packs:0")],
            history_row,
        )

    lines.append("")
    lines.extend(booking_line(booking) for booking in upcoming)
    lines.extend(
        [
            "",
            "Отменить запись можно не позднее чем за {} до начала — "
            "занятие вернётся на баланс. Если занятие перенесла студия, "
            "отменить запись можно до нового начала.".format(deadline_text()),
        ]
    )
    rows = [
        [
            button(
                "Отменить: {} · {}".format(
                    format_class_time(booking.starts_at),
                    CLASS_SHORT_LABELS[booking.class_key],
                ),
                "cancel_booking:{}".format(booking.slot_id),
            )
        ]
        for booking in upcoming
        if booking.can_cancel()
    ]
    rows.extend(waitlist_rows)
    rows.append([button(BOOK_BUTTON, "slots:all")])
    rows.append(history_row)
    return "\n".join(lines), markup(*rows)


# ---------- Абонементы ----------


def package_by_key(key: str) -> Optional[LessonPackage]:
    return next(
        (package for package in PURCHASE_PACKAGES.values() if package.key == key),
        None,
    )


def package_button_text(package: LessonPackage) -> str:
    text = "{} — {}".format(package.title, format_price(package.price_rub))
    if package.lessons > 1:
        text += " ({} за занятие)".format(
            format_price(package.price_rub // package.lessons)
        )
    return text


MAX_PENDING_BILLS = 3


async def pending_bills(
    repository: Any, telegram_id: int
) -> list[LessonPaymentHistoryItem]:
    """Неоплаченные счета — чтобы к ним можно было вернуться из бота.

    Ссылка на оплату и «Проверить оплату» иначе живут только в одном
    сообщении, которое легко потерять.
    """
    payments = await repository.list_lesson_payments_for_telegram_id(telegram_id)
    return [payment for payment in payments if payment.status == "pending"][
        :MAX_PENDING_BILLS
    ]


def bill_title(payment: Any) -> str:
    return "Счёт №{} на {} — {}".format(
        payment.id, format_price(payment.amount_minor // 100), payment.package_title
    )


def packages_screen(
    credits: int,
    slot_id: int = 0,
    notice: str = "",
    bills: Sequence[LessonPaymentHistoryItem] = (),
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [notice, ""] if notice else []
    lines.extend(["Покупки", balance_line(credits) + "."])
    if bills:
        lines.extend(
            [
                "",
                "Есть неоплаченный счёт — его можно оплатить или проверить "
                "кнопкой ниже, новый создавать не нужно."
                if len(bills) == 1
                else "Есть неоплаченные счета — их можно оплатить или проверить "
                "кнопками ниже.",
            ]
        )
    lines.extend(
        [
            "",
            "Выберите абонемент. Оплата — на странице ЮKassa, занятия "
            "добавятся на баланс сразу после подтверждения оплаты.",
        ]
    )
    rows = [
        [button(bill_title(bill), "bill:{}:{}".format(bill.id, slot_id))]
        for bill in bills
    ]
    rows.extend(
        [
            button(
                package_button_text(package), "pack:{}:{}".format(package.key, slot_id)
            )
        ]
        for package in PURCHASE_PACKAGES.values()
    )
    if slot_id:
        rows.append([button(BACK + " к занятиям", "slots:all")])
    else:
        rows.append(
            [
                button("🧾 История покупок", "purch:hist"),
                button("🎟 Мои абонементы", "mypacks"),
            ]
        )
        rows.append([button(ABOUT_BUTTON, "about")])
    return "\n".join(lines), markup(*rows)


async def packages_view(
    repository: Any,
    telegram_id: int,
    slot_id: int = 0,
    notice: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    credits = await repository.get_lesson_credits(telegram_id)
    bills = await pending_bills(repository, telegram_id)
    return packages_screen(credits, slot_id, notice, bills)


def package_screen(
    package: LessonPackage,
    slot_id: int = 0,
    notice: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [notice, ""] if notice else []
    lines.extend(
        [
            package.title,
            "{} за {}.".format(
                lessons_count(package.lessons), format_price(package.price_rub)
            ),
            "",
            "После нажатия «Оплатить» откроется ссылка на страницу ЮKassa. "
            "Занятия добавятся на баланс сразу после подтверждения оплаты.",
        ]
    )
    return "\n".join(lines), markup(
        [
            button(
                "Оплатить {}".format(format_price(package.price_rub)),
                "pay:{}:{}:{}".format(package.key, package.price_rub, slot_id),
            )
        ],
        [button(BACK, "packs:{}".format(slot_id))],
    )


# Импортируется и веб-API (фоновая сверка платежей) — поэтому здесь, а не в
# purchases.py: тот модуль тянет за собой роутеры бота.
def payment_confirmed_notifier(bot: Any, repository: Any):
    """Сообщение пользователю, когда оплату зачла фоновая сверка."""

    async def notify(telegram_id: int, result: Any) -> None:
        credits = result.credits
        if credits is None:
            credits = await repository.get_lesson_credits(telegram_id)
        lessons = result.payment.lessons if result.payment is not None else None
        headline = (
            "Оплата прошла. Зачислено {}. {}.".format(
                lessons_count(lessons), balance_line(credits)
            )
            if lessons is not None
            else "Оплата прошла. {}.".format(balance_line(credits))
        )
        await bot.send_message(
            telegram_id,
            headline,
            reply_markup=markup([button(BOOK_BUTTON, "slots:all")]),
        )

    return notify
