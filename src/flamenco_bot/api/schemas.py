"""Pydantic-схемы запросов/ответов веб-API.

Явные схемы вместо прямой отдачи объектов репозитория — ``password_hash`` и
другие внутренние поля никогда не попадают в ответ.
"""

import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any, Dict, List, Literal, Mapping, Optional

from pydantic import BaseModel, Field, field_validator

from ..class_catalog import CLASS_LABELS
from ..studio_rules import rules
from ..database.repository import (
    ClassBooking,
    ClassSlot,
    LessonPaymentHistoryItem,
    SupportTicket,
    UserBooking,
    UserProfile,
    WebUserRecord,
)
from ..database.studio_models import (
    CreditSource,
    NotificationSettings,
    PackageSummary,
    SupportTicketThread,
    UserNotification,
    UserPackage,
)
from ..notification_texts import render_notification
from ..payments import LessonPackage
from ..presentation import PACKAGE_STATUS_LABELS
from ..services import (
    CheckoutResult,
    ClientOverview,
    HistoryOperation,
    SupportSubmission,
    normalize_user_name,
)
from ..services.history import BOOKING_STATUS_LABELS, booking_status


# Идентификаторы в PostgreSQL — BIGINT. Число за его пределами должно
# отклоняться валидацией (422), а не падать в драйвере БД (500).
MAX_DB_ID = 2**63 - 1
DbId = Annotated[int, Field(gt=0, le=MAX_DB_ID)]


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=100)


class LoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=256)


class TelegramAuthRequest(BaseModel):
    """Данные, которые Telegram Login Widget передаёт фронтенду.

    Подпись (``hash``) проверяется на backend — см. ``services.auth``.
    """

    id: int
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None
    photo_url: Optional[str] = None
    auth_date: int
    hash: str

    def to_payload(self) -> Mapping[str, Any]:
        return self.model_dump(exclude_none=True)


class SetCredentialsRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)


class UserResponse(BaseModel):
    id: int
    email: Optional[str]
    telegram_id: Optional[int]
    display_name: str
    created_at: datetime
    # Права проверяет backend на каждом /api/admin/*; сайту роль нужна
    # только чтобы показать нужные разделы админки и окно нагрузки.
    is_admin: bool = False
    # admin, teacher или None (не сотрудник).
    role: Optional[str] = None

    @classmethod
    def from_record(cls, record: WebUserRecord) -> "UserResponse":
        return cls(
            id=record.id,
            email=record.email,
            telegram_id=record.telegram_id,
            display_name=record.display_name,
            created_at=record.created_at,
            is_admin=record.is_admin,
            role=record.role,
        )


class ClassSlotResponse(BaseModel):
    id: int
    class_key: str
    class_label: str
    starts_at: datetime
    capacity: int
    remaining: int
    # open — запись открыта, closed — запись закрыта, cancelled — занятие
    # отменено студией (в общем расписании не показывается).
    status: str
    # Можно ли записаться прямо сейчас (открыто, есть места, не началось).
    bookable: bool = False
    # Занятие переносила студия (время уже новое).
    rescheduled: bool = False
    cancel_reason: Optional[str] = None
    # Преподаватель (имя для расписания), если назначен.
    teacher_name: Optional[str] = None

    @classmethod
    def from_record(cls, slot: ClassSlot) -> "ClassSlotResponse":
        return cls(
            id=slot.id,
            class_key=slot.class_key,
            class_label=CLASS_LABELS[slot.class_key],
            starts_at=slot.starts_at,
            capacity=slot.capacity,
            remaining=slot.remaining,
            status=slot.status,
            bookable=slot.status == "open"
            and slot.remaining > 0
            and slot.starts_at > datetime.now(timezone.utc),
            rescheduled=slot.rescheduled_at is not None,
            cancel_reason=slot.cancel_reason,
            teacher_name=slot.teacher_name,
        )


