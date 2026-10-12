"""Админ-панель в боте: расписание, клиенты, поддержка, финансы.

Четыре раздела админ-меню, внутри — карточки с inline-кнопками вместо
длинного списка команд. Каждое нажатие заново проверяет права по
``bot_users.is_admin`` (личный чат), а опасные действия (отмена и перенос
занятия, выдача/отзыв абонемента, корректировка баланса) выполняются только
после экрана подтверждения; подтверждение хранится в FSM и действует
ограниченное время. Бизнес-логика — в сервисах (``ScheduleService``,
``PackageService``, ``CreditService``, ``SupportService``).

callback_data (префикс ``a:``, ≤ 64 байт):

  a:sch                расписание            a:new / a:new:<class>   создать
  a:s:<id>             карточка занятия      a:newok                 подтвердить
  a:sp:<id>            участники             a:sm:<id> / a:smok:<id> перенос
  a:sc:<id> / a:so:<id> закрыть / открыть    a:sx:<id> / a:sxr:<id> / a:sxok:<id>
  a:scap:<id>          вместимость                                  отмена
  a:rp:<n> / a:tp:<tid> серия и преподаватель при создании
  a:cw / a:cw:<0|1> / a:cwn:<0|1>:<n> / a:cwok  скопировать неделю
  a:bk:<id> / a:bkc:<id>:<tid> / a:bkok:<id>:<tid>  записать участника
  a:bkx:<id>:<tid> / a:bkxok:<id>:<tid>              отменить его запись
  a:cli                поиск клиента         a:c:<tid>               карточка
  a:cb:<tid>:<offset>  занятия клиента       a:cp:<tid>              абонементы
  a:cpay:<tid>         покупки               a:co:<tid>[:<смещ.>]   операции
  a:ct:<tid>           обращения             a:cg:<tid> / a:cgp:<tid>:<pkg> / a:cgok
  a:gr:<grant> / a:grok  отзыв выдачи        a:ca:<tid>              ± баланс
  a:cn:<tid> / a:cph:<tid>  имя / телефон
  a:tk                 открытые обращения    a:t:<id>                переписка
  a:tr:<id> / a:tc:<id> / a:to:<id>  ответить / закрыть / открыть снова
  a:fin                финансы               a:fl[:<смещение>] / a:fa[:<смещение>] / a:fr
"""

import logging
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ..class_catalog import ACTIVE_CLASS_KEYS, CLASS_LABELS, CLASS_SHORT_LABELS
from ..config import Config
from ..database.repository import (
    STAFF_ADMIN,
    STAFF_TEACHER,
    BookingCooldownError,
    BookingNotFoundError,
    CancellationWindowExpiredError,
    InsufficientLessonCreditsError,
    SlotUnavailableError,
)
from ..database.studio_models import (
    PackageGrantNotFoundError,
    SlotNotFoundError,
    SlotStateError,
    SupportTicketStateError,
    normalize_reason,
)
from ..keyboards.admin import (
    ADMIN_CLIENTS,
    ADMIN_FINANCE,
    ADMIN_SCHEDULE,
    SUPPORT_TICKETS,
    admin_action_keyboard,
    admin_input_keyboard,
)
from ..keyboards.user.screens import button, markup, parse_int, show
from ..payments import PURCHASE_PACKAGES
from ..presentation import (
    balance_line,
    deadline_text,
    format_amount,
    format_class_time,
    format_date,
    lessons_count,
    package_line,
)
from ..runtime.admin_access import get_admin_id, get_staff_id
from ..services import (
    BookingService,
    CreditService,
    HistoryService,
    PackageService,
    ProfileService,
    ScheduleService,
    SupportService,
    catalog_package,
)
from ..services.audit import AUDIT_ACTION_LABELS
from ..services.schedule import shift_weeks
from ..services.history import BOOKING_STATUS_LABELS, booking_status
from ..studio_time import format_studio_datetime, parse_admin_datetime, to_studio_time
from .admin import (
    CREDIT_AUDIT_LIMIT,
    _format_credit_mismatch,
    actions_logger,
    prepare_credit_adjustment,
)
from .states import AdminForm
from ..services.support import close_ticket_and_notify
from .support import deliver_support_reply


logger = logging.getLogger("bot.handlers.admin_panel")
router = Router(name="admin_panel")

# Подтверждение опасного действия действует столько секунд.
CONFIRMATION_TTL_SECONDS = 600
PAGE_SIZE = 10
LOG_PAGE_SIZE = 7  # операции и журнал действий: по 7 записей на странице
_ADJUSTMENT = re.compile(r"^([+-]\d{1,9})\s+(.+)$", re.DOTALL)
SLOT_STATUS_LABELS = {
    "open": "открыто для записи",
    "closed": "запись закрыта",
    "cancelled": "отменено студией",
}
PAYMENT_STATUS_LABELS = {
    "pending": "ожидает оплаты",
    "succeeded": "оплачен",
    "canceled": "отменён",
    "refund_pending": "возврат в обработке",
    "refunded": "возвращён",
}


# ---------- доступ и подтверждения ----------


async def callback_admin_id(callback: CallbackQuery, repository: Any) -> Optional[int]:
    message = callback.message
    chat = getattr(message, "chat", None)
    if message is None or getattr(chat, "type", None) != "private":
        return None
    profile = await repository.get_profile(callback.from_user.id)
    if profile is None or not profile.is_admin:
        return None
    return callback.from_user.id


async def _admin(callback: CallbackQuery, repository: Any) -> Optional[int]:
    admin_id = await callback_admin_id(callback, repository)
    if admin_id is None:
        await callback.answer("Доступно только администратору студии.", show_alert=True)
        logger.warning(
            "Rejected admin panel action telegram_id=%s data=%s",
            callback.from_user.id,
            (callback.data or "")[:20],
        )
        actions_logger().warning("action=denied actor_id=%s", callback.from_user.id)
    return admin_id


async def callback_staff_role(
    callback: CallbackQuery, repository: Any
) -> Optional[str]:
    message = callback.message
    chat = getattr(message, "chat", None)
    if message is None or getattr(chat, "type", None) != "private":
        return None
    profile = await repository.get_profile(callback.from_user.id)
    return profile.role if profile is not None else None


async def _staff(
    callback: CallbackQuery, repository: Any, slot_id: Optional[int] = None
) -> Optional[int]:
    """Сотрудник (администратор или преподаватель) — расписание и поддержка.

    С ``slot_id`` преподаватель проходит только для своего занятия.
    """
    role = await callback_staff_role(callback, repository)
    telegram_id = callback.from_user.id
    if role is None:
        await callback.answer("Доступно только сотрудникам студии.", show_alert=True)
        actions_logger().warning("action=denied actor_id=%s", telegram_id)
        return None
    if (
        slot_id is not None
        and role != STAFF_ADMIN
        and not await repository.is_slot_teacher(slot_id, telegram_id)
    ):
        await callback.answer(
            "Это занятие ведёт другой преподаватель.", show_alert=True
        )
        return None
    return telegram_id


async def _set_pending(state: FSMContext, **action: Any) -> None:
    action["expires_at"] = time.time() + CONFIRMATION_TTL_SECONDS
    await state.set_state(None)
    await state.update_data(admin_pending=action)


async def _take_pending(
    state: FSMContext, action: str, **expected: Any
) -> Optional[dict]:
    """Действие из экрана подтверждения, если оно совпадает и не устарело.

    Не удаляет его: если операция упадёт (например, БД недоступна), повторное
    нажатие использует тот же ключ идемпотентности.
    """
    pending = (await state.get_data()).get("admin_pending")
    if (
        not pending
        or pending.get("action") != action
        or time.time() > pending.get("expires_at", 0)
        or any(pending.get(key) != value for key, value in expected.items())
    ):
        return None
    return pending


async def _clear_pending(state: FSMContext) -> None:
    data = await state.get_data()
    if "admin_pending" in data:
        await state.update_data(admin_pending=None)


