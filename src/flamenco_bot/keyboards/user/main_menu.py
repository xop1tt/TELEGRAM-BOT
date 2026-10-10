import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ...class_catalog import (
    ACTIVE_CLASS_KEYS,
    CLASS_DESCRIPTIONS,
    CLASS_KEYS_BY_LABEL,
    CLASS_LABELS,
    CLASS_LEVELS,
)
from ...payments import PURCHASE_OPTIONS, PURCHASE_PACKAGES
from ...presentation import (
    balance_line,
    deadline_text,
    format_class_time,
    format_price,
    validity_text,
    website_line,
)
from ...runtime.admin_access import get_staff_role
from . import (
    ABOUT,
    BOOK,
    CANCEL,
    HELP,
    LEGACY_TO_SECTION,
    MAIN_MENU,
    MY_CLASSES,
    PACKAGES,
    PROFILE,
    PROFILE_PHONE,
    PURCHASES,
    main_menu_keyboard,
)
from .navigation import cancel_current_action
from .screens import (
    BOOK_BUTTON,
    button,
    markup,
    my_classes_screen,
    package_screen,
    packages_view,
    slots_screen,
    upcoming_bookings,
)


logger = logging.getLogger("bot.handlers.menu")
router = Router(name="main_menu_keyboard")


async def ensure_profile(message: Message, repository: Any) -> Any:
    sender = message.from_user
    if sender is None:
        raise ValueError("У сообщения отсутствует Telegram-пользователь")

    user_name = sender.full_name.strip() or "Участник студии"
    return await repository.get_or_create_profile(
        telegram_id=sender.id,
        user_name=user_name,
    )


async def account_summary(repository: Any, telegram_id: int) -> str:
    """Короткая сводка состояния аккаунта: баланс и ближайшее занятие."""
    credits = await repository.get_lesson_credits(telegram_id)
    lines = [balance_line(credits) + "."]
    upcoming = upcoming_bookings(
        await repository.list_bookings_for_telegram_id(telegram_id)
    )
    if upcoming:
        nearest = upcoming[0]
        lines.append(
            "Ближайшее занятие: {} — {}.".format(
                format_class_time(nearest.starts_at), CLASS_LABELS[nearest.class_key]
            )
        )
    return "\n".join(lines)


async def send_main_menu(message: Message, repository: Any, text: str) -> None:
    sender = message.from_user
    summary = await account_summary(repository, sender.id) if sender else ""
    await message.answer(
        "{}\n\n{}".format(text, summary) if summary else text,
        reply_markup=main_menu_keyboard(role=await get_staff_role(message, repository)),
    )


def about_text() -> str:
    directions = "\n\n".join(
        "{}\n{}\nУровень: {}.".format(
            CLASS_LABELS[key], CLASS_DESCRIPTIONS[key], CLASS_LEVELS[key].lower()
        )
        for key in ACTIVE_CLASS_KEYS
    )
    prices = "\n".join(
        "• {} — {}{}".format(
            package.title,
            format_price(package.price_rub),
            ", действует {}".format(validity_text(package.validity_days))
            if package.validity_days
            else "",
        )
        for package in PURCHASE_PACKAGES.values()
    )
    return (
        "Mirada Studio — студия фламенко для тех, кто хочет танцевать в своём "
        "темпе: от первого урока до сцены.\n\n"
        "Направления\n\n{}\n\n"
        "Стоимость\n{}\n\n"
        "Как записаться\n"
        "1. Купите абонемент в «💳 Покупки» — занятия появятся на балансе.\n"
        "2. В «📅 Расписание» выберите время — с баланса спишется 1 занятие.\n"
        "3. Отменить запись можно не позднее чем за {deadline} до начала — "
        "занятие вернётся на баланс. Если студия перенесла занятие, запись "
        "можно отменить до нового начала; если отменила — занятие вернётся "
        "на баланс автоматически.\n\n"
        "Первое занятие\n"
        "Приходите за 10–15 минут до начала — познакомимся, расскажем, как "
        "проходит занятие, поможем с первыми движениями. Никакой специальной "
        "подготовки не нужно.\n\n"
        "Этот же аккаунт работает и на сайте студии — вход через Telegram."
        "{}"
    ).format(
        directions,
        prices,
        "\n" + website_line() if website_line() else "",
        deadline=deadline_text(),
    )