class ClassFormatResponse(BaseModel):
    """Направление занятий из ``class_catalog`` — того же каталога, которым
    пользуется бот: название, описание и уровень. Фронтенд ничего из этого
    не хардкодит (см. ``src/lib/directions.ts`` в проекте сайта).
    """

    key: str
    label: str
    description: str
    level: str


class LessonPackageResponse(BaseModel):
    key: str
    title: str
    lessons: int
    price_rub: int
    # Срок действия с даты покупки, дней; None — бессрочно.
    validity_days: Optional[int] = None

    @classmethod
    def from_package(cls, package: LessonPackage) -> "LessonPackageResponse":
        return cls(
            key=package.key,
            title=package.title,
            lessons=package.lessons,
            price_rub=package.price_rub,
            validity_days=package.validity_days,
        )


class BookingRequest(BaseModel):
    slot_id: DbId


class WaitlistEntryResponse(BaseModel):
    slot: ClassSlotResponse
    joined_at: datetime
    queue_size: int

    @classmethod
    def from_entry(cls, entry: Any) -> "WaitlistEntryResponse":
        return cls(
            slot=ClassSlotResponse.from_record(entry.slot),
            joined_at=entry.joined_at,
            queue_size=entry.queue_size,
        )


class AdminWaitlistMemberResponse(BaseModel):
    telegram_id: int
    user_name: str
    phone: Optional[str]
    joined_at: datetime


class AdminSeriesRequest(BaseModel):
    class_key: str = Field(min_length=1, max_length=32)
    starts_at: datetime
    capacity: int = Field(ge=1, le=100)
    teacher_telegram_id: Optional[int] = Field(default=None, gt=0, le=MAX_DB_ID)
    weeks: int = Field(ge=1, le=12)


class AdminCopyWeekRequest(BaseModel):
    # Любой день недели-источника (по часам студии); берётся её понедельник.
    week_start: date
    weeks: int = Field(ge=1, le=12)


class CreditSourceResponse(BaseModel):
    """Абонемент, с которого списано занятие."""

    kind: str
    id: int
    title: str
    lessons: int
    remaining: int

    @classmethod
    def from_source(cls, source: CreditSource) -> "CreditSourceResponse":
        return cls(
            kind=source.kind,
            id=source.id,
            title=source.title,
            lessons=source.lessons,
            remaining=source.remaining,
        )


class BookingResponse(BaseModel):
    id: int
    slot_id: int
    class_key: str
    class_label: str
    starts_at: datetime
    already_booked: bool
    # Для новой записи: баланс после списания и абонемент-источник (None —
    # списано с занятий вне абонементов).
    balance: Optional[int] = None
    source: Optional[CreditSourceResponse] = None

    @classmethod
    def from_booking(cls, booking: ClassBooking) -> "BookingResponse":
        return cls(
            id=booking.id,
            slot_id=booking.slot_id,
            class_key=booking.class_key,
            class_label=CLASS_LABELS[booking.class_key],
            starts_at=booking.starts_at,
            already_booked=booking.already_booked,
            balance=booking.balance,
            source=(
                CreditSourceResponse.from_source(booking.source)
                if booking.source is not None
                else None
            ),
        )


class UserBookingResponse(BaseModel):
    id: int
    slot_id: int
    class_key: str
    class_label: str
    starts_at: datetime
    booking_status: str
    slot_status: str
    # До какого момента запись можно отменить — правило окна отмены живёт
    # в репозитории, сайт только показывает срок (как и бот). Если студия
    # перенесла занятие после записи — это время начала.
    cancellable_until: datetime
    can_cancel: bool = False
    # upcoming | attended | cancelled_by_user | cancelled_by_studio
    status: str = "upcoming"
    status_label: str = ""
    cancelled_by: Optional[str] = None
    # Время до последнего переноса студией (None — не переносилось).
    previous_starts_at: Optional[datetime] = None
    slot_cancel_reason: Optional[str] = None

    @classmethod
    def from_record(cls, booking: UserBooking) -> "UserBookingResponse":
        status = booking_status(booking)
        return cls(
            id=booking.id,
            slot_id=booking.slot_id,
            class_key=booking.class_key,
            class_label=CLASS_LABELS[booking.class_key],
            starts_at=booking.starts_at,
            booking_status=booking.booking_status,
            slot_status=booking.slot_status,
            cancellable_until=booking.cancellation_deadline,
            can_cancel=booking.can_cancel(),
            status=status,
            status_label=BOOKING_STATUS_LABELS[status],
            cancelled_by=booking.cancelled_by,
            previous_starts_at=booking.previous_starts_at,
            slot_cancel_reason=booking.slot_cancel_reason,
        )


