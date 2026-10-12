"""Настройки студии в боте: каталог, правила, сотрудники, сбои, заметки.

Раздел «⚙️ Настройки» админ-меню (только администратор; `/settings`) — то же,
что в админке сайта: абонементы и направления, правила записи, роли
сотрудников, сбои доставки уведомлений, а в карточке участника — служебные
заметки. Бизнес-логика — в ``CatalogService`` и репозитории; здесь только
экраны, ввод значений и подтверждение: каждое изменение выполняется после
экрана «было → станет» (подтверждение живёт 10 минут, как в админ-панели).

callback_data (префикс ``s:``, ≤ 64 байт):

  s:m                         меню настроек
  s:l:<pk|cl>                 список абонементов / направлений
  s:c:<k>:<key>               карточка          s:e:<k>:<key>:<i>  поле i
  s:t:<k>:<key>               показать/скрыть   s:n:<k>            новый
  s:okc                       подтвердить изменение каталога
  s:ru / s:re:<i> / s:rok     правила студии
  s:st / s:sc:<tid>           сотрудники        s:sa / s:sar:<role>
  s:sr:<tid>:<role> / s:sn:<tid> / s:sd:<tid>   роль / имя / снять
  s:oks / s:okx               подтвердить назначение / снятие роли
  s:nf[:<before>]             сбои доставки уведомлений
  s:nl:<tid> / s:na:<tid> / s:nd:<tid>:<id> / s:okn   заметки участника
"""

import logging
import re
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ..class_catalog import ClassFormat
from ..database.repository import STAFF_ADMIN, STAFF_TEACHER
from ..database.staff_postgres import LastAdminError
from ..keyboards.admin import ADMIN_SETTINGS, admin_input_keyboard
from ..keyboards.user.screens import button, markup, parse_int, show
from ..payments.catalog import LessonPackage
from ..runtime.admin_access import get_admin_id
from ..services import CatalogService
from ..studio_time import format_studio_datetime
from .admin import actions_logger
from .admin_panel import (
    _admin,
    _clear_pending,
    _expired,
    _set_pending,
    _take_pending,
    _tid,
)
from .states import AdminForm

logger = logging.getLogger("bot.handlers.admin_settings")
router = Router(name="admin_settings")

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_INTEGER = re.compile(r"^-?\d{1,9}$")
ROLE_LABELS = {STAFF_ADMIN: "администратор", STAFF_TEACHER: "преподаватель"}
FAILED_PAGE = 10


# ---------- описание полей каталога ----------


def _text(minimum: int, maximum: int, blank_dash: bool = False) -> Callable:
    def parse(raw: str) -> str:
        value = raw.strip()
        if blank_dash and value == "-":
            return ""
        if not minimum <= len(value) <= maximum:
            raise ValueError(
                "Длина — от {} до {} символов{}".format(
                    minimum, maximum, ", «-» — очистить" if blank_dash else ""
                )
            )
        return value

    return parse


def _number(minimum: int, maximum: int) -> Callable:
    def parse(raw: str) -> int:
        value = raw.strip()
        if not _INTEGER.match(value) or not minimum <= int(value) <= maximum:
            raise ValueError("Введите целое число от {} до {}".format(minimum, maximum))
        return int(value)

    return parse


def _validity(raw: str) -> Optional[int]:
    value = _number(0, 3650)(raw)
    return value or None


@dataclass(frozen=True)
class _Field:
    name: str
    label: str
    parse: Callable
    hint: str = ""

    def show(self, value: Any) -> str:
        if self.name == "validity_days":
            return "{} дн.".format(value) if value else "бессрочно"
        if self.name == "price_rub":
            return "{} ₽".format(value)
        return str(value) if value not in (None, "") else "—"


@dataclass(frozen=True)
class _Kind:
    code: str
    plural: str
    one: str
    fields: tuple
    # Поля, которые спрашиваются подряд при создании (после ключа).
    maker: Callable


def _package(values: dict) -> LessonPackage:
    return LessonPackage(**values)


def _format(values: dict) -> ClassFormat:
    return ClassFormat(**values)


