"""«Записаться» и «Мои занятия» — inline-экраны поверх BookingService.

Бизнес-правила (вместимость, баланс, окно отмены 24 ч, пауза 12 ч на
повторную запись) проверяет репозиторий в транзакции; здесь — только
когда и какой кнопкой их вызвать и как показать результат.
"""

import logging
from typing import Any, Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from ...class_catalog import CLASS_LABELS
from ...database.repository import (
    BookingCooldownError,
    BookingNotFoundError,
    CancellationWindowExpiredError,
    InsufficientLessonCreditsError,
    SlotUnavailableError,
    UserBooking,
)
from ...presentation import (
    balance_line,
    booking_cancellation_hint,
    can_cancel,
    cancellation_hint,
    deadline_text_genitive,
    format_class_time,
    plural,
    source_line,
)
from ...services import AdminNotifier, BookingService
from .screens import (
    BOOK_BUTTON,
    is_slot_filter,
    button,
    find_slot,
    markup,
    my_classes_screen,
    packages_view,
    parse_int,
    show,
    slots_screen,
    upcoming_bookings,
)
from ...studio_rules import rules
from ...database.waitlist_postgres import AlreadyBookedError


logger = logging.getLogger("bot.handlers.lessons")
router = Router(name="lessons_keyboard")


async def ensure_callback_profile(callback: CallbackQuery, repository: Any) -> None:
    sender = callback.from_user
    await repository.get_or_create_profile(
        telegram_id=sender.id,
        user_name=sender.full_name.strip() or "Участник студии",
    )


# ---------- Записаться ----------


