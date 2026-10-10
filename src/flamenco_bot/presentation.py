"""Единые правила отображения данных клиенту в боте.

Только форматирование: бизнес-правила (окно отмены, списание занятий)
живут в репозитории и сервисах, здесь лишь показываются пользователю.

Время показывается в часовом поясе студии (``STUDIO_TIMEZONE``, см.
``studio_time.py``) — так же, как на сайте: один и тот же момент из
PostgreSQL выглядит одинаково в боте и в браузере.
"""

from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from .config import Config
from .studio_rules import rules
from .studio_time import to_studio_time


WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def plural(count: int, forms: Sequence[str]) -> str:
    """Русское склонение: forms = ("занятие", "занятия", "занятий")."""
    value = abs(count) % 100
    if 11 <= value <= 14:
        return forms[2]
    value %= 10
    if value == 1:
        return forms[0]
    if 2 <= value <= 4:
        return forms[1]
    return forms[2]


LESSON_FORMS = ("занятие", "занятия", "занятий")
PLACE_FORMS = ("место", "места", "мест")


def lessons_count(count: int) -> str:
    return "{} {}".format(count, plural(count, LESSON_FORMS))


def balance_line(credits: int) -> str:
    return "Баланс: {}".format(lessons_count(credits))


def format_class_time(moment: datetime, now: Optional[datetime] = None) -> str:
    """«Вт 14.10 · 19:00» в поясе студии; год — только если он не текущий."""
    local = to_studio_time(moment)
    current = to_studio_time(now or datetime.now(timezone.utc))
    date_format = "%d.%m" if local.year == current.year else "%d.%m.%Y"
    return "{} {} · {}".format(
        WEEKDAYS[local.weekday()],
        local.strftime(date_format),
        local.strftime("%H:%M"),
    )


def format_price(rubles: int) -> str:
    return "{:,} ₽".format(rubles).replace(",", " ")


def hours_text(hours: int) -> str:
    """«24 часа», «1 час», «12 часов»."""
    return "{} {}".format(hours, plural(hours, ("час", "часа", "часов")))


def validity_text(days: int) -> str:
    """Срок действия абонемента: «30 дней»."""
    return "{} {}".format(days, plural(days, ("день", "дня", "дней")))


def deadline_text() -> str:
    """Окно отмены по текущему правилу студии: «за 24 часа»."""
    return hours_text(rules().cancellation_deadline_hours)


def deadline_text_genitive() -> str:
    """То же после «меньше»: «меньше 24 часов», «меньше 1 часа»."""
    hours = rules().cancellation_deadline_hours
    return "{} {}".format(hours, plural(hours, ("часа", "часов", "часов")))


def cancellation_deadline(starts_at: datetime) -> datetime:
    return starts_at - rules().cancellation_deadline


def can_cancel(starts_at: datetime, now: Optional[datetime] = None) -> bool:
    """Подсказка для интерфейса; окончательно решает репозиторий."""
    current = now or datetime.now(timezone.utc)
    return current < cancellation_deadline(starts_at)


def cancellation_hint(starts_at: datetime, now: Optional[datetime] = None) -> str:
    if can_cancel(starts_at, now):
        return "Отменить запись можно до {}.".format(
            format_class_time(cancellation_deadline(starts_at), now)
        )
    return "Отменить эту запись уже нельзя — до начала меньше {}.".format(
        deadline_text_genitive()
    )


def website_line(prefix: str = "Сайт студии") -> str:
    """Ссылка на сайт, если WEBSITE_URL задан (иначе пустая строка)."""
    url = Config.WEBSITE_URL
    return "{}: {}".format(prefix, url) if url else ""


def format_date(moment: datetime) -> str:
    """«02.10.2026» в поясе студии."""
    return to_studio_time(moment).strftime("%d.%m.%Y")


def format_amount(amount_minor: int) -> str:
    return format_price(amount_minor // 100)


def booking_cancellation_hint(booking: Any, now: Optional[datetime] = None) -> str:
    """Срок отмены конкретной записи (учитывает перенос занятия студией)."""
    if booking.can_cancel(now):
        deadline = booking.cancellation_deadline
        if deadline >= booking.starts_at:
            return "Занятие перенесла студия — отменить запись можно до начала."
        return "Отменить запись можно до {}.".format(format_class_time(deadline, now))
    return "Отменить эту запись уже нельзя — до начала меньше {}.".format(
        deadline_text_genitive()
    )


PACKAGE_STATUS_LABELS = {
    "active": "действует",
    "used": "использован",
    "refund_pending": "возврат оплаты в обработке",
    "refunded": "оплата возвращена",
    "revoked": "отозван студией",
}


def package_line(package: Any) -> str:
    """«Абонемент на 8 занятий» — осталось 5 из 8 · с 02.10.2026."""
    origin = " (от студии)" if package.kind == "grant" else ""
    if package.status == "active":
        state = "осталось {} из {}".format(package.remaining, package.lessons)
    else:
        state = PACKAGE_STATUS_LABELS.get(package.status, package.status)
    return "«{}»{} — {} · с {}".format(
        package.title, origin, state, format_date(package.acquired_at)
    )


def source_line(source: Any) -> str:
    """Откуда списано занятие при записи."""
    if source is None:
        return "Списано 1 занятие с баланса."
    return "Списано 1 занятие — «{}», осталось {} из {}.".format(
        source.title, source.remaining, source.lessons
    )
