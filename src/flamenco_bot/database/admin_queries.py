"""Выборки для админ-панели сайта: сводка, аккаунты, платежи, обращения,
показатели базы данных.

Только чтение: изменения (расписание, ответы в поддержку) идут через
существующие методы репозитория и сервисы, как и в админке бота. Отдельный
модуль — только из-за размера repository.py (как studio_postgres.py).
"""

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence


# Как в статистике бота (/stats): «онлайн» — активность за 5 минут.
ONLINE_WINDOW = timedelta(minutes=5)


@dataclass(frozen=True)
class AdminDashboard:
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
    sales_today_minor: int
    sales_month_count: int
    sales_month_minor: int
    pending_payments: int
    open_tickets: int


@dataclass(frozen=True)
class AdminAccount:
    """Участник глазами администратора: веб-аккаунт и/или профиль бота.

    Одна строка на человека — веб-аккаунт с привязанным Telegram и его
    профиль бота склеены по telegram_id.
    """

    web_user_id: Optional[int]
    email: Optional[str]
    telegram_id: Optional[int]
    name: str
    phone: Optional[str]
    lesson_credits: int
    is_admin: bool
    role: Optional[str]
    registered_at: datetime
    last_seen_at: Optional[datetime]


@dataclass(frozen=True)
class AdminPayment:
    id: int
    telegram_id: int
    user_name: Optional[str]
    package_title: str
    lessons: int
    amount_minor: int
    status: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    refund_reason: Optional[str] = None
    refunded_at: Optional[datetime] = None


_ADMIN_PAYMENT_SELECT = """
    SELECT payment.id, payment.telegram_id, profile.user_name,
           payment.package_title, payment.lessons,
           payment.amount_minor, payment.status, payment.created_at,
           payment.completed_at, payment.expires_at, payment.refund_reason,
           payment.refunded_at
    FROM lesson_payments AS payment
    LEFT JOIN bot_users AS profile ON profile.telegram_id = payment.telegram_id
"""


@dataclass(frozen=True)
class AdminSupportTicket:
    id: int
    telegram_id: int
    user_name: Optional[str]
    status: str
    created_at: datetime
    updated_at: datetime
    last_message: str


@dataclass(frozen=True)
class DatabaseMetrics:
    backend: str
    ping_ms: float
    pool_size: int
    idle_connections: int
    database_size_bytes: Optional[int]
    connections: Optional[int]
    active_web_sessions: int
    notifications_pending: int
    notifications_failed: int


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 200:
        raise ValueError("Количество строк должно быть от 1 до 200")


def _normalize_query(query: str) -> str:
    normalized = query.strip()
    if len(normalized) > 100:
        raise ValueError("Поисковый запрос должен быть не длиннее 100 символов")
    return normalized


