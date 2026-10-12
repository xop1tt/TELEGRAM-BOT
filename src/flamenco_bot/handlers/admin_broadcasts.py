"""Рассылки в боте: сегмент, текст, предпросмотр, отправка, история.

То же, что «Рассылки» в админке сайта: сообщение уходит каждому участнику
сегмента через общую очередь уведомлений (бот и сайт показывают его как
«объявление студии»). Сегменты — «все участники», «с занятиями на балансе»
(можно — без записи N дней), «записанные на занятие», «ходили на направление
за N дней». Администратору доступны все сегменты и история; преподаватель
пишет только записанным на своё занятие — кнопкой «📣 Написать участникам» в
карточке занятия. Отправка — после экрана с числом получателей и текстом.

callback_data (префикс ``b:``, ≤ 64 байт):

  b:m                      меню           b:n  новая          b:h  история
  b:s:<all|wc|sl|dr>       сегмент        b:wc:<days>  с балансом (0 — любые)
  b:sl:<slot_id>           занятие        b:dr:<key> / b:dd:<days>  направление
  b:t:<slot_id>            написать записанным (преподаватель и админ)
  b:ok                     подтвердить отправку
"""

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ..class_catalog import ACTIVE_CLASS_KEYS, CLASS_LABELS, CLASS_SHORT_LABELS
from ..database.engagement_postgres import MAX_BROADCAST_LENGTH
from ..database.repository import STAFF_ADMIN
from ..keyboards.admin import ADMIN_BROADCASTS, admin_input_keyboard
from ..keyboards.user.screens import button, markup, parse_int, show
from ..presentation import format_class_time
from ..runtime.admin_access import get_admin_id, get_staff_id
from ..services import ScheduleService
from ..studio_time import format_studio_datetime
from .admin import actions_logger
from .admin_panel import (
    _admin,
    _clear_pending,
    _expired,
    _set_pending,
    _staff,
    _take_pending,
    _tid,
    callback_staff_role,
)
from .states import AdminForm

logger = logging.getLogger("bot.handlers.admin_broadcasts")
router = Router(name="admin_broadcasts")

SEGMENT_LABELS = {
    "all": "все участники",
    "with_credits": "с занятиями на балансе",
    "slot": "записанные на занятие",
    "direction": "ходили на направление",
}
INACTIVE_DAYS = (14, 30, 60)
DIRECTION_DAYS = (30, 60, 90)
HISTORY_LIMIT = 10
SLOTS_TO_PICK = 10


# ---------- меню и история ----------


def menu_screen() -> tuple[str, InlineKeyboardMarkup]:
    return "📣 Рассылки", markup(
        [button("✍️ Новая рассылка", "b:n")],
        [button("🕘 История рассылок", "b:h")],
    )


@router.message(F.text == ADMIN_BROADCASTS)
@router.message(Command("broadcast"))
async def open_broadcasts(message: Message, state: FSMContext, repository: Any) -> None:
    if await get_admin_id(message, repository) is None:
        await message.answer("Команда доступна только администратору студии.")
        return
    await state.clear()
    text, reply_markup = menu_screen()
    await message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data == "b:m")
