import asyncio
import logging
import os
import platform
import threading
from functools import lru_cache
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ..config import Config
from ..runtime.admin_access import get_admin_id, get_staff_role
from ..runtime.logging_utils import create_admin_actions_logger
from ..runtime.bot_lifecycle import RestartController
from .states import AdminForm
from ..keyboards.admin import (
    ADMIN_MENU,
    BOT_STATUS,
    CREDITS_ADJUST_CANCEL,
    CREDITS_ADJUST_CONFIRM,
    admin_action_keyboard,
    admin_menu_keyboard,
    credit_adjustment_confirmation_keyboard,
)
from ..keyboards.common import MAIN_MENU
from ..keyboards.user import main_menu_keyboard
from ..runtime.runtime_resources import get_process_resources
from ..payments import YooKassaClient
from ..services import (
    CreditService,
    InvalidUserNameError,
    PaymentService,
    RefundStatus,
    normalize_user_name,
)
from ..studio_time import format_studio_datetime
from ..database.repository import InsufficientLessonCreditsError
from ..keyboards.user.screens import button, markup
from ..services.profile import normalize_phone


logger = logging.getLogger("bot.handlers.admin")


@lru_cache(maxsize=1)
def actions_logger() -> logging.Logger:
    """Журнал действий администраторов — создаётся при первом действии.

    Не при импорте модуля: открытие файла в /var/log/flamenco при импорте
    роняло бы любой процесс без этого каталога (например, API в контейнере
    с read-only корнем), стоит ему косвенно импортировать этот модуль.
    """
    return create_admin_actions_logger()


router = Router(name="admin_commands")


async def _admin_id(message: Message, repository: Any) -> Optional[int]:
    return await get_admin_id(message, repository)


async def _deny_non_admin(message: Message, repository: Any) -> bool:
    if await _admin_id(message, repository) is not None:
        return False
    await message.answer("Команда доступна только администратору студии.")
    logger.warning(
        "Rejected admin action telegram_id=%s",
        message.from_user.id if message.from_user else None,
    )
    actions_logger().warning(
        "action=denied actor_id=%s",
        message.from_user.id if message.from_user else None,
    )
    return True