@router.callback_query(F.data.startswith("slots:"))
async def show_slots(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    filter_key = (callback.data or "").split(":", 1)[1]
    if not is_slot_filter(filter_key):
        filter_key = "all"
    # Переход в другой сценарий — незавершённый ввод текста не должен
    # «поймать» следующее сообщение пользователя.
    await state.clear()
    text, reply_markup = await slots_screen(
        repository, callback.from_user.id, filter_key
    )
    await show(callback, text, reply_markup)
    await callback.answer()


@router.callback_query(F.data.startswith("book:"))
@router.callback_query(F.data.startswith("bookok:"))
async def book_class_slot(
    callback: CallbackQuery,
    repository: Any,
    state: FSMContext,
) -> None:
    """Запись на занятие.

    ``book:`` — нажатие на занятие. Запись, которую можно отменить (до
    начала больше окна отмены), подтверждается сразу: ошибку исправит отмена.
    Запись позже окна отмены необратима — для неё сначала экран
    подтверждения. ``bookok:`` — подтверждённая запись.
    """
    parts = (callback.data or "").split(":")
    slot_id = parse_int(parts[2]) if len(parts) == 3 else None
    if slot_id is None or parts[1] not in CLASS_LABELS:
        await callback.answer("Некорректная запись.", show_alert=True)
        return
    bot = callback.bot
    if bot is None:
        raise RuntimeError("Telegram bot is unavailable for callback handling")
    await state.clear()
    if parts[0] == "book":
        slot = await find_slot(repository, slot_id)
        if slot is not None and not can_cancel(slot.starts_at):
            await callback.answer()
            await show(
                callback,
                "Записаться на занятие?\n\n{}\n{}\n\nДо начала меньше {} — "
                "отменить эту запись будет нельзя, занятие спишется с баланса.".format(
                    CLASS_LABELS[slot.class_key],
                    format_class_time(slot.starts_at),
                    deadline_text_genitive(),
                ),
                markup(
                    [button("Записаться", "bookok:{}:{}".format(parts[1], slot_id))],
                    [button("⬅️ Назад к занятиям", "slots:all")],
                ),
            )
            return
    await ensure_callback_profile(callback, repository)
    sender = callback.from_user
    booking_service = BookingService(repository, AdminNotifier(bot, repository))
    try:
        booking = await booking_service.book(slot_id, sender.id)
    except SlotUnavailableError:
        await callback.answer(
            "На это занятие уже нет свободных мест или запись закрыта. На "
            "заполненное занятие можно встать в лист ожидания (🔔).",
            show_alert=True,
        )
        text, reply_markup = await slots_screen(
            repository, sender.id, notice="Список занятий обновлён."
        )
        await show(callback, text, reply_markup)
        return
    except InsufficientLessonCreditsError:
        await callback.answer()
        text, reply_markup = await packages_view(
            repository,
            sender.id,
            slot_id=slot_id,
            notice=(
                "Для записи нужно 1 занятие на балансе, сейчас их нет. Выберите "
                "абонемент — после оплаты вернётесь к выбранному занятию."
            ),
        )
        await show(callback, text, reply_markup)
        return
    except BookingCooldownError as error:
        await callback.answer(str(error), show_alert=True)
        return

    credits = (
        booking.balance
        if booking.balance is not None
        else await repository.get_lesson_credits(sender.id)
    )
    lines = [
        "Вы уже записаны на это занятие."
        if booking.already_booked
        else "Вы записаны ✓",
        "",
        CLASS_LABELS[booking.class_key],
        format_class_time(booking.starts_at),
        "",
    ]
    hint = cancellation_hint(booking.starts_at)
    cancellable = can_cancel(booking.starts_at)
    if booking.already_booked:
        # Уже существующая запись могла пережить перенос — срок отмены у неё
        # свой (см. booking_cancellation_deadline).
        existing = await find_upcoming_booking(repository, sender.id, slot_id)
        if existing is not None:
            hint = booking_cancellation_hint(existing)
            cancellable = existing.can_cancel()
    else:
        lines.append(source_line(booking.source))
    lines.extend([balance_line(credits) + ".", hint])
    rows = []
    if cancellable:
        rows.append([button("Отменить запись", "cancel_booking:{}".format(slot_id))])
    rows.append([button("💃 Мои занятия", "my"), button("Записаться ещё", "slots:all")])

    await callback.answer(
        "Вы уже записаны." if booking.already_booked else "Вы записаны!"
    )
    await show(callback, "\n".join(lines), markup(*rows))
    if not booking.already_booked:
        await booking_service.notify_admins_about_booking(booking, sender.full_name)


# ---------- Лист ожидания ----------


@router.callback_query(F.data.startswith("wl:"))
async def join_waitlist(callback: CallbackQuery, repository: Any) -> None:
    """Встать в лист ожидания на заполненное занятие (или показать место в
    очереди, если уже стоит)."""
    slot_id = parse_int((callback.data or "").split(":", 1)[1])
    if slot_id is None:
        await callback.answer("Некорректное занятие.", show_alert=True)
        return
    await ensure_callback_profile(callback, repository)
    telegram_id = callback.from_user.id
    already = slot_id in await repository.user_waitlisted_slot_ids(telegram_id)
    try:
        entry = await BookingService(
            repository, AdminNotifier(callback.bot, repository)
        ).join_waitlist(slot_id, telegram_id)
    except InsufficientLessonCreditsError:
        await callback.answer()
        text, reply_markup = await packages_view(
            repository,
            telegram_id,
            notice=(
                "В лист ожидания можно встать с занятием на балансе. Выберите "
                "абонемент — после оплаты вернитесь к расписанию."
            ),
        )
        await show(callback, text, reply_markup)
        return
    except AlreadyBookedError as error:
        await callback.answer(str(error), show_alert=True)
        return
    except SlotUnavailableError as error:
        await callback.answer(str(error), show_alert=True)
        text, reply_markup = await slots_screen(
            repository, telegram_id, notice="Список занятий обновлён."
        )
        await show(callback, text, reply_markup)
        return
    await callback.answer("Вы в листе ожидания" if not already else "Вы уже в очереди")
    await show(
        callback,
        "🔔 Лист ожидания\n\n{}\n{}\n\nВ очереди: {}. Когда место освободится, "
        "мы пришлём сообщение — запишется тот, кто успеет первым. Занятие "
        "спишется только при записи.".format(
            CLASS_LABELS[entry.slot.class_key],
            format_class_time(entry.slot.starts_at),
            entry.queue_size,
        ),
        markup(
            [button("Выйти из очереди", "wlx:{}".format(slot_id))],
            [button("💃 Мои занятия", "my"), button("⬅️ К расписанию", "slots:all")],
        ),
    )


@router.callback_query(F.data.startswith("wlx:"))
async def leave_waitlist(callback: CallbackQuery, repository: Any) -> None:
    slot_id = parse_int((callback.data or "").split(":", 1)[1])
    if slot_id is None:
        await callback.answer("Некорректное занятие.", show_alert=True)
        return
    left = await repository.leave_waitlist(slot_id, callback.from_user.id)
    await callback.answer("Вы вышли из очереди" if left else "Вас нет в этой очереди")
    text, reply_markup = await my_classes_screen(repository, callback.from_user.id)
    await show(callback, text, reply_markup)


# ---------- Мои занятия ----------


@router.callback_query(F.data == "my")
async def show_my_classes(
    callback: CallbackQuery,
    repository: Any,
    state: FSMContext,
) -> None:
    await state.clear()
    text, reply_markup = await my_classes_screen(repository, callback.from_user.id)
    await show(callback, text, reply_markup)
    await callback.answer()


async def find_upcoming_booking(
    repository: Any,
    telegram_id: int,
    slot_id: int,
) -> Optional[UserBooking]:
    bookings = upcoming_bookings(
        await repository.list_bookings_for_telegram_id(telegram_id)
    )
    return next((item for item in bookings if item.slot_id == slot_id), None)


def booking_summary(booking: UserBooking) -> str:
    summary = "{}\n{}".format(
        CLASS_LABELS[booking.class_key], format_class_time(booking.starts_at)
    )
    if booking.previous_starts_at is not None:
        summary += "\nПеренесено студией, было {}".format(
            format_class_time(booking.previous_starts_at)
        )
    return summary


@router.callback_query(F.data.startswith("cancel_booking:"))
async def request_booking_cancel(callback: CallbackQuery, repository: Any) -> None:
    """Первый шаг отмены — только экран подтверждения.

    Кнопки «❌ Отменить» со старых сообщений бота ведут сюда же, поэтому
    случайное нажатие больше не отменяет запись сразу.
    """
    parts = (callback.data or "").split(":")
    slot_id = parse_int(parts[1]) if len(parts) == 2 else None
    if slot_id is None:
        await callback.answer("Некорректный запрос.", show_alert=True)
        return
    telegram_id = callback.from_user.id
    booking = await find_upcoming_booking(repository, telegram_id, slot_id)
    if booking is None:
        await callback.answer(
            "Эта запись уже отменена или занятие прошло.", show_alert=True
        )
        text, reply_markup = await my_classes_screen(repository, telegram_id)
        await show(callback, text, reply_markup)
        return
    if not booking.can_cancel():
        await callback.answer(
            "Отменить уже нельзя — до начала меньше {}.".format(
                deadline_text_genitive()
            ),
            show_alert=True,
        )
        text, reply_markup = await my_classes_screen(repository, telegram_id)
        await show(callback, text, reply_markup)
        return

    cooldown_hours = rules().rebook_cooldown_hours
    text = (
        "Отменить запись?\n\n{}\n\nЗанятие вернётся на баланс. Записаться на это "
        "же занятие снова можно будет только через {} {}.".format(
            booking_summary(booking),
            cooldown_hours,
            plural(cooldown_hours, ("час", "часа", "часов")),
        )
    )
    await show(
        callback,
        text,
        markup(
            [button("Да, отменить запись", "cancel_ok:{}".format(slot_id))],
            [button("⬅️ Нет, оставить запись", "my")],
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_ok:"))
async def cancel_booking_callback(callback: CallbackQuery, repository: Any) -> None:
    parts = (callback.data or "").split(":")
    slot_id = parse_int(parts[1]) if len(parts) == 2 else None
    if slot_id is None:
        await callback.answer("Некорректный запрос.", show_alert=True)
        return
    bot = callback.bot
    if bot is None:
        raise RuntimeError("Telegram bot is unavailable for callback handling")
    telegram_id = callback.from_user.id
    booking = await find_upcoming_booking(repository, telegram_id, slot_id)
    booking_service = BookingService(repository, AdminNotifier(bot, repository))
    try:
        cancelled = await booking_service.cancel(slot_id, telegram_id)
    except BookingNotFoundError:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    except CancellationWindowExpiredError as error:
        await callback.answer(str(error), show_alert=True)
        text, reply_markup = await my_classes_screen(repository, telegram_id)
        await show(callback, text, reply_markup)
        return

    if not cancelled:
        await callback.answer("Эта запись уже отменена (вами или студией).")
        text, reply_markup = await my_classes_screen(repository, telegram_id)
        await show(callback, text, reply_markup)
        return

    credits = await repository.get_lesson_credits(telegram_id)
    lines = ["Запись отменена."]
    if booking is not None:
        lines.extend(["", booking_summary(booking)])
    lines.extend(["", "Занятие вернулось на баланс. {}.".format(balance_line(credits))])
    await callback.answer("Запись отменена.")
    await show(
        callback,
        "\n".join(lines),
        markup([button("💃 Мои занятия", "my"), button(BOOK_BUTTON, "slots:all")]),
    )