class PostgresAdminMixin:
    _pool: Any

    async def get_admin_dashboard(
        self, day_start: datetime, day_end: datetime, week_end: datetime
    ) -> AdminDashboard:
        """Сводка: «сегодня» — [day_start, day_end), «неделя» — [сейчас, week_end)."""
        month_start = day_start - timedelta(days=30)
        online_since = datetime.now(timezone.utc) - ONLINE_WINDOW
        async with self._pool.acquire() as connection:
            record = await connection.fetchrow(
                """
                WITH slot_load AS (
                    SELECT slot.starts_at, slot.capacity, slot.status,
                           (SELECT COUNT(*) FROM lesson_bookings AS booking
                            WHERE booking.slot_id = slot.id
                              AND booking.status = 'confirmed') AS booked
                    FROM lesson_slots AS slot
                    WHERE slot.status <> 'cancelled'
                      AND slot.starts_at >= LEAST($1::TIMESTAMPTZ, NOW())
                      AND slot.starts_at < GREATEST($2::TIMESTAMPTZ, $3::TIMESTAMPTZ)
                ),
                sales AS (
                    SELECT amount_minor, COALESCE(completed_at, created_at) AS paid_at
                    FROM lesson_payments
                    WHERE status IN ('succeeded', 'refund_pending')
                      AND COALESCE(completed_at, created_at) >= $5::TIMESTAMPTZ
                )
                SELECT
                    (SELECT COUNT(*) FROM slot_load
                     WHERE starts_at >= $1 AND starts_at < $2) AS slots_today,
                    (SELECT COALESCE(SUM(booked), 0) FROM slot_load
                     WHERE starts_at >= $1 AND starts_at < $2) AS bookings_today,
                    (SELECT COALESCE(SUM(GREATEST(capacity - booked, 0)), 0)
                     FROM slot_load
                     WHERE status = 'open' AND starts_at >= $1 AND starts_at < $2)
                        AS free_seats_today,
                    (SELECT COUNT(*) FROM slot_load
                     WHERE starts_at >= NOW() AND starts_at < $3) AS slots_week,
                    (SELECT COALESCE(SUM(booked), 0) FROM slot_load
                     WHERE starts_at >= NOW() AND starts_at < $3) AS bookings_week,
                    (SELECT COALESCE(SUM(GREATEST(capacity - booked, 0)), 0)
                     FROM slot_load
                     WHERE status = 'open' AND starts_at >= NOW() AND starts_at < $3)
                        AS free_seats_week,
                    (SELECT COUNT(*) FROM bot_users) AS total_profiles,
                    (SELECT COUNT(*) FROM users) AS total_web_users,
                    (SELECT COUNT(*) FROM bot_users WHERE registered_at >= $1)
                        AS new_profiles_today,
                    (SELECT COUNT(*) FROM users WHERE created_at >= $1)
                        AS new_web_users_today,
                    (SELECT COUNT(*) FROM bot_users WHERE last_seen_at >= $4)
                        AS online_users,
                    (SELECT COUNT(*) FROM sales WHERE paid_at >= $1)
                        AS sales_today_count,
                    (SELECT COALESCE(SUM(amount_minor), 0) FROM sales
                     WHERE paid_at >= $1) AS sales_today_minor,
                    (SELECT COUNT(*) FROM sales) AS sales_month_count,
                    (SELECT COALESCE(SUM(amount_minor), 0) FROM sales)
                        AS sales_month_minor,
                    (SELECT COUNT(*) FROM lesson_payments WHERE status = 'pending')
                        AS pending_payments,
                    (SELECT COUNT(*) FROM support_tickets WHERE status = 'open')
                        AS open_tickets
                """,
                day_start,
                day_end,
                week_end,
                online_since,
                month_start,
            )
        return AdminDashboard(**{key: int(record[key]) for key in record.keys()})

    async def search_admin_accounts(
        self, query: str = "", limit: int = 50
    ) -> Sequence[AdminAccount]:
        normalized = _normalize_query(query)
        _validate_limit(limit)
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT account.id AS web_user_id, account.email,
                       COALESCE(profile.telegram_id, account.telegram_id)
                           AS telegram_id,
                       COALESCE(profile.user_name, account.display_name) AS name,
                       profile.phone,
                       COALESCE(profile.lesson_credits, 0) AS lesson_credits,
                       staff.role IS NOT DISTINCT FROM 'admin' AS is_admin,
                       staff.role,
                       LEAST(account.created_at, profile.registered_at)
                           AS registered_at,
                       profile.last_seen_at
                FROM users AS account
                FULL OUTER JOIN bot_users AS profile
                    ON profile.telegram_id = account.telegram_id
                LEFT JOIN staff_members AS staff
                    ON staff.telegram_id = COALESCE(
                        profile.telegram_id, account.telegram_id
                    )
                WHERE $1 = ''
                   OR account.email ILIKE $2
                   OR account.display_name ILIKE $2
                   OR profile.user_name ILIKE $2
                   OR profile.phone ILIKE $2
                   OR profile.telegram_id::TEXT = $1
                ORDER BY LEAST(account.created_at, profile.registered_at) DESC
                LIMIT $3
                """,
                normalized,
                "%{}%".format(normalized),
                limit,
            )
        return [AdminAccount(**dict(record)) for record in records]

    async def list_admin_payments(
        self,
        limit: int = 50,
        status: Optional[str] = None,
        telegram_id: Optional[int] = None,
        before_id: Optional[int] = None,
    ) -> Sequence[AdminPayment]:
        """Платежи от новых к старым; фильтры — статус и участник,
        страницы — ``before_id`` (id последнего показанного)."""
        _validate_limit(limit)
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                _ADMIN_PAYMENT_SELECT
                + """
                WHERE ($2::TEXT IS NULL OR payment.status = $2)
                  AND ($3::BIGINT IS NULL OR payment.telegram_id = $3)
                  AND ($4::BIGINT IS NULL OR payment.id < $4)
                ORDER BY payment.id DESC
                LIMIT $1
                """,
                limit,
                status,
                telegram_id,
                before_id,
            )
        return [AdminPayment(**dict(record)) for record in records]

    async def get_admin_payment(self, payment_id: int) -> Optional[AdminPayment]:
        async with self._pool.acquire() as connection:
            record = await connection.fetchrow(
                _ADMIN_PAYMENT_SELECT + "WHERE payment.id = $1", payment_id
            )
        return AdminPayment(**dict(record)) if record is not None else None

    async def list_admin_support_tickets(
        self, status: str = "open", limit: int = 50
    ) -> Sequence[AdminSupportTicket]:
        if status not in {"open", "closed"}:
            raise ValueError("Статус обращения: open или closed")
        _validate_limit(limit)
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT ticket.id, ticket.telegram_id, profile.user_name,
                       ticket.status, ticket.created_at, ticket.updated_at,
                       COALESCE(message.body, '') AS last_message
                FROM support_tickets AS ticket
                LEFT JOIN bot_users AS profile
                    ON profile.telegram_id = ticket.telegram_id
                LEFT JOIN LATERAL (
                    SELECT body FROM support_messages
                    WHERE ticket_id = ticket.id
                    ORDER BY created_at DESC, id DESC LIMIT 1
                ) AS message ON TRUE
                WHERE ticket.status = $1
                ORDER BY ticket.updated_at DESC
                LIMIT $2
                """,
                status,
                limit,
            )
        return [AdminSupportTicket(**dict(record)) for record in records]

    async def get_database_metrics(self) -> DatabaseMetrics:
        async with self._pool.acquire() as connection:
            started = time.perf_counter()
            await connection.execute("SELECT 1")
            ping_ms = (time.perf_counter() - started) * 1000
            record = await connection.fetchrow(
                """
                SELECT pg_database_size(current_database()) AS database_size_bytes,
                       (SELECT COUNT(*) FROM pg_stat_activity
                        WHERE datname = current_database()) AS connections,
                       (SELECT COUNT(*) FROM web_sessions WHERE expires_at > NOW())
                           AS active_web_sessions,
                       (SELECT COUNT(*) FROM user_notifications
                        WHERE delivery_status IN ('pending', 'sending'))
                           AS notifications_pending,
                       (SELECT COUNT(*) FROM user_notifications
                        WHERE delivery_status = 'failed') AS notifications_failed
                """
            )
        return DatabaseMetrics(
            backend="postgres",
            ping_ms=round(ping_ms, 2),
            pool_size=self._pool.get_size(),
            idle_connections=self._pool.get_idle_size(),
            database_size_bytes=record["database_size_bytes"],
            connections=record["connections"],
            active_web_sessions=record["active_web_sessions"],
            notifications_pending=record["notifications_pending"],
            notifications_failed=record["notifications_failed"],
        )