class BookingRulesResponse(BaseModel):
    """Правила записи для отображения клиенту — те же, по которым их
    проверяет репозиторий (studio_settings), чтобы сайт не хардкодил числа."""

    cancellation_deadline_hours: int
    rebook_cooldown_hours: int

    @classmethod
    def current(cls) -> "BookingRulesResponse":
        current = rules()
        return cls(
            cancellation_deadline_hours=current.cancellation_deadline_hours,
            rebook_cooldown_hours=current.rebook_cooldown_hours,
        )


class SupportMessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class SupportSubmissionResponse(BaseModel):
    ticket_id: int
    created: bool
    admins_notified: bool

    @classmethod
    def from_submission(
        cls, submission: SupportSubmission
    ) -> "SupportSubmissionResponse":
        return cls(
            ticket_id=submission.ticket_id,
            created=submission.created,
            admins_notified=submission.notifications.complete,
        )


class SupportTicketResponse(BaseModel):
    id: int
    status: str
    created_at: datetime
    updated_at: datetime
    last_message: str

    @classmethod
    def from_record(cls, ticket: SupportTicket) -> "SupportTicketResponse":
        return cls(
            id=ticket.id,
            status=ticket.status,
            created_at=ticket.created_at,
            updated_at=ticket.updated_at,
            last_message=ticket.last_message,
        )


class ProfileResponse(BaseModel):
    """Данные профиля из ``bot_users`` — те же, что видит сам бот в /account.

    Не путать с ``UserResponse``: это данные веб-аккаунта (``users``),
    а это — данные привязанного Telegram-профиля (баланс занятий и т.д.).
    """

    telegram_id: int
    user_name: str
    phone: Optional[str]
    lesson_credits: int
    registered_at: datetime
    is_admin: bool

    @classmethod
    def from_record(cls, profile: UserProfile) -> "ProfileResponse":
        return cls(
            telegram_id=profile.telegram_id,
            user_name=profile.user_name,
            phone=profile.phone,
            lesson_credits=profile.lesson_credits,
            registered_at=profile.registered_at,
            is_admin=profile.is_admin,
        )


class UpdateProfileNameRequest(BaseModel):
    user_name: str

    # То же правило, что в боте (services.profile): strip + 1–64 символа.
    _normalize_user_name = field_validator("user_name")(normalize_user_name)


class CheckoutRequest(BaseModel):
    package_key: str = Field(min_length=1, max_length=64)


class CheckoutResponse(BaseModel):
    payment_id: int
    package_key: str
    amount_minor: int
    # None, если провайдер не вернул безопасную HTTPS-ссылку — см.
    # services.payments.safe_confirmation_url. Фронтенд должен сам решить,
    # что показать пользователю в этом случае (не редиректить на пустоту).
    confirmation_url: Optional[str]

    @classmethod
    def from_result(cls, result: CheckoutResult) -> "CheckoutResponse":
        return cls(
            payment_id=result.payment.id,
            package_key=result.package_key,
            amount_minor=result.amount_minor,
            confirmation_url=result.confirmation_url,
        )


class PaymentStatusResponse(BaseModel):
    status: str
    credits: Optional[int] = None


