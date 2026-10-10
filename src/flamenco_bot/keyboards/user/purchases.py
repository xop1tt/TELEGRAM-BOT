"""«Абонементы»: выбор пакета, оплата, проверка оплаты.

Без FSM: платёж создаётся только кнопкой «Оплатить N ₽» (callback), ни
одно текстовое сообщение пользователя к оплате не ведёт — нечему «зависнуть»
и нечего случайно подтвердить. Создание и сверку платежа по-прежнему
делает PaymentService (идемпотентность, ровно однократное зачисление).
"""

import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ...handlers.states import LessonForm
from ...payments import LessonPackage, YooKassaClient
from ...presentation import balance_line, format_class_time, format_price, lessons_count
from ...services import CheckoutFailedError, PaymentCheckStatus, PaymentService
from ...services.payments import safe_confirmation_url
from .screens import (
    BACK,
    BOOK_BUTTON,
    bill_title,
    button,
    find_slot,
    is_bookable,
    markup,
    package_by_key,
    package_screen,
    packages_view,
    parse_int,
    show,
)


logger = logging.getLogger("bot.handlers.purchases")
router = Router(name="purchases_keyboard")

HELP_HINT = "напишите в «💬 Поддержка» — администратор проверит платёж"


@router.callback_query(F.data.startswith("packs:"))
async def show_packages(
    callback: CallbackQuery,
    repository: Any,
    state: FSMContext,
) -> None:
    slot_id = parse_int((callback.data or "").split(":", 1)[1]) or 0
    await state.clear()
    text, reply_markup = await packages_view(repository, callback.from_user.id, slot_id)
    await show(callback, text, reply_markup)
    await callback.answer()


