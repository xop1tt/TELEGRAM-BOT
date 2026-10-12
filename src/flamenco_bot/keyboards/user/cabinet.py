"""Личный кабинет в боте: профиль, абонементы, покупки, история, уведомления.

Inline-экраны поверх общих сервисов (``ProfileService``, ``PackageService``,
``HistoryService``, ``NotificationService``, ``SupportService``) — те же
данные и правила, что отдаёт сайту ``/api``.

callback_data (≤ 64 байт):

  prof                    профиль (сводка и быстрые действия)
  prof:name | prof:phone  изменить имя / телефон
  mypacks                 мои абонементы
  purch:hist[:<offset>]   история покупок (по 5, offset — сколько пропустить)
  hist:c:<offset>         история занятий
  hist:o[:<offset>]       история операций (ledger, по 5)
  notif                   настройки уведомлений
  notif:<rem|low>:<0|1>   выключить / включить
  tickets                 мои обращения в поддержку
  ticket:<id>             переписка по обращению
  about                   «О студии»
"""

import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from ...class_catalog import CLASS_LABELS
from ...presentation import (
    balance_line,
    format_amount,
    format_class_time,
    format_date,
    lessons_count,
    package_line,
    website_line,
)
from ...services import (
    HistoryService,
    NotificationService,
    PackageService,
    ProfileService,
)
from ...services.history import BOOKING_STATUS_LABELS, booking_status
from ...studio_time import to_studio_time
from .screens import BACK, BOOK_BUTTON, button, markup, parse_int, show


logger = logging.getLogger("bot.handlers.cabinet")
router = Router(name="cabinet_keyboard")

PAGE_SIZE = 10
# Покупки и операции листаются по пять записей.
SHORT_PAGE_SIZE = 5
PAYMENT_STATUS_LABELS = {
    "pending": "ожидает оплаты",
    "succeeded": "оплачен",
    "canceled": "отменён",
    "refund_pending": "возврат в обработке",
    "refunded": "возвращён",
}
TICKET_STATUS_LABELS = {"open": "открыто", "closed": "закрыто"}


def _on(value: bool) -> str:
    return "вкл" if value else "выкл"


# ---------- профиль ----------