class PaymentHistoryItemResponse(BaseModel):
    id: int
    package_key: str
    package_title: str
    lessons: int
    amount_minor: int
    status: str
    created_at: datetime

    @classmethod
    def from_record(
        cls, item: LessonPaymentHistoryItem
    ) -> "PaymentHistoryItemResponse":
        return cls(
            id=item.id,
            package_key=item.package_key,
            package_title=item.package_title,
            lessons=item.lessons,
            amount_minor=item.amount_minor,
            status=item.status,
            created_at=item.created_at,
        )


# ---------- абонементы ----------


class UserPackageResponse(BaseModel):
    kind: str  # payment — куплен, grant — выдан студией
    id: int
    package_key: str
    title: str
    lessons: int
    remaining: int
    # active | used | expired | refund_pending | refunded | revoked
    status: str
    status_label: str
    acquired_at: datetime
    amount_minor: Optional[int] = None
    # Когда сгорает остаток; None — бессрочно.
    expires_at: Optional[datetime] = None

    @classmethod
    def from_package(cls, package: UserPackage) -> "UserPackageResponse":
        return cls(
            kind=package.kind,
            id=package.id,
            package_key=package.package_key,
            title=package.title,
            lessons=package.lessons,
            remaining=package.remaining,
            status=package.status,
            status_label=PACKAGE_STATUS_LABELS.get(package.status, package.status),
            acquired_at=package.acquired_at,
            amount_minor=package.amount_minor,
            expires_at=package.expires_at,
        )


class PackageSummaryResponse(BaseModel):
    """Баланс по абонементам: сумма ``remaining`` + ``unallocated`` = ``balance``."""

    balance: int
    # Бессрочные занятия вне абонементов (возврат студией после срока).
    unallocated: int
    # С какого абонемента спишется следующая запись (None — вне абонементов).
    next_source_title: Optional[str]
    packages: List[UserPackageResponse]

    @classmethod
    def from_summary(cls, summary: PackageSummary) -> "PackageSummaryResponse":
        next_source = summary.next_source
        return cls(
            balance=summary.balance,
            unallocated=summary.unallocated,
            next_source_title=next_source.title if next_source else None,
            packages=[
                UserPackageResponse.from_package(package)
                for package in summary.packages
            ],
        )


# ---------- история ----------


class HistoryOperationResponse(BaseModel):
    id: int
    created_at: datetime
    delta: int
    # purchase | lesson_use | booking_refund | studio_cancellation |
    # manual_credit | manual_debit | package_grant | package_revoke | ...
    operation: str
    label: str
    reason: Optional[str] = None
    package_title: Optional[str] = None
    class_key: Optional[str] = None
    class_label: Optional[str] = None
    starts_at: Optional[datetime] = None

    @classmethod
    def from_operation(cls, item: HistoryOperation) -> "HistoryOperationResponse":
        entry = item.entry
        return cls(
            id=entry.id,
            created_at=entry.created_at,
            delta=entry.delta,
            operation=item.operation,
            label=item.label,
            reason=item.visible_reason,
            package_title=entry.package_title,
            class_key=entry.class_key,
            class_label=CLASS_LABELS.get(entry.class_key) if entry.class_key else None,
            starts_at=entry.starts_at,
        )


class HistoryOperationsPage(BaseModel):
    items: List[HistoryOperationResponse]
    # Передайте как before_id, чтобы получить следующую (более старую)
    # страницу; None — дальше записей нет.
    next_before_id: Optional[int] = None


class BookingHistoryPage(BaseModel):
    items: List[UserBookingResponse]
    next_offset: Optional[int] = None


# ---------- уведомления ----------


class NotificationResponse(BaseModel):
    id: int
    kind: str
    title: str
    body: str
    created_at: datetime
    read: bool

    @classmethod
    def from_notification(
        cls, notification: UserNotification
    ) -> "NotificationResponse":
        message = render_notification(notification.kind, notification.payload)
        return cls(
            id=notification.id,
            kind=notification.kind,
            title=message.title,
            body=message.body,
            created_at=notification.created_at,
            read=notification.read_at is not None,
        )


