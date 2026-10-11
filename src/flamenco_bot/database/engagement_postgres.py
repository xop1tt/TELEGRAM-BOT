"""PostgreSQL: рассылки, заметки об участниках, сбои уведомлений, отчёты.

Рассылка ставит каждому получателю уведомление ``announcement`` в общий
outbox одной транзакцией (дальше — обычная отправка); сегменты — решение
владельца студии (миграция 020). Отчёты и выгрузки — только чтение.

Модуль импортируется из ``repository.py`` после объявления общих типов.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from .repository import is_staff_admin
from .staff_postgres import record_staff_action

logger = logging.getLogger("bot.database")

SEGMENTS = ("all", "with_credits", "slot", "direction")
MAX_BROADCAST_LENGTH = 2000
MAX_NOTE_LENGTH = 1000


@dataclass(frozen=True)
class Broadcast:
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


@dataclass(frozen=True)
class ClientNote:
    id: int
    telegram_id: int
    author_telegram_id: int
    author_name: Optional[str]
    body: str
    created_at: datetime


@dataclass(frozen=True)
class FailedNotification:
    id: int
    telegram_id: int
    user_name: Optional[str]
    kind: str
    delivery_status: str
    attempts: int
    last_error: Optional[str]
    created_at: datetime


def _segment_query(segment: str, params: Mapping[str, Any]) -> tuple:
    """SQL выборки получателей сегмента и его параметры."""
    if segment == "all":
        return "SELECT telegram_id FROM bot_users", ()
    if segment == "with_credits":
        inactive_days = params.get("inactive_days")
        if inactive_days:
            return (
                """
                SELECT profile.telegram_id FROM bot_users AS profile
                WHERE profile.lesson_credits > 0
                  AND NOT EXISTS (
                      SELECT 1 FROM lesson_bookings AS booking
                      WHERE booking.telegram_id = profile.telegram_id
                        AND booking.booked_at > NOW() - make_interval(days => $1)
                  )
                """,
                (int(inactive_days),),
            )
        return "SELECT telegram_id FROM bot_users WHERE lesson_credits > 0", ()
    if segment == "slot":
        return (
            """
            SELECT telegram_id FROM lesson_bookings
            WHERE slot_id = $1 AND status = 'confirmed'
            """,
            (int(params["slot_id"]),),
        )
    if segment == "direction":
        return (
            """
            SELECT DISTINCT booking.telegram_id
            FROM lesson_bookings AS booking
            JOIN lesson_slots AS slot ON slot.id = booking.slot_id
            WHERE slot.class_key = $1
              AND booking.status <> 'cancelled'
              AND slot.status <> 'cancelled'
              AND slot.starts_at > NOW() - make_interval(days => $2)
            """,
            (str(params["class_key"]), int(params.get("days") or 60)),
        )
    raise ValueError("Неизвестный сегмент")


def validate_segment(segment: str, params: Mapping[str, Any]) -> dict:
    """Проверенные параметры сегмента (``ValueError`` — некорректные)."""
    if segment not in SEGMENTS:
        raise ValueError("Неизвестный сегмент получателей")
    clean: dict = {}
    if segment == "with_credits" and params.get("inactive_days"):
        days = int(params["inactive_days"])
        if not 1 <= days <= 365:
            raise ValueError("Дней без записи — от 1 до 365")
        clean["inactive_days"] = days
    if segment == "slot":
        slot_id = int(params.get("slot_id") or 0)
        if slot_id <= 0:
            raise ValueError("Выберите занятие")
        clean["slot_id"] = slot_id
    if segment == "direction":
        class_key = str(params.get("class_key") or "")
        if not class_key:
            raise ValueError("Выберите направление")
        days = int(params.get("days") or 60)
        if not 1 <= days <= 365:
            raise ValueError("Период — от 1 до 365 дней")
        clean.update(class_key=class_key, days=days)
    return clean


class PostgresEngagementMixin:
    _pool: Any

    # ---------- рассылки ----------

    async def count_segment(self, segment: str, params: Mapping[str, Any]) -> int:
        query, args = _segment_query(segment, validate_segment(segment, params))
        async with self._pool.acquire() as connection:
            return await connection.fetchval(
                "SELECT COUNT(*) FROM ({}) AS recipients".format(query), *args
            )

    async def create_broadcast(
        self,
        author_telegram_id: int,
        body: str,
        segment: str,
        params: Mapping[str, Any],
        author_is_admin_required: bool = True,
    ) -> Broadcast:
        """Рассылка: уведомление ``announcement`` каждому получателю.

        ``author_is_admin_required=False`` — преподаватель пишет участникам
        своего занятия (право проверено интерфейсом)."""
        text = body.strip()
        if not 1 <= len(text) <= MAX_BROADCAST_LENGTH:
            raise ValueError(
                "Текст рассылки — от 1 до {} символов".format(MAX_BROADCAST_LENGTH)
            )
        clean = validate_segment(segment, params)
        query, args = _segment_query(segment, clean)
        offset = len(args)
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                if author_is_admin_required and not await is_staff_admin(
                    connection, author_telegram_id
                ):
                    raise PermissionError("Рассылку может сделать только админ")
                broadcast_id = await connection.fetchval(
                    """
                    INSERT INTO broadcasts (
                        author_telegram_id, body, segment, segment_params
                    )
                    VALUES ($1, $2, $3, $4::JSONB)
                    RETURNING id
                    """,
                    author_telegram_id,
                    text,
                    segment,
                    json.dumps(clean),
                )
                recipients = await connection.fetchval(
                    """
                    WITH targets AS ({query}),
                    queued AS (
                        INSERT INTO user_notifications (
                            telegram_id, kind, payload, dedupe_key
                        )
                        SELECT targets.telegram_id, 'announcement',
                               jsonb_build_object(
                                   'broadcast_id', ${b}::BIGINT, 'body', ${t}::TEXT
                               ),
                               'broadcast:' || ${b}::BIGINT || ':'
                                   || targets.telegram_id
                        FROM targets
                        ON CONFLICT (dedupe_key) DO NOTHING
                        RETURNING 1
                    )
                    SELECT COUNT(*) FROM queued
                    """.format(query=query, b=offset + 1, t=offset + 2),
                    *args,
                    broadcast_id,
                    text,
                )
                await connection.execute(
                    "UPDATE broadcasts SET recipients = $2 WHERE id = $1",
                    broadcast_id,
                    recipients,
                )
                await record_staff_action(
                    connection,
                    author_telegram_id,
                    "broadcast_sent",
                    None,
                    {
                        "broadcast_id": broadcast_id,
                        "segment": segment,
                        "recipients": recipients,
                        **({"slot_id": clean["slot_id"]} if "slot_id" in clean else {}),
                    },
                )
        logger.warning(
            "Broadcast queued id=%s segment=%s recipients=%s author=%s",
            broadcast_id,
            segment,
            recipients,
            author_telegram_id,
        )
        return (await self.list_broadcasts(limit=1, only_id=broadcast_id))[0]

    async def list_broadcasts(
        self, limit: int = 30, only_id: Optional[int] = None
    ) -> Sequence[Broadcast]:
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT broadcast.*, author.user_name AS author_name,
                       COUNT(notification.id) FILTER (
                           WHERE notification.delivery_status = 'sent'
                       )::INTEGER AS sent,
                       COUNT(notification.id) FILTER (
                           WHERE notification.delivery_status IN ('failed', 'skipped')
                       )::INTEGER AS failed
                FROM broadcasts AS broadcast
                LEFT JOIN bot_users AS author
                    ON author.telegram_id = broadcast.author_telegram_id
                LEFT JOIN user_notifications AS notification
                    ON notification.kind = 'announcement'
                   AND (notification.payload->>'broadcast_id')::BIGINT = broadcast.id
                WHERE ($2::BIGINT IS NULL OR broadcast.id = $2)
                GROUP BY broadcast.id, author.user_name
                ORDER BY broadcast.id DESC
                LIMIT $1
                """,
                limit,
                only_id,
            )
        return [
            Broadcast(
                id=record["id"],
                author_telegram_id=record["author_telegram_id"],
                author_name=record["author_name"],
                body=record["body"],
                segment=record["segment"],
                segment_params=json.loads(record["segment_params"])
                if isinstance(record["segment_params"], str)
                else dict(record["segment_params"]),
                recipients=record["recipients"],
                created_at=record["created_at"],
                sent=record["sent"],
                failed=record["failed"],
            )
            for record in records
        ]

    # ---------- заметки ----------

    async def list_client_notes(self, telegram_id: int) -> Sequence[ClientNote]:
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT note.*, author.user_name AS author_name
                FROM client_notes AS note
                LEFT JOIN bot_users AS author
                    ON author.telegram_id = note.author_telegram_id
                WHERE note.telegram_id = $1
                ORDER BY note.created_at DESC
                """,
                telegram_id,
            )
        return [ClientNote(**dict(record)) for record in records]

    async def add_client_note(
        self, telegram_id: int, author_telegram_id: int, body: str
    ) -> ClientNote:
        text = body.strip()
        if not 1 <= len(text) <= MAX_NOTE_LENGTH:
            raise ValueError("Заметка — от 1 до {} символов".format(MAX_NOTE_LENGTH))
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                if not await is_staff_admin(connection, author_telegram_id):
                    raise PermissionError("Заметки ведёт администратор")
                if not await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM bot_users WHERE telegram_id = $1)",
                    telegram_id,
                ):
                    raise LookupError("Участник не найден")
                note_id = await connection.fetchval(
                    """
                    INSERT INTO client_notes (telegram_id, author_telegram_id, body)
                    VALUES ($1, $2, $3) RETURNING id
                    """,
                    telegram_id,
                    author_telegram_id,
                    text,
                )
                await record_staff_action(
                    connection,
                    author_telegram_id,
                    "note_added",
                    telegram_id,
                    {"note_id": note_id},
                )
        notes = await self.list_client_notes(telegram_id)
        return next(note for note in notes if note.id == note_id)

    async def delete_client_note(
        self, telegram_id: int, note_id: int, actor_telegram_id: int
    ) -> bool:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                if not await is_staff_admin(connection, actor_telegram_id):
                    raise PermissionError("Заметки ведёт администратор")
                deleted = await connection.fetchval(
                    "DELETE FROM client_notes WHERE id = $1 AND telegram_id = $2 "
                    "RETURNING id",
                    note_id,
                    telegram_id,
                )
                if deleted is not None:
                    await record_staff_action(
                        connection,
                        actor_telegram_id,
                        "note_deleted",
                        telegram_id,
                        {"note_id": note_id},
                    )
        return deleted is not None

    async def latest_notes_for_slot(self, slot_id: int) -> dict:
        """Последняя заметка каждого записанного — для преподавателя."""
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT DISTINCT ON (note.telegram_id) note.telegram_id, note.body
                FROM client_notes AS note
                JOIN lesson_bookings AS booking
                    ON booking.telegram_id = note.telegram_id
                WHERE booking.slot_id = $1
                ORDER BY note.telegram_id, note.created_at DESC
                """,
                slot_id,
            )
        return {record["telegram_id"]: record["body"] for record in records}

    # ---------- сбои уведомлений ----------

    async def list_failed_notifications(
        self, limit: int = 50, before_id: Optional[int] = None
    ) -> Sequence[FailedNotification]:
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT notification.id, notification.telegram_id,
                       profile.user_name, notification.kind,
                       notification.delivery_status, notification.attempts,
                       notification.last_error, notification.created_at
                FROM user_notifications AS notification
                LEFT JOIN bot_users AS profile
                    ON profile.telegram_id = notification.telegram_id
                WHERE notification.delivery_status IN ('failed', 'skipped')
                  AND ($2::BIGINT IS NULL OR notification.id < $2)
                ORDER BY notification.id DESC
                LIMIT $1
                """,
                limit,
                before_id,
            )
        return [FailedNotification(**dict(record)) for record in records]

    async def notification_delivery_stats(self, days: int = 30) -> dict:
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT delivery_status, COUNT(*)::INTEGER AS count
                FROM user_notifications
                WHERE created_at > NOW() - make_interval(days => $1)
                GROUP BY delivery_status
                """,
                days,
            )
            unreachable = await connection.fetchval(
                """
                SELECT COUNT(DISTINCT telegram_id) FROM user_notifications
                WHERE delivery_status = 'failed'
                  AND created_at > NOW() - make_interval(days => $1)
                """,
                days,
            )
        stats = {record["delivery_status"]: record["count"] for record in records}
        stats["unreachable_users"] = unreachable
        return stats
