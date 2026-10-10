"""Данные жизненного цикла занятий, абонементов, истории и уведомлений.

Отдельный модуль от ``repository.py`` только ради размера: типы здесь не
зависят от репозитория, а репозиторий (PostgreSQL и in-memory) возвращает
их из методов, добавленных в ``studio_postgres.py`` / ``studio_memory.py``.
Чистые функции правил (остаток абонементов, источник списания) живут здесь,
чтобы PostgreSQL, in-memory и отображение считали одинаково.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence, Tuple


# Сообщение о малом остатке — когда после записи на балансе осталось не
# больше стольких занятий. Отправляется не чаще одного раза за «цикл»
# пополнения баланса (см. ``low_balance_dedupe_key``).
LOW_BALANCE_THRESHOLD = 1
MAX_REASON_LENGTH = 300
# Сколько живёт запрос входа/привязки через бота (deep link).
TELEGRAM_CONNECT_TTL = timedelta(minutes=10)

NOTIFICATION_KINDS = frozenset(
    {
        "booking_confirmed",
        "booking_cancelled",
        "slot_cancelled",
        "slot_rescheduled",
        "lesson_reminder",
        "low_balance",
        "package_granted",
        "package_revoked",
        "credits_adjusted",
        "package_expiring",
        "credits_expired",
    }
)


class SlotNotFoundError(LookupError):
    """Слота с таким id нет."""


class SlotStateError(RuntimeError):
    """Действие недопустимо в текущем состоянии слота.

    Например, перенос или отмена уже начавшегося либо отменённого занятия.
    """


class PackageGrantNotFoundError(LookupError):
    """Выданного абонемента с таким id нет."""


class TelegramConnectError(ValueError):
    """Запрос входа/привязки через бота не найден, истёк или уже использован."""


class SupportTicketStateError(RuntimeError):
    """Обращение нельзя перевести в запрошенный статус."""


def normalize_reason(value: Optional[str], required: bool) -> Optional[str]:
    """Причина действия администратора: 1–300 символов или ``None``."""
    normalized = (value or "").strip()
    if not normalized:
        if required:
            raise ValueError(
                "Причина должна содержать от 1 до {} символов".format(MAX_REASON_LENGTH)
            )
        return None
    if len(normalized) > MAX_REASON_LENGTH:
        raise ValueError(
            "Причина должна содержать от 1 до {} символов".format(MAX_REASON_LENGTH)
        )
    return normalized


def effective_remaining(balance: int, raw: Sequence[int]) -> Tuple[list, int]:
    """Остаток каждого активного абонемента так, чтобы сумма равнялась балансу.

    ``raw`` — сумма ledger по каждому активному источнику (платёж или выдача)
    в порядке списания: сначала тот, что сгорает раньше (миграция 018).
    Возвращает ``(остатки, вне абонементов)``. Если баланс больше суммы
    остатков, разница — бессрочные занятия вне абонементов (возврат студией
    за отменённое занятие после конца срока партии); они списываются
    последними. Если меньше (ручное списание без привязки к абонементу),
    недостача снимается с источников в порядке списания.
    """
    effective = [max(0, value) for value in raw]
    unallocated = balance - sum(effective)
    if unallocated < 0:
        deficit = -unallocated
        for index, value in enumerate(effective):
            take = min(value, deficit)
            effective[index] = value - take
            deficit -= take
            if deficit == 0:
                break
        unallocated = 0
    return effective, unallocated


def pick_credit_source_index(balance: int, raw: Sequence[int]) -> Optional[int]:
    """Индекс источника, с которого спишется следующее занятие.

    Сначала — абонементы со сроком (в порядке сгорания), бессрочные занятия
    вне абонементов — последними. ``None`` — списание с занятий вне
    абонементов (или занятий нет).
    """
    effective, _ = effective_remaining(balance, raw)
    for index, value in enumerate(effective):
        if value > 0:
            return index
    return None


FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def source_order_key(item: Any) -> tuple:
    """Порядок списания: срок (бессрочные — в конце), дата получения, вид, id.

    ``item`` — строка БД (asyncpg.Record) или ``UserPackage``.
    """

    def field(name: str) -> Any:
        return getattr(item, name) if hasattr(item, name) else item[name]

    return (
        field("expires_at") or FAR_FUTURE,
        field("acquired_at"),
        field("kind"),
        field("id"),
    )


def low_balance_dedupe_key(telegram_id: int, cycle_marker: int) -> str:
    """Одно уведомление о малом остатке на одно пополнение баланса.

    ``cycle_marker`` — id последней строки ledger, пополнившей баланс
    (покупка, выдача, ручное начисление). Возврат за отмену записи цикл не
    сбрасывает — иначе запись/отмена/запись присылали бы его снова.
    """
    return "low_balance:{}:{}".format(telegram_id, cycle_marker)


def reminder_dedupe_key(booking_id: int, starts_at: datetime) -> str:
    """Время начала в ключе: после переноса напоминание ставится заново."""
    return "reminder:{}:{}".format(booking_id, int(starts_at.timestamp()))


@dataclass(frozen=True)
class SlotParticipant:
    booking_id: int
    telegram_id: int
    user_name: str
    phone: Optional[str]
    booked_at: datetime
    status: str


@dataclass(frozen=True)
class SlotRefund:
    booking_id: int
    telegram_id: int
    balance: Optional[int]


@dataclass(frozen=True)
class SlotCancellation:
    # ClassSlot из repository.py (без импорта — модуль от него не зависит).
    slot: Any
    already_cancelled: bool
    refunds: Tuple[SlotRefund, ...] = ()


@dataclass(frozen=True)
class SlotReschedule:
    slot: Any
    old_starts_at: datetime
    new_starts_at: datetime
    participants: Tuple[int, ...] = ()


@dataclass(frozen=True)
class SlotEvent:
    id: int
    slot_id: int
    event_type: str
    actor_telegram_id: Optional[int]
    old_starts_at: Optional[datetime]
    new_starts_at: Optional[datetime]
    old_capacity: Optional[int]
    new_capacity: Optional[int]
    reason: Optional[str]
    created_at: datetime
    class_key: Optional[str] = None

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "SlotEvent":
        return cls(
            id=record["id"],
            slot_id=record["slot_id"],
            event_type=record["event_type"],
            actor_telegram_id=record["actor_telegram_id"],
            old_starts_at=record["old_starts_at"],
            new_starts_at=record["new_starts_at"],
            old_capacity=record["old_capacity"],
            new_capacity=record["new_capacity"],
            reason=record["reason"],
            created_at=record["created_at"],
            class_key=record.get("class_key"),
        )


@dataclass(frozen=True)
class CreditSource:
    """Абонемент, с которого списано (или вернётся) занятие."""

    kind: str  # 'payment' | 'grant'
    id: int
    title: str
    lessons: int
    remaining: int
    # Когда сгорает остаток; None — бессрочно.
    expires_at: Optional[datetime] = None


@dataclass(frozen=True)
class UserPackage:
    """Абонемент участника: купленный (платёж) или выданный студией.

    ``status``: active — есть остаток; used — все занятия использованы;
    expired — срок истёк (остаток сгорел); refund_pending / refunded —
    возврат оплаты; revoked — выдача отозвана.
    """

    kind: str
    id: int
    package_key: str
    title: str
    lessons: int
    remaining: int
    status: str
    acquired_at: datetime
    amount_minor: Optional[int] = None
    reason: Optional[str] = None
    expires_at: Optional[datetime] = None

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class PackageSummary:
    balance: int
    packages: Tuple[UserPackage, ...]
    # Бессрочные занятия вне абонементов (возврат студией после срока партии).
    unallocated: int

    @property
    def active_packages(self) -> Tuple[UserPackage, ...]:
        return tuple(package for package in self.packages if package.is_active)

    @property
    def next_source(self) -> Optional[UserPackage]:
        """С какого абонемента спишется следующая запись (None — с занятий
        вне абонементов): сначала тот, что сгорает раньше."""
        return next(iter(self.active_packages_by_expiry), None)

    @property
    def active_packages_by_expiry(self) -> Tuple[UserPackage, ...]:
        return tuple(sorted(self.active_packages, key=source_order_key))


@dataclass(frozen=True)
class PackageGrant:
    id: int
    telegram_id: int
    package_key: str
    title: str
    lessons: int
    status: str
    granted_by: int
    reason: str
    created_at: datetime

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "PackageGrant":
        return cls(
            id=record["id"],
            telegram_id=record["telegram_id"],
            package_key=record["package_key"],
            title=record["package_title"],
            lessons=record["lessons"],
            status=record["status"],
            granted_by=record["granted_by"],
            reason=record["reason"],
            created_at=record["created_at"],
        )


@dataclass(frozen=True)
class PackageGrantResult:
    grant: PackageGrant
    ledger_id: Optional[int]
    balance: int
    # False — выдача с этим ключом идемпотентности уже была выполнена.
    applied: bool


@dataclass(frozen=True)
class PackageRevokeResult:
    grant: PackageGrant
    revoked_lessons: int
    balance: int
    applied: bool


@dataclass(frozen=True)
class LedgerEntry:
    id: int
    telegram_id: int
    entry_type: str
    delta: int
    created_at: datetime
    reason: Optional[str] = None
    actor_telegram_id: Optional[int] = None
    payment_id: Optional[int] = None
    grant_id: Optional[int] = None
    booking_id: Optional[int] = None
    package_title: Optional[str] = None
    class_key: Optional[str] = None
    starts_at: Optional[datetime] = None
    user_name: Optional[str] = None

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "LedgerEntry":
        return cls(
            id=record["id"],
            telegram_id=record["telegram_id"],
            entry_type=record["entry_type"],
            delta=record["delta"],
            created_at=record["created_at"],
            reason=record["reason"],
            actor_telegram_id=record["actor_telegram_id"],
            payment_id=record["payment_id"],
            grant_id=record["grant_id"],
            booking_id=record["booking_id"],
            package_title=record.get("package_title"),
            class_key=record.get("class_key"),
            starts_at=record.get("starts_at"),
            user_name=record.get("user_name"),
        )


def _payload_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


@dataclass(frozen=True)
class UserNotification:
    id: int
    telegram_id: int
    kind: str
    payload: dict
    created_at: datetime
    delivery_status: str = "pending"
    attempts: int = 0
    read_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "UserNotification":
        return cls(
            id=record["id"],
            telegram_id=record["telegram_id"],
            kind=record["kind"],
            payload=_payload_dict(record["payload"]),
            created_at=record["created_at"],
            delivery_status=record["delivery_status"],
            attempts=record["attempts"],
            read_at=record["read_at"],
            expires_at=record["expires_at"],
            sent_at=record["sent_at"],
        )


@dataclass(frozen=True)
class NotificationSettings:
    reminders: bool = True
    low_balance: bool = True


@dataclass(frozen=True)
class TelegramConnectRequest:
    id: int
    purpose: str  # 'login' | 'link'
    status: str
    expires_at: datetime
    web_user_id: Optional[int] = None
    telegram_id: Optional[int] = None
    # Email веб-аккаунта при привязке — бот показывает его (маскированным),
    # чтобы участник видел, к какому аккаунту подключает Telegram.
    web_user_email: Optional[str] = None

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "TelegramConnectRequest":
        return cls(
            id=record["id"],
            purpose=record["purpose"],
            status=record["status"],
            expires_at=record["expires_at"],
            web_user_id=record["web_user_id"],
            telegram_id=record["telegram_id"],
            web_user_email=record.get("web_user_email"),
        )


@dataclass(frozen=True)
class SupportThreadMessage:
    id: int
    sender_role: str
    body: str
    created_at: datetime


@dataclass(frozen=True)
class SupportTicketThread:
    id: int
    telegram_id: int
    status: str
    created_at: datetime
    updated_at: datetime
    user_name: str
    messages: Tuple[SupportThreadMessage, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AuditEvent:
    """Строка журнала аудита для администратора.

    ``source``: ledger (ручные операции с балансом и абонементами, возвраты
    за отмену студией), slot (изменения расписания), payment (возвраты
    оплаты). Все строки уже лежат в своих таблицах — журнал их только
    объединяет при чтении.
    """

    source: str
    action: str
    created_at: datetime
    actor_telegram_id: Optional[int]
    target_telegram_id: Optional[int] = None
    slot_id: Optional[int] = None
    payment_id: Optional[int] = None
    delta: Optional[int] = None
    reason: Optional[str] = None
    actor_name: Optional[str] = None
    target_name: Optional[str] = None
