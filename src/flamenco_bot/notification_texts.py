"""Тексты уведомлений участникам — одни для Telegram и ленты на сайте.

Outbox хранит тип уведомления и данные события (``payload``), а не готовый
текст: формулировки и формат времени (пояс студии) задаются здесь, в одном
месте. ``actions`` — кнопки бота (callback_data экранов бота); сайт их не
показывает.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Tuple

from .class_catalog import CLASS_LABELS
from .presentation import balance_line, format_class_time, lessons_count
from .studio_time import format_studio_datetime, to_studio_time


MY_CLASSES_ACTION = ("💃 Мои занятия", "my")
SCHEDULE_ACTION = ("📅 Расписание", "slots:all")
BUY_ACTION = ("💳 Купить абонемент", "packs:0")
PACKAGES_ACTION = ("🎟 Абонементы", "mypacks")


# Название студии в уведомлениях об обслуживании сайта — как на сайте и в
# боте.
STUDIO_NAME = "Mirada Studio"


@dataclass(frozen=True)
class NotificationMessage:
    title: str
    body: str
    actions: Tuple[Tuple[str, str], ...] = ()

    @property
    def text(self) -> str:
        return "{}\n\n{}".format(self.title, self.body)


def _moment(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _class_line(payload: Mapping[str, Any], key: str = "starts_at") -> str:
    label = CLASS_LABELS.get(payload.get("class_key", ""), "Занятие")
    moment = _moment(payload.get(key))
    return "{} — {}".format(format_class_time(moment), label) if moment else label


def _balance(payload: Mapping[str, Any]) -> str:
    balance = payload.get("balance")
    return balance_line(int(balance)) + "." if balance is not None else ""


def _reason(payload: Mapping[str, Any]) -> str:
    reason = payload.get("reason")
    return "Причина: {}.".format(str(reason).rstrip(".")) if reason else ""


def _join(*lines: str) -> str:
    return "\n".join(line for line in lines if line)


def _day_word(moment: datetime, now: datetime) -> str:
    days = (to_studio_time(moment).date() - to_studio_time(now).date()).days
    if days == 0:
        return "Сегодня"
    if days == 1:
        return "Завтра"
    return format_class_time(moment).split(" · ")[0]


def _maintenance_end(payload: Mapping[str, Any], unknown: str) -> str:
    ends_at = _moment(payload.get("ends_at"))
    if ends_at is None:
        return unknown
    return "Ожидаемое завершение: {}.".format(format_studio_datetime(ends_at))


def _paragraphs(*blocks: str) -> str:
    return "\n\n".join(block for block in blocks if block)


def _render_maintenance(kind: str, payload: Mapping[str, Any]) -> NotificationMessage:
    starts_at = _moment(payload.get("starts_at"))
    start_line = format_studio_datetime(starts_at) if starts_at else ""
    if kind == "maintenance_warning":
        return NotificationMessage(
            "{}: плановое обслуживание".format(STUDIO_NAME),
            _paragraphs(
                "Начало: {}".format(start_line) if start_line else "",
                _join(
                    "Мы проведём технические работы, чтобы улучшить работу платформы.",
                    _maintenance_end(
                        payload, "Время завершения сообщим дополнительно."
                    ),
                ),
                "Во время обслуживания сайт может быть временно недоступен.",
                "Мы сообщим, когда работы начнутся и когда сайт снова будет доступен.",
            ),
            (SCHEDULE_ACTION,),
        )
    if kind == "maintenance_started":
        return NotificationMessage(
            "{}: техническое обслуживание началось".format(STUDIO_NAME),
            _paragraphs(
                "Сайт временно недоступен, пока мы проводим запланированные работы.",
                _maintenance_end(payload, "Время завершения пока неизвестно."),
                "Если время завершения изменится, мы сообщим дополнительно.",
                "Telegram-бот работает как обычно: расписание и запись — по "
                "кнопке ниже.",
                "Спасибо за понимание!",
            ),
            (SCHEDULE_ACTION,),
        )
    if kind == "maintenance_rescheduled":
        if payload.get("phase") == "active":
            return NotificationMessage(
                "{}: время завершения работ изменилось".format(STUDIO_NAME),
                _paragraphs(
                    _maintenance_end(
                        payload, "Точное время завершения пока неизвестно."
                    ),
                    "Мы сообщим, когда сайт снова будет доступен.",
                ),
                (SCHEDULE_ACTION,),
            )
        return NotificationMessage(
            "{}: обслуживание перенесено".format(STUDIO_NAME),
            _paragraphs(
                _join(
                    "Новое время начала: {}".format(start_line) if start_line else "",
                    _maintenance_end(
                        payload, "Время завершения сообщим дополнительно."
                    ),
                ),
                "До начала работ сайт работает как обычно.",
            ),
            (SCHEDULE_ACTION,),
        )
    if kind == "maintenance_cancelled":
        return NotificationMessage(
            "{}: обслуживание отменено".format(STUDIO_NAME),
            _paragraphs(
                "Технические работы{} не состоятся.".format(
                    ", запланированные на {},".format(start_line) if start_line else ""
                ),
                "Сайт работает в обычном режиме.",
            ),
            (SCHEDULE_ACTION,),
        )
    return NotificationMessage(
        "{} снова работает!".format(STUDIO_NAME),
        _paragraphs(
            "Техническое обслуживание завершено, сайт снова доступен.",
            "Спасибо за терпение. Возвращаемся к занятиям и танцам!",
        ),
        (SCHEDULE_ACTION,),
    )


def render_notification(
    kind: str,
    payload: Mapping[str, Any],
    now: Optional[datetime] = None,
) -> NotificationMessage:
    current = now or datetime.now(timezone.utc)
    if kind == "booking_confirmed":
        source = payload.get("source_title")
        return NotificationMessage(
            "Вы записаны ✓",
            _join(
                _class_line(payload),
                "Списано 1 занятие{}.".format(
                    " — «{}»".format(source) if source else " с баланса"
                ),
                _balance(payload),
            ),
            (MY_CLASSES_ACTION,),
        )
    if kind == "booking_cancelled":
        return NotificationMessage(
            "Запись отменена",
            _join(
                _class_line(payload),
                "Занятие вернулось на баланс.",
                _balance(payload),
            ),
            (MY_CLASSES_ACTION, SCHEDULE_ACTION),
        )
    if kind == "slot_cancelled":
        return NotificationMessage(
            "Занятие отменено студией",
            _join(
                _class_line(payload),
                _reason(payload),
                "Занятие вернулось на баланс." if payload.get("refunded") else "",
                _balance(payload),
                "Выберите другое время в расписании.",
            ),
            (SCHEDULE_ACTION, MY_CLASSES_ACTION),
        )
    if kind == "slot_rescheduled":
        old = _moment(payload.get("old_starts_at"))
        new = _moment(payload.get("new_starts_at"))
        return NotificationMessage(
            "Занятие перенесено",
            _join(
                CLASS_LABELS.get(payload.get("class_key", ""), "Занятие"),
                "Было: {}".format(format_class_time(old)) if old else "",
                "Стало: {}".format(format_class_time(new)) if new else "",
                _reason(payload),
                "Запись сохранена. Если новое время не подходит, её можно "
                "отменить до начала занятия — занятие вернётся на баланс.",
            ),
            (MY_CLASSES_ACTION,),
        )
    if kind == "lesson_reminder":
        moment = _moment(payload.get("starts_at"))
        label = CLASS_LABELS.get(payload.get("class_key", ""), "занятие")
        headline = (
            "{} в {} — {}.".format(
                _day_word(moment, current),
                to_studio_time(moment).strftime("%H:%M"),
                label,
            )
            if moment
            else label
        )
        return NotificationMessage(
            "Напоминание о занятии",
            _join(headline, "Приходите за 10–15 минут до начала."),
            (MY_CLASSES_ACTION,),
        )
    if kind == "low_balance":
        balance = int(payload.get("balance") or 0)
        return NotificationMessage(
            "Занятия на балансе заканчиваются"
            if balance > 0
            else "Занятия на балансе закончились",
            _join(
                "Осталось {}.".format(lessons_count(balance)),
                "Продлите абонемент, чтобы записываться дальше.",
            ),
            (BUY_ACTION,),
        )
    if kind == "package_granted":
        lessons = int(payload.get("lessons") or 0)
        return NotificationMessage(
            "Студия выдала абонемент",
            _join(
                "«{}»: +{}.".format(
                    payload.get("title", "Абонемент"), lessons_count(lessons)
                ),
                _balance(payload),
            ),
            (SCHEDULE_ACTION, PACKAGES_ACTION),
        )
    if kind == "package_revoked":
        revoked = int(payload.get("revoked_lessons") or 0)
        return NotificationMessage(
            "Абонемент отозван студией",
            _join(
                "«{}».".format(payload.get("title", "Абонемент")),
                "Списано неиспользованных занятий: {}.".format(revoked)
                if revoked
                else "",
                _reason(payload),
                _balance(payload),
            ),
            (PACKAGES_ACTION,),
        )
    if kind == "credits_adjusted":
        delta = int(payload.get("delta") or 0)
        return NotificationMessage(
            "Баланс изменён студией",
            _join(
                "{}{}.".format("+" if delta > 0 else "−", lessons_count(abs(delta))),
                _reason(payload),
                _balance(payload),
            ),
            (PACKAGES_ACTION,),
        )
    if kind == "announcement":
        return NotificationMessage(
            "Сообщение студии", str(payload.get("body") or ""), (SCHEDULE_ACTION,)
        )
    if kind == "waitlist_seat":
        class_key = str(payload.get("class_key") or "")
        slot_id = payload.get("slot_id")
        actions = (SCHEDULE_ACTION,)
        if class_key and slot_id:
            actions = (("✅ Записаться", "book:{}:{}".format(class_key, slot_id)),)
        return NotificationMessage(
            "Освободилось место",
            _join(
                _class_line(payload),
                "Вы в листе ожидания — запишитесь, пока место свободно: его "
                "получит тот, кто запишется первым.",
            ),
            actions,
        )
    if kind.startswith("maintenance_"):
        return _render_maintenance(kind, payload)
    if kind == "package_expiring":
        expires_at = _moment(payload.get("expires_at"))
        lessons = int(payload.get("lessons") or 0)
        return NotificationMessage(
            "Абонемент скоро сгорит",
            _join(
                "«{}»: осталось {} — действуют до {}.".format(
                    payload.get("title") or "Абонемент",
                    lessons_count(lessons),
                    to_studio_time(expires_at).strftime("%d.%m.%Y %H:%M")
                    if expires_at
                    else "конца срока",
                ),
                "Запишитесь на занятие, чтобы не потерять их.",
            ),
            (SCHEDULE_ACTION,),
        )
    if kind == "credits_expired":
        lessons = int(payload.get("lessons") or 0)
        return NotificationMessage(
            "Срок абонемента истёк",
            _join(
                "«{}»: {} сгорели — срок действия закончился.".format(
                    payload.get("title") or "Абонемент", lessons_count(lessons)
                )
                if lessons != 1
                else "«{}»: 1 занятие сгорело — срок действия закончился.".format(
                    payload.get("title") or "Абонемент"
                ),
                _balance(payload),
            ),
            (BUY_ACTION,),
        )
    return NotificationMessage("Уведомление студии", "", ())