class NotificationsPage(BaseModel):
    items: List[NotificationResponse]
    unread_count: int
    next_before_id: Optional[int] = None


class UnreadCountResponse(BaseModel):
    unread_count: int


class MarkNotificationsReadRequest(BaseModel):
    # Не задано — отметить прочитанными все свои уведомления.
    ids: Optional[List[DbId]] = Field(default=None, max_length=100)


class MarkNotificationsReadResponse(BaseModel):
    updated: int


class NotificationSettingsResponse(BaseModel):
    reminders: bool
    low_balance: bool

    @classmethod
    def from_settings(
        cls, settings: NotificationSettings
    ) -> "NotificationSettingsResponse":
        return cls(reminders=settings.reminders, low_balance=settings.low_balance)


class UpdateNotificationSettingsRequest(BaseModel):
    reminders: Optional[bool] = None
    low_balance: Optional[bool] = None


# ---------- профиль ----------


class OverviewResponse(BaseModel):
    """Всё для главной страницы личного кабинета одним запросом."""

    profile: ProfileResponse
    packages: PackageSummaryResponse
    upcoming: List[UserBookingResponse]
    unread_notifications: int
    notification_settings: NotificationSettingsResponse

    @classmethod
    def from_overview(cls, overview: ClientOverview) -> "OverviewResponse":
        return cls(
            profile=ProfileResponse.from_record(overview.profile),
            packages=PackageSummaryResponse.from_summary(overview.packages),
            upcoming=[
                UserBookingResponse.from_record(booking)
                for booking in overview.upcoming
            ],
            unread_notifications=overview.unread_notifications,
            notification_settings=NotificationSettingsResponse.from_settings(
                overview.settings
            ),
        )


# ---------- поддержка ----------


class SupportThreadMessageResponse(BaseModel):
    id: int
    # user — участник, admin — студия.
    sender_role: str
    body: str
    created_at: datetime


class SupportThreadResponse(BaseModel):
    id: int
    status: str
    created_at: datetime
    updated_at: datetime
    messages: List[SupportThreadMessageResponse]

    @classmethod
    def from_thread(cls, thread: SupportTicketThread) -> "SupportThreadResponse":
        return cls(
            id=thread.id,
            status=thread.status,
            created_at=thread.created_at,
            updated_at=thread.updated_at,
            messages=[
                SupportThreadMessageResponse(
                    id=message.id,
                    sender_role=message.sender_role,
                    body=message.body,
                    created_at=message.created_at,
                )
                for message in thread.messages
            ],
        )


# ---------- вход через бота ----------


class TelegramConnectRequestBody(BaseModel):
    # login — войти (аккаунт найдётся или создастся по Telegram);
    # link — привязать Telegram к текущему (вошедшему) аккаунту.
    purpose: Literal["login", "link"] = "login"


class TelegramConnectStartResponse(BaseModel):
    # Откройте в новой вкладке/приложении: t.me/<бот>?start=c_<token>.
    deep_link: str
    expires_at: datetime
    # Как часто сайту опрашивать /connect/complete.
    poll_interval_seconds: int = 2


class TelegramConnectStatusResponse(BaseModel):
    # pending — ждём подтверждения в боте; completed — вход выполнен (cookie
    # сессии установлена); rejected — отклонено в боте; expired — ссылка
    # устарела; used — запрос уже использован.
    status: str
    user: Optional[UserResponse] = None


# ---------- админ-панель сайта ----------


class AdminDashboardResponse(BaseModel):
    slots_today: int
    bookings_today: int
    free_seats_today: int
    slots_week: int
    bookings_week: int
    free_seats_week: int
    total_profiles: int
    total_web_users: int
    new_profiles_today: int
    new_web_users_today: int
    online_users: int
    sales_today_count: int
    sales_today_rub: float
    sales_month_count: int
    sales_month_rub: float
    pending_payments: int
    open_tickets: int