@router.message(Command("admin"))
async def admin_command(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    role = await get_staff_role(message, repository)
    if role is None:
        await message.answer("Команда доступна только сотрудникам студии.")
        return

    await state.clear()
    await message.answer(
        "Админ-меню. Выберите действие:",
        reply_markup=admin_menu_keyboard(role),
    )
    logger.info("Opened admin menu telegram_id=%s role=%s", message.from_user.id, role)


@router.message(F.text == ADMIN_MENU)
async def open_admin_menu(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    await admin_command(message, state, repository)


@router.message(F.text == BOT_STATUS)
async def show_bot_status(
    message: Message,
    restart_controller: RestartController,
    repository: Any,
) -> None:
    if await _deny_non_admin(message, repository):
        return
    uptime_seconds = max(0, int(time.monotonic() - restart_controller.started_at))
    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    db_started = time.monotonic()
    statistics = await repository.get_user_statistics(
        datetime.now(timezone.utc) - timedelta(minutes=5)
    )
    db_ms = (time.monotonic() - db_started) * 1000
    memory_mb, cpu_seconds = get_process_resources()
    started_at = datetime.now(timezone.utc) - timedelta(seconds=uptime_seconds)
    lines = [
        "Бот работает.",
        "Время работы: {} ч {} мин {} сек.".format(days * 24 + hours, minutes, seconds),
        "Запущен: {}.".format(format_studio_datetime(started_at)),
        "Режим: {}.".format(
            "приём обновлений идёт"
            if getattr(restart_controller, "polling_started", False)
            else "приём обновлений ещё не начат"
        ),
        "",
        "CPU с запуска: {:.1f} сек.".format(cpu_seconds),
        "Пиковая память: {:.1f} МБ.".format(memory_mb),
        "Задач asyncio: {}.".format(len(asyncio.all_tasks())),
        "Потоков: {}.".format(threading.active_count()),
        "",
        "База данных: отвечает за {:.0f} мс.".format(db_ms),
        "Пользователей всего: {}.".format(statistics.total_users),
        "Активны за 5 минут: {}.".format(statistics.online_users),
        "",
        "Python {} · {} · PID {}.".format(
            platform.python_version(), platform.system(), os.getpid()
        ),
    ]
    bot_info = None
    try:
        bot_info = await message.bot.get_me()
    except Exception:
        logger.debug("Не удалось получить данные бота для статуса", exc_info=True)
    if bot_info is not None and getattr(bot_info, "username", None):
        lines.append("Telegram: @{} (id {}).".format(bot_info.username, bot_info.id))
    await message.answer("\n".join(lines))


@router.message(F.text == MAIN_MENU)
async def leave_admin_menu(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    await state.clear()
    await message.answer(
        "Главное меню студии фламенко.",
        reply_markup=main_menu_keyboard(role=await get_staff_role(message, repository)),
    )


async def start_participant_search(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    if await _deny_non_admin(message, repository):
        return
    await state.set_state(AdminForm.waiting_for_search)
    await message.answer(
        "Введите Telegram ID, имя или часть номера телефона для поиска.",
        reply_markup=admin_action_keyboard(),
    )


@router.message(AdminForm.waiting_for_search)
async def search_participants(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    if await _deny_non_admin(message, repository):
        await state.clear()
        return

    query = (message.text or "").strip()
    if not query or len(query) > 100:
        await message.answer("Введите поисковый запрос длиной от 1 до 100 символов.")
        return

    profiles = await repository.search_profiles(query, limit=20)
    await state.clear()
    if not profiles:
        await message.answer(
            "Участники не найдены.",
            reply_markup=admin_menu_keyboard(),
        )
        actions_logger().info(
            "action=search admin_id=%s query_length=%s results=0",
            await _admin_id(message, repository),
            len(query),
        )
        return

    lines = ["Найденные участники (максимум 20) — нажмите, чтобы открыть карточку:"]
    for profile in profiles:
        lines.append(
            "ID: {} | {} | телефон: {} | баланс: {}".format(
                profile.telegram_id,
                profile.user_name,
                profile.phone or "не указан",
                profile.lesson_credits,
            )
        )
    await message.answer(
        "\n".join(lines),
        reply_markup=markup(
            *(
                [
                    button(
                        "{} · {}".format(profile.user_name[:40], profile.telegram_id),
                        "a:c:{}".format(profile.telegram_id),
                    )
                ]
                for profile in profiles
            )
        ),
    )
    actions_logger().info(
        "action=search admin_id=%s query_length=%s results=%s",
        await _admin_id(message, repository),
        len(query),
        len(profiles),
    )


@router.message(AdminForm.waiting_for_name_value)
async def save_participant_name(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    if await _deny_non_admin(message, repository):
        await state.clear()
        return
    try:
        user_name = normalize_user_name(message.text or "")
    except InvalidUserNameError as error:
        await message.answer("{}.".format(error))
        return

    form_data = await state.get_data()
    telegram_id = form_data.get("target_telegram_id")
    if not isinstance(telegram_id, int):
        await state.clear()
        await message.answer("Не удалось определить участника.")
        return
    if await repository.get_profile(telegram_id) is None:
        await state.clear()
        await message.answer(
            "Участник с таким Telegram ID не найден.",
            reply_markup=admin_menu_keyboard(),
        )
        actions_logger().warning(
            "action=edit_name admin_id=%s target_id=%s result=not_found",
            await _admin_id(message, repository),
            telegram_id,
        )
        return

    await repository.admin_update_profile(
        telegram_id, message.from_user.id, user_name=user_name
    )
    await state.clear()
    await message.answer("Имя участника обновлено.", reply_markup=admin_menu_keyboard())
    actions_logger().info(
        "action=edit_name admin_id=%s target_id=%s",
        await _admin_id(message, repository),
        telegram_id,
    )


@router.message(AdminForm.waiting_for_phone_value)
async def save_participant_phone(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    if await _deny_non_admin(message, repository):
        await state.clear()
        return
    try:
        phone = normalize_phone(message.text or "")
    except ValueError as error:
        await message.answer("{}.".format(error))
        return

    form_data = await state.get_data()
    telegram_id = form_data.get("target_telegram_id")
    if not isinstance(telegram_id, int):
        await state.clear()
        await message.answer("Не удалось определить участника.")
        return
    if await repository.get_profile(telegram_id) is None:
        await state.clear()
        await message.answer(
            "Участник с таким Telegram ID не найден.",
            reply_markup=admin_menu_keyboard(),
        )
        actions_logger().warning(
            "action=edit_phone admin_id=%s target_id=%s result=not_found",
            await _admin_id(message, repository),
            telegram_id,
        )
        return

    await repository.admin_update_profile(
        telegram_id, message.from_user.id, phone=phone
    )
    await state.clear()
    await message.answer(
        "Телефон участника обновлён.",
        reply_markup=admin_menu_keyboard(),
    )
    actions_logger().info(
        "action=edit_phone admin_id=%s target_id=%s",
        await _admin_id(message, repository),
        telegram_id,
    )


async def _process_lesson_refund(
    message: Message,
    payment_id: int,
    repository: Any,
    payment_gateway: YooKassaClient,
    admin_id: int,
    reason: Optional[str] = None,
) -> None:
    payment_service = PaymentService(repository, payment_gateway)
    result = await payment_service.process_refund(payment_id, admin_id, reason)

    if result.status is RefundStatus.INVALID:
        await message.answer(result.error_message)
    elif result.status is RefundStatus.UNAVAILABLE:
        await message.answer(
            "Возврат недоступен: платёж не найден или не находится в статусе "
            "успешной оплаты."
        )
    elif result.status is RefundStatus.PROVIDER_UNAVAILABLE:
        await message.answer(
            "Не удалось подтвердить результат возврата в ЮKassa. Баланс занятий "
            "зарезервирован; повторите /refund_check {} для сверки.".format(payment_id)
        )
    elif result.status is RefundStatus.MISMATCH:
        await message.answer(
            "Данные возврата не совпали. Баланс зарезервирован; требуется "
            "ручная сверка с ЮKassa."
        )
    elif result.status is RefundStatus.COMPLETED:
        await message.answer(
            "Возврат подтверждён. Списание {} неиспользованных занятий "
            "зафиксировано.".format(result.lessons)
        )
        actions_logger().warning(
            "action=refund payment_id=%s admin_id=%s refund_id=%s",
            payment_id,
            admin_id,
            result.provider_refund_id,
        )
    elif result.status is RefundStatus.ALREADY_PROCESSED:
        await message.answer(
            "ЮKassa подтвердила возврат, но локальная запись уже обработана. "
            "Проверьте аудит платежа."
        )
    elif result.status is RefundStatus.PENDING:
        await message.answer(
            "ЮKassa приняла возврат, он ещё обрабатывается. Повторите "
            "/refund_check {} позже.".format(payment_id)
        )
    elif result.status is RefundStatus.RELEASED:
        await message.answer(
            "ЮKassa отменила возврат. Резерв занятий снят, баланс восстановлен. "
            "При необходимости можно повторить /refund {} причина.".format(payment_id)
        )
    else:
        await message.answer(
            "ЮKassa вернула статус «{}». Баланс остаётся зарезервированным; "
            "требуется сверка с провайдером.".format(result.provider_status)
        )


@router.message(Command("refund"))
async def refund_lesson_payment(
    message: Message,
    repository: Any,
    payment_gateway: YooKassaClient,
) -> None:
    admin_id = await _admin_id(message, repository)
    if admin_id is None:
        await message.answer("Команда доступна только администратору студии.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].strip():
        await message.answer("Формат команды: /refund ID причина")
        return
    await _process_lesson_refund(
        message,
        int(parts[1]),
        repository,
        payment_gateway,
        admin_id,
        parts[2],
    )


@router.message(Command("refund_check"))
async def check_lesson_refund(
    message: Message,
    repository: Any,
    payment_gateway: YooKassaClient,
) -> None:
    admin_id = await _admin_id(message, repository)
    if admin_id is None:
        await message.answer("Команда доступна только администратору студии.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат команды: /refund_check ID")
        return
    await _process_lesson_refund(
        message,
        int(parts[1]),
        repository,
        payment_gateway,
        admin_id,
    )


# Подтверждение корректировки баланса действует недолго: не применять
# «забытую» операцию через часы, когда баланс уже мог измениться.
CREDIT_ADJUSTMENT_CONFIRMATION_TTL_SECONDS = 600
CREDIT_ADJUSTMENT_FORMAT = "Формат: +N причина или -N причина, например «+1 бонус»."
CREDIT_AUDIT_LIMIT = 20


def _credit_service(repository: Any) -> CreditService:
    return CreditService(repository, Config.CREDIT_ADJUSTMENT_MAX_DELTA)


async def prepare_credit_adjustment(
    message: Message,
    state: FSMContext,
    repository: Any,
    telegram_id: int,
    delta: int,
    reason_text: str,
) -> None:
    """Предпросмотр корректировки и ожидание подтверждения кнопкой.

    Шаг кнопки «± Баланс» в карточке участника; сама операция —
    ``confirm_credit_adjustment``.
    """
    service = _credit_service(repository)
    try:
        reason = service.validate_adjustment(delta, reason_text)
    except ValueError as error:
        await message.answer("{}.\n{}".format(error, CREDIT_ADJUSTMENT_FORMAT))
        return
    profile = await repository.get_profile(telegram_id)
    if profile is None:
        await message.answer("Участник с таким Telegram ID не найден.")
        return
    if profile.lesson_credits + delta < 0:
        await message.answer(
            "Баланс не может стать отрицательным: у участника {} занятий, "
            "списать {} нельзя.".format(profile.lesson_credits, -delta)
        )
        return
    await state.set_state(AdminForm.waiting_for_credit_adjustment_confirmation)
    await state.update_data(
        credit_adjustment={
            "telegram_id": telegram_id,
            "delta": delta,
            "reason": reason,
            "key": str(uuid.uuid4()),
            "expires_at": time.time() + CREDIT_ADJUSTMENT_CONFIRMATION_TTL_SECONDS,
        }
    )
    await message.answer(
        "Корректировка баланса\n"
        "Участник: {} (ID {})\n"
        "Изменение: {:+d}\n"
        "Баланс: {} → {}\n"
        "Причина: {} (её увидит участник в истории операций)\n\n"
        "Применить? Подтверждение действует {} мин.".format(
            profile.user_name,
            telegram_id,
            delta,
            profile.lesson_credits,
            profile.lesson_credits + delta,
            reason,
            CREDIT_ADJUSTMENT_CONFIRMATION_TTL_SECONDS // 60,
        ),
        reply_markup=credit_adjustment_confirmation_keyboard(),
    )


@router.message(F.text == CREDITS_ADJUST_CONFIRM)
async def confirm_credit_adjustment(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    if await _deny_non_admin(message, repository):
        await state.clear()
        return
    admin_id = await _admin_id(message, repository)
    pending = (await state.get_data()).get("credit_adjustment")
    if (
        await state.get_state()
        != AdminForm.waiting_for_credit_adjustment_confirmation.state
        or pending is None
    ):
        await message.answer(
            "Нет корректировки для подтверждения. Начните с «± Баланс» в "
            "карточке участника.",
            reply_markup=admin_menu_keyboard(),
        )
        return
    if time.time() > pending["expires_at"]:
        await state.clear()
        await message.answer(
            "Время подтверждения истекло. Повторите «± Баланс» в карточке участника.",
            reply_markup=admin_menu_keyboard(),
        )
        return
    try:
        result = await _credit_service(repository).adjust(
            pending["telegram_id"],
            pending["delta"],
            pending["reason"],
            admin_id,
            uuid.UUID(pending["key"]),
        )
    except InsufficientLessonCreditsError as error:
        # Баланс успел измениться после предпросмотра: списание отклонено.
        await state.clear()
        await message.answer(
            "{}. Баланс не изменён.".format(error),
            reply_markup=admin_menu_keyboard(),
        )
        return
    except (LookupError, ValueError, PermissionError) as error:
        await state.clear()
        await message.answer(
            "Корректировка не выполнена: {}.".format(error),
            reply_markup=admin_menu_keyboard(),
        )
        return
    # Прочие сбои (например, БД недоступна) оставляют подтверждение в силе:
    # повторное нажатие использует тот же ключ и не удвоит корректировку.
    await state.clear()
    if result.applied:
        text = "Готово: баланс участника {} изменён на {:+d}, теперь {}.".format(
            result.telegram_id, result.delta, result.balance
        )
    else:
        text = (
            "Эта корректировка уже была применена ранее. Баланс участника {}: {}."
        ).format(result.telegram_id, result.balance)
    await message.answer(text, reply_markup=admin_menu_keyboard())
    actions_logger().warning(
        "action=adjust_credits admin_id=%s target_id=%s delta=%s ledger_id=%s "
        "applied=%s",
        admin_id,
        result.telegram_id,
        result.delta,
        result.ledger_id,
        result.applied,
    )


@router.message(F.text == CREDITS_ADJUST_CANCEL)
async def cancel_credit_adjustment(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    if await _deny_non_admin(message, repository):
        await state.clear()
        return
    await state.clear()
    await message.answer(
        "Корректировка отменена, баланс не изменён.",
        reply_markup=admin_menu_keyboard(),
    )


@router.message(AdminForm.waiting_for_credit_adjustment_confirmation)
async def require_credit_adjustment_confirmation(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    if await _deny_non_admin(message, repository):
        await state.clear()
        return
    await message.answer(
        "Нажмите «Применить корректировку» или отмените действие.",
        reply_markup=credit_adjustment_confirmation_keyboard(),
    )


def _format_credit_mismatch(mismatch: Any) -> str:
    line = "{} | {} | баланс {} | ledger {} | разница {:+d} | записей {}".format(
        mismatch.telegram_id,
        mismatch.user_name,
        mismatch.lesson_credits,
        mismatch.ledger_total,
        mismatch.difference,
        mismatch.ledger_entries,
    )
    if mismatch.ledger_entries == 0:
        return line + " | в ledger нет записей"
    return line + " | последняя запись {}".format(
        format_studio_datetime(mismatch.last_entry_at)
    )


@router.message(Command("credits_audit"))
async def audit_credit_balances(message: Message, repository: Any) -> None:
    admin_id = await _admin_id(message, repository)
    if admin_id is None:
        await _deny_non_admin(message, repository)
        return
    service = _credit_service(repository)
    result = await service.reconcile(CREDIT_AUDIT_LIMIT)
    if not result.mismatched_users:
        await message.answer(
            "Сверка баланса: расхождений нет (проверено участников: {}).".format(
                result.checked_users
            )
        )
        return
    lines = [
        "Сверка баланса: расхождения у {} из {} участников"
        " (показаны первые {}).".format(
            result.mismatched_users, result.checked_users, len(result.mismatches)
        ),
        "ID | имя | баланс | сумма ledger | разница | записей ledger",
    ]
    lines.extend(_format_credit_mismatch(item) for item in result.mismatches)
    lines.append(
        "Баланс автоматически не исправляется. Если записей в ledger нет, "
        "баланс мог появиться до введения ledger."
    )
    await message.answer("\n".join(lines))
    actions_logger().info(
        "action=audit_credits admin_id=%s mismatched=%s",
        admin_id,
        result.mismatched_users,
    )