async def profile_screen(
    repository: Any, telegram_id: int, notice: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    overview = await ProfileService(repository).overview(telegram_id)
    profile = overview.profile
    lines = [notice, ""] if notice else []
    lines.extend(
        [
            "👤 Профиль",
            "Имя: {}".format(profile.user_name),
            "Телефон: {}".format(profile.phone or "не указан"),
            balance_line(overview.packages.balance) + ".",
        ]
    )
    active = overview.packages.active_packages
    if active:
        lines.append("")
        lines.append("Абонементы:")
        lines.extend("• " + package_line(package) for package in active)
    if overview.upcoming:
        lines.append("")
        lines.append("Ближайшие занятия:")
        lines.extend(
            "• {} — {}".format(
                format_class_time(booking.starts_at), CLASS_LABELS[booking.class_key]
            )
            for booking in overview.upcoming
        )
    lines.extend(
        [
            "",
            "Уведомления: напоминания — {}, о малом остатке — {}.".format(
                _on(overview.settings.reminders), _on(overview.settings.low_balance)
            ),
        ]
    )
    site = website_line("Личный кабинет")
    if site:
        lines.extend(["", site])
    return "\n".join(lines), markup(
        [button("✏️ Имя", "prof:name"), button("📱 Телефон", "prof:phone")],
        [button("💃 Мои занятия", "my"), button("🎟 Абонементы", "mypacks")],
        [button("💳 Покупки", "packs:0"), button("🧾 Операции", "hist:o")],
        [button("📚 История занятий", "hist:c:0"), button("🔔 Уведомления", "notif")],
        [button("📨 Мои обращения", "tickets")],
    )


@router.callback_query(F.data == "prof")
async def show_profile(callback: CallbackQuery, repository: Any, state: FSMContext):
    await state.clear()
    text, reply_markup = await profile_screen(repository, callback.from_user.id)
    await show(callback, text, reply_markup)
    await callback.answer()


@router.callback_query(F.data.in_({"prof:name", "prof:phone"}))
async def start_profile_edit(callback: CallbackQuery, state: FSMContext) -> None:
    from .account import request_name, request_phone

    await callback.answer()
    if callback.message is None:
        return
    if callback.data == "prof:name":
        await request_name(callback.message, state)
    else:
        await request_phone(callback.message, state)


# ---------- абонементы и покупки ----------


async def my_packages_screen(
    repository: Any, telegram_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    summary = await PackageService(repository).summary(telegram_id)
    lines = ["🎟 Абонементы", balance_line(summary.balance) + "."]
    active = summary.active_packages_by_expiry
    if active:
        lines.extend(["", "Действующие:"])
        lines.extend("• " + package_line(package) for package in active)
    if summary.unallocated:
        lines.extend(
            [
                "",
                "Бессрочно (возврат за отменённые студией занятия): {}.".format(
                    lessons_count(summary.unallocated)
                ),
            ]
        )
    finished = [package for package in summary.packages if not package.is_active]
    if finished:
        lines.extend(["", "Завершённые:"])
        lines.extend("• " + package_line(package) for package in finished[-5:])
    if not summary.packages and not summary.unallocated:
        lines.extend(["", "Абонементов пока нет."])
    next_source = summary.next_source
    if summary.balance > 0:
        lines.extend(
            [
                "",
                "Следующая запись спишет занятие {}.".format(
                    "из «{}»".format(next_source.title)
                    if next_source is not None
                    else "из бессрочных занятий"
                ),
                "Сначала расходуется абонемент, который сгорает раньше; "
                "бессрочные занятия — в последнюю очередь.",
            ]
        )
    return "\n".join(lines), markup(
        [button("💳 Купить абонемент", "packs:0"), button(BOOK_BUTTON, "slots:all")],
        [button("🧾 История операций", "hist:o"), button("👤 Профиль", "prof")],
    )


@router.callback_query(F.data == "mypacks")
async def show_my_packages(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    await state.clear()
    text, reply_markup = await my_packages_screen(repository, callback.from_user.id)
    await show(callback, text, reply_markup)
    await callback.answer()


async def purchase_history_screen(
    repository: Any, telegram_id: int, offset: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    payments = await repository.list_lesson_payments_for_telegram_id(
        telegram_id, limit=SHORT_PAGE_SIZE + 1, offset=offset
    )
    has_more = len(payments) > SHORT_PAGE_SIZE
    payments = list(payments)[:SHORT_PAGE_SIZE]
    lines = ["🧾 История покупок"]
    if not payments:
        lines.extend(
            ["", "Покупок пока нет." if offset == 0 else "Больше покупок нет."]
        )
    else:
        lines.append("")
        lines.extend(
            "• {} — {}, {} · {}".format(
                format_date(payment.created_at),
                payment.package_title,
                format_amount(payment.amount_minor),
                PAYMENT_STATUS_LABELS.get(payment.status, payment.status),
            )
            for payment in payments
        )
    return "\n".join(lines), markup(
        _page_navigation("purch:hist", offset, has_more),
        [button(BACK + " к покупкам", "packs:0"), button("🎟 Абонементы", "mypacks")],
    )


def _page_navigation(prefix: str, offset: int, has_more: bool) -> list:
    """Кнопки «назад / вперёд» по страницам из пяти записей."""
    navigation = []
    if offset > 0:
        navigation.append(
            button(
                "⬅️ Новее",
                "{}:{}".format(prefix, max(0, offset - SHORT_PAGE_SIZE)),
            )
        )
    if has_more:
        navigation.append(
            button("Раньше ➡️", "{}:{}".format(prefix, offset + SHORT_PAGE_SIZE))
        )
    return navigation


@router.callback_query(F.data.startswith("purch:hist"))
async def show_purchase_history(callback: CallbackQuery, repository: Any) -> None:
    parts = (callback.data or "").split(":")
    offset = min(parse_int(parts[2]) or 0, 10_000) if len(parts) > 2 else 0
    text, reply_markup = await purchase_history_screen(
        repository, callback.from_user.id, offset
    )
    await show(callback, text, reply_markup)
    await callback.answer()


# ---------- история ----------


async def class_history_screen(
    repository: Any, telegram_id: int, offset: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    bookings = await HistoryService(repository).bookings(
        telegram_id, limit=PAGE_SIZE + 1, offset=offset
    )
    has_more = len(bookings) > PAGE_SIZE
    bookings = list(bookings)[:PAGE_SIZE]
    lines = ["📚 История занятий"]
    if not bookings:
        lines.extend(
            ["", "Записей пока нет." if offset == 0 else "Больше записей нет."]
        )
    else:
        lines.append("")
        for booking in bookings:
            line = "• {} — {} · {}".format(
                format_class_time(booking.starts_at),
                CLASS_LABELS[booking.class_key],
                BOOKING_STATUS_LABELS[booking_status(booking)],
            )
            if booking.previous_starts_at is not None:
                line += " (перенесено с {})".format(
                    format_class_time(booking.previous_starts_at)
                )
            if booking.cancelled_by == "studio" and booking.slot_cancel_reason:
                line += " — {}".format(booking.slot_cancel_reason)
            lines.append(line)
    navigation = []
    if offset > 0:
        navigation.append(
            button("⬅️ Новее", "hist:c:{}".format(max(0, offset - PAGE_SIZE)))
        )
    if has_more:
        navigation.append(button("Раньше ➡️", "hist:c:{}".format(offset + PAGE_SIZE)))
    return "\n".join(lines), markup(
        navigation,
        [button("💃 Мои занятия", "my"), button("👤 Профиль", "prof")],
    )


@router.callback_query(F.data.startswith("hist:c:"))
async def show_class_history(callback: CallbackQuery, repository: Any) -> None:
    offset = parse_int((callback.data or "").split(":")[2]) or 0
    text, reply_markup = await class_history_screen(
        repository, callback.from_user.id, min(offset, 10_000)
    )
    await show(callback, text, reply_markup)
    await callback.answer()


def operation_line(operation: Any) -> str:
    entry = operation.entry
    local = to_studio_time(entry.created_at)
    line = "• {} · {:+d} · {}".format(
        local.strftime("%d.%m %H:%M"), entry.delta, operation.label
    )
    details = []
    if entry.class_key and entry.starts_at:
        details.append(
            "{}, {}".format(
                CLASS_LABELS[entry.class_key], format_class_time(entry.starts_at)
            )
        )
    if entry.package_title and operation.operation in {
        "purchase",
        "package_grant",
        "package_revoke",
    }:
        details.append("«{}»".format(entry.package_title))
    if operation.visible_reason:
        details.append(operation.visible_reason)
    if details:
        line += " — " + "; ".join(details)
    return line


async def operations_screen(
    repository: Any,
    telegram_id: int,
    offset: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    operations = await HistoryService(repository).operations(
        telegram_id, limit=SHORT_PAGE_SIZE + 1, offset=offset
    )
    has_more = len(operations) > SHORT_PAGE_SIZE
    operations = list(operations)[:SHORT_PAGE_SIZE]
    credits = await repository.get_lesson_credits(telegram_id)
    lines = ["🧾 История операций", balance_line(credits) + "."]
    if not operations:
        lines.extend(
            ["", "Операций пока нет." if offset == 0 else "Больше операций нет."]
        )
    else:
        lines.append("")
        lines.extend(operation_line(operation) for operation in operations)
    return "\n".join(lines), markup(
        _page_navigation("hist:o", offset, has_more),
        [button("🎟 Абонементы", "mypacks"), button("👤 Профиль", "prof")],
    )


@router.callback_query(F.data.startswith("hist:o"))
async def show_operations(callback: CallbackQuery, repository: Any) -> None:
    parts = (callback.data or "").split(":")
    offset = min(parse_int(parts[2]) or 0, 10_000) if len(parts) > 2 else 0
    text, reply_markup = await operations_screen(
        repository, callback.from_user.id, offset
    )
    await show(callback, text, reply_markup)
    await callback.answer()


# ---------- уведомления ----------


async def notifications_screen(
    repository: Any, telegram_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    settings = await NotificationService(repository).settings(telegram_id)
    lines = [
        "🔔 Уведомления",
        "",
        "Напоминание о занятии — {}.".format(_on(settings.reminders)),
        "Сообщение, когда на балансе остаётся мало занятий — {}.".format(
            _on(settings.low_balance)
        ),
        "",
        "Об отмене или переносе занятия студией и об изменении баланса бот "
        "сообщает всегда.",
    ]
    return "\n".join(lines), markup(
        [
            button(
                ("🔕 Выключить" if settings.reminders else "🔔 Включить")
                + " напоминания",
                "notif:rem:{}".format(0 if settings.reminders else 1),
            )
        ],
        [
            button(
                ("🔕 Выключить" if settings.low_balance else "🔔 Включить")
                + " о малом остатке",
                "notif:low:{}".format(0 if settings.low_balance else 1),
            )
        ],
        [button("👤 Профиль", "prof")],
    )


@router.callback_query(F.data == "notif")
async def show_notification_settings(callback: CallbackQuery, repository: Any) -> None:
    text, reply_markup = await notifications_screen(repository, callback.from_user.id)
    await show(callback, text, reply_markup)
    await callback.answer()


@router.callback_query(F.data.startswith("notif:"))
async def toggle_notification_setting(callback: CallbackQuery, repository: Any) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[1] not in {"rem", "low"} or parts[2] not in {"0", "1"}:
        await callback.answer("Некорректная настройка.", show_alert=True)
        return
    enabled = parts[2] == "1"
    service = NotificationService(repository)
    if parts[1] == "rem":
        await service.update_settings(callback.from_user.id, reminders=enabled)
    else:
        await service.update_settings(callback.from_user.id, low_balance=enabled)
    text, reply_markup = await notifications_screen(repository, callback.from_user.id)
    await show(callback, text, reply_markup)
    await callback.answer("Сохранено.")


# ---------- обращения ----------


async def tickets_screen(
    repository: Any, telegram_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    tickets = await repository.list_support_tickets_for_telegram_id(
        telegram_id, limit=10
    )
    lines = ["📨 Мои обращения"]
    if not tickets:
        lines.extend(["", "Обращений пока нет. Написать в студию — «💬 Поддержка»."])
    rows = [
        [
            button(
                "№{} · {} · {}".format(
                    ticket.id,
                    TICKET_STATUS_LABELS.get(ticket.status, ticket.status),
                    format_date(ticket.updated_at),
                ),
                "ticket:{}".format(ticket.id),
            )
        ]
        for ticket in tickets
    ]
    rows.append([button("👤 Профиль", "prof")])
    return "\n".join(lines), markup(*rows)


@router.callback_query(F.data == "tickets")
async def show_tickets(callback: CallbackQuery, repository: Any) -> None:
    text, reply_markup = await tickets_screen(repository, callback.from_user.id)
    await show(callback, text, reply_markup)
    await callback.answer()


@router.callback_query(F.data.startswith("ticket:"))
async def show_ticket(callback: CallbackQuery, repository: Any) -> None:
    ticket_id = parse_int((callback.data or "").split(":", 1)[1])
    thread = (
        await repository.get_support_ticket_thread(ticket_id, callback.from_user.id)
        if ticket_id is not None
        else None
    )
    if thread is None:
        await callback.answer("Обращение не найдено.", show_alert=True)
        return
    lines = [
        "Обращение №{} · {}".format(
            thread.id, TICKET_STATUS_LABELS.get(thread.status, thread.status)
        ),
        "",
    ]
    for message in thread.messages[-10:]:
        author = "Вы" if message.sender_role == "user" else "Студия"
        lines.append(
            "{} · {}:\n{}".format(
                to_studio_time(message.created_at).strftime("%d.%m %H:%M"),
                author,
                message.body[:500],
            )
        )
        lines.append("")
    rows = []
    if thread.status == "open":
        rows.append([button("✍️ Дописать", "support_reply")])
    rows.append([button(BACK, "tickets")])
    await show(callback, "\n".join(lines).strip(), markup(*rows))
    await callback.answer()


# ---------- О студии ----------


@router.callback_query(F.data == "about")
async def show_about_screen(callback: CallbackQuery) -> None:
    from .main_menu import about_text

    await show(
        callback,
        about_text(),
        markup(
            [button(BOOK_BUTTON, "slots:all"), button("💳 Купить абонемент", "packs:0")]
        ),
    )
    await callback.answer()
