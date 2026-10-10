"""PostgreSQL: сотрудники студии (роли), преподаватель занятия, журнал действий.

Роль — единственный источник прав (миграция 016): ``admin`` или ``teacher``
по Telegram ID. В студии всегда остаётся хотя бы один администратор:
понизить или удалить последнего нельзя (проверка под блокировкой строк
администраторов, так что два параллельных запроса не обойдут её).

Действия, которые не оставляют следа в других журналах (сотрудники,
правка профиля, настройки, рассылки, заметки), пишутся в
``staff_audit_log`` той же транзакцией, что и само изменение.

Модуль импортируется из ``repository.py`` после объявления общих типов.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from .repository import (
    SLOT_SELECT,
    STAFF_ADMIN,
    STAFF_ROLES,
    ClassSlot,
    is_staff_admin,
)

logger = logging.getLogger("bot.database")

MAX_DISPLAY_NAME_LENGTH = 64


class LastAdminError(ValueError):
    """Нельзя понизить или удалить последнего администратора студии."""


@dataclass(frozen=True)
class StaffMember:
    telegram_id: int
    role: str
    display_name: Optional[str]
    user_name: str
    phone: Optional[str]
    has_web_account: bool
    created_at: datetime

    @property
    def public_name(self) -> str:
        return self.display_name or self.user_name

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "StaffMember":
        return cls(
            telegram_id=record["telegram_id"],
            role=record["role"],
            display_name=record["display_name"],
            user_name=record["user_name"],
            phone=record["phone"],
            has_web_account=record["has_web_account"],
            created_at=record["created_at"],
        )


@dataclass(frozen=True)
class StaffAuditEntry:
    id: int
    actor_telegram_id: int
    actor_name: Optional[str]
    action: str
    target_telegram_id: Optional[int]
    target_name: Optional[str]
    details: dict
    created_at: datetime


_STAFF_SELECT = """
    SELECT staff.telegram_id, staff.role, staff.display_name, staff.created_at,
           profile.user_name, profile.phone,
           EXISTS (
               SELECT 1 FROM users AS account
               WHERE account.telegram_id = staff.telegram_id
           ) AS has_web_account
    FROM staff_members AS staff
    JOIN bot_users AS profile ON profile.telegram_id = staff.telegram_id
