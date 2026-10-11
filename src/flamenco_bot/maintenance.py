"""Техническое обслуживание сайта: правила без ввода-вывода.

Окно обслуживания хранится в ``site_maintenance`` (миграция 021 DATABASE) —
единственный источник состояния для сайта, API и бота. Время — TIMESTAMPTZ
(UTC); людям показывается в поясе студии (``STUDIO_TIMEZONE``).

- Окно с наступившим началом активно сразу: сайт и API вычисляют это по
  ``starts_at`` (``effective_status``), не дожидаясь фоновой задачи, — сбой
  планировщика или Telegram не задерживает включение обслуживания.
- Наступление ``ends_at`` обслуживание не выключает: завершает его только
  администратор.
- Предупреждение за N минут отправляется, только пока оно ещё «вовремя»
  (``warning_decision``): если окно создали позже момента предупреждения
  или процесс долго не работал, запоздавшее предупреждение пропускается, а
  не приходит как будто вовремя.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple


STATUS_SCHEDULED = "scheduled"
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
OPEN_STATUSES = (STATUS_SCHEDULED, STATUS_ACTIVE)

MODE_NORMAL = "normal"
MODE_SCHEDULED = "scheduled"
MODE_ACTIVE = "active"

AUDIENCES = ("all", "staff")

DEFAULT_TITLE = "Небольшая пауза для больших улучшений"
DEFAULT_DESCRIPTION = (
    "Мы обновляем Mirada Studio, чтобы сделать занятия и запись на них ещё "
    "удобнее. Сайт временно недоступен. Скоро вернёмся!"
)
DEFAULT_WARNING_MINUTES: Tuple[int, ...] = (1440, 60, 10)
MAX_WARNINGS = 6
MAX_WARNING_MINUTES = 7 * 24 * 60
MAX_TITLE_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 1000
# Запоздание предупреждения, после которого оно уже не «вовремя»: четверть
# интервала, но не больше 30 минут и не меньше минуты (за 10 минут — 2,5
# минуты, за час — 15 минут, за сутки — 30 минут).
MAX_WARNING_DELAY = timedelta(minutes=30)
MIN_WARNING_DELAY = timedelta(minutes=1)

KIND_WARNING = "maintenance_warning"
KIND_STARTED = "maintenance_started"
KIND_RESCHEDULED = "maintenance_rescheduled"
KIND_CANCELLED = "maintenance_cancelled"
KIND_COMPLETED = "maintenance_completed"
MAINTENANCE_KINDS = (
    KIND_WARNING,
    KIND_STARTED,
    KIND_RESCHEDULED,
    KIND_CANCELLED,
    KIND_COMPLETED,
)


class MaintenanceConflictError(RuntimeError):
    """Действие недопустимо в текущем состоянии обслуживания."""


class MaintenanceNotFoundError(LookupError):
    """Окна обслуживания с таким id нет."""


@dataclass(frozen=True)
class MaintenanceWindow:
    id: int
    status: str
    title: str
    description: str
    starts_at: datetime
    ends_at: Optional[datetime]
    warning_minutes: Tuple[int, ...]
    notify_users: bool
    audience: str
    revision: int
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    created_by: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "MaintenanceWindow":
        return cls(
            id=record["id"],
            status=record["status"],
            title=record["title"],
            description=record["description"],
            starts_at=record["starts_at"],
            ends_at=record["ends_at"],
            warning_minutes=tuple(record["warning_minutes"] or ()),
            notify_users=record["notify_users"],
            audience=record["audience"],
            revision=record["revision"],
            started_at=record["started_at"],
            finished_at=record["finished_at"],
            created_by=record["created_by"],
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )

    def effective_status(self, now: datetime) -> str:
        """Запланированное окно с наступившим началом уже активно."""
        if self.status == STATUS_SCHEDULED and self.starts_at <= now:
            return STATUS_ACTIVE
        return self.status


@dataclass(frozen=True)
class WarningPlan:
    minutes: int
    due_at: datetime
    # 'wait' — ещё рано, 'send' — пора, 'late' — запоздало, не отправляется.
    state: str


def site_mode(window: Optional[MaintenanceWindow], now: datetime) -> str:
    if window is None:
        return MODE_NORMAL
    status = window.effective_status(now)
    if status == STATUS_ACTIVE:
        return MODE_ACTIVE
    if status == STATUS_SCHEDULED:
        return MODE_SCHEDULED
    return MODE_NORMAL


def normalize_text(value: Optional[str], limit: int, field: str) -> str:
    text = (value or "").strip()
    if not 1 <= len(text) <= limit:
        raise ValueError("{} — от 1 до {} символов".format(field, limit))
    return text


def normalize_warning_minutes(values: Iterable[int]) -> Tuple[int, ...]:
    """Уникальные моменты предупреждений по убыванию (``ValueError`` — неверные)."""
    cleaned = set()
    for value in values:
        minutes = int(value)
        if not 1 <= minutes <= MAX_WARNING_MINUTES:
            raise ValueError(
                "Предупреждение — от 1 минуты до {} дней до начала".format(
                    MAX_WARNING_MINUTES // 1440
                )
            )
        cleaned.add(minutes)
    if len(cleaned) > MAX_WARNINGS:
        raise ValueError("Не больше {} предупреждений".format(MAX_WARNINGS))
    return tuple(sorted(cleaned, reverse=True))


def validate_period(
    starts_at: datetime,
    ends_at: Optional[datetime],
    now: datetime,
    *,
    future_start: bool,
) -> None:
    """Начало (в будущем, если ``future_start``) и предполагаемое окончание."""
    if starts_at.tzinfo is None or (ends_at is not None and ends_at.tzinfo is None):
        raise ValueError("Время должно быть с часовым поясом")
    if future_start and starts_at <= now:
        raise ValueError("Начало обслуживания должно быть в будущем")
    if ends_at is not None:
        if ends_at <= starts_at:
            raise ValueError("Окончание должно быть позже начала")
        if ends_at <= now:
            raise ValueError("Предполагаемое окончание уже прошло")


def warning_delay_limit(minutes: int) -> timedelta:
    return max(
        MIN_WARNING_DELAY, min(MAX_WARNING_DELAY, timedelta(minutes=minutes) / 4)
    )


def warning_decision(minutes: int, starts_at: datetime, now: datetime) -> str:
    due_at = starts_at - timedelta(minutes=minutes)
    if now >= starts_at:
        return "late"
    if now < due_at:
        return "wait"
    return "send" if now - due_at <= warning_delay_limit(minutes) else "late"


def plan_warnings(window: MaintenanceWindow, now: datetime) -> Sequence[WarningPlan]:
    """Расписание предупреждений окна — для предпросмотра и проверки."""
    return [
        WarningPlan(
            minutes=minutes,
            due_at=window.starts_at - timedelta(minutes=minutes),
            state=warning_decision(minutes, window.starts_at, now),
        )
        for minutes in window.warning_minutes
    ]


def warning_stage(minutes: int) -> str:
    return "warning:{}".format(minutes)


def notification_payload(window: MaintenanceWindow, **extra: Any) -> dict:
    payload = {
        "maintenance_id": window.id,
        "revision": window.revision,
        "starts_at": window.starts_at.isoformat(),
        "ends_at": window.ends_at.isoformat() if window.ends_at else None,
    }
    payload.update(extra)
    return payload


def retry_after_seconds(window: Optional[MaintenanceWindow], now: datetime) -> int:
    """Подсказка Retry-After для ответа 503 на время работ."""
    if window is not None and window.ends_at is not None and window.ends_at > now:
        return max(60, min(int((window.ends_at - now).total_seconds()), 86_400))
    return 600