class AdminRequestStatsResponse(BaseModel):
    window_seconds: int
    total_since_start: int
    in_flight: int
    requests_last_minute: int
    requests_in_window: int
    errors_in_window: int
    client_errors_in_window: int
    avg_ms: Optional[float]
    p95_ms: Optional[float]
    max_ms: Optional[float]


class AdminProcessStatsResponse(BaseModel):
    pid: int
    uptime_seconds: int
    cpu_percent: Optional[float]
    cpu_count: int
    memory_rss_bytes: Optional[int]
    memory_is_peak: bool
    load_average: Optional[List[float]]
    threads: int
    asyncio_tasks: int
    event_loop_lag_ms: float
    python_version: str
    platform: str


class AdminDatabaseStatsResponse(BaseModel):
    backend: str
    ping_ms: float
    pool_size: int
    idle_connections: int
    database_size_bytes: Optional[int]
    connections: Optional[int]
    active_web_sessions: int
    notifications_pending: int
    notifications_failed: int


class AdminBackgroundTaskResponse(BaseModel):
    name: str
    running: bool


class AdminMetricsResponse(BaseModel):
    collected_at: datetime
    requests: AdminRequestStatsResponse
    process: AdminProcessStatsResponse
    database: AdminDatabaseStatsResponse
    background_tasks: List[AdminBackgroundTaskResponse]
    online_users: int
    open_tickets: int
    pending_payments: int


class AdminSlotResponse(ClassSlotResponse):
    booked: int = 0
    teacher_telegram_id: Optional[int] = None

    @classmethod
    def from_record(cls, slot: ClassSlot) -> "AdminSlotResponse":
        base = ClassSlotResponse.from_record(slot)
        return cls(
            **base.model_dump(),
            booked=slot.booked_count,
            teacher_telegram_id=slot.teacher_telegram_id,
        )


class AdminSlotsCreatedResponse(BaseModel):
    created: List[AdminSlotResponse]
    skipped: int


class AdminCreateSlotRequest(BaseModel):
    class_key: str = Field(min_length=1, max_length=32)
    # Время с часовым поясом (ISO 8601); без пояса — 422.
    starts_at: datetime
    capacity: int = Field(ge=1, le=100)
    teacher_telegram_id: Optional[int] = Field(default=None, gt=0, le=MAX_DB_ID)


class AdminSlotTeacherRequest(BaseModel):
    # None — снять преподавателя.
    teacher_telegram_id: Optional[int] = Field(default=None, gt=0, le=MAX_DB_ID)


class StaffMemberResponse(BaseModel):
    telegram_id: int
    role: str
    display_name: Optional[str]
    user_name: str
    public_name: str
    phone: Optional[str]
    has_web_account: bool
    created_at: datetime


CATALOG_KEY_PATTERN = "^[a-z][a-z0-9_]{1,31}$"


class AdminClassFormat(BaseModel):
    """Направление в админке (и запрос, и ответ)."""

    key: str = Field(pattern=CATALOG_KEY_PATTERN)
    title: str = Field(min_length=1, max_length=100)
    short_title: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=1000)
    level: str = Field(default="", max_length=100)
    sort_order: int = Field(default=0, ge=-10000, le=10000)
    is_active: bool = True


class AdminLessonPackage(BaseModel):
    """Абонемент в админке; цена — в целых рублях."""

    key: str = Field(pattern=CATALOG_KEY_PATTERN)
    title: str = Field(min_length=1, max_length=100)
    lessons: int = Field(ge=1, le=100)
    price_rub: int = Field(ge=1, le=10_000_000)
    validity_days: Optional[int] = Field(default=None, ge=1, le=3650)
    sort_order: int = Field(default=0, ge=-10000, le=10000)
    is_active: bool = True


