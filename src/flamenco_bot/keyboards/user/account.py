import hashlib
import hmac
import logging
import secrets
import time
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from . import (
    PROFILE,
    PROFILE_NAME,
    PROFILE_PHONE,
    input_keyboard,
    main_menu_keyboard,
    phone_request_keyboard,
)
from .main_menu import ensure_profile
from ...handlers.states import AccountForm
from ...presentation import balance_line, website_line
from ...runtime.admin_access import get_staff_role
from ...services import InvalidUserNameError, normalize_user_name


logger = logging.getLogger("bot.handlers.account")
router = Router(name="account_keyboard")
PHONE_CODE_TTL_SECONDS = 300
PHONE_CODE_MAX_ATTEMPTS = 5
# Брошенный ввод имени не должен через час превратить случайное сообщение
# в новое имя: по истечении срока ввод считается отменённым.
NAME_INPUT_TTL_SECONDS = 15 * 60


async def profile_text(message: Message, repository: Any) -> str:
    profile = await ensure_profile(message, repository)
    return ("Профиль\nИмя: {}\nТелефон: {}\n{}.{}").format(
        profile.user_name,
        profile.phone or "не указан",
        balance_line(profile.lesson_credits),
        "\n\n" + website_line("Личный кабинет") if website_line() else "",
    )


async def _menu(message: Message, repository: Any):
    """После ввода имени/телефона — снова главное меню вместо поля ввода."""
    return main_menu_keyboard(role=await get_staff_role(message, repository))