"""


def normalize_display_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) > MAX_DISPLAY_NAME_LENGTH:
        raise ValueError(
            "Имя в расписании — не длиннее {} символов".format(MAX_DISPLAY_NAME_LENGTH)
        )
    return normalized


async def record_staff_action(
    connection: Any,
    actor_telegram_id: int,
    action: str,
    target_telegram_id: Optional[int] = None,
    details: Optional[Mapping[str, Any]] = None,
) -> None:
    """Запись в журнал действий — внутри транзакции самого действия."""
    await connection.execute(
        """
        INSERT INTO staff_audit_log (
            actor_telegram_id, action, target_telegram_id, details
        )
        VALUES ($1, $2, $3, $4::JSONB)
        """,
        actor_telegram_id,
        action,
        target_telegram_id,
        json.dumps(dict(details or {}), ensure_ascii=False, default=str),
    )


class PostgresStaffMixin:
    _pool: Any

    # ---------- сотрудники ----------

    async def list_staff(self) -> Sequence[StaffMember]:
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                _STAFF_SELECT + "ORDER BY staff.role, profile.user_name"
            )
        return [StaffMember.from_record(record) for record in records]

    async def get_staff_member(self, telegram_id: int) -> Optional[StaffMember]:
        async with self._pool.acquire() as connection:
            record = await connection.fetchrow(
                _STAFF_SELECT + "WHERE staff.telegram_id = $1", telegram_id
            )
        return StaffMember.from_record(record) if record is not None else None

    async def get_staff_role(self, telegram_id: int) -> Optional[str]:
        async with self._pool.acquire() as connection:
            return await connection.fetchval(
                "SELECT role FROM staff_members WHERE telegram_id = $1", telegram_id
            )

    @staticmethod
    async def _lock_admins_and_check_last(
        connection: Any, telegram_id: int, new_role: Optional[str]
    ) -> None:
        """Под блокировкой: после изменения останется хотя бы один админ."""
        admins = [
            record["telegram_id"]
            for record in await connection.fetch(
                "SELECT telegram_id FROM staff_members WHERE role = 'admin' "
                "ORDER BY telegram_id FOR UPDATE"
            )
        ]
        if admins == [telegram_id] and new_role != STAFF_ADMIN:
            raise LastAdminError(
                "Это последний администратор студии — сначала назначьте другого"
            )

    async def set_staff_role(
        self,
        telegram_id: int,
        role: str,
        actor_telegram_id: int,
        display_name: Optional[str] = None,
    ) -> StaffMember:
        """Назначает роль (или меняет её и имя в расписании).

        ``PermissionError`` — действующий не администратор; ``LookupError`` —
        у человека нет профиля бота (сначала /start в боте или вход на сайт
        через Telegram); ``LastAdminError`` — понижение последнего админа.
        """
        if role not in STAFF_ROLES:
            raise ValueError("Неизвестная роль")
        name = normalize_display_name(display_name)
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                if not await is_staff_admin(connection, actor_telegram_id):
                    raise PermissionError("Назначать сотрудников может только админ")
                if not await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM bot_users WHERE telegram_id = $1)",
                    telegram_id,
                ):
                    raise LookupError(
                        "Профиль с таким Telegram ID не найден: человек должен "
                        "сначала открыть бота (/start) или войти на сайт через "
                        "Telegram"
                    )
                await self._lock_admins_and_check_last(connection, telegram_id, role)
                previous = await connection.fetchrow(
                    "SELECT role, display_name FROM staff_members "
                    "WHERE telegram_id = $1",
                    telegram_id,
                )
                await connection.execute(
                    """
                    INSERT INTO staff_members (
                        telegram_id, role, display_name, granted_by
                    )
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (telegram_id) DO UPDATE
                        SET role = EXCLUDED.role,
                            display_name = EXCLUDED.display_name,
                            updated_at = NOW()
                    """,
                    telegram_id,
                    role,
                    name,
                    actor_telegram_id,
                )
                await record_staff_action(
                    connection,
                    actor_telegram_id,
                    "staff_role_set",
                    telegram_id,
                    {
                        "role": role,
                        "previous_role": previous["role"] if previous else None,
                        "display_name": name,
                    },
                )
                record = await connection.fetchrow(
                    _STAFF_SELECT + "WHERE staff.telegram_id = $1", telegram_id
                )
        logger.warning(
            "Staff role set telegram_id=%s role=%s admin_id=%s",
            telegram_id,
            role,
            actor_telegram_id,
        )
        return StaffMember.from_record(record)

    async def remove_staff(self, telegram_id: int, actor_telegram_id: int) -> bool:
        """Снимает роль; ``False`` — человек не был сотрудником.

        Занятия бывшего преподавателя остаются, но без преподавателя.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                if not await is_staff_admin(connection, actor_telegram_id):
                    raise PermissionError("Снимать сотрудников может только админ")
                await self._lock_admins_and_check_last(connection, telegram_id, None)
                role = await connection.fetchval(
                    "DELETE FROM staff_members WHERE telegram_id = $1 RETURNING role",
                    telegram_id,
                )
                if role is None:
                    return False
                await record_staff_action(
                    connection,
                    actor_telegram_id,
                    "staff_removed",
                    telegram_id,
                    {"previous_role": role},
                )
        logger.warning(
            "Staff removed telegram_id=%s admin_id=%s", telegram_id, actor_telegram_id
        )
        return True

    async def bootstrap_admin(self, telegram_id: int, user_name: str) -> StaffMember:
        """Серверная команда manage.py: первый (или аварийный) администратор.

        Не требует действующего администратора; профиль бота создаётся, если
        его ещё нет. Через API не вызывается.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO bot_users (telegram_id, user_name)
                    VALUES ($1, $2)
                    ON CONFLICT (telegram_id) DO NOTHING
                    """,
                    telegram_id,
                    user_name,
                )
                await connection.execute(
                    """
                    INSERT INTO staff_members (telegram_id, role)
                    VALUES ($1, 'admin')
                    ON CONFLICT (telegram_id) DO UPDATE
                        SET role = 'admin', updated_at = NOW()
                    """,
                    telegram_id,
                )
                record = await connection.fetchrow(
                    _STAFF_SELECT + "WHERE staff.telegram_id = $1", telegram_id
                )
        logger.warning("Admin bootstrapped from server telegram_id=%s", telegram_id)
        return StaffMember.from_record(record)

    async def revoke_staff_from_server(self, telegram_id: int) -> bool:
        """Серверная команда manage.py: снять роль (с той же защитой)."""
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._lock_admins_and_check_last(connection, telegram_id, None)
                role = await connection.fetchval(
                    "DELETE FROM staff_members WHERE telegram_id = $1 RETURNING role",
                    telegram_id,
                )
        return role is not None

    # ---------- преподаватель занятия ----------

    async def set_slot_teacher(
        self,
        slot_id: int,
        teacher_telegram_id: Optional[int],
        actor_telegram_id: int,
    ) -> Optional[ClassSlot]:
        """Назначает (или снимает, ``None``) преподавателя занятия.

        ``None`` — занятия нет; ``LookupError`` — не сотрудник.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                if not await is_staff_admin(connection, actor_telegram_id):
                    raise PermissionError("Назначать преподавателя может только админ")
                if teacher_telegram_id is not None and not await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM staff_members "
                    "WHERE telegram_id = $1)",
                    teacher_telegram_id,
                ):
                    raise LookupError("Преподаватель не найден среди сотрудников")
                previous = await connection.fetchrow(
                    "SELECT teacher_telegram_id FROM lesson_slots WHERE id = $1 "
                    "FOR UPDATE",
                    slot_id,
                )
                if previous is None:
                    return None
                await connection.execute(
                    "UPDATE lesson_slots SET teacher_telegram_id = $2, "
                    "updated_at = NOW() WHERE id = $1",
                    slot_id,
                    teacher_telegram_id,
                )
                await record_staff_action(
                    connection,
                    actor_telegram_id,
                    "slot_teacher_set",
                    teacher_telegram_id,
                    {
                        "slot_id": slot_id,
                        "previous_teacher": previous["teacher_telegram_id"],
                    },
                )
                record = await connection.fetchrow(
                    SLOT_SELECT + "WHERE slot.id = $1", slot_id
                )
        return ClassSlot.from_record(record)

    async def is_slot_teacher(self, slot_id: int, telegram_id: int) -> bool:
        async with self._pool.acquire() as connection:
            return bool(
                await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM lesson_slots "
                    "WHERE id = $1 AND teacher_telegram_id = $2)",
                    slot_id,
                    telegram_id,
                )
            )

    # ---------- журнал действий ----------

    async def record_staff_action(
        self,
        actor_telegram_id: int,
        action: str,
        target_telegram_id: Optional[int] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        async with self._pool.acquire() as connection:
            await record_staff_action(
                connection, actor_telegram_id, action, target_telegram_id, details
            )

    async def list_staff_audit(
        self,
        limit: int = 50,
        before_id: Optional[int] = None,
        target_telegram_id: Optional[int] = None,
    ) -> Sequence[StaffAuditEntry]:
        if not 1 <= limit <= 200:
            raise ValueError("Количество записей должно быть от 1 до 200")
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT entry.id, entry.actor_telegram_id, entry.action,
                       entry.target_telegram_id, entry.details, entry.created_at,
                       actor.user_name AS actor_name,
                       target.user_name AS target_name
                FROM staff_audit_log AS entry
                LEFT JOIN bot_users AS actor
                    ON actor.telegram_id = entry.actor_telegram_id
                LEFT JOIN bot_users AS target
                    ON target.telegram_id = entry.target_telegram_id
                WHERE ($2::BIGINT IS NULL OR entry.id < $2)
                  AND ($3::BIGINT IS NULL OR entry.target_telegram_id = $3)
                ORDER BY entry.id DESC
                LIMIT $1
                """,
                limit,
                before_id,
                target_telegram_id,
            )
        return [
            StaffAuditEntry(
                id=record["id"],
                actor_telegram_id=record["actor_telegram_id"],
                actor_name=record["actor_name"],
                action=record["action"],
                target_telegram_id=record["target_telegram_id"],
                target_name=record["target_name"],
                details=json.loads(record["details"])
                if isinstance(record["details"], str)
                else dict(record["details"]),
                created_at=record["created_at"],
            )
            for record in records
        ]