class StudioRulesModel(BaseModel):
    cancellation_deadline_hours: int = Field(ge=0, le=168)
    rebook_cooldown_hours: int = Field(ge=0, le=168)
    manual_credit_validity_days: int = Field(ge=1, le=3650)
    expiry_warning_days: int = Field(ge=0, le=60)


class StaffRoleRequest(BaseModel):
    role: str = Field(pattern="^(admin|teacher)$")
    display_name: Optional[str] = Field(default=None, max_length=64)


class AdminUpdateSlotRequest(BaseModel):
    capacity: Optional[int] = Field(default=None, ge=1, le=100)
    starts_at: Optional[datetime] = None
    reason: Optional[str] = Field(default=None, max_length=300)


class AdminCancelSlotRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=300)


class AdminCancelSlotResponse(BaseModel):
    slot: AdminSlotResponse
    already_cancelled: bool
    refunded_participants: int


class AdminParticipantResponse(BaseModel):
    booking_id: int
    telegram_id: int
    user_name: str
    phone: Optional[str]
    booked_at: datetime
    status: str
    # Последняя служебная заметка об участнике (уровень, ограничения).
    note: Optional[str] = None


class AdminAccountResponse(BaseModel):
    web_user_id: Optional[int]
    email: Optional[str]
    telegram_id: Optional[int]
    name: str
    phone: Optional[str]
    lesson_credits: int
    is_admin: bool
    registered_at: datetime
    last_seen_at: Optional[datetime]


class AdminPaymentResponse(BaseModel):
    id: int
    telegram_id: int
    user_name: Optional[str]
    package_title: str
    lessons: int
    amount_rub: float
    status: str
    status_label: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    refund_reason: Optional[str] = None
    refunded_at: Optional[datetime] = None

    @classmethod
    def from_record(cls, payment: Any) -> "AdminPaymentResponse":
        from ..keyboards.user.cabinet import PAYMENT_STATUS_LABELS

        return cls(
            id=payment.id,
            telegram_id=payment.telegram_id,
            user_name=payment.user_name,
            package_title=payment.package_title,
            lessons=payment.lessons,
            amount_rub=round(payment.amount_minor / 100, 2),
            status=payment.status,
            status_label=PAYMENT_STATUS_LABELS.get(payment.status, payment.status),
            created_at=payment.created_at,
            completed_at=payment.completed_at,
            expires_at=payment.expires_at,
            refund_reason=payment.refund_reason,
            refunded_at=payment.refunded_at,
        )


class AdminPaymentsPage(BaseModel):
    items: List[AdminPaymentResponse]
    next_before_id: Optional[int] = None


class AdminClientCardResponse(BaseModel):
    profile: ProfileResponse
    role: Optional[str] = None
    email: Optional[str] = None
    has_web_account: bool = False
    packages: PackageSummaryResponse
    upcoming: List[UserBookingResponse]


class AdminClientProfileRequest(BaseModel):
    user_name: Optional[str] = Field(default=None, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=64)


class AdminGrantRequest(BaseModel):
    package_key: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=300)
    # Ключ формы: повторная отправка той же формы ничего не начислит.
    idempotence_key: uuid.UUID


class AdminRevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


class AdminCreditAdjustmentRequest(BaseModel):
    delta: int = Field(ge=-100, le=100)
    reason: str = Field(min_length=1, max_length=300)
    idempotence_key: uuid.UUID


class AdminRefundRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


class AdminOperationResultResponse(BaseModel):
    applied: bool
    balance: Optional[int] = None
    lessons: Optional[int] = None
    status: Optional[str] = None
    message: Optional[str] = None


class AdminLedgerOperationResponse(HistoryOperationResponse):
    """Операция для администратора: причина видна всегда, плюс участник и
    кто выполнил."""

    telegram_id: int
    user_name: Optional[str] = None
    actor_telegram_id: Optional[int] = None

    @classmethod
    def from_operation(cls, item: HistoryOperation) -> "AdminLedgerOperationResponse":
        base = HistoryOperationResponse.from_operation(item).model_dump()
        base["reason"] = item.entry.reason
        return cls(
            **base,
            telegram_id=item.entry.telegram_id,
            user_name=item.entry.user_name,
            actor_telegram_id=item.entry.actor_telegram_id,
        )