@router.message(F.text == PROFILE)
async def open_profile(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    """Профиль: сводка (баланс, абонементы, ближайшие занятия) и быстрые
    действия inline-кнопками — см. ``cabinet.profile_screen``."""
    from .cabinet import profile_screen

    await state.clear()
    profile = await ensure_profile(message, repository)
    text, reply_markup = await profile_screen(repository, profile.telegram_id)
    await message.answer(text, reply_markup=reply_markup)
    logger.info(
        "Opened profile telegram_id=%s",
        message.from_user.id if message.from_user else None,
    )


@router.message(F.text == PROFILE_PHONE)
async def request_phone(message: Message, state: FSMContext) -> None:
    await state.set_state(AccountForm.waiting_for_phone)
    await message.answer(
        "Чтобы изменить телефон, отправьте свой контакт кнопкой ниже. "
        "После этого бот пришлёт одноразовый код подтверждения в этот Telegram-чат.",
        reply_markup=phone_request_keyboard(),
    )
    logger.info(
        "Started phone update telegram_id=%s",
        message.from_user.id if message.from_user else None,
    )


@router.message(F.text == PROFILE_NAME)
async def request_name(message: Message, state: FSMContext) -> None:
    await state.set_state(AccountForm.waiting_for_name)
    await state.update_data(name_input_started_at=time.time())
    await message.answer(
        "Введите имя для профиля (до 64 символов).",
        reply_markup=input_keyboard(),
    )
    logger.info(
        "Started profile name update telegram_id=%s",
        message.from_user.id if message.from_user else None,
    )


@router.message(AccountForm.waiting_for_phone)
async def save_phone(
    message: Message,
    state: FSMContext,
) -> None:
    sender = message.from_user
    contact = message.contact
    if sender is None or contact is None or contact.user_id != sender.id:
        await message.answer(
            "Нужно отправить именно свой контакт кнопкой «📲 Отправить мой номер». "
            "Передумали — нажмите «❌ Отмена»."
        )
        logger.warning(
            "Rejected unverified phone update telegram_id=%s",
            sender.id if sender else None,
        )
        return

    code = "{:06d}".format(secrets.randbelow(1_000_000))
    await state.update_data(
        pending_phone=contact.phone_number,
        phone_code_hash=hashlib.sha256(code.encode("ascii")).hexdigest(),
        phone_code_expires_at=time.time() + PHONE_CODE_TTL_SECONDS,
        phone_code_attempts=0,
    )
    await state.set_state(AccountForm.waiting_for_phone_code)
    await message.answer(
        "Одноразовый код для подтверждения действия в этом Telegram-чате: {}. "
        "Он действует 5 минут. Введите код ответным сообщением.".format(code),
        reply_markup=input_keyboard(),
    )
    logger.info("Sent phone verification code telegram_id=%s", sender.id)


@router.message(AccountForm.waiting_for_phone_code)
async def verify_phone_code(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    sender = message.from_user
    if sender is None:
        raise ValueError("У сообщения отсутствует Telegram-пользователь")

    challenge = await state.get_data()
    expires_at = challenge.get("phone_code_expires_at")
    if not expires_at or time.time() >= expires_at:
        await state.clear()
        logger.warning("Phone verification expired telegram_id=%s", sender.id)
        await message.answer(
            "Срок действия кода истёк. Чтобы изменить телефон, откройте "
            "«👤 Профиль» → «📱 Телефон» ещё раз.",
            reply_markup=await _menu(message, repository),
        )
        return

    code = (message.text or "").strip()
    expected_hash = challenge.get("phone_code_hash", "")
    supplied_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    if (
        not code.isdigit()
        or len(code) != 6
        or not hmac.compare_digest(
            supplied_hash,
            expected_hash,
        )
    ):
        attempts = challenge.get("phone_code_attempts", 0) + 1
        if attempts >= PHONE_CODE_MAX_ATTEMPTS:
            await state.clear()
            await message.answer(
                "Лимит попыток исчерпан. Чтобы изменить телефон, откройте "
                "«👤 Профиль» → «📱 Телефон» ещё раз.",
                reply_markup=await _menu(message, repository),
            )
            logger.warning(
                "Phone verification attempts exhausted telegram_id=%s",
                sender.id,
            )
            return

        await state.update_data(phone_code_attempts=attempts)
        logger.warning(
            "Phone verification failed telegram_id=%s attempts=%s",
            sender.id,
            attempts,
        )
        await message.answer(
            "Код не подошёл. Осталось попыток: {}.".format(
                PHONE_CODE_MAX_ATTEMPTS - attempts
            ),
            reply_markup=input_keyboard(),
        )
        return

    phone = challenge.get("pending_phone")
    if not phone:
        raise ValueError("Для подтверждения телефона отсутствует ожидающий номер")

    await repository.update_phone(sender.id, phone)
    await state.clear()
    await message.answer(
        "Телефон подтверждён и сохранён.\n\n" + await profile_text(message, repository),
        reply_markup=await _menu(message, repository),
    )
    logger.info("Verified and saved phone telegram_id=%s", sender.id)


@router.message(AccountForm.waiting_for_name)
async def save_name(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    sender = message.from_user
    started_at = (await state.get_data()).get("name_input_started_at")
    if not started_at or time.time() - started_at > NAME_INPUT_TTL_SECONDS:
        await state.clear()
        await message.answer(
            "Ввод имени отменён — прошло слишком много времени. Чтобы изменить "
            "имя, откройте «👤 Профиль» → «✏️ Имя» ещё раз.",
            reply_markup=await _menu(message, repository),
        )
        return
    try:
        user_name = normalize_user_name(message.text or "")
    except InvalidUserNameError as error:
        logger.warning(
            "Rejected profile name update telegram_id=%s length=%s",
            message.from_user.id if message.from_user else None,
            len((message.text or "").strip()),
        )
        await message.answer(
            "{}. Введите имя ещё раз или нажмите «❌ Отмена».".format(error)
        )
        return
    if sender is None:
        raise ValueError("У сообщения отсутствует Telegram-пользователь")

    await repository.update_user_name(sender.id, user_name)
    await state.clear()
    await message.answer(
        "Имя сохранено.\n\n" + await profile_text(message, repository),
        reply_markup=await _menu(message, repository),
    )
    logger.info("Saved profile name telegram_id=%s", sender.id)