@router.callback_query(F.data.startswith("pack:"))
async def show_package(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    package = package_by_key(parts[1]) if len(parts) == 3 else None
    slot_id = parse_int(parts[2]) if len(parts) == 3 else None
    if package is None or slot_id is None:
        await callback.answer("Абонемент не найден.", show_alert=True)
        return
    text, reply_markup = package_screen(package, slot_id)
    await show(callback, text, reply_markup)
    await callback.answer()


@router.callback_query(F.data.startswith("pay:"))
async def pay_for_package(
    callback: CallbackQuery,
    repository: Any,
    payment_gateway: YooKassaClient,
) -> None:
    parts = (callback.data or "").split(":")
    package = package_by_key(parts[1]) if len(parts) == 4 else None
    shown_price = parse_int(parts[2]) if len(parts) == 4 else None
    slot_id = parse_int(parts[3]) if len(parts) == 4 else None
    if package is None or shown_price is None or slot_id is None:
        await callback.answer("Абонемент не найден.", show_alert=True)
        return
    # Пользователь подтверждает ту сумму, которую видел на кнопке. Если
    # каталог успел измениться, платить по старой кнопке нельзя.
    if shown_price != package.price_rub:
        await callback.answer()
        text, reply_markup = package_screen(
            package,
            slot_id,
            notice="Цена изменилась — проверьте сумму и нажмите «Оплатить» ещё раз.",
        )
        await show(callback, text, reply_markup)
        return

    telegram_id = callback.from_user.id
    payment_service = PaymentService(repository, payment_gateway)
    if not payment_service.checkout_available:
        logger.warning(
            "Purchase checkout unavailable configured=%s",
            payment_gateway.is_configured,
        )
        await callback.answer()
        await show(
            callback,
            "Оплата недоступна — онлайн-оплата сейчас не подключена. Напишите "
            "в «💬 Поддержка», подскажем, как оплатить абонемент.",
            None,
        )
        return

    await repository.get_or_create_profile(
        telegram_id=telegram_id,
        user_name=callback.from_user.full_name.strip() or "Участник студии",
    )
    snapshot = LessonPackage(
        key=package.key,
        title=package.title,
        lessons=package.lessons,
        price_rub=package.price_rub,
    )
    try:
        checkout = await payment_service.start_checkout(telegram_id, snapshot)
    except CheckoutFailedError:
        await callback.answer()
        await show(
            callback,
            "Не удалось подтвердить создание платежа. Не оплачивайте повторно — "
            + HELP_HINT
            + ".",
            None,
        )
        return

    payment = checkout.payment
    await callback.answer()
    if checkout.confirmation_url is None:
        await show(
            callback,
            "Платёж №{} зарегистрирован, но получить безопасную ссылку на оплату "
            "не удалось. Не создавайте новый платёж — {}.".format(
                payment.id, HELP_HINT
            ),
            None,
        )
        return
    await show(
        callback,
        "Счёт №{} на {} — {}.\n\n"
        "1. Нажмите «Перейти к оплате» — откроется страница ЮKassa.\n"
        "2. После оплаты вернитесь сюда и нажмите «Проверить оплату».".format(
            payment.id,
            format_price(checkout.amount_minor // 100),
            package.title,
        ),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Перейти к оплате", url=checkout.confirmation_url
                    )
                ],
                [
                    button(
                        "Проверить оплату",
                        "lesson_payment_check:{}:{}".format(payment.id, slot_id),
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data.startswith("bill:"))
async def show_bill(callback: CallbackQuery, repository: Any) -> None:
    """Неоплаченный счёт: перейти к оплате или проверить оплату."""
    parts = (callback.data or "").split(":")
    payment_id = parse_int(parts[1]) if len(parts) == 3 else None
    slot_id = parse_int(parts[2]) if len(parts) == 3 else None
    if payment_id is None or slot_id is None:
        await callback.answer("Счёт не найден.", show_alert=True)
        return
    telegram_id = callback.from_user.id
    payment = await repository.get_lesson_payment(payment_id, telegram_id)
    if payment is None or payment.status != "pending":
        await callback.answer("Этот счёт уже оплачен или закрыт.", show_alert=True)
        text, reply_markup = await packages_view(repository, telegram_id, slot_id)
        await show(callback, text, reply_markup)
        return

    rows = []
    url = safe_confirmation_url(payment.confirmation_url)
    if url is not None:
        rows.append([InlineKeyboardButton(text="Перейти к оплате", url=url)])
    rows.append(
        [
            button(
                "Проверить оплату",
                "lesson_payment_check:{}:{}".format(payment.id, slot_id),
            )
        ]
    )
    rows.append([button(BACK, "packs:{}".format(slot_id))])
    await show(
        callback,
        "{}.\n\nУже оплатили — нажмите «Проверить оплату». Ещё нет — "
        "«Перейти к оплате».".format(bill_title(payment)),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


async def next_step_markup(
    repository: Any,
    slot_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """После оплаты — вернуть человека к занятию, ради которого он платил."""
    slot = await find_slot(repository, slot_id) if slot_id else None
    if slot is not None and is_bookable(slot):
        return "", markup(
            [
                button(
                    "Записаться на выбранное занятие: {}".format(
                        format_class_time(slot.starts_at)
                    ),
                    "book:{}:{}".format(slot.class_key, slot.id),
                )
            ],
            [button("Выбрать другое занятие", "slots:all")],
        )
    notice = ""
    if slot_id:
        notice = "Выбранное занятие уже недоступно — выберите другое время."
    return notice, markup([button(BOOK_BUTTON, "slots:all")])


@router.callback_query(F.data.startswith("lesson_payment_check:"))
async def check_lesson_payment(
    callback: CallbackQuery,
    repository: Any,
    payment_gateway: YooKassaClient,
) -> None:
    if callback.from_user is None or callback.message is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    payment_id = parse_int(parts[1]) if len(parts) in (2, 3) else None
    slot_id = (parse_int(parts[2]) or 0) if len(parts) == 3 else 0
    if payment_id is None:
        await callback.answer("Некорректный номер платежа.", show_alert=True)
        return

    payment_service = PaymentService(repository, payment_gateway)
    result = await payment_service.check_payment(payment_id, callback.from_user.id)
    status = result.status

    if status in (
        PaymentCheckStatus.CONFIRMED,
        PaymentCheckStatus.ALREADY_SUCCEEDED,
        PaymentCheckStatus.ALREADY_PROCESSED,
    ):
        credits = result.credits
        if credits is None:
            credits = await repository.get_lesson_credits(callback.from_user.id)
        if status is PaymentCheckStatus.CONFIRMED and result.payment is not None:
            await callback.answer("Оплата подтверждена.")
            headline = "Оплата прошла. Зачислено {}. {}.".format(
                lessons_count(result.payment.lessons), balance_line(credits)
            )
        else:
            await callback.answer("Оплата уже подтверждена.")
            headline = "Оплата уже подтверждена. {}.".format(balance_line(credits))
        notice, reply_markup = await next_step_markup(repository, slot_id)
        text = headline + ("\n\n" + notice if notice else "")
        await callback.message.answer(text, reply_markup=reply_markup)
    elif status is PaymentCheckStatus.NOT_FOUND:
        await callback.answer("Платёж не найден.", show_alert=True)
    elif status in (PaymentCheckStatus.CANCELED, PaymentCheckStatus.PROVIDER_CANCELED):
        await callback.answer(
            "Платёж отменён. Чтобы купить абонемент, выберите его заново "
            "в «💳 Покупки».",
            show_alert=True,
        )
    elif status is PaymentCheckStatus.REFUND_PENDING:
        await callback.answer(
            "Возврат уже обрабатывается. Занятия временно недоступны.",
            show_alert=True,
        )
    elif status is PaymentCheckStatus.PROVIDER_UNAVAILABLE:
        await callback.answer(
            "Не удалось проверить платёж. Попробуйте через минуту.",
            show_alert=True,
        )
    elif status is PaymentCheckStatus.MISMATCH:
        await callback.answer(
            "Данные платежа не совпали. Напишите в «💬 Поддержка».",
            show_alert=True,
        )
    elif status is PaymentCheckStatus.STATUS_CHANGED:
        await callback.answer("Статус платежа изменился.")
        await callback.message.answer(
            "Статус платежа изменился во время проверки. Нажмите «Проверить "
            "оплату» ещё раз через минуту или напишите в «💬 Поддержка»."
        )
    else:
        await callback.answer(
            "Оплата ещё не подтверждена. Если вы уже оплатили, проверьте ещё "
            "раз через минуту.",
            show_alert=True,
        )


@router.message(LessonForm.waiting_for_purchase_confirmation)
async def legacy_purchase_confirmation(message: Message, state: FSMContext) -> None:
    """Защитный обработчик прежнего FSM-подтверждения покупки.

    Раньше любое сообщение в этом состоянии создавало платёж. Новая версия
    в это состояние не переводит; если пользователь всё же в нём (старая
    сессия), сообщение ничего не покупает — состояние сбрасывается, а бот
    повторяет, как оплатить.
    """
    await state.clear()
    await message.answer(
        "Чтобы купить абонемент, откройте «💳 Покупки», выберите вариант "
        "и нажмите кнопку «Оплатить». Сообщение в чате оплату не запускает."
    )