KINDS = {
    "pk": _Kind(
        "pk",
        "🎟 Абонементы",
        "абонемент",
        (
            _Field("title", "Название", _text(1, 100)),
            _Field("lessons", "Занятий в абонементе", _number(1, 100)),
            _Field("price_rub", "Цена, ₽", _number(1, 10_000_000)),
            _Field(
                "validity_days",
                "Срок, дней",
                _validity,
                "0 — бессрочно",
            ),
            _Field("sort_order", "Порядок в списке", _number(-10000, 10000)),
        ),
        _package,
    ),
    "cl": _Kind(
        "cl",
        "💃 Направления",
        "направление",
        (
            _Field("title", "Название", _text(1, 100)),
            _Field("short_title", "Короткое название (для кнопок)", _text(1, 32)),
            _Field("description", "Описание", _text(0, 1000, True), "«-» — пусто"),
            _Field("level", "Уровень", _text(0, 100, True), "«-» — пусто"),
            _Field("sort_order", "Порядок в списке", _number(-10000, 10000)),
        ),
        _format,
    ),
}

RULE_FIELDS = (
    (
        "cancellation_deadline_hours",
        "Окно отмены записи, часов до начала",
        _number(0, 168),
    ),
    (
        "rebook_cooldown_hours",
        "Пауза повторной записи после своей отмены, часов",
        _number(0, 168),
    ),
    (
        "manual_credit_validity_days",
        "Срок ручных начислений, дней",
        _number(1, 3650),
    ),
    (
        "expiry_warning_days",
        "Предупреждать о скором окончании срока, дней (0 — не надо)",
        _number(0, 60),
    ),
)


async def _items(kind: _Kind, repository: Any) -> list:
    service = CatalogService(repository)
    if kind.code == "pk":
        return list(await service.packages())
    return list(await service.class_formats())


async def _find(kind: _Kind, repository: Any, key: str) -> Any:
    for item in await _items(kind, repository):
        if item.key == key:
            return item
    return None


async def _save(
    kind: _Kind, repository: Any, item: Any, admin_id: int, create: bool
) -> Any:
    service = CatalogService(repository)
    if kind.code == "pk":
        return await service.save_package(item, admin_id, create)
    return await service.save_class_format(item, admin_id, create)


def _kind(callback: CallbackQuery, index: int = 2) -> Optional[_Kind]:
    parts = (callback.data or "").split(":")
    return KINDS.get(parts[index]) if len(parts) > index else None


# ---------- меню ----------


def settings_menu() -> tuple[str, InlineKeyboardMarkup]:
    return "⚙️ Настройки студии", markup(
        [button(KINDS["pk"].plural, "s:l:pk"), button(KINDS["cl"].plural, "s:l:cl")],
        [button("📏 Правила записи", "s:ru"), button("👥 Сотрудники", "s:st")],
        [button("🔔 Сбои уведомлений", "s:nf")],
    )


@router.message(F.text == ADMIN_SETTINGS)
@router.message(Command("settings"))
async def open_settings(message: Message, state: FSMContext, repository: Any) -> None:
    if await get_admin_id(message, repository) is None:
        await message.answer("Команда доступна только администратору студии.")
        return
    await state.clear()
    text, reply_markup = settings_menu()
    await message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data == "s:m")
