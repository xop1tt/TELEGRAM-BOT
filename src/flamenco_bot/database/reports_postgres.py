"""PostgreSQL: отчёты администратора и выгрузки CSV (только чтение).

Даты группируются по часовому поясу студии (``STUDIO_TIMEZONE``), чтобы
«месяц» и «день недели» совпадали с тем, что видит студия.

Модуль импортируется из ``repository.py`` после объявления общих типов.
"""

from datetime import datetime
from typing import Any, Sequence

from ..config import Config


def _tz() -> str:
    return str(Config.STUDIO_TIMEZONE)


class PostgresReportsMixin:
    _pool: Any

    async def get_report(
        self, start: datetime, end: datetime, sleeping_days: int = 30
    ) -> dict:
        """Сводный отчёт за период [start, end)."""
        tz = _tz()
        async with self._pool.acquire() as connection:
            async with connection.transaction(
                isolation="repeatable_read", readonly=True
            ):
                revenue = await connection.fetch(
                    """
                    SELECT to_char(date_trunc('month', completed_at AT TIME ZONE $3),
                                   'YYYY-MM') AS month,
                           COUNT(*)::INTEGER AS payments,
                           SUM(amount_minor)::BIGINT AS amount_minor,
                           COUNT(*) FILTER (WHERE status = 'refunded')::INTEGER
                               AS refunded,
                           COALESCE(SUM(amount_minor) FILTER (
                               WHERE status = 'refunded'
                           ), 0)::BIGINT AS refunded_minor
                    FROM lesson_payments
                    WHERE completed_at >= $1 AND completed_at < $2
                    GROUP BY 1 ORDER BY 1
                    """,
                    start,
                    end,
                    tz,
                )
                packages = await connection.fetch(
                    """
                    SELECT package_title AS title, COUNT(*)::INTEGER AS payments,
                           SUM(amount_minor)::BIGINT AS amount_minor
                    FROM lesson_payments
                    WHERE completed_at >= $1 AND completed_at < $2
                      AND status <> 'refunded'
                    GROUP BY package_title ORDER BY 3 DESC
                    """,
                    start,
                    end,
                )
                grants = await connection.fetchval(
                    """
                    SELECT COALESCE(SUM(lessons), 0)::INTEGER
                    FROM lesson_package_grants
                    WHERE created_at >= $1 AND created_at < $2
                    """,
                    start,
                    end,
                )
                slots_cte = """
                    WITH slots AS (
                        SELECT slot.id, slot.class_key, slot.capacity,
                               slot.starts_at AT TIME ZONE $3 AS local_start,
                               (SELECT COUNT(*) FROM lesson_bookings AS booking
                                WHERE booking.slot_id = slot.id
                                  AND booking.status <> 'cancelled')::INTEGER
                                   AS booked
                        FROM lesson_slots AS slot
                        WHERE slot.starts_at >= $1 AND slot.starts_at < $2
                          AND slot.starts_at < NOW()
                          AND slot.status <> 'cancelled'
                    )
                """
                by_direction = await connection.fetch(
                    slots_cte
                    + """
                    SELECT class_key AS key, COUNT(*)::INTEGER AS slots,
                           SUM(booked)::INTEGER AS booked,
                           SUM(capacity)::INTEGER AS capacity
                    FROM slots GROUP BY class_key ORDER BY 3 DESC
                    """,
                    start,
                    end,
                    tz,
                )
                by_weekday = await connection.fetch(
                    slots_cte
                    + """
                    SELECT EXTRACT(ISODOW FROM local_start)::INTEGER AS key,
                           COUNT(*)::INTEGER AS slots,
                           SUM(booked)::INTEGER AS booked,
                           SUM(capacity)::INTEGER AS capacity
                    FROM slots GROUP BY 1 ORDER BY 1
                    """,
                    start,
                    end,
                    tz,
                )
                by_hour = await connection.fetch(
                    slots_cte
                    + """
                    SELECT EXTRACT(HOUR FROM local_start)::INTEGER AS key,
                           COUNT(*)::INTEGER AS slots,
                           SUM(booked)::INTEGER AS booked,
                           SUM(capacity)::INTEGER AS capacity
                    FROM slots GROUP BY 1 ORDER BY 1
                    """,
                    start,
                    end,
                    tz,
                )
                cancellations = await connection.fetchrow(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE booking.status = 'cancelled'
                            AND slot.status <> 'cancelled')::INTEGER AS by_users,
                        COUNT(*) FILTER (WHERE slot.status = 'cancelled')::INTEGER
                            AS by_studio,
                        COUNT(*)::INTEGER AS total
                    FROM lesson_bookings AS booking
                    JOIN lesson_slots AS slot ON slot.id = booking.slot_id
                    WHERE slot.starts_at >= $1 AND slot.starts_at < $2
                    """,
                    start,
                    end,
                )
                repeat = await connection.fetchrow(
                    """
                    SELECT COUNT(*) FILTER (WHERE paid >= 1)::INTEGER AS buyers,
                           COUNT(*) FILTER (WHERE paid >= 2)::INTEGER AS repeat_buyers
                    FROM (
                        SELECT telegram_id, COUNT(*) AS paid FROM lesson_payments
                        WHERE status IN ('succeeded', 'refund_pending')
                        GROUP BY telegram_id
                    ) AS buyers
                    """
                )
                new_users = await connection.fetchval(
                    "SELECT COUNT(*)::INTEGER FROM bot_users "
                    "WHERE registered_at >= $1 AND registered_at < $2",
                    start,
                    end,
                )
                sleeping = await connection.fetch(
                    """
                    SELECT profile.telegram_id, profile.user_name, profile.phone,
                           profile.lesson_credits,
                           (SELECT MAX(booking.booked_at)
                            FROM lesson_bookings AS booking
                            WHERE booking.telegram_id = profile.telegram_id)
                               AS last_booking_at
                    FROM bot_users AS profile
                    WHERE profile.lesson_credits > 0
                      AND NOT EXISTS (
                          SELECT 1 FROM lesson_bookings AS booking
                          WHERE booking.telegram_id = profile.telegram_id
                            AND booking.booked_at > NOW() - make_interval(days => $1)
                      )
                    ORDER BY profile.lesson_credits DESC, profile.telegram_id
                    LIMIT 100
                    """,
                    sleeping_days,
                )
        return {
            "revenue_by_month": [dict(record) for record in revenue],
            "packages": [dict(record) for record in packages],
            "granted_lessons": grants,
            "fill_by_direction": [dict(record) for record in by_direction],
            "fill_by_weekday": [dict(record) for record in by_weekday],
            "fill_by_hour": [dict(record) for record in by_hour],
            "cancellations": dict(cancellations),
            "buyers": repeat["buyers"],
            "repeat_buyers": repeat["repeat_buyers"],
            "new_users": new_users,
            "sleeping_days": sleeping_days,
            "sleeping_clients": [dict(record) for record in sleeping],
        }

    # ---------- выгрузки ----------

    async def export_rows(
        self, kind: str, start: datetime, end: datetime
    ) -> Sequence[dict]:
        """Строки выгрузки: ``payments`` и ``bookings`` — за период,
        ``clients`` — все участники."""
        tz = _tz()
        queries = {
            "payments": (
                """
                SELECT payment.id, payment.created_at AT TIME ZONE $3 AS created,
                       payment.completed_at AT TIME ZONE $3 AS paid,
                       payment.telegram_id, profile.user_name,
                       payment.package_title, payment.lessons,
                       payment.amount_minor / 100.0 AS amount_rub, payment.status,
                       payment.refunded_at AT TIME ZONE $3 AS refunded,
                       payment.refund_reason
                FROM lesson_payments AS payment
                LEFT JOIN bot_users AS profile
                    ON profile.telegram_id = payment.telegram_id
                WHERE payment.created_at >= $1 AND payment.created_at < $2
                ORDER BY payment.id
                """,
                (start, end, tz),
            ),
            "bookings": (
                """
                SELECT slot.starts_at AT TIME ZONE $3 AS starts, slot.id AS slot_id,
                       format.title AS direction, slot.status AS slot_status,
                       booking.telegram_id, profile.user_name, profile.phone,
                       booking.status AS booking_status,
                       booking.booked_at AT TIME ZONE $3 AS booked
                FROM lesson_bookings AS booking
                JOIN lesson_slots AS slot ON slot.id = booking.slot_id
                JOIN class_formats AS format ON format.key = slot.class_key
                JOIN bot_users AS profile ON profile.telegram_id = booking.telegram_id
                WHERE slot.starts_at >= $1 AND slot.starts_at < $2
                ORDER BY slot.starts_at, profile.user_name
                """,
                (start, end, tz),
            ),
            "clients": (
                """
                SELECT profile.telegram_id, profile.user_name, profile.phone,
                       account.email, profile.lesson_credits,
                       profile.registered_at AT TIME ZONE $1 AS registered,
                       profile.last_seen_at AT TIME ZONE $1 AS last_seen,
                       staff.role
                FROM bot_users AS profile
                LEFT JOIN users AS account
                    ON account.telegram_id = profile.telegram_id
                LEFT JOIN staff_members AS staff
                    ON staff.telegram_id = profile.telegram_id
                ORDER BY profile.registered_at
                """,
                (tz,),
            ),
        }
        if kind not in queries:
            raise ValueError("Неизвестная выгрузка")
        query, args = queries[kind]
        async with self._pool.acquire() as connection:
            records = await connection.fetch(query, *args)
        return [dict(record) for record in records]