@router.message(F.text == MAIN_MENU)
async def show_main_menu(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    await state.clear()
    await send_main_menu(message, repository, "Главное меню.")


@router.message(F.text == BOOK)
async def open_booking(message: Message, state: FSMContext, repository: Any) -> None:
    await state.clear()
    profile = await ensure_profile(message, repository)
    text, reply_markup = await slots_screen(repository, profile.telegram_id)
    await message.answer(text, reply_markup=reply_markup)


@router.message(F.text == MY_CLASSES)
async def open_my_classes(message: Message, state: FSMContext, repository: Any) -> None:
    await state.clear()
    profile = await ensure_profile(message, repository)
    text, reply_markup = await my_classes_screen(repository, profile.telegram_id)
    await message.answer(text, reply_markup=reply_markup)


@router.message(F.text == PACKAGES)
async def open_packages(message: Message, state: FSMContext, repository: Any) -> None:
    """«🎟 Абонементы»: действующие абонементы и остатки."""
    from .cabinet import my_packages_screen

    await state.clear()
    profile = await ensure_profile(message, repository)
    text, reply_markup = await my_packages_screen(repository, profile.telegram_id)
    await message.answer(text, reply_markup=reply_markup)


@router.message(F.text == PURCHASES)
async def open_purchases(message: Message, state: FSMContext, repository: Any) -> None:
    """«💳 Покупки»: каталог, неоплаченные счета, история покупок."""
    await state.clear()
    profile = await ensure_profile(message, repository)
    text, reply_markup = await packages_view(repository, profile.telegram_id)
    await message.answer(text, reply_markup=reply_markup)


@router.message(F.text == ABOUT)
async def show_about(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        about_text(),
        reply_markup=markup(
            [button(BOOK_BUTTON, "slots:all"), button("💳 Купить абонемент", "packs:0")]
        ),
    )


@router.message(F.text == CANCEL)
async def cancel_from_menu(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    await cancel_current_action(message, state, repository)


# ---------- Кнопки прежней версии меню ----------

LEGACY_LABELS = (
    set(LEGACY_TO_SECTION) | set(CLASS_KEYS_BY_LABEL) | set(PURCHASE_OPTIONS)
)


@router.message(F.text.in_(LEGACY_LABELS))
async def open_legacy_section(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    """Старая reply-клавиатура → соответствующий новый раздел.

    Ни одна старая кнопка не выполняет действие с деньгами или записью:
    пакет открывает экран с кнопкой «Оплатить», направление — список
    занятий. Заодно пользователь получает новую клавиатуру.
    """
    label = message.text or ""
    await state.clear()
    target = LEGACY_TO_SECTION.get(label)
    if target == PROFILE:
        from .account import open_profile

        await open_profile(message, state, repository)
        return
    if target == PROFILE_PHONE:
        from .account import request_phone

        await request_phone(message, state)
        return
    if target == HELP:
        from ...handlers.support import start_support

        await start_support(message, state)
        return

    await send_main_menu(message, repository, "Меню бота обновилось — разделы внизу.")
    profile = await ensure_profile(message, repository)
    telegram_id = profile.telegram_id
    if target == BOOK:
        text, reply_markup = await slots_screen(repository, telegram_id)
    elif target == MY_CLASSES:
        text, reply_markup = await my_classes_screen(repository, telegram_id)
    elif target == PACKAGES:
        from .cabinet import my_packages_screen

        text, reply_markup = await my_packages_screen(repository, telegram_id)
    elif target == PURCHASES:
        text, reply_markup = await packages_view(repository, telegram_id)
    elif target == ABOUT:
        await show_about(message, state)
        return
    elif label in CLASS_KEYS_BY_LABEL:
        text, reply_markup = await slots_screen(
            repository, telegram_id, CLASS_KEYS_BY_LABEL[label]
        )
    elif label in PURCHASE_OPTIONS:
        text, reply_markup = package_screen(PURCHASE_OPTIONS[label])
    else:
        return
    await message.answer(text, reply_markup=reply_markup)