async def show_settings(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    await state.set_state(None)
    await show(callback, *settings_menu())
    await callback.answer()


# ---------- каталог: абонементы и направления ----------


def _item_lines(kind: _Kind, item: Any) -> list:
    lines = ["{} «{}» ({})".format(kind.one.capitalize(), item.title, item.key)]
    for field in kind.fields[1:]:
        lines.append(
            "{}: {}".format(field.label, field.show(getattr(item, field.name)))
        )
    lines.append("Показывается: {}".format("да" if item.is_active else "нет (скрыт)"))
    return lines


@router.callback_query(F.data.startswith("s:l:"))
async def show_catalog(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    kind = _kind(callback)
    if await _admin(callback, repository) is None or kind is None:
        return
    await state.set_state(None)
    items = await _items(kind, repository)
    lines = [kind.plural]
    if not items:
        lines.append("Список пуст.")
    rows = [
        [
            button(
                "{}{}".format("" if item.is_active else "🙈 ", item.title[:40]),
                "s:c:{}:{}".format(kind.code, item.key),
            )
        ]
        for item in items
    ]
    rows.append([button("➕ Добавить", "s:n:{}".format(kind.code))])
    rows.append([button("⬅️ Настройки", "s:m")])
    await show(callback, "\n".join(lines), markup(*rows))
    await callback.answer()


async def _catalog_card(
    kind: _Kind, repository: Any, key: str, notice: str = ""
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    item = await _find(kind, repository, key)
    if item is None:
        return None
    lines = [notice, ""] if notice else []
    lines.extend(_item_lines(kind, item))
    rows = [
        [
            button(
                "✏️ {}".format(field.label[:30]),
                "s:e:{}:{}:{}".format(kind.code, key, i),
            )
        ]
        for i, field in enumerate(kind.fields)
    ]
    rows.append(
        [
            button(
                "🙈 Скрыть" if item.is_active else "👁 Показать",
                "s:t:{}:{}".format(kind.code, key),
            )
        ]
    )
    rows.append([button("⬅️ К списку", "s:l:{}".format(kind.code))])
    return "\n".join(lines), markup(*rows)


@router.callback_query(F.data.startswith("s:c:"))
async def show_catalog_item(callback: CallbackQuery, repository: Any) -> None:
    kind = _kind(callback)
    if await _admin(callback, repository) is None or kind is None:
        return
    key = (callback.data or "").split(":")[3]
    screen = await _catalog_card(kind, repository, key)
    if screen is None:
        await callback.answer("Не найдено.", show_alert=True)
        return
    await show(callback, *screen)
    await callback.answer()


@router.callback_query(F.data.startswith("s:e:"))
async def start_catalog_edit(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    kind = _kind(callback)
    if await _admin(callback, repository) is None or kind is None:
        return
    parts = (callback.data or "").split(":")
    index = parse_int(parts[4]) if len(parts) > 4 else None
    item = await _find(kind, repository, parts[3])
    if item is None or index is None or index >= len(kind.fields):
        await callback.answer("Не найдено.", show_alert=True)
        return
    field = kind.fields[index]
    await state.set_state(AdminForm.waiting_for_catalog_value)
    await state.update_data(
        catalog_edit={"kind": kind.code, "key": item.key, "field": field.name}
    )
    await callback.answer()
    await callback.message.answer(
        "{}: сейчас {}.\nВведите новое значение{}.".format(
            field.label,
            field.show(getattr(item, field.name)),
            " ({})".format(field.hint) if field.hint else "",
        ),
        reply_markup=admin_input_keyboard(),
    )


@router.message(AdminForm.waiting_for_catalog_value)
async def save_catalog_value(
    message: Message, state: FSMContext, repository: Any
) -> None:
    admin_id = await get_admin_id(message, repository)
    if admin_id is None:
        await state.clear()
        return
    data = (await state.get_data()).get("catalog_edit") or {}
    if data.get("create") and data.get("kind") in KINDS:
        await _create_step(message, state, repository, data)
        return
    kind = KINDS.get(data.get("kind", ""))
    field = (
        next((f for f in kind.fields if f.name == data.get("field")), None)
        if kind
        else None
    )
    item = await _find(kind, repository, data.get("key", "")) if kind else None
    if kind is None or field is None or item is None:
        await state.clear()
        await message.answer("Не удалось продолжить — откройте карточку заново.")
        return
    try:
        value = field.parse(message.text or "")
    except ValueError as error:
        await message.answer("{}.".format(error))
        return
    changed = replace(item, **{field.name: value})
    await _set_pending(
        state,
        action="catalog_save",
        kind=kind.code,
        values=asdict(changed),
        create=False,
    )
    await message.answer(
        "Изменить «{}»?\n\n{}: {} → {}\n\n"
        "Новое значение действует для следующих покупок и выдач; уже купленные "
        "абонементы и записанные занятия не меняются.".format(
            item.title,
            field.label,
            field.show(getattr(item, field.name)),
            field.show(value),
        ),
        reply_markup=markup(
            [button("✅ Применить", "s:okc")],
            [button("Отмена", "s:c:{}:{}".format(kind.code, item.key))],
        ),
    )


@router.callback_query(F.data.startswith("s:t:"))
async def confirm_toggle_screen(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    kind = _kind(callback)
    if await _admin(callback, repository) is None or kind is None:
        return
    item = await _find(kind, repository, (callback.data or "").split(":")[3])
    if item is None:
        await callback.answer("Не найдено.", show_alert=True)
        return
    changed = replace(item, is_active=not item.is_active)
    await _set_pending(
        state,
        action="catalog_save",
        kind=kind.code,
        values=asdict(changed),
        create=False,
    )
    await show(
        callback,
        "{} «{}»?\n{}".format(
            "Показать" if changed.is_active else "Скрыть",
            item.title,
            "Он снова появится в продаже и расписании."
            if changed.is_active
            else "Новые покупки и записи будут недоступны; прошлые остаются.",
        ),
        markup(
            [button("✅ Да", "s:okc")],
            [button("Отмена", "s:c:{}:{}".format(kind.code, item.key))],
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "s:okc")
async def confirm_catalog_save(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    admin_id = await _admin(callback, repository)
    if admin_id is None:
        return
    pending = await _take_pending(state, "catalog_save")
    if pending is None:
        await _expired(callback)
        return
    kind = KINDS[pending["kind"]]
    item = kind.maker(pending["values"])
    try:
        await _save(kind, repository, item, admin_id, pending["create"])
    except (ValueError, LookupError) as error:
        await _clear_pending(state)
        await callback.answer("Не сохранено: {}".format(error), show_alert=True)
        return
    await _clear_pending(state)
    actions_logger().info(
        "action=catalog_save admin_id=%s kind=%s key=%s create=%s",
        admin_id,
        kind.code,
        item.key,
        pending["create"],
    )
    screen = await _catalog_card(kind, repository, item.key, "Сохранено ✓")
    await show(callback, *(screen or settings_menu()))
    await callback.answer("Сохранено")


# --- создание ---


@router.callback_query(F.data.startswith("s:n:"))
async def start_catalog_create(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    kind = _kind(callback)
    if await _admin(callback, repository) is None or kind is None:
        return
    await state.set_state(AdminForm.waiting_for_catalog_value)
    await state.update_data(
        catalog_edit={"kind": kind.code, "create": True, "values": {}, "step": -1}
    )
    await callback.answer()
    await callback.message.answer(
        "Новый {}. Введите ключ — латиницей, строчными буквами, цифрами и "
        "«_», 2–32 символа, начинается с буквы (например, pack_12). "
        "Позже его изменить нельзя.".format(kind.one),
        reply_markup=admin_input_keyboard(),
    )


async def _create_step(
    message: Message, state: FSMContext, repository: Any, data: dict
) -> None:
    kind = KINDS[data["kind"]]
    step = data["step"]
    text = (message.text or "").strip()
    values = data["values"]
    if step == -1:
        if not KEY_PATTERN.match(text):
            await message.answer(
                "Ключ: латиница, цифры и «_», 2–32 символа, начинается с буквы."
            )
            return
        if await _find(kind, repository, text) is not None:
            await message.answer("Такой ключ уже есть. Введите другой.")
            return
        values["key"] = text
    else:
        field = kind.fields[step]
        try:
            values[field.name] = field.parse(text)
        except ValueError as error:
            await message.answer("{}.".format(error))
            return
    step += 1
    if step < len(kind.fields):
        field = kind.fields[step]
        data.update(step=step, values=values)
        await state.update_data(catalog_edit=data)
        await message.answer(
            "{}{}:".format(
                field.label, " ({})".format(field.hint) if field.hint else ""
            )
        )
        return
    values["is_active"] = True
    item = kind.maker(values)
    await _set_pending(
        state, action="catalog_save", kind=kind.code, values=asdict(item), create=True
    )
    await message.answer(
        "Создать {}?\n\n{}".format(kind.one, "\n".join(_item_lines(kind, item))),
        reply_markup=markup(
            [button("✅ Создать", "s:okc")],
            [button("Отмена", "s:l:{}".format(kind.code))],
        ),
    )


# ---------- правила студии ----------


async def _rules_screen(
    repository: Any, notice: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    current = await repository.get_studio_rules()
    lines = [notice, ""] if notice else []
    lines.append("📏 Правила записи")
    rows = []
    for index, (name, label, _) in enumerate(RULE_FIELDS):
        lines.append("• {}: {}".format(label, getattr(current, name)))
        rows.append([button("✏️ {}".format(label[:40]), "s:re:{}".format(index))])
    rows.append([button("⬅️ Настройки", "s:m")])
    return "\n".join(lines), markup(*rows)


@router.callback_query(F.data == "s:ru")
async def show_rules(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    await state.set_state(None)
    await show(callback, *await _rules_screen(repository))
    await callback.answer()


@router.callback_query(F.data.startswith("s:re:"))
async def start_rule_edit(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    index = _tid(callback)
    if index >= len(RULE_FIELDS):
        await callback.answer("Не найдено.", show_alert=True)
        return
    name, label, _ = RULE_FIELDS[index]
    current = getattr(await repository.get_studio_rules(), name)
    await state.set_state(AdminForm.waiting_for_rule_value)
    await state.update_data(rule_index=index)
    await callback.answer()
    await callback.message.answer(
        "{}: сейчас {}.\nВведите новое число.".format(label, current),
        reply_markup=admin_input_keyboard(),
    )


@router.message(AdminForm.waiting_for_rule_value)
async def save_rule_value(message: Message, state: FSMContext, repository: Any) -> None:
    if await get_admin_id(message, repository) is None:
        await state.clear()
        return
    index = (await state.get_data()).get("rule_index")
    if not isinstance(index, int) or not 0 <= index < len(RULE_FIELDS):
        await state.clear()
        await message.answer("Не удалось продолжить — откройте правила заново.")
        return
    name, label, parse = RULE_FIELDS[index]
    try:
        value = parse(message.text or "")
    except ValueError as error:
        await message.answer("{}.".format(error))
        return
    current = getattr(await repository.get_studio_rules(), name)
    await _set_pending(state, action="rule_save", field=name, value=value)
    await message.answer(
        "Изменить правило?\n\n{}: {} → {}\n\n"
        "Новое значение действует сразу для всех новых записей и отмен.".format(
            label, current, value
        ),
        reply_markup=markup(
            [button("✅ Применить", "s:rok")], [button("Отмена", "s:ru")]
        ),
    )


@router.callback_query(F.data == "s:rok")
async def confirm_rule_save(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    admin_id = await _admin(callback, repository)
    if admin_id is None:
        return
    pending = await _take_pending(state, "rule_save")
    if pending is None:
        await _expired(callback)
        return
    try:
        await CatalogService(repository).update_rules(
            admin_id, **{pending["field"]: pending["value"]}
        )
    except (ValueError, LookupError) as error:
        await _clear_pending(state)
        await callback.answer("Не сохранено: {}".format(error), show_alert=True)
        return
    await _clear_pending(state)
    actions_logger().info(
        "action=rules_save admin_id=%s field=%s value=%s",
        admin_id,
        pending["field"],
        pending["value"],
    )
    await show(callback, *await _rules_screen(repository, "Сохранено ✓"))
    await callback.answer("Сохранено")


# ---------- сотрудники ----------


async def _staff_screen(
    repository: Any, notice: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    members = list(await repository.list_staff())
    lines = [notice, ""] if notice else []
    lines.append("👥 Сотрудники")
    rows = [
        [
            button(
                "{} · {}".format(member.public_name[:30], ROLE_LABELS[member.role]),
                "s:sc:{}".format(member.telegram_id),
            )
        ]
        for member in members
    ]
    rows.append([button("➕ Назначить", "s:sa")])
    rows.append([button("⬅️ Настройки", "s:m")])
    return "\n".join(lines), markup(*rows)


@router.callback_query(F.data == "s:st")
async def show_staff(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    await state.set_state(None)
    await show(callback, *await _staff_screen(repository))
    await callback.answer()


@router.callback_query(F.data.startswith("s:sc:"))
async def show_staff_member(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    tid = _tid(callback)
    member = await repository.get_staff_member(tid)
    if member is None:
        await callback.answer("Сотрудник не найден.", show_alert=True)
        return
    other = STAFF_TEACHER if member.role == STAFF_ADMIN else STAFF_ADMIN
    lines = [
        "{} (ID {})".format(member.user_name, tid),
        "Роль: {}".format(ROLE_LABELS[member.role]),
        "Имя в расписании: {}".format(member.display_name or "— (как в профиле)"),
    ]
    await show(
        callback,
        "\n".join(lines),
        markup(
            [
                button(
                    "Сделать: {}".format(ROLE_LABELS[other]),
                    "s:sr:{}:{}".format(tid, other),
                )
            ],
            [button("✏️ Имя в расписании", "s:sn:{}".format(tid))],
            [button("🗑 Снять роль", "s:sd:{}".format(tid))],
            [button("⬅️ К списку", "s:st")],
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("s:sr:"))
async def confirm_staff_role_screen(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    parts = (callback.data or "").split(":")
    tid = parse_int(parts[2]) if len(parts) > 3 else None
    role = parts[3] if len(parts) > 3 else ""
    member = await repository.get_staff_member(tid) if tid else None
    if member is None or role not in ROLE_LABELS:
        await callback.answer("Сотрудник не найден.", show_alert=True)
        return
    await _set_pending(
        state,
        action="staff_set",
        telegram_id=tid,
        role=role,
        display_name=member.display_name,
    )
    await show(
        callback,
        "Сменить роль?\n\n{}: {} → {}".format(
            member.user_name, ROLE_LABELS[member.role], ROLE_LABELS[role]
        ),
        markup(
            [button("✅ Применить", "s:oks")],
            [button("Отмена", "s:sc:{}".format(tid))],
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("s:sn:"))
async def start_staff_name(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    tid = _tid(callback)
    member = await repository.get_staff_member(tid)
    if member is None:
        await callback.answer("Сотрудник не найден.", show_alert=True)
        return
    await state.set_state(AdminForm.waiting_for_staff_name)
    await state.update_data(staff_edit={"telegram_id": tid, "role": member.role})
    await callback.answer()
    await callback.message.answer(
        "Имя в расписании (до 64 символов) — его видят участники. «-» — "
        "показывать имя из профиля.",
        reply_markup=admin_input_keyboard(),
    )


@router.message(AdminForm.waiting_for_staff_name)
async def save_staff_name(message: Message, state: FSMContext, repository: Any) -> None:
    if await get_admin_id(message, repository) is None:
        await state.clear()
        return
    data = (await state.get_data()).get("staff_edit") or {}
    text = (message.text or "").strip()
    if not data.get("telegram_id") or not 1 <= len(text) <= 64:
        await message.answer("Введите имя длиной от 1 до 64 символов (или «-»).")
        return
    await _set_pending(
        state,
        action="staff_set",
        telegram_id=data["telegram_id"],
        role=data["role"],
        display_name=None if text == "-" else text,
    )
    await message.answer(
        "Имя в расписании: {}. Применить?".format(
            "из профиля" if text == "-" else "«{}»".format(text)
        ),
        reply_markup=markup(
            [button("✅ Применить", "s:oks")],
            [button("Отмена", "s:sc:{}".format(data["telegram_id"]))],
        ),
    )


@router.callback_query(F.data == "s:sa")
async def start_staff_add(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    await state.set_state(AdminForm.waiting_for_staff_id)
    await callback.answer()
    await callback.message.answer(
        "Введите Telegram ID человека. У него уже должен быть профиль в боте "
        "(он нажимал /start).",
        reply_markup=admin_input_keyboard(),
    )


@router.message(AdminForm.waiting_for_staff_id)
async def save_staff_id(message: Message, state: FSMContext, repository: Any) -> None:
    if await get_admin_id(message, repository) is None:
        await state.clear()
        return
    tid = parse_int((message.text or "").strip())
    profile = await repository.get_profile(tid) if tid else None
    if profile is None:
        await message.answer(
            "Профиль с таким ID не найден. Человек должен сначала нажать /start "
            "в боте. Введите другой ID."
        )
        return
    await state.set_state(None)
    await state.update_data(staff_edit={"telegram_id": tid})
    await message.answer(
        "{} (ID {}). Какая роль?".format(profile.user_name, tid),
        reply_markup=markup(
            [button("Преподаватель", "s:sar:{}".format(STAFF_TEACHER))],
            [button("Администратор", "s:sar:{}".format(STAFF_ADMIN))],
            [button("Отмена", "s:st")],
        ),
    )


@router.callback_query(F.data.startswith("s:sar:"))
async def confirm_staff_add_screen(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    role = (callback.data or "").split(":")[2]
    tid = ((await state.get_data()).get("staff_edit") or {}).get("telegram_id")
    profile = await repository.get_profile(tid) if tid else None
    if profile is None or role not in ROLE_LABELS:
        await callback.answer("Начните назначение заново.", show_alert=True)
        return
    await _set_pending(
        state, action="staff_set", telegram_id=tid, role=role, display_name=None
    )
    await show(
        callback,
        "Назначить?\n\n{} (ID {}) — {}".format(
            profile.user_name, tid, ROLE_LABELS[role]
        ),
        markup([button("✅ Назначить", "s:oks")], [button("Отмена", "s:st")]),
    )
    await callback.answer()


@router.callback_query(F.data == "s:oks")
async def confirm_staff_set(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    admin_id = await _admin(callback, repository)
    if admin_id is None:
        return
    pending = await _take_pending(state, "staff_set")
    if pending is None:
        await _expired(callback)
        return
    try:
        await repository.set_staff_role(
            pending["telegram_id"],
            pending["role"],
            admin_id,
            pending.get("display_name"),
        )
    except (LastAdminError, LookupError, ValueError, PermissionError) as error:
        await _clear_pending(state)
        await callback.answer("Не сохранено: {}".format(error), show_alert=True)
        return
    await _clear_pending(state)
    actions_logger().info(
        "action=staff_set admin_id=%s telegram_id=%s role=%s",
        admin_id,
        pending["telegram_id"],
        pending["role"],
    )
    await show(callback, *await _staff_screen(repository, "Сохранено ✓"))
    await callback.answer("Сохранено")


@router.callback_query(F.data.startswith("s:sd:"))
async def confirm_staff_remove_screen(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    tid = _tid(callback)
    member = await repository.get_staff_member(tid)
    if member is None:
        await callback.answer("Сотрудник не найден.", show_alert=True)
        return
    await _set_pending(state, action="staff_remove", telegram_id=tid)
    await show(
        callback,
        "Снять роль «{}» с {}?\nЕго занятия останутся, но без преподавателя.".format(
            ROLE_LABELS[member.role], member.user_name
        ),
        markup(
            [button("✅ Снять", "s:okx")], [button("Отмена", "s:sc:{}".format(tid))]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "s:okx")
async def confirm_staff_remove(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    admin_id = await _admin(callback, repository)
    if admin_id is None:
        return
    pending = await _take_pending(state, "staff_remove")
    if pending is None:
        await _expired(callback)
        return
    try:
        removed = await repository.remove_staff(pending["telegram_id"], admin_id)
    except (LastAdminError, PermissionError) as error:
        await _clear_pending(state)
        await callback.answer("Не снято: {}".format(error), show_alert=True)
        return
    await _clear_pending(state)
    actions_logger().info(
        "action=staff_remove admin_id=%s telegram_id=%s removed=%s",
        admin_id,
        pending["telegram_id"],
        removed,
    )
    await show(
        callback,
        *await _staff_screen(
            repository, "Роль снята ✓" if removed else "Он уже не сотрудник"
        ),
    )
    await callback.answer("Готово")


# ---------- сбои доставки уведомлений ----------

DELIVERY_LABELS = {
    "pending": "ждут отправки",
    "sending": "отправляются",
    "sent": "доставлено",
    "failed": "не доставлено",
    "skipped": "пропущено",
}


@router.callback_query(F.data.startswith("s:nf"))
async def show_failed_notifications(callback: CallbackQuery, repository: Any) -> None:
    if await _admin(callback, repository) is None:
        return
    parts = (callback.data or "").split(":")
    before = parse_int(parts[2]) if len(parts) > 2 else None
    items = list(
        await repository.list_failed_notifications(FAILED_PAGE + 1, before_id=before)
    )
    has_more = len(items) > FAILED_PAGE
    items = items[:FAILED_PAGE]
    stats = await repository.notification_delivery_stats(30)
    lines = ["🔔 Доставка уведомлений за 30 дней"]
    for status in DELIVERY_LABELS:
        if status in stats:
            lines.append("• {}: {}".format(DELIVERY_LABELS[status], stats[status]))
    if stats.get("unreachable_users"):
        lines.append(
            "Не удалось доставить участникам: {}".format(stats["unreachable_users"])
        )
    lines.append("")
    if not items:
        lines.append("Сбоев нет.")
    for item in items:
        lines.append(
            "• №{} {} — {} ({}), попыток {}{}".format(
                item.id,
                item.user_name or item.telegram_id,
                item.kind,
                DELIVERY_LABELS.get(item.delivery_status, item.delivery_status),
                item.attempts,
                ": {}".format(item.last_error[:80]) if item.last_error else "",
            )
        )
    rows = []
    if has_more and items:
        rows.append([button("Раньше ➡️", "s:nf:{}".format(items[-1].id))])
    rows.append([button("⬅️ Настройки", "s:m")])
    await show(callback, "\n".join(lines), markup(*rows))
    await callback.answer()


# ---------- заметки об участнике ----------


async def _notes_screen(
    repository: Any, tid: int, notice: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    notes = list(await repository.list_client_notes(tid))
    lines = [notice, ""] if notice else []
    lines.append("📝 Заметки об участнике {}".format(tid))
    if not notes:
        lines.append("Заметок пока нет.")
    rows = []
    for note in notes[:10]:
        lines.append(
            "\n{} · {}\n{}".format(
                format_studio_datetime(note.created_at),
                note.author_name or note.author_telegram_id,
                note.body,
            )
        )
        rows.append(
            [button("🗑 №{}".format(note.id), "s:nd:{}:{}".format(tid, note.id))]
        )
    rows.append([button("➕ Добавить заметку", "s:na:{}".format(tid))])
    rows.append([button("⬅️ К карточке", "a:c:{}".format(tid))])
    return "\n".join(lines), markup(*rows)


@router.callback_query(F.data.startswith("s:nl:"))
async def show_notes(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    await state.set_state(None)
    await show(callback, *await _notes_screen(repository, _tid(callback)))
    await callback.answer()


@router.callback_query(F.data.startswith("s:na:"))
async def start_note(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    await state.set_state(AdminForm.waiting_for_note)
    await state.update_data(note_for=_tid(callback))
    await callback.answer()
    await callback.message.answer(
        "Введите текст заметки (до 1000 символов). Её видит только персонал; "
        "преподаватель видит последнюю заметку у участников своего занятия.",
        reply_markup=admin_input_keyboard(),
    )


@router.message(AdminForm.waiting_for_note)
async def save_note(message: Message, state: FSMContext, repository: Any) -> None:
    admin_id = await get_admin_id(message, repository)
    if admin_id is None:
        await state.clear()
        return
    tid = (await state.get_data()).get("note_for")
    if not tid:
        await state.clear()
        await message.answer("Не удалось продолжить — откройте карточку заново.")
        return
    try:
        await repository.add_client_note(tid, admin_id, message.text or "")
    except ValueError as error:
        await message.answer("{}.".format(error))
        return
    except LookupError as error:
        await state.clear()
        await message.answer("{}.".format(error))
        return
    await state.set_state(None)
    actions_logger().info("action=note_add admin_id=%s telegram_id=%s", admin_id, tid)
    text, reply_markup = await _notes_screen(repository, tid, "Заметка добавлена ✓")
    await message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data.startswith("s:nd:"))
async def confirm_note_delete_screen(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    if await _admin(callback, repository) is None:
        return
    tid, note_id = _tid(callback), _tid(callback, 3)
    await _set_pending(state, action="note_delete", telegram_id=tid, note_id=note_id)
    await show(
        callback,
        "Удалить заметку №{}?".format(note_id),
        markup(
            [button("✅ Удалить", "s:okn")], [button("Отмена", "s:nl:{}".format(tid))]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "s:okn")
async def confirm_note_delete(
    callback: CallbackQuery, repository: Any, state: FSMContext
) -> None:
    admin_id = await _admin(callback, repository)
    if admin_id is None:
        return
    pending = await _take_pending(state, "note_delete")
    if pending is None:
        await _expired(callback)
        return
    deleted = await repository.delete_client_note(
        pending["telegram_id"], pending["note_id"], admin_id
    )
    await _clear_pending(state)
    actions_logger().info(
        "action=note_delete admin_id=%s telegram_id=%s note_id=%s deleted=%s",
        admin_id,
        pending["telegram_id"],
        pending["note_id"],
        deleted,
    )
    await show(
        callback,
        *await _notes_screen(
            repository,
            pending["telegram_id"],
            "Заметка удалена ✓" if deleted else "Заметки уже нет",
        ),
    )
    await callback.answer("Готово")