async def show_menu(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    await state.set_state(None)
    await show(callback, *menu_screen())
    await callback.answer()


def _segment_text(segment: str, params: dict, slot: Any = None) -> str:
    if segment == "with_credits" and params.get("inactive_days"):
        return "с занятиями на балансе, без записи {} дн.".format(
            params["inactive_days"]
        )
    if segment == "slot":
        if slot is not None:
            return "записанные на «{}», {}".format(
                CLASS_LABELS[slot.class_key], format_class_time(slot.starts_at)
            )
        return "записанные на занятие №{}".format(params.get("slot_id"))
    if segment == "direction":
        return "ходили на «{}» за {} дн.".format(
            CLASS_LABELS.get(params.get("class_key"), params.get("class_key")),
            params.get("days"),
        )
    return SEGMENT_LABELS[segment]


@router.callback_query(F.data == "b:h")
async def show_history(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    items = list(await repository.list_broadcasts(limit=HISTORY_LIMIT))
    lines = ["🕘 История рассылок"]
    if not items:
        lines.extend(["", "Рассылок пока не было."])
    for item in items:
        preview = item.body.replace("\n", " ")
        lines.append(
            "\n{} · {}\n{} — получателей {}, доставлено {}, сбоев {}\n«{}»".format(
                format_studio_datetime(item.created_at),
                item.author_name or item.author_telegram_id,
                _segment_text(item.segment, item.segment_params),
                item.recipients,
                item.sent,
                item.failed,
                preview[:80] + ("…" if len(preview) > 80 else ""),
            )
        )
    await show(
        callback,
        "\n".join(lines),
        markup([button("✍️ Новая рассылка", "b:n")], [button("⬅️ Рассылки", "b:m")]),
    )
    await callback.answer()


# ---------- выбор сегмента ----------


@router.callback_query(F.data == "b:n")
async def choose_segment(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    await state.set_state(None)
    await show(
        callback,
        "Кому отправить рассылку?",
        markup(
            [button("Все участники", "b:s:all")],
            [button("С занятиями на балансе", "b:s:wc")],
            [button("Записанные на занятие", "b:s:sl")],
            [button("Ходили на направление", "b:s:dr")],
            [button("⬅️ Рассылки", "b:m")],
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("b:s:"))
async def choose_segment_details(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    kind = (callback.data or "").split(":")[2]
    if kind == "all":
        await _ask_text(callback, state, "all", {})
        return
    if kind == "wc":
        await show(
            callback,
            "С занятиями на балансе. Кому именно?",
            markup(
                [button("Всем с балансом", "b:wc:0")],
                *[
                    [button("Без записи {} дн.".format(days), "b:wc:{}".format(days))]
                    for days in INACTIVE_DAYS
                ],
                [button("⬅️ Назад", "b:n")],
            ),
        )
    elif kind == "sl":
        slots = [
            slot
            for slot in await ScheduleService(repository).admin_schedule(limit=25)
            if slot.status != "cancelled" and slot.booked_count > 0
        ][:SLOTS_TO_PICK]
        rows = [
            [
                button(
                    "{} · {} · 👥 {}".format(
                        format_class_time(slot.starts_at),
                        CLASS_SHORT_LABELS[slot.class_key],
                        slot.booked_count,
                    ),
                    "b:sl:{}".format(slot.id),
                )
            ]
            for slot in slots
        ]
        rows.append([button("⬅️ Назад", "b:n")])
        await show(
            callback,
            "Выберите занятие — рассылка уйдёт записанным на него."
            if slots
            else "Занятий с записями пока нет.",
            markup(*rows),
        )
    elif kind == "dr":
        rows = [
            [button(CLASS_LABELS[key], "b:dr:{}".format(key))]
            for key in ACTIVE_CLASS_KEYS
        ]
        rows.append([button("⬅️ Назад", "b:n")])
        await show(callback, "Какое направление?", markup(*rows))
    else:
        await callback.answer("Неизвестный сегмент.", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("b:wc:"))
async def choose_inactive_days(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    days = _tid(callback)
    params = {"inactive_days": days} if days else {}
    await _ask_text(callback, state, "with_credits", params)


@router.callback_query(F.data.startswith("b:dr:"))
async def choose_direction(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    key = (callback.data or "").split(":")[2]
    if key not in ACTIVE_CLASS_KEYS:
        await callback.answer("Неизвестное направление.", show_alert=True)
        return
    await show(
        callback,
        "«{}». За какой период смотреть записи?".format(CLASS_LABELS[key]),
        markup(
            [
                button("{} дн.".format(days), "b:dd:{}:{}".format(key, days))
                for days in DIRECTION_DAYS
            ],
            [button("⬅️ Назад", "b:s:dr")],
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("b:dd:"))
async def choose_direction_days(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    parts = (callback.data or "").split(":")
    key = parts[2] if len(parts) > 3 else ""
    days = parse_int(parts[3]) if len(parts) > 3 else None
    if key not in ACTIVE_CLASS_KEYS or days not in DIRECTION_DAYS:
        await callback.answer("Неверные параметры.", show_alert=True)
        return
    await _ask_text(callback, state, "direction", {"class_key": key, "days": days})


@router.callback_query(F.data.startswith("b:sl:"))
async def choose_slot(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    slot_id = _tid(callback)
    if await _staff(callback, repository, slot_id=slot_id) is None:
        return
    await _ask_text(callback, state, "slot", {"slot_id": slot_id})


@router.callback_query(F.data.startswith("b:t:"))
async def write_to_slot_participants(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    """Из карточки занятия: преподавателю — только свои, админу — любые."""
    slot_id = _tid(callback)
    if await _staff(callback, repository, slot_id=slot_id) is None:
        return
    await _ask_text(callback, state, "slot", {"slot_id": slot_id})


# ---------- текст и подтверждение ----------


async def _ask_text(
    callback: CallbackQuery, state: FSMContext, segment: str, params: dict
) -> None:
    await state.set_state(AdminForm.waiting_for_broadcast_text)
    await state.update_data(broadcast={"segment": segment, "params": params})
    await callback.answer()
    await callback.message.answer(
        "Введите текст сообщения (до {} символов). Его получат участники "
        "сегмента «{}».".format(MAX_BROADCAST_LENGTH, _segment_text(segment, params)),
        reply_markup=admin_input_keyboard(),
    )


@router.message(AdminForm.waiting_for_broadcast_text)
async def save_broadcast_text(
    message: Message, state: FSMContext, repository: Any
) -> None:
    draft = (await state.get_data()).get("broadcast") or {}
    segment, params = draft.get("segment"), draft.get("params") or {}
    if segment not in SEGMENT_LABELS:
        await state.clear()
        await message.answer("Не удалось продолжить — начните рассылку заново.")
        return
    sender = await get_staff_id(message, repository, params.get("slot_id"))
    if sender is None or (
        segment != "slot" and await get_admin_id(message, repository) is None
    ):
        await state.clear()
        return
    body = (message.text or "").strip()
    if not 1 <= len(body) <= MAX_BROADCAST_LENGTH:
        await message.answer(
            "Текст — от 1 до {} символов.".format(MAX_BROADCAST_LENGTH)
        )
        return
    try:
        recipients = await repository.count_segment(segment, params)
    except ValueError as error:
        await state.clear()
        await message.answer("{}.".format(error))
        return
    slot = (
        await ScheduleService(repository).get(params["slot_id"])
        if segment == "slot"
        else None
    )
    await _set_pending(
        state, action="broadcast", segment=segment, params=params, body=body
    )
    await message.answer(
        "Отправить рассылку?\n\nКому: {}\nПолучателей: {}\n\n{}".format(
            _segment_text(segment, params, slot), recipients, body
        ),
        reply_markup=markup(
            [button("✅ Отправить", "b:ok")] if recipients else [],
            [button("Отмена", "b:m" if segment != "slot" else "a:sch")],
        ),
    )


@router.callback_query(F.data == "b:ok")
async def confirm_broadcast(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    role = await callback_staff_role(callback, repository)
    if role is None:
        await callback.answer("Доступно только сотрудникам студии.", show_alert=True)
        return
    pending = await _take_pending(state, "broadcast")
    if pending is None:
        await _expired(callback)
        return
    sender = callback.from_user.id
    is_admin = role == STAFF_ADMIN
    if not is_admin and (
        pending["segment"] != "slot"
        or not await repository.is_slot_teacher(pending["params"]["slot_id"], sender)
    ):
        await _clear_pending(state)
        await callback.answer(
            "Преподаватель пишет только участникам своего занятия.", show_alert=True
        )
        return
    try:
        broadcast = await repository.create_broadcast(
            sender,
            pending["body"],
            pending["segment"],
            pending["params"],
            author_is_admin_required=is_admin,
        )
    except (ValueError, PermissionError) as error:
        await _clear_pending(state)
        await callback.answer("Не отправлено: {}".format(error), show_alert=True)
        return
    await _clear_pending(state)
    await state.update_data(broadcast=None)
    actions_logger().info(
        "action=broadcast admin_id=%s broadcast_id=%s segment=%s recipients=%s",
        sender,
        broadcast.id,
        pending["segment"],
        broadcast.recipients,
    )
    await show(
        callback,
        "Рассылка №{} поставлена в очередь ✓\nПолучателей: {}. Доставка начнётся "
        "в течение минуты; итог — в «🕘 История рассылок».".format(
            broadcast.id, broadcast.recipients
        ),
        markup([button("⬅️ Рассылки", "b:m")])
        if is_admin
        else markup([button("⬅️ К расписанию", "a:sch")]),
    )
    await callback.answer("Отправлено")