class AdminLedgerPage(BaseModel):
    items: List[AdminLedgerOperationResponse]
    next_before_id: Optional[int] = None


class AdminAuditEventResponse(BaseModel):
    source: str
    action: str
    label: str
    created_at: datetime
    actor_telegram_id: Optional[int] = None
    actor_name: Optional[str] = None
    target_telegram_id: Optional[int] = None
    target_name: Optional[str] = None
    slot_id: Optional[int] = None
    payment_id: Optional[int] = None
    delta: Optional[int] = None
    reason: Optional[str] = None


class AdminAuditPage(BaseModel):
    items: List[AdminAuditEventResponse]
    # Передайте как before для следующей (более старой) страницы.
    next_before: Optional[datetime] = None


class AdminReconciliationMismatch(BaseModel):
    telegram_id: int
    user_name: str
    lesson_credits: int
    ledger_total: int
    difference: int
    ledger_entries: int
    last_entry_at: Optional[datetime] = None


class AdminReconciliationResponse(BaseModel):
    checked_users: int
    mismatched_users: int
    mismatches: List[AdminReconciliationMismatch]

    @classmethod
    def from_result(cls, result: Any) -> "AdminReconciliationResponse":
        return cls(
            checked_users=result.checked_users,
            mismatched_users=result.mismatched_users,
            mismatches=[
                AdminReconciliationMismatch(
                    telegram_id=item.telegram_id,
                    user_name=item.user_name,
                    lesson_credits=item.lesson_credits,
                    ledger_total=item.ledger_total,
                    difference=item.difference,
                    ledger_entries=item.ledger_entries,
                    last_entry_at=item.last_entry_at,
                )
                for item in result.mismatches
            ],
        )


class AdminSupportTicketResponse(BaseModel):
    id: int
    telegram_id: int
    user_name: Optional[str]
    status: str
    created_at: datetime
    updated_at: datetime
    last_message: str


class AdminSupportThreadResponse(SupportThreadResponse):
    telegram_id: int
    user_name: Optional[str]


class AdminSupportReplyRequest(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class AdminSupportActionResponse(BaseModel):
    # delivered — ответ доставлен в Telegram; saved_not_delivered — сохранён,
    # но Telegram не доставил; closed — обращение закрыто.
    status: str


# ---------- рассылки, заметки, уведомления ----------


class AdminSegmentRequest(BaseModel):
    segment: str = Field(pattern="^(all|with_credits|slot|direction)$")
    params: dict = Field(default_factory=dict)


class AdminSegmentCountResponse(BaseModel):
    recipients: int


class AdminBroadcastRequest(AdminSegmentRequest):
    body: str = Field(min_length=1, max_length=2000)


class AdminBroadcastResponse(BaseModel):
    id: int
    author_telegram_id: int
    author_name: Optional[str]
    body: str
    segment: str
    segment_params: dict
    recipients: int
    created_at: datetime
    sent: int = 0
    failed: int = 0


class AdminNoteRequest(BaseModel):
    body: str = Field(min_length=1, max_length=1000)


class AdminNoteResponse(BaseModel):
    id: int
    telegram_id: int
    author_telegram_id: int
    author_name: Optional[str]
    body: str
    created_at: datetime


class AdminFailedNotificationResponse(BaseModel):
    id: int
    telegram_id: int
    user_name: Optional[str]
    kind: str
    delivery_status: str
    attempts: int
    last_error: Optional[str]
    created_at: datetime


class AdminFailedNotificationsPage(BaseModel):
    # Число уведомлений по статусам за 30 дней и сколько участников
    # недоступно (бот заблокирован и т. п.).
    stats: Dict[str, int]
    items: List[AdminFailedNotificationResponse]
    next_before_id: Optional[int] = None
