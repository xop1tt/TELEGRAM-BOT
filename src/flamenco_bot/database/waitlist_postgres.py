"""PostgreSQL: лист ожидания на заполненные занятия (миграция 019).

Встать в очередь — только на открытое будущее занятие без свободных мест,
не будучи на него записанным и с занятием на балансе (решение владельца
студии). Когда место освобождается, всем в очереди ставится уведомление
``waitlist_seat`` в той же транзакции, что и само освобождение; записаться
может любой — кто первым успеет (автозаписи нет). Запись на занятие
удаляет участника из его очереди.

Модуль импортируется из ``repository.py`` после объявления общих типов.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from .repository import (
    SLOT_SELECT,
    ClassSlot,
    InsufficientLessonCreditsError,
    SlotUnavailableError,
)


class AlreadyBookedError(ValueError):
    """Участник уже записан на это занятие — очередь не нужна."""


@dataclass(frozen=True)
class WaitlistEntry:
    slot: ClassSlot
    joined_at: datetime
    # Сколько человек в очереди на это занятие (включая этого участника).
    queue_size: int


@dataclass(frozen=True)
class WaitlistMember:
    telegram_id: int
    user_name: str
    phone: Optional[str]
    joined_at: datetime


async def notify_waitlist(
    repository: Any, connection: Any, slot_id: int, reason: str
) -> int:
    """Освободилось место: уведомление всем в очереди (внутри транзакции).

    Только если занятие открыто, ещё не началось и места действительно
    есть. ``reason`` — для ключа дедупликации (одно уведомление на
    участника за одно освобождение). Возвращает число уведомлений.
    """
    slot = await connection.fetchrow(SLOT_SELECT + "WHERE slot.id = $1", slot_id)
    if (
        slot is None
        or slot["status"] != "open"
        or slot["starts_at"] <= datetime.now(timezone.utc)
        or slot["booked_count"] >= slot["capacity"]
    ):
        return 0
    members = await connection.fetch(
        """
        SELECT waitlist.telegram_id FROM lesson_waitlist AS waitlist
        WHERE waitlist.slot_id = $1
          AND NOT EXISTS (
              SELECT 1 FROM lesson_bookings AS booking
              WHERE booking.slot_id = waitlist.slot_id
                AND booking.telegram_id = waitlist.telegram_id
                AND booking.status = 'confirmed'
          )
        ORDER BY waitlist.created_at
        """,
        slot_id,
    )
    marker = "{}:{}".format(reason, uuid.uuid4())
    for member in members:
        await repository._enqueue_notification(
            connection,
            member["telegram_id"],
            "waitlist_seat",
            {
                "slot_id": slot_id,
                "class_key": slot["class_key"],
                "starts_at": slot["starts_at"].isoformat(),
                "remaining": slot["capacity"] - slot["booked_count"],
            },
            "waitlist_seat:{}:{}:{}".format(slot_id, member["telegram_id"], marker)[
                :200
            ],
            expires_at=slot["starts_at"],
        )
    return len(members)


class PostgresWaitlistMixin:
    _pool: Any

    async def join_waitlist(self, slot_id: int, telegram_id: int) -> WaitlistEntry:
        """Встать в очередь.

        ``SlotUnavailableError`` — занятие закрыто, началось, отменено или в
        нём есть места (тогда нужно просто записаться); ``AlreadyBookedError``
        — уже записан; ``InsufficientLessonCreditsError`` — нет занятий на
        балансе. Повторный вызов ничего не меняет.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                slot = await connection.fetchrow(
                    SLOT_SELECT + "WHERE slot.id = $1", slot_id
                )
                if (
                    slot is None
                    or slot["status"] != "open"
                    or slot["starts_at"] <= datetime.now(timezone.utc)
                ):
                    raise SlotUnavailableError("Запись на это занятие недоступна")
                if slot["booked_count"] < slot["capacity"]:
                    raise SlotUnavailableError(
                        "На занятии есть свободные места — можно записаться"
                    )
                if await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM lesson_bookings WHERE slot_id = $1 "
                    "AND telegram_id = $2 AND status = 'confirmed')",
                    slot_id,
                    telegram_id,
                ):
                    raise AlreadyBookedError("Вы уже записаны на это занятие")
                credits = await connection.fetchval(
                    "SELECT lesson_credits FROM bot_users WHERE telegram_id = $1",
                    telegram_id,
                )
                if credits is None:
                    raise LookupError("Профиль не найден")
                if credits <= 0:
                    raise InsufficientLessonCreditsError(
                        "В лист ожидания можно встать с занятием на балансе — "
                        "сначала купите абонемент"
                    )
                await connection.execute(
                    """
                    INSERT INTO lesson_waitlist (slot_id, telegram_id)
                    VALUES ($1, $2)
                    ON CONFLICT (slot_id, telegram_id) DO NOTHING
                    """,
                    slot_id,
                    telegram_id,
                )
                record = await connection.fetchrow(
                    """
                    SELECT created_at,
                           (SELECT COUNT(*) FROM lesson_waitlist
                            WHERE slot_id = $1)::INTEGER AS queue_size
                    FROM lesson_waitlist WHERE slot_id = $1 AND telegram_id = $2
                    """,
                    slot_id,
                    telegram_id,
                )
        return WaitlistEntry(
            slot=ClassSlot.from_record(slot),
            joined_at=record["created_at"],
            queue_size=record["queue_size"],
        )

    async def leave_waitlist(self, slot_id: int, telegram_id: int) -> bool:
        async with self._pool.acquire() as connection:
            result = await connection.execute(
                "DELETE FROM lesson_waitlist WHERE slot_id = $1 AND telegram_id = $2",
                slot_id,
                telegram_id,
            )
        return result == "DELETE 1"

    async def list_user_waitlist(self, telegram_id: int) -> Sequence[WaitlistEntry]:
        """Очереди участника на будущие неотменённые занятия."""
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                "SELECT slots.*, waitlist.created_at AS joined_at, "
                "(SELECT COUNT(*) FROM lesson_waitlist AS other "
                " WHERE other.slot_id = slots.id)::INTEGER AS queue_size "
                "FROM ("
                + SLOT_SELECT
                + """
                ) AS slots
                JOIN lesson_waitlist AS waitlist ON waitlist.slot_id = slots.id
                WHERE waitlist.telegram_id = $1
                  AND slots.starts_at > NOW()
                  AND slots.status <> 'cancelled'
                ORDER BY slots.starts_at
                """,
                telegram_id,
            )
        return [
            WaitlistEntry(
                slot=ClassSlot.from_record(record),
                joined_at=record["joined_at"],
                queue_size=record["queue_size"],
            )
            for record in records
        ]

    async def list_slot_waitlist(self, slot_id: int) -> Sequence[WaitlistMember]:
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT waitlist.telegram_id, profile.user_name, profile.phone,
                       waitlist.created_at AS joined_at
                FROM lesson_waitlist AS waitlist
                JOIN bot_users AS profile
                    ON profile.telegram_id = waitlist.telegram_id
                WHERE waitlist.slot_id = $1
                ORDER BY waitlist.created_at
                """,
                slot_id,
            )
        return [WaitlistMember(**dict(record)) for record in records]

    async def user_waitlisted_slot_ids(self, telegram_id: int) -> Sequence[int]:
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                "SELECT slot_id FROM lesson_waitlist WHERE telegram_id = $1",
                telegram_id,
            )
        return [record["slot_id"] for record in records]