async def _expired(callback: CallbackQuery) -> None:
    await callback.answer(
        "Подтверждение устарело — откройте карточку и повторите действие.",
        show_alert=True,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- расписание ----------


def slot_button_text(slot: Any) -> str:
    if slot.status == "cancelled":
        mark = "❌ "
    elif slot.starts_at <= _now():
        mark = "▶️ "
    elif slot.status == "closed":
        mark = "🔒 "
    else:
        mark = ""
    return "{}{} · {} · {}/{}".format(
        mark,
        format_class_time(slot.starts_at),
        CLASS_SHORT_LABELS[slot.class_key],
        slot.booked_count,
        slot.capacity,
    )


async def schedule_screen(
    repository: Any, viewer_id: Optional[int] = None, notice: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    """Расписание админки; преподаватель (``viewer_id`` с ролью teacher)
    видит только свои занятия и не создаёт новые."""
    role = await repository.get_staff_role(viewer_id) if viewer_id is not None else None
    is_teacher = role is not None and role != STAFF_ADMIN
    slots = await ScheduleService(repository).admin_schedule(
        limit=25, teacher_telegram_id=viewer_id if is_teacher else None
    )
    lines = [notice, ""] if notice else []
    lines += [
        "🗓 Мои занятия" if is_teacher else "🗓 Расписание занятий",
        "Нажмите на занятие: участники, перенос, отмена, закрытие записи.",
        "❌ — отменено, 🔒 — запись закрыта, ▶️ — уже идёт.",
    ]
    if not slots:
        lines.extend(["", "Будущих занятий пока нет."])
    rows = [
        [button(slot_button_text(slot), "a:s:{}".format(slot.id))] for slot in slots
    ]
    if not is_teacher:
        rows.append(
            [
                button("➕ Создать занятие", "a:new"),
                button("📋 Скопировать неделю", "a:cw"),
            ]
        )
    return "\n".join(lines), markup(*rows)


async def slot_card(
    repository: Any, slot_id: int, notice: str = "", is_admin: bool = False
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    service = ScheduleService(repository)
    slot = await service.get(slot_id)
    if slot is None:
        return None
    participants = await service.participants(slot_id)
    started = slot.starts_at <= _now()
    status = SLOT_STATUS_LABELS[slot.status]
    if started and slot.status != "cancelled":
        status = "уже началось"
    lines = [notice, ""] if notice else []
    lines.extend(
        [
            "Занятие №{} — {}".format(slot.id, CLASS_LABELS[slot.class_key]),
            format_studio_datetime(slot.starts_at),
            "Записано: {} из {}".format(slot.booked_count, slot.capacity),
            "Статус: {}".format(status),
        ]
    )
    if slot.teacher_name:
        lines.append("Преподаватель: {}".format(slot.teacher_name))
    if slot.status == "cancelled" and slot.cancel_reason:
        lines.append("Причина отмены: {}".format(slot.cancel_reason))
    if slot.rescheduled_at is not None:
        lines.append(
            "Перенесено студией {}".format(format_studio_datetime(slot.rescheduled_at))
        )
    if participants:
        notes = await repository.latest_notes_for_slot(slot_id)
        lines.extend(["", "Участники:"])
        lines.extend(
            "• {} (ID {}){}{}".format(
                participant.user_name,
                participant.telegram_id,
                ", " + participant.phone if participant.phone else "",
                "\n  📝 " + notes[participant.telegram_id][:120]
                if participant.telegram_id in notes
                else "",
            )
            for participant in participants[:20]
        )
        if len(participants) > 20:
            lines.append("… и ещё {}".format(len(participants) - 20))
    rows = []
    if slot.status != "cancelled" and not started:
        rows.append(
            [
                button("🕒 Перенести", "a:sm:{}".format(slot.id)),
                button("❌ Отменить занятие", "a:sx:{}".format(slot.id)),
            ]
        )
        rows.append(
            [
                button("🔒 Закрыть запись", "a:sc:{}".format(slot.id))
                if slot.status == "open"
                else button("🔓 Открыть запись", "a:so:{}".format(slot.id)),
                button("👥 Вместимость", "a:scap:{}".format(slot.id)),
            ]
        )
    if is_admin and slot.status == "open" and not started:
        rows.append([button("➕ Записать участника", "a:bk:{}".format(slot.id))])
    if slot.status != "cancelled" and slot.booked_count > 0:
        rows.append([button("📣 Написать участникам", "b:t:{}".format(slot.id))])
    rows.append([button("📋 Все записи и отмены", "a:sp:{}".format(slot.id))])
    rows.append([button("⬅️ К расписанию", "a:sch")])
    return "\n".join(lines), markup(*rows)


async def _show_slot(callback: CallbackQuery, repository: Any, slot_id: int, notice=""):
    is_admin = await callback_staff_role(callback, repository) == STAFF_ADMIN
    screen = await slot_card(repository, slot_id, notice, is_admin)
    if screen is None:
        await callback.answer("Занятие не найдено.", show_alert=True)
        return
    await show(callback, *screen)


@router.message(F.text == ADMIN_SCHEDULE)
@router.message(Command("slots"))
async def open_schedule(message: Message, state: FSMContext, repository: Any) -> None:
    viewer_id = await get_staff_id(message, repository)
    if viewer_id is None:
        await message.answer("Команда доступна только сотрудникам студии.")
        return
    await state.clear()
    text, reply_markup = await schedule_screen(repository, viewer_id)
    await message.answer(text, reply_markup=reply_markup)


@router.message(Command("slot"))
async def open_slot_command(message: Message, repository: Any) -> None:
    if await get_staff_id(message, repository) is None:
        await message.answer("Команда доступна только сотрудникам студии.")
        return
    parts = (message.text or "").split()
    slot_id = parse_int(parts[1]) if len(parts) == 2 else None
    if slot_id and await get_staff_id(message, repository, slot_id) is None:
        await message.answer("Это занятие ведёт другой преподаватель.")
        return
    screen = await slot_card(repository, slot_id) if slot_id else None
    if screen is None:
        await message.answer("Формат: /slot ID (ID — из «🗓 Расписание занятий»).")
        return
    await message.answer(screen[0], reply_markup=screen[1])


@router.callback_query(F.data == "a:sch")
async def show_schedule(callback: CallbackQuery, repository: Any, state: FSMContext):
    viewer_id = await _staff(callback, repository)
    if viewer_id is None:
        return
    await state.clear()
    await show(callback, *await schedule_screen(repository, viewer_id))
    await callback.answer()


@router.callback_query(F.data.startswith("a:s:"))
async def show_slot(callback: CallbackQuery, repository: Any) -> None:
    if await _staff(callback, repository, slot_id=_tid(callback)) is None:
        return
    slot_id = parse_int((callback.data or "").split(":")[2])
    await _show_slot(callback, repository, slot_id or 0)
    await callback.answer()


@router.callback_query(F.data.startswith("a:sp:"))
async def show_slot_participants(callback: CallbackQuery, repository: Any) -> None:
    if await _staff(callback, repository, slot_id=_tid(callback)) is None:
        return
    slot_id = parse_int((callback.data or "").split(":")[2]) or 0
    participants = await ScheduleService(repository).participants(
        slot_id, include_cancelled=True
    )
    lines = ["Все записи на занятие №{}".format(slot_id)]
    if not participants:
        lines.append("Записей нет.")
    for participant in participants:
        lines.append(
            "• {} (ID {}) — {}, записался {}".format(
                participant.user_name,
                participant.telegram_id,
                "записан" if participant.status == "confirmed" else "отменено",
                format_studio_datetime(participant.booked_at),
            )
        )
    slot = await ScheduleService(repository).get(slot_id)
    can_cancel = (
        await callback_staff_role(callback, repository) == STAFF_ADMIN
        and slot is not None
        and slot.starts_at > _now()
    )
    rows = []
    for participant in participants[:15]:
        row = [
            button(
                "{} · {}".format(participant.user_name[:30], participant.telegram_id),
                "a:c:{}".format(participant.telegram_id),
            )
        ]
        if can_cancel and participant.status == "confirmed":
            row.append(
                button(
                    "✖ Отменить",
                    "a:bkx:{}:{}".format(slot_id, participant.telegram_id),
                )
            )
        rows.append(row)
    rows.append([button("⬅️ К занятию", "a:s:{}".format(slot_id))])
    await show(callback, "\n".join(lines), markup(*rows))
    await callback.answer()


# --- создание ---


@router.callback_query(F.data == "a:new")
async def choose_new_slot_class(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    rows = [
        [button(CLASS_LABELS[key], "a:new:{}".format(key))] for key in ACTIVE_CLASS_KEYS
    ]
    rows.append([button("⬅️ К расписанию", "a:sch")])
    await show(callback, "Новое занятие. Выберите направление:", markup(*rows))
    await callback.answer()


@router.callback_query(F.data.startswith("a:new:"))
async def start_new_slot(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    class_key = (callback.data or "").split(":")[2]
    if class_key not in ACTIVE_CLASS_KEYS:
        await callback.answer("Неизвестное направление.", show_alert=True)
        return
    await state.set_state(AdminForm.waiting_for_slot_time)
    await state.update_data(new_slot={"class_key": class_key})
    await callback.answer()
    await callback.message.answer(
        "«{}». Введите дату и время по времени студии, например 14.10 19:00 "
        "или 14.10.2026 19:00.".format(CLASS_LABELS[class_key]),
        reply_markup=admin_input_keyboard(),
    )


@router.message(AdminForm.waiting_for_slot_time)
async def save_new_slot_time(message: Message, state: FSMContext, repository: Any):
    if await get_admin_id(message, repository) is None:
        await state.clear()
        return
    try:
        starts_at = parse_admin_datetime(message.text or "")
    except ValueError as error:
        await message.answer("{}.".format(error))
        return
    if starts_at <= _now():
        await message.answer("Время должно быть в будущем. Введите ещё раз.")
        return
    data = (await state.get_data()).get("new_slot") or {}
    data["starts_at"] = starts_at.isoformat()
    await state.update_data(new_slot=data)
    await state.set_state(AdminForm.waiting_for_slot_capacity)
    await message.answer(
        "{}. Сколько мест (1–100)?".format(format_studio_datetime(starts_at))
    )


@router.message(AdminForm.waiting_for_slot_capacity)
async def save_new_slot_capacity(message: Message, state: FSMContext, repository: Any):
    admin_id = await get_admin_id(message, repository)
    if admin_id is None:
        await state.clear()
        return
    capacity = parse_int((message.text or "").strip())
    if capacity is None or not 1 <= capacity <= 100:
        await message.answer("Введите число мест от 1 до 100.")
        return
    data = (await state.get_data()).get("new_slot") or {}
    if data.get("class_key") not in ACTIVE_CLASS_KEYS or not data.get("starts_at"):
        await state.clear()
        await message.answer("Не удалось продолжить — начните создание заново.")
        return
    data["capacity"] = capacity
    await state.update_data(new_slot=data)
    await state.set_state(None)
    await message.answer(
        "Мест: {}. Повторять занятие каждую неделю?".format(capacity),
        reply_markup=markup(
            [button("Только это занятие", "a:rp:1")],
            [
                button("×{}".format(weeks), "a:rp:{}".format(weeks))
                for weeks in (2, 3, 4)
            ],
            [
                button("×{}".format(weeks), "a:rp:{}".format(weeks))
                for weeks in (6, 8, 12)
            ],
            [button("Отмена", "a:sch")],
        ),
    )


async def _new_slot_draft(state: FSMContext) -> Optional[dict]:
    data = (await state.get_data()).get("new_slot") or {}
    if (
        data.get("class_key") not in ACTIVE_CLASS_KEYS
        or not data.get("starts_at")
        or not data.get("capacity")
    ):
        return None
    return data


async def _confirm_new_slot_screen(
    callback: CallbackQuery, repository: Any, state: FSMContext, data: dict
) -> None:
    teacher_id = data.get("teacher_telegram_id")
    weeks = data.get("weeks", 1)
    await _set_pending(
        state,
        action="create_slot",
        class_key=data["class_key"],
        starts_at=data["starts_at"],
        capacity=data["capacity"],
        weeks=weeks,
        teacher_telegram_id=teacher_id,
    )
    starts_at = datetime.fromisoformat(data["starts_at"])
    lines = [
        "Создать занятие?" if weeks == 1 else "Создать серию занятий?",
        "",
        CLASS_LABELS[data["class_key"]],
        format_studio_datetime(starts_at),
        "Мест: {}".format(data["capacity"]),
    ]
    if weeks > 1:
        last = shift_weeks(to_studio_time(starts_at), weeks - 1)
        lines.append(
            "Каждую неделю, всего {} (последнее — {})".format(
                weeks, format_studio_datetime(last)
            )
        )
    if teacher_id:
        teacher = await repository.get_staff_member(teacher_id)
        lines.append(
            "Преподаватель: {}".format(
                teacher.public_name if teacher is not None else teacher_id
            )
        )
    await show(
        callback,
        "\n".join(lines),
        markup([button("✅ Создать", "a:newok")], [button("Отмена", "a:sch")]),
    )


@router.callback_query(F.data.startswith("a:rp:"))
async def choose_series_weeks(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    weeks = parse_int((callback.data or "").split(":")[2])
    data = await _new_slot_draft(state)
    if weeks is None or not 1 <= weeks <= 12 or data is None:
        await callback.answer("Начните создание занятия заново.", show_alert=True)
        return
    data["weeks"] = weeks
    await state.update_data(new_slot=data)
    teachers = [
        member
        for member in await repository.list_staff()
        if member.role == STAFF_TEACHER
    ]
    if not teachers:
        await _confirm_new_slot_screen(callback, repository, state, data)
        await callback.answer()
        return
    rows = [[button("Без преподавателя", "a:tp:0")]]
    rows.extend(
        [button(teacher.public_name[:40], "a:tp:{}".format(teacher.telegram_id))]
        for teacher in teachers[:20]
    )
    rows.append([button("Отмена", "a:sch")])
    await show(callback, "Кто ведёт занятие?", markup(*rows))
    await callback.answer()


@router.callback_query(F.data.startswith("a:tp:"))
async def choose_slot_teacher(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    teacher_id = parse_int((callback.data or "").split(":")[2])
    data = await _new_slot_draft(state)
    if teacher_id is None or data is None:
        await callback.answer("Начните создание занятия заново.", show_alert=True)
        return
    if teacher_id and await repository.get_staff_role(teacher_id) != STAFF_TEACHER:
        await callback.answer("Преподаватель не найден.", show_alert=True)
        return
    data["teacher_telegram_id"] = teacher_id or None
    await state.update_data(new_slot=data)
    await _confirm_new_slot_screen(callback, repository, state, data)
    await callback.answer()


@router.callback_query(F.data == "a:newok")
async def confirm_new_slot(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    admin_id = await _admin(callback, repository)
    if admin_id is None:
        return
    pending = await _take_pending(state, "create_slot")
    if pending is None:
        await _expired(callback)
        return
    weeks = pending.get("weeks", 1)
    service = ScheduleService(repository)
    starts_at = datetime.fromisoformat(pending["starts_at"])
    try:
        if weeks > 1:
            created, skipped = await service.create_series(
                pending["class_key"],
                starts_at,
                pending["capacity"],
                admin_id,
                weeks,
                teacher_telegram_id=pending.get("teacher_telegram_id"),
            )
        else:
            slot = await service.create(
                pending["class_key"],
                starts_at,
                pending["capacity"],
                admin_id,
                teacher_telegram_id=pending.get("teacher_telegram_id"),
            )
            created, skipped = [slot], 0
    except (ValueError, LookupError) as error:
        await _clear_pending(state)
        await callback.answer("Не создано: {}".format(error), show_alert=True)
        return
    await _clear_pending(state)
    await state.update_data(new_slot=None)
    actions_logger().info(
        "action=create_class_slot admin_id=%s slot_ids=%s class_key=%s "
        "capacity=%s weeks=%s skipped=%s",
        admin_id,
        [slot.id for slot in created],
        pending["class_key"],
        pending["capacity"],
        weeks,
        skipped,
    )
    if weeks == 1 and created:
        await _show_slot(callback, repository, created[0].id, "Занятие создано ✓")
    else:
        notice = "Создано занятий: {} ✓".format(len(created))
        if skipped:
            notice += " (уже были в это время: {})".format(skipped)
        await show(callback, *await schedule_screen(repository, admin_id, notice))
    await callback.answer("Создано")


# --- скопировать неделю ---


def _week_monday(offset: int) -> date:
    """Понедельник текущей (0) или следующей (1) недели по часам студии."""
    today = to_studio_time(_now()).date()
    return today - timedelta(days=today.weekday()) + timedelta(weeks=offset)


def _week_label(monday: date) -> str:
    sunday = monday + timedelta(days=6)
    return "{} – {}".format(monday.strftime("%d.%m"), sunday.strftime("%d.%m"))


@router.callback_query(F.data == "a:cw")
async def choose_copy_source_week(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    await show(
        callback,
        "Скопировать неделю: занятия какой недели повторить?\n"
        "Копируются неотменённые занятия с тем же временем, вместимостью и "
        "преподавателем; уже существующие и прошедшие пропускаются.",
        markup(
            [button("Эта неделя ({})".format(_week_label(_week_monday(0))), "a:cw:0")],
            [button("Следующая ({})".format(_week_label(_week_monday(1))), "a:cw:1")],
            [button("⬅️ К расписанию", "a:sch")],
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("a:cw:"))
async def choose_copy_weeks(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    offset = parse_int((callback.data or "").split(":")[2])
    if offset not in (0, 1):
        await callback.answer("Неделя не найдена.", show_alert=True)
        return
    monday = _week_monday(offset)
    counts = (1, 2, 3, 4, 8, 12)
    await show(
        callback,
        "Копируем неделю {}. На сколько недель вперёд?".format(_week_label(monday)),
        markup(
            [
                button("{}".format(count), "a:cwn:{}:{}".format(offset, count))
                for count in counts[:3]
            ],
            [
                button("{}".format(count), "a:cwn:{}:{}".format(offset, count))
                for count in counts[3:]
            ],
            [button("⬅️ Назад", "a:cw")],
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("a:cwn:"))
async def confirm_copy_week_screen(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    parts = (callback.data or "").split(":")
    offset = parse_int(parts[2]) if len(parts) > 3 else None
    weeks = parse_int(parts[3]) if len(parts) > 3 else None
    if offset not in (0, 1) or weeks is None or not 1 <= weeks <= 12:
        await callback.answer("Неверные параметры.", show_alert=True)
        return
    monday = _week_monday(offset)
    await _set_pending(
        state, action="copy_week", week_start=monday.isoformat(), weeks=weeks
    )
    await show(
        callback,
        "Скопировать занятия недели {} на {} нед. вперёд?\n"
        "Участники и записи не копируются — только расписание.".format(
            _week_label(monday), weeks
        ),
        markup([button("✅ Скопировать", "a:cwok")], [button("Отмена", "a:sch")]),
    )
    await callback.answer()


@router.callback_query(F.data == "a:cwok")
async def confirm_copy_week(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    admin_id = await _admin(callback, repository)
    if admin_id is None:
        return
    pending = await _take_pending(state, "copy_week")
    if pending is None:
        await _expired(callback)
        return
    try:
        created, skipped = await ScheduleService(repository).copy_week(
            date.fromisoformat(pending["week_start"]), pending["weeks"], admin_id
        )
    except (ValueError, LookupError) as error:
        await _clear_pending(state)
        await callback.answer("Не скопировано: {}".format(error), show_alert=True)
        return
    await _clear_pending(state)
    actions_logger().info(
        "action=copy_week admin_id=%s week_start=%s weeks=%s created=%s skipped=%s",
        admin_id,
        pending["week_start"],
        pending["weeks"],
        len(created),
        skipped,
    )
    notice = "Создано занятий: {} ✓".format(len(created))
    if skipped:
        notice += " (пропущено — уже есть или прошли: {})".format(skipped)
    await show(callback, *await schedule_screen(repository, admin_id, notice))
    await callback.answer("Готово")


# --- запись участника администратором ---


BOOKING_ERRORS = (
    SlotUnavailableError,
    InsufficientLessonCreditsError,
    BookingCooldownError,
)


@router.callback_query(F.data.startswith("a:bk:"))
async def start_admin_booking(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    slot_id = _tid(callback)
    slot = await ScheduleService(repository).get(slot_id)
    if slot is None or slot.status != "open" or slot.starts_at <= _now():
        await callback.answer("На это занятие записать нельзя.", show_alert=True)
        return
    await state.set_state(AdminForm.waiting_for_booking_search)
    await state.update_data(booking_slot_id=slot_id)
    await callback.answer()
    await callback.message.answer(
        "Занятие №{}. Введите Telegram ID, имя или часть телефона участника.\n"
        "С баланса спишется 1 занятие, запись — по тем же правилам, что "
        "самозапись.".format(slot_id),
        reply_markup=admin_input_keyboard(),
    )


@router.message(AdminForm.waiting_for_booking_search)
async def search_booking_participant(
    message: Message, state: FSMContext, repository: Any
) -> None:
    if await get_admin_id(message, repository) is None:
        await state.clear()
        return
    query = (message.text or "").strip()
    slot_id = (await state.get_data()).get("booking_slot_id")
    if not slot_id:
        await state.clear()
        await message.answer("Не удалось продолжить — откройте занятие заново.")
        return
    if not query or len(query) > 100:
        await message.answer("Введите запрос длиной от 1 до 100 символов.")
        return
    profiles = await repository.search_profiles(query, limit=10)
    if not profiles:
        await message.answer("Участники не найдены. Попробуйте другой запрос.")
        return
    await state.set_state(None)
    rows = [
        [
            button(
                "{} · {} · баланс {}".format(
                    profile.user_name[:24], profile.telegram_id, profile.lesson_credits
                ),
                "a:bkc:{}:{}".format(slot_id, profile.telegram_id),
            )
        ]
        for profile in profiles
    ]
    rows.append([button("⬅️ К занятию", "a:s:{}".format(slot_id))])
    await message.answer("Кого записать?", reply_markup=markup(*rows))


@router.callback_query(F.data.startswith("a:bkc:"))
async def confirm_admin_booking_screen(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    slot_id, tid = _tid(callback), _tid(callback, 3)
    slot = await ScheduleService(repository).get(slot_id)
    profile = await repository.get_profile(tid)
    if slot is None or profile is None:
        await callback.answer("Занятие или участник не найдены.", show_alert=True)
        return
    await _set_pending(state, action="admin_book", slot_id=slot_id, telegram_id=tid)
    await show(
        callback,
        "Записать участника?\n\n{} (ID {})\nБаланс: {}\n\n{}\n{}\n"
        "Спишется 1 занятие, участник получит уведомление.".format(
            profile.user_name,
            tid,
            lessons_count(profile.lesson_credits),
            CLASS_LABELS[slot.class_key],
            format_studio_datetime(slot.starts_at),
        ),
        markup(
            [button("✅ Записать", "a:bkok:{}:{}".format(slot_id, tid))],
            [button("Отмена", "a:s:{}".format(slot_id))],
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("a:bkok:"))
async def confirm_admin_booking(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    admin_id = await _admin(callback, repository)
    if admin_id is None:
        return
    slot_id, tid = _tid(callback), _tid(callback, 3)
    if (
        await _take_pending(state, "admin_book", slot_id=slot_id, telegram_id=tid)
        is None
    ):
        await _expired(callback)
        return
    try:
        booking = await BookingService(repository, None).admin_book(
            slot_id, tid, admin_id
        )
    except BOOKING_ERRORS as error:
        await _clear_pending(state)
        await callback.answer("Не записано: {}".format(error), show_alert=True)
        return
    await _clear_pending(state)
    actions_logger().info(
        "action=admin_book admin_id=%s slot_id=%s telegram_id=%s duplicate=%s",
        admin_id,
        slot_id,
        tid,
        booking.already_booked,
    )
    await _show_slot(
        callback,
        repository,
        slot_id,
        "Участник уже был записан" if booking.already_booked else "Участник записан ✓",
    )
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("a:bkx:"))
async def confirm_admin_cancel_screen(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    slot_id, tid = _tid(callback), _tid(callback, 3)
    slot = await ScheduleService(repository).get(slot_id)
    profile = await repository.get_profile(tid)
    if slot is None or profile is None or slot.starts_at <= _now():
        await callback.answer("Эту запись отменить нельзя.", show_alert=True)
        return
    await _set_pending(state, action="admin_cancel", slot_id=slot_id, telegram_id=tid)
    await show(
        callback,
        "Отменить запись участника?\n\n{} (ID {})\n{}\n{}\n"
        "Занятие вернётся на баланс даже позже окна отмены, участник получит "
        "уведомление.".format(
            profile.user_name,
            tid,
            CLASS_LABELS[slot.class_key],
            format_studio_datetime(slot.starts_at),
        ),
        markup(
            [button("✅ Отменить запись", "a:bkxok:{}:{}".format(slot_id, tid))],
            [button("Назад", "a:sp:{}".format(slot_id))],
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("a:bkxok:"))
async def confirm_admin_cancel(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    admin_id = await _admin(callback, repository)
    if admin_id is None:
        return
    slot_id, tid = _tid(callback), _tid(callback, 3)
    if (
        await _take_pending(state, "admin_cancel", slot_id=slot_id, telegram_id=tid)
        is None
    ):
        await _expired(callback)
        return
    try:
        cancelled = await BookingService(repository, None).admin_cancel(
            slot_id, tid, admin_id
        )
    except (BookingNotFoundError, CancellationWindowExpiredError) as error:
        await _clear_pending(state)
        await callback.answer("Не отменено: {}".format(error), show_alert=True)
        return
    await _clear_pending(state)
    actions_logger().info(
        "action=admin_cancel_booking admin_id=%s slot_id=%s telegram_id=%s "
        "cancelled=%s",
        admin_id,
        slot_id,
        tid,
        cancelled,
    )
    await _show_slot(
        callback,
        repository,
        slot_id,
        "Запись отменена, занятие вернулось на баланс ✓"
        if cancelled
        else "Запись уже была отменена",
    )
    await callback.answer("Готово")


# --- перенос ---


@router.callback_query(F.data.startswith("a:sm:"))
async def start_reschedule(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _staff(callback, repository, slot_id=_tid(callback)) is None:
        return
    slot_id = parse_int((callback.data or "").split(":")[2]) or 0
    slot = await ScheduleService(repository).get(slot_id)
    if slot is None or slot.status == "cancelled" or slot.starts_at <= _now():
        await callback.answer("Это занятие перенести нельзя.", show_alert=True)
        return
    await state.set_state(AdminForm.waiting_for_reschedule_time)
    await state.update_data(reschedule_slot_id=slot_id)
    await callback.answer()
    await callback.message.answer(
        "Перенос «{}», сейчас — {}.\nВведите новое время по времени студии, "
        "например 15.10 20:00. Записи и баланс сохранятся, участники получат "
        "уведомление.".format(
            CLASS_LABELS[slot.class_key], format_studio_datetime(slot.starts_at)
        ),
        reply_markup=admin_input_keyboard(),
    )


@router.message(AdminForm.waiting_for_reschedule_time)
async def save_reschedule_time(message: Message, state: FSMContext, repository: Any):
    slot_id = (await state.get_data()).get("reschedule_slot_id")
    if await get_staff_id(message, repository, slot_id or 0) is None:
        await state.clear()
        return
    slot = await ScheduleService(repository).get(slot_id) if slot_id else None
    if slot is None:
        await state.clear()
        await message.answer("Занятие не найдено — начните перенос заново.")
        return
    try:
        new_starts_at = parse_admin_datetime(message.text or "")
    except ValueError as error:
        await message.answer("{}.".format(error))
        return
    if new_starts_at <= _now():
        await message.answer("Новое время должно быть в будущем. Введите ещё раз.")
        return
    if new_starts_at == slot.starts_at:
        await message.answer("Это текущее время занятия. Введите другое.")
        return
    await _set_pending(
        state,
        action="reschedule",
        slot_id=slot.id,
        starts_at=new_starts_at.isoformat(),
    )
    await message.answer(
        "Перенести занятие?\n\n{}\nБыло: {}\nСтанет: {}\n\nЗаписано: {}. Записи и "
        "баланс сохранятся; участники получат уведомление и смогут отменить "
        "запись до нового начала без ограничения «за {} до начала».".format(
            CLASS_LABELS[slot.class_key],
            format_studio_datetime(slot.starts_at),
            format_studio_datetime(new_starts_at),
            slot.booked_count,
            deadline_text(),
        ),
        reply_markup=markup(
            [button("✅ Перенести", "a:smok:{}".format(slot.id))],
            [button("↩️ Не переносить", "a:s:{}".format(slot.id))],
        ),
    )


@router.callback_query(F.data.startswith("a:smok:"))
async def confirm_reschedule(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    admin_id = await _staff(callback, repository, slot_id=_tid(callback))
    if admin_id is None:
        return
    slot_id = parse_int((callback.data or "").split(":")[2]) or 0
    pending = await _take_pending(state, "reschedule", slot_id=slot_id)
    if pending is None:
        await _expired(callback)
        return
    try:
        result = await ScheduleService(repository).reschedule(
            slot_id, datetime.fromisoformat(pending["starts_at"]), admin_id
        )
    except (SlotNotFoundError, SlotStateError, ValueError) as error:
        await _clear_pending(state)
        await callback.answer("Не перенесено: {}".format(error), show_alert=True)
        return
    await _clear_pending(state)
    actions_logger().warning(
        "action=reschedule_class_slot admin_id=%s slot_id=%s participants=%s",
        admin_id,
        slot_id,
        len(result.participants),
    )
    await _show_slot(
        callback,
        repository,
        slot_id,
        "Занятие перенесено ✓ Уведомлений участникам: {}.".format(
            len(result.participants)
        ),
    )
    await callback.answer("Перенесено")


# --- отмена ---


async def _cancel_confirmation(
    repository: Any, slot_id: int, reason: Optional[str]
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    slot = await ScheduleService(repository).get(slot_id)
    if slot is None:
        return None
    text = (
        "Отменить занятие?\n\n{}\n{}\n\nЗаписано: {}. Каждому вернётся 1 занятие "
        "на баланс, участники получат уведомление.{}\n\nОтмену нельзя "
        "отменить: занятие закроется навсегда.".format(
            CLASS_LABELS[slot.class_key],
            format_studio_datetime(slot.starts_at),
            slot.booked_count,
            "\nПричина: {}".format(reason) if reason else "",
        )
    )
    rows = [
        [
            button(
                "❌ Отменить занятие" if reason else "❌ Отменить без причины",
                "a:sxok:{}".format(slot_id),
            )
        ]
    ]
    if not reason:
        rows.append([button("✍️ Указать причину", "a:sxr:{}".format(slot_id))])
    rows.append([button("↩️ Не отменять", "a:s:{}".format(slot_id))])
    return text, markup(*rows)


@router.callback_query(F.data.startswith("a:sx:"))
async def request_slot_cancel(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _staff(callback, repository, slot_id=_tid(callback)) is None:
        return
    slot_id = parse_int((callback.data or "").split(":")[2]) or 0
    screen = await _cancel_confirmation(repository, slot_id, None)
    if screen is None:
        await callback.answer("Занятие не найдено.", show_alert=True)
        return
    await _set_pending(state, action="cancel_slot", slot_id=slot_id, reason=None)
    await show(callback, *screen)
    await callback.answer()


@router.callback_query(F.data.startswith("a:sxr:"))
async def ask_cancel_reason(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _staff(callback, repository, slot_id=_tid(callback)) is None:
        return
    slot_id = parse_int((callback.data or "").split(":")[2]) or 0
    await state.set_state(AdminForm.waiting_for_cancel_reason)
    await state.update_data(cancel_slot_id=slot_id)
    await callback.answer()
    await callback.message.answer(
        "Причина отмены (до 300 символов) — её увидят участники в уведомлении.",
        reply_markup=admin_input_keyboard(),
    )


@router.message(AdminForm.waiting_for_cancel_reason)
async def save_cancel_reason(message: Message, state: FSMContext, repository: Any):
    cancel_slot_id = (await state.get_data()).get("cancel_slot_id") or 0
    if await get_staff_id(message, repository, cancel_slot_id) is None:
        await state.clear()
        return
    try:
        reason = normalize_reason(message.text, required=True)
    except ValueError as error:
        await message.answer("{}.".format(error))
        return
    slot_id = (await state.get_data()).get("cancel_slot_id") or 0
    screen = await _cancel_confirmation(repository, slot_id, reason)
    if screen is None:
        await state.clear()
        await message.answer("Занятие не найдено.")
        return
    await _set_pending(state, action="cancel_slot", slot_id=slot_id, reason=reason)
    await message.answer(screen[0], reply_markup=screen[1])


@router.callback_query(F.data.startswith("a:sxok:"))
async def confirm_slot_cancel(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    admin_id = await _staff(callback, repository, slot_id=_tid(callback))
    if admin_id is None:
        return
    slot_id = parse_int((callback.data or "").split(":")[2]) or 0
    pending = await _take_pending(state, "cancel_slot", slot_id=slot_id)
    if pending is None:
        await _expired(callback)
        return
    try:
        result = await ScheduleService(repository).cancel(
            slot_id, admin_id, pending.get("reason")
        )
    except (SlotNotFoundError, SlotStateError, ValueError) as error:
        await _clear_pending(state)
        await callback.answer("Не отменено: {}".format(error), show_alert=True)
        return
    await _clear_pending(state)
    if result.already_cancelled:
        notice = "Занятие уже было отменено — повторно ничего не начислено."
    else:
        notice = (
            "Занятие отменено ✓ Возвращено занятий: {}. Участники получат "
            "уведомление.".format(len(result.refunds))
        )
        actions_logger().warning(
            "action=cancel_class_slot admin_id=%s slot_id=%s refunds=%s",
            admin_id,
            slot_id,
            len(result.refunds),
        )
    await _show_slot(callback, repository, slot_id, notice)
    await callback.answer()


# --- закрыть / открыть / вместимость ---


@router.callback_query(F.data.startswith("a:sc:"))
@router.callback_query(F.data.startswith("a:so:"))
async def toggle_slot(callback: CallbackQuery, repository: Any) -> None:
    admin_id = await _staff(callback, repository, slot_id=_tid(callback))
    if admin_id is None:
        return
    parts = (callback.data or "").split(":")
    slot_id = parse_int(parts[2]) or 0
    service = ScheduleService(repository)
    if parts[1] == "sc":
        changed = await service.close(slot_id, admin_id)
        notice = "Запись на занятие закрыта." if changed else "Занятие уже закрыто."
    else:
        changed = await service.reopen(slot_id, admin_id)
        notice = (
            "Запись снова открыта."
            if changed
            else "Открыть нельзя: занятие отменено, уже началось или открыто."
        )
    if changed:
        actions_logger().info(
            "action=%s admin_id=%s slot_id=%s",
            "close_class_slot" if parts[1] == "sc" else "reopen_class_slot",
            admin_id,
            slot_id,
        )
    await _show_slot(callback, repository, slot_id, notice)
    await callback.answer()


@router.callback_query(F.data.startswith("a:scap:"))
async def start_capacity_change(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _staff(callback, repository, slot_id=_tid(callback)) is None:
        return
    slot_id = parse_int((callback.data or "").split(":")[2]) or 0
    await state.set_state(AdminForm.waiting_for_capacity_change)
    await state.update_data(capacity_slot_id=slot_id)
    await callback.answer()
    await callback.message.answer(
        "Новая вместимость (1–100, не меньше числа записанных):",
        reply_markup=admin_input_keyboard(),
    )


@router.message(AdminForm.waiting_for_capacity_change)
async def save_capacity_change(message: Message, state: FSMContext, repository: Any):
    admin_id = await get_staff_id(
        message, repository, (await state.get_data()).get("capacity_slot_id") or 0
    )
    if admin_id is None:
        await state.clear()
        return
    capacity = parse_int((message.text or "").strip())
    if capacity is None:
        await message.answer("Введите число от 1 до 100.")
        return
    slot_id = (await state.get_data()).get("capacity_slot_id") or 0
    try:
        updated = await ScheduleService(repository).set_capacity(
            slot_id, capacity, admin_id
        )
    except ValueError as error:
        await message.answer("{}.".format(error))
        return
    await state.clear()
    screen = await slot_card(
        repository,
        slot_id,
        "Вместимость обновлена." if updated else "Занятие не найдено.",
    )
    if screen is None:
        await message.answer("Занятие не найдено.")
        return
    actions_logger().info(
        "action=change_class_slot_capacity admin_id=%s slot_id=%s capacity=%s",
        admin_id,
        slot_id,
        capacity,
    )
    await message.answer(screen[0], reply_markup=screen[1])


# ---------- клиенты ----------


async def client_card(
    repository: Any, telegram_id: int, notice: str = ""
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    try:
        overview = await ProfileService(repository).overview(telegram_id)
    except LookupError:
        return None
    profile = overview.profile
    lines = [notice, ""] if notice else []
    lines.extend(
        [
            "👤 {} (ID {}){}".format(
                profile.user_name,
                profile.telegram_id,
                " · администратор" if profile.is_admin else "",
            ),
            "Телефон: {}".format(profile.phone or "не указан"),
            "В студии с {}".format(format_date(profile.registered_at)),
            balance_line(overview.packages.balance) + ".",
        ]
    )
    if overview.packages.active_packages:
        lines.append("Абонементы:")
        lines.extend(
            "• " + package_line(package)
            for package in overview.packages.active_packages
        )
    if overview.packages.unallocated:
        lines.append(
            "Бессрочно: {}.".format(lessons_count(overview.packages.unallocated))
        )
    if overview.upcoming:
        lines.append("Ближайшие занятия:")
        lines.extend(
            "• {} — {}".format(
                format_class_time(booking.starts_at), CLASS_LABELS[booking.class_key]
            )
            for booking in overview.upcoming
        )
    tid = profile.telegram_id
    return "\n".join(lines), markup(
        [
            button("📖 Занятия", "a:cb:{}:0".format(tid)),
            button("🎟 Абонементы", "a:cp:{}".format(tid)),
        ],
        [
            button("💳 Покупки", "a:cpay:{}".format(tid)),
            button("🧾 Операции", "a:co:{}".format(tid)),
        ],
        [
            button("📨 Обращения", "a:ct:{}".format(tid)),
            button("📝 Заметки", "s:nl:{}".format(tid)),
        ],
        [
            button("➕ Выдать абонемент", "a:cg:{}".format(tid)),
            button("± Баланс", "a:ca:{}".format(tid)),
        ],
        [
            button("✏️ Имя", "a:cn:{}".format(tid)),
            button("📱 Телефон", "a:cph:{}".format(tid)),
        ],
    )


async def _show_client(callback: CallbackQuery, repository: Any, tid: int, notice=""):
    screen = await client_card(repository, tid, notice)
    if screen is None:
        await callback.answer("Участник не найден.", show_alert=True)
        return
    await show(callback, *screen)


def _tid(callback: CallbackQuery, index: int = 2) -> int:
    parts = (callback.data or "").split(":")
    return (parse_int(parts[index]) if len(parts) > index else None) or 0


@router.message(F.text == ADMIN_CLIENTS)
async def open_clients(message: Message, state: FSMContext, repository: Any) -> None:
    from .admin import start_participant_search

    await start_participant_search(message, state, repository)


@router.message(Command("client"))
async def open_client_command(
    message: Message, state: FSMContext, repository: Any
) -> None:
    if await get_admin_id(message, repository) is None:
        await message.answer("Команда доступна только администратору студии.")
        return
    parts = (message.text or "").split()
    telegram_id = parse_int(parts[1]) if len(parts) == 2 else None
    screen = await client_card(repository, telegram_id) if telegram_id else None
    if screen is None:
        await open_clients(message, state, repository)
        return
    await message.answer(screen[0], reply_markup=screen[1])


@router.callback_query(F.data == "a:cli")
async def search_clients(callback: CallbackQuery, repository: Any, state: FSMContext):
    if await _admin(callback, repository) is None:
        return
    await state.set_state(AdminForm.waiting_for_search)
    await callback.answer()
    await callback.message.answer(
        "Введите Telegram ID, имя или часть номера телефона для поиска.",
        reply_markup=admin_action_keyboard(),
    )


@router.callback_query(F.data.startswith("a:c:"))
async def show_client(callback: CallbackQuery, repository: Any, state: FSMContext):
    if await _admin(callback, repository) is None:
        return
    await state.set_state(None)
    await _show_client(callback, repository, _tid(callback))
    await callback.answer()


def _back_to_client(tid: int) -> list:
    return [button("⬅️ К карточке", "a:c:{}".format(tid))]


@router.callback_query(F.data.startswith("a:cb:"))
async def show_client_bookings(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    tid = _tid(callback)
    offset = min(_tid(callback, 3), 10_000)
    bookings = list(
        await HistoryService(repository).bookings(
            tid, limit=PAGE_SIZE + 1, offset=offset
        )
    )
    has_more = len(bookings) > PAGE_SIZE
    lines = ["Занятия участника {}".format(tid)]
    if not bookings:
        lines.append("Записей нет.")
    for booking in bookings[:PAGE_SIZE]:
        lines.append(
            "• {} — {} · {} (занятие №{})".format(
                format_class_time(booking.starts_at),
                CLASS_LABELS[booking.class_key],
                BOOKING_STATUS_LABELS[booking_status(booking)],
                booking.slot_id,
            )
        )
    navigation = []
    if offset:
        navigation.append(
            button("⬅️ Новее", "a:cb:{}:{}".format(tid, max(0, offset - PAGE_SIZE)))
        )
    if has_more:
        navigation.append(
            button("Раньше ➡️", "a:cb:{}:{}".format(tid, offset + PAGE_SIZE))
        )
    await show(callback, "\n".join(lines), markup(navigation, _back_to_client(tid)))
    await callback.answer()


@router.callback_query(F.data.startswith("a:cpay:"))
async def show_client_payments(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    tid = _tid(callback)
    payments = await repository.list_lesson_payments_for_telegram_id(tid, limit=15)
    lines = ["Покупки участника {}".format(tid)]
    if not payments:
        lines.append("Платежей нет.")
    lines.extend(
        "• №{} · {} · {} · {} · {}".format(
            payment.id,
            format_date(payment.created_at),
            payment.package_title,
            format_amount(payment.amount_minor),
            PAYMENT_STATUS_LABELS.get(payment.status, payment.status),
        )
        for payment in payments
    )
    lines.append("")
    lines.append(
        "Возврат оплаты: /refund ID причина (только неиспользованный абонемент)."
    )
    await show(callback, "\n".join(lines), markup(_back_to_client(tid)))
    await callback.answer()


@router.callback_query(F.data.startswith("a:cp:"))
async def show_client_packages(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    tid = _tid(callback)
    summary = await PackageService(repository).summary(tid)
    lines = ["Абонементы участника {}".format(tid), balance_line(summary.balance) + "."]
    if not summary.packages:
        lines.append("Абонементов нет.")
    for package in summary.packages:
        line = "• " + package_line(package)
        if package.kind == "grant" and package.reason:
            line += " — {}".format(package.reason)
        lines.append(line)
    if summary.unallocated:
        lines.append("Бессрочно: {}.".format(lessons_count(summary.unallocated)))
    rows = [
        [
            button(
                "Отозвать «{}» от {}".format(
                    package.title[:25], format_date(package.acquired_at)
                ),
                "a:gr:{}".format(package.id),
            )
        ]
        for package in summary.packages
        if package.kind == "grant" and package.status in {"active", "used"}
    ]
    rows.append([button("➕ Выдать абонемент", "a:cg:{}".format(tid))])
    rows.append(_back_to_client(tid))
    await show(callback, "\n".join(lines), markup(*rows))
    await callback.answer()


def _admin_operation_line(operation: Any) -> str:
    """Операция в две строки: время, изменение и тип; ниже — детали."""
    entry = operation.entry
    head = "• {} · {} · {}".format(
        to_studio_time(entry.created_at).strftime("%d.%m %H:%M"),
        "{:+d}".format(entry.delta),
        operation.label,
    )
    details = []
    if entry.user_name:
        details.append("{} ({})".format(entry.user_name, entry.telegram_id))
    if entry.class_key and entry.starts_at:
        details.append(
            "{} {}".format(
                CLASS_SHORT_LABELS[entry.class_key], format_class_time(entry.starts_at)
            )
        )
    if entry.package_title:
        details.append("«{}»".format(entry.package_title))
    if entry.actor_telegram_id:
        details.append("админ {}".format(entry.actor_telegram_id))
    if entry.reason:
        details.append(entry.reason)
    return head + ("\n   " + "; ".join(details) if details else "")


def _scroll_row(prefix: str, offset: int, has_more: bool) -> list:
    """Кнопки прокрутки страниц: ``prefix:<смещение>``."""
    row = []
    if offset > 0:
        row.append(
            button("⬅️ Новее", "{}:{}".format(prefix, max(0, offset - LOG_PAGE_SIZE)))
        )
    if has_more:
        row.append(button("Старее ➡️", "{}:{}".format(prefix, offset + LOG_PAGE_SIZE)))
    return row


async def _operations_screen(
    repository: Any, telegram_id: Optional[int], offset: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    operations = list(
        await HistoryService(repository).operations(
            telegram_id, limit=LOG_PAGE_SIZE + 1, offset=offset
        )
    )
    has_more = len(operations) > LOG_PAGE_SIZE
    operations = operations[:LOG_PAGE_SIZE]
    title = (
        "Операции участника {}".format(telegram_id)
        if telegram_id
        else "🧾 Последние операции (все участники)"
    )
    if offset:
        title += " · с {}-й".format(offset + 1)
    lines = [title]
    if not operations:
        lines.append("Операций нет.")
    lines.extend(_admin_operation_line(operation) for operation in operations)
    prefix = "a:co:{}".format(telegram_id) if telegram_id else "a:fl"
    back = (
        _back_to_client(telegram_id) if telegram_id else [button("⬅️ Финансы", "a:fin")]
    )
    return "\n\n".join(lines)[:4000], markup(
        _scroll_row(prefix, offset, has_more), back
    )


@router.callback_query(F.data.startswith("a:co:"))
async def show_client_operations(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    parts = (callback.data or "").split(":")
    offset = (parse_int(parts[3]) or 0) if len(parts) == 4 else 0
    await show(callback, *await _operations_screen(repository, _tid(callback), offset))
    await callback.answer()


@router.callback_query(F.data.startswith("a:ct:"))
async def show_client_tickets(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    tid = _tid(callback)
    tickets = await repository.list_support_tickets_for_telegram_id(tid, limit=10)
    lines = ["Обращения участника {}".format(tid)]
    if not tickets:
        lines.append("Обращений нет.")
    rows = [
        [
            button(
                "№{} · {} · {}".format(
                    ticket.id,
                    "открыто" if ticket.status == "open" else "закрыто",
                    ticket.last_message.replace("\n", " ")[:30],
                ),
                "a:t:{}".format(ticket.id),
            )
        ]
        for ticket in tickets
    ]
    rows.append(_back_to_client(tid))
    await show(callback, "\n".join(lines), markup(*rows))
    await callback.answer()


# --- выдача и отзыв абонемента ---


@router.callback_query(F.data.startswith("a:cg:"))
async def choose_grant_package(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    tid = _tid(callback)
    rows = [
        [
            button(
                "{} — {}".format(package.title, lessons_count(package.lessons)),
                "a:cgp:{}:{}".format(tid, package.key),
            )
        ]
        for package in PURCHASE_PACKAGES.values()
    ]
    rows.append(_back_to_client(tid))
    await show(
        callback,
        "Выдать абонемент участнику {} без онлайн-оплаты (например, оплата "
        "наличными). Выберите абонемент:".format(tid),
        markup(*rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("a:cgp:"))
async def ask_grant_reason(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    parts = (callback.data or "").split(":")
    tid = _tid(callback)
    package = catalog_package(parts[3]) if len(parts) == 4 else None
    if package is None:
        await callback.answer("Абонемент не найден.", show_alert=True)
        return
    await state.set_state(AdminForm.waiting_for_grant_reason)
    await state.update_data(
        grant_target={"telegram_id": tid, "package_key": package.key}
    )
    await callback.answer()
    await callback.message.answer(
        "«{}» участнику {}. Причина выдачи (например «оплата наличными») — "
        "её увидит участник в истории операций.".format(package.title, tid),
        reply_markup=admin_input_keyboard(),
    )


@router.message(AdminForm.waiting_for_grant_reason)
async def save_grant_reason(message: Message, state: FSMContext, repository: Any):
    if await get_admin_id(message, repository) is None:
        await state.clear()
        return
    try:
        reason = normalize_reason(message.text, required=True)
    except ValueError as error:
        await message.answer("{}.".format(error))
        return
    target = (await state.get_data()).get("grant_target") or {}
    package = catalog_package(target.get("package_key", ""))
    profile = await repository.get_profile(target.get("telegram_id") or 0)
    if package is None or profile is None:
        await state.clear()
        await message.answer("Участник или абонемент не найден.")
        return
    await _set_pending(
        state,
        action="grant",
        telegram_id=profile.telegram_id,
        package_key=package.key,
        reason=reason,
        key=str(uuid.uuid4()),
    )
    await message.answer(
        "Выдать абонемент?\n\nУчастник: {} (ID {})\n«{}» — {}\nБаланс: {} → {}\n"
        "Причина: {}".format(
            profile.user_name,
            profile.telegram_id,
            package.title,
            lessons_count(package.lessons),
            profile.lesson_credits,
            profile.lesson_credits + package.lessons,
            reason,
        ),
        reply_markup=markup(
            [button("✅ Выдать", "a:cgok")],
            [button("Отмена", "a:c:{}".format(profile.telegram_id))],
        ),
    )


@router.callback_query(F.data == "a:cgok")
async def confirm_grant(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    admin_id = await _admin(callback, repository)
    if admin_id is None:
        return
    pending = await _take_pending(state, "grant")
    if pending is None:
        await _expired(callback)
        return
    try:
        result = await PackageService(repository).grant(
            pending["telegram_id"],
            pending["package_key"],
            pending["reason"],
            admin_id,
            uuid.UUID(pending["key"]),
        )
    except (ValueError, LookupError, PermissionError) as e:
        await _clear_pending(state)
        await callback.answer("Не выдано: {}".format(e), show_alert=True)
        return
    await _clear_pending(state)
    actions_logger().warning(
        "action=grant_package admin_id=%s target_id=%s grant_id=%s applied=%s",
        admin_id,
        pending["telegram_id"],
        result.grant.id,
        result.applied,
    )
    notice = (
        "Абонемент выдан ✓ Баланс: {}.".format(result.balance)
        if result.applied
        else "Эта выдача уже была выполнена — повторно не начислено."
    )
    await _show_client(callback, repository, pending["telegram_id"], notice)
    await callback.answer()


@router.callback_query(F.data.startswith("a:gr:"))
async def ask_revoke_reason(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    grant_id = _tid(callback)
    grant = await repository.get_package_grant(grant_id)
    if grant is None or grant.status == "revoked":
        await callback.answer("Выдача не найдена или уже отозвана.", show_alert=True)
        return
    await state.set_state(AdminForm.waiting_for_revoke_reason)
    await state.update_data(revoke_grant_id=grant_id)
    await callback.answer()
    await callback.message.answer(
        "Отзыв «{}» у участника {}. Спишутся только неиспользованные занятия "
        "этого абонемента. Причина (её увидит участник):".format(
            grant.title, grant.telegram_id
        ),
        reply_markup=admin_input_keyboard(),
    )


@router.message(AdminForm.waiting_for_revoke_reason)
async def save_revoke_reason(message: Message, state: FSMContext, repository: Any):
    if await get_admin_id(message, repository) is None:
        await state.clear()
        return
    try:
        reason = normalize_reason(message.text, required=True)
    except ValueError as error:
        await message.answer("{}.".format(error))
        return
    grant_id = (await state.get_data()).get("revoke_grant_id") or 0
    grant = await repository.get_package_grant(grant_id)
    if grant is None:
        await state.clear()
        await message.answer("Выдача не найдена.")
        return
    summary = await PackageService(repository).summary(grant.telegram_id)
    unused = next(
        (
            package.remaining
            for package in summary.packages
            if package.kind == "grant" and package.id == grant_id
        ),
        0,
    )
    await _set_pending(state, action="revoke", grant_id=grant_id, reason=reason)
    await message.answer(
        "Отозвать абонемент?\n\n«{}» у участника {}\nБудет списано "
        "неиспользованных занятий: {}\nПричина: {}".format(
            grant.title, grant.telegram_id, unused, reason
        ),
        reply_markup=markup(
            [button("✅ Отозвать", "a:grok")],
            [button("Отмена", "a:c:{}".format(grant.telegram_id))],
        ),
    )


@router.callback_query(F.data == "a:grok")
async def confirm_revoke(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    admin_id = await _admin(callback, repository)
    if admin_id is None:
        return
    pending = await _take_pending(state, "revoke")
    if pending is None:
        await _expired(callback)
        return
    try:
        result = await PackageService(repository).revoke(
            pending["grant_id"], pending["reason"], admin_id
        )
    except (
        ValueError,
        PermissionError,
        PackageGrantNotFoundError,
    ) as error:
        await _clear_pending(state)
        await callback.answer("Не отозвано: {}".format(error), show_alert=True)
        return
    await _clear_pending(state)
    actions_logger().warning(
        "action=revoke_package admin_id=%s grant_id=%s lessons=%s applied=%s",
        admin_id,
        result.grant.id,
        result.revoked_lessons,
        result.applied,
    )
    notice = (
        "Абонемент отозван ✓ Списано: {}. Баланс: {}.".format(
            result.revoked_lessons, result.balance
        )
        if result.applied
        else "Абонемент уже был отозван."
    )
    await _show_client(callback, repository, result.grant.telegram_id, notice)
    await callback.answer()


# --- баланс, имя, телефон ---


@router.callback_query(F.data.startswith("a:ca:"))
async def start_client_adjustment(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    await state.set_state(AdminForm.waiting_for_client_adjustment)
    await state.update_data(adjust_telegram_id=_tid(callback))
    await callback.answer()
    await callback.message.answer(
        "Введите изменение и причину, например «+2 оплата наличными» или "
        "«-1 ошибочное начисление». Причину увидит участник.",
        reply_markup=admin_input_keyboard(),
    )


@router.message(AdminForm.waiting_for_client_adjustment)
async def save_client_adjustment(message: Message, state: FSMContext, repository: Any):
    if await get_admin_id(message, repository) is None:
        await state.clear()
        return
    match = _ADJUSTMENT.fullmatch((message.text or "").strip())
    if match is None:
        await message.answer("Формат: +N причина или -N причина, например «+1 бонус».")
        return
    telegram_id = (await state.get_data()).get("adjust_telegram_id") or 0
    await state.clear()
    await prepare_credit_adjustment(
        message, state, repository, telegram_id, int(match.group(1)), match.group(2)
    )


@router.callback_query(F.data.startswith("a:cn:"))
@router.callback_query(F.data.startswith("a:cph:"))
async def start_client_profile_edit(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    is_name = (callback.data or "").startswith("a:cn:")
    await state.set_state(
        AdminForm.waiting_for_name_value
        if is_name
        else AdminForm.waiting_for_phone_value
    )
    await state.update_data(target_telegram_id=_tid(callback))
    await callback.answer()
    await callback.message.answer(
        "Введите новое имя (от 1 до 64 символов)."
        if is_name
        else "Введите новый номер телефона (до 32 символов).",
        reply_markup=admin_action_keyboard(),
    )


# ---------- поддержка ----------


async def tickets_screen(repository: Any) -> tuple[str, InlineKeyboardMarkup]:
    tickets = await repository.list_open_support_tickets(limit=20)
    lines = ["📨 Открытые обращения"]
    if not tickets:
        lines.append("Открытых обращений нет.")
    rows = [
        [
            button(
                "№{} · {} · {}".format(
                    ticket.id,
                    ticket.telegram_id,
                    ticket.last_message.replace("\n", " ")[:30],
                ),
                "a:t:{}".format(ticket.id),
            )
        ]
        for ticket in tickets
    ]
    return "\n".join(lines), markup(*rows) if rows else markup()


async def ticket_screen(
    repository: Any, ticket_id: int, notice: str = "", viewer_id: Optional[int] = None
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    """Переписка; кнопка «Клиент» — только администратору (у преподавателя
    нет доступа к карточкам участников)."""
    thread = await SupportService(repository, None, None).thread(ticket_id)
    if thread is None:
        return None
    lines = [notice, ""] if notice else []
    lines.extend(
        [
            "Обращение №{} · {} · {} (ID {})".format(
                thread.id,
                "открыто" if thread.status == "open" else "закрыто",
                thread.user_name,
                thread.telegram_id,
            ),
            "",
        ]
    )
    for message in thread.messages[-12:]:
        lines.append(
            "{} · {}:\n{}\n".format(
                to_studio_time(message.created_at).strftime("%d.%m %H:%M"),
                "участник" if message.sender_role == "user" else "студия",
                message.body[:600],
            )
        )
    rows = []
    if thread.status == "open":
        rows.append(
            [
                button("✍️ Ответить", "a:tr:{}".format(thread.id)),
                button("✅ Закрыть", "a:tc:{}".format(thread.id)),
            ]
        )
    else:
        rows.append([button("↩️ Открыть снова", "a:to:{}".format(thread.id))])
    if viewer_id is None or await repository.get_staff_role(viewer_id) == STAFF_ADMIN:
        rows.append([button("👤 Клиент", "a:c:{}".format(thread.telegram_id))])
    rows.append([button("⬅️ Все обращения", "a:tk")])
    text = "\n".join(lines).strip()
    return text[:4000], markup(*rows)


@router.message(F.text == SUPPORT_TICKETS)
@router.message(Command("support_tickets"))
async def open_tickets(message: Message, state: FSMContext, repository: Any) -> None:
    if await get_staff_id(message, repository) is None:
        await message.answer("Команда доступна только сотрудникам студии.")
        return
    await state.clear()
    text, reply_markup = await tickets_screen(repository)
    await message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data == "a:tk")
async def show_tickets(callback: CallbackQuery, repository: Any) -> None:
    if await _staff(callback, repository) is None:
        return
    await show(callback, *await tickets_screen(repository))
    await callback.answer()


@router.callback_query(F.data.startswith("a:t:"))
async def show_ticket(callback: CallbackQuery, repository: Any) -> None:
    viewer_id = await _staff(callback, repository)
    if viewer_id is None:
        return
    screen = await ticket_screen(repository, _tid(callback), viewer_id=viewer_id)
    if screen is None:
        await callback.answer("Обращение не найдено.", show_alert=True)
        return
    await show(callback, *screen)
    await callback.answer()


@router.callback_query(F.data.startswith("a:tr:"))
async def start_ticket_reply(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _staff(callback, repository) is None:
        return
    await state.set_state(AdminForm.waiting_for_support_reply)
    await state.update_data(reply_ticket_id=_tid(callback))
    await callback.answer()
    await callback.message.answer(
        "Текст ответа по обращению №{} (до 2000 символов):".format(_tid(callback)),
        reply_markup=admin_input_keyboard(),
    )


@router.message(AdminForm.waiting_for_support_reply)
async def save_ticket_reply(message: Message, state: FSMContext, repository: Any):
    admin_id = await get_staff_id(message, repository)
    if admin_id is None:
        await state.clear()
        return
    body = (message.text or "").strip()
    if not body or len(body) > 2000:
        await message.answer("Ответ должен содержать от 1 до 2000 символов.")
        return
    ticket_id = (await state.get_data()).get("reply_ticket_id") or 0
    await state.clear()
    result = await deliver_support_reply(
        message.bot, repository, ticket_id, admin_id, body
    )
    screen = await ticket_screen(repository, ticket_id, result, viewer_id=admin_id)
    if screen is None:
        await message.answer(result)
        return
    await message.answer(screen[0], reply_markup=screen[1])


@router.callback_query(F.data.startswith("a:tc:"))
async def close_ticket(callback: CallbackQuery, repository: Any) -> None:
    admin_id = await _staff(callback, repository)
    if admin_id is None:
        return
    ticket_id = _tid(callback)
    closed = await close_ticket_and_notify(
        callback.bot, repository, ticket_id, admin_id
    )
    screen = await ticket_screen(
        repository,
        ticket_id,
        "Обращение закрыто, участник уведомлён." if closed else "Уже закрыто.",
        viewer_id=admin_id,
    )
    if screen is not None:
        await show(callback, *screen)
    await callback.answer()


@router.callback_query(F.data.startswith("a:to:"))
async def reopen_ticket(callback: CallbackQuery, repository: Any) -> None:
    admin_id = await _staff(callback, repository)
    if admin_id is None:
        return
    ticket_id = _tid(callback)
    try:
        reopened = await SupportService(repository, None, None).reopen(
            ticket_id, admin_id
        )
    except SupportTicketStateError as error:
        await callback.answer(str(error), show_alert=True)
        return
    screen = await ticket_screen(
        repository,
        ticket_id,
        "Обращение снова открыто." if reopened else "Обращение не найдено.",
        viewer_id=admin_id,
    )
    if screen is not None:
        await show(callback, *screen)
    await callback.answer()


# ---------- финансы ----------


def finance_screen() -> tuple[str, InlineKeyboardMarkup]:
    return (
        "💰 Финансы\n\nКорректировка баланса — в карточке участника "
        "(«± Баланс»). Возврат оплаты — /refund ID причина.",
        markup(
            [button("🧾 Последние операции", "a:fl")],
            [button("🕵️ Журнал операций", "a:fa")],
            [button("⚖️ Сверка баланса с ledger", "a:fr")],
            [button("👥 Найти участника", "a:cli")],
        ),
    )


@router.message(F.text == ADMIN_FINANCE)
async def open_finance(message: Message, state: FSMContext, repository: Any) -> None:
    if await get_admin_id(message, repository) is None:
        await message.answer("Команда доступна только администратору студии.")
        return
    await state.clear()
    text, reply_markup = finance_screen()
    await message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data == "a:fin")
async def show_finance(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    await show(callback, *finance_screen())
    await callback.answer()


@router.callback_query(F.data.startswith("a:fl"))
async def show_ledger(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    parts = (callback.data or "").split(":")
    offset = (parse_int(parts[2]) or 0) if len(parts) == 3 else 0
    await show(callback, *await _operations_screen(repository, None, offset))
    await callback.answer()


@router.message(Command("ledger"))
async def ledger_command(message: Message, repository: Any) -> None:
    if await get_admin_id(message, repository) is None:
        await message.answer("Команда доступна только администратору студии.")
        return
    parts = (message.text or "").split()
    telegram_id = parse_int(parts[1]) if len(parts) == 2 else None
    text, reply_markup = await _operations_screen(repository, telegram_id, None)
    await message.answer(text, reply_markup=reply_markup)


async def audit_screen(
    repository: Any, offset: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    events = list(
        await repository.list_audit_events(limit=LOG_PAGE_SIZE + 1, offset=offset)
    )
    has_more = len(events) > LOG_PAGE_SIZE
    events = events[:LOG_PAGE_SIZE]
    lines = ["🕵️ Журнал операций" + (" · с {}-й".format(offset + 1) if offset else "")]
    if not events:
        lines.append("Записей нет.")
    for event in events:
        target = []
        if event.target_telegram_id:
            target.append("участник {}".format(event.target_telegram_id))
        if event.slot_id:
            target.append("занятие №{}".format(event.slot_id))
        if event.payment_id:
            target.append("платёж №{}".format(event.payment_id))
        if event.delta:
            target.append("{:+d}".format(event.delta))
        if event.reason:
            target.append(event.reason)
        lines.append(
            "• {} · {} · {}{}".format(
                to_studio_time(event.created_at).strftime("%d.%m %H:%M"),
                "админ {}".format(event.actor_telegram_id)
                if event.actor_telegram_id
                else "система",
                AUDIT_ACTION_LABELS.get(event.action, event.action),
                "\n   " + "; ".join(target) if target else "",
            )
        )
    return "\n\n".join(lines)[:4000], markup(
        _scroll_row("a:fa", offset, has_more), [button("⬅️ Финансы", "a:fin")]
    )


@router.callback_query(F.data.startswith("a:fa"))
async def show_audit(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    parts = (callback.data or "").split(":")
    offset = (parse_int(parts[2]) or 0) if len(parts) == 3 else 0
    await show(callback, *await audit_screen(repository, offset))
    await callback.answer()


@router.message(Command("audit"))
async def audit_command(message: Message, repository: Any) -> None:
    if await get_admin_id(message, repository) is None:
        await message.answer("Команда доступна только администратору студии.")
        return
    text, reply_markup = await audit_screen(repository)
    await message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data == "a:fr")
async def show_reconciliation(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    service = CreditService(repository, Config.CREDIT_ADJUSTMENT_MAX_DELTA)
    result = await service.reconcile(CREDIT_AUDIT_LIMIT)
    if not result.mismatched_users:
        text = "Сверка баланса: расхождений нет (проверено участников: {}).".format(
            result.checked_users
        )
    else:
        lines = [
            "Сверка баланса: расхождения у {} из {} участников.".format(
                result.mismatched_users, result.checked_users
            )
        ]
        lines.extend(_format_credit_mismatch(item) for item in result.mismatches)
        lines.append("Баланс автоматически не исправляется.")
        text = "\n".join(lines)
    await show(callback, text[:4000], markup([button("⬅️ Финансы", "a:fin")]))
    await callback.answer()
