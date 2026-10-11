"""PostgreSQL: техническое обслуживание сайта (миграция 021 DATABASE).

Действия администратора (создать, изменить, начать, завершить, отменить) и
автоматический старт меняют только состояние окна и пишут журнал.
Уведомления ставит отдельный идемпотентный шаг ``process_maintenance``:
API вызывает его сразу после действия, фоновая задача — каждые несколько
секунд. Каждый этап рассылки (предупреждение за N минут для редакции
времени, начало, перенос, отмена, завершение) ставится в outbox одной
транзакцией вместе со строкой ``site_maintenance_dispatches``, первичный
ключ которой не даёт поставить этап дважды — ни после перезапуска, ни при
параллельной работе бота и API. Поэтому сбой постановки уведомлений не
откатывает и не задерживает включение обслуживания, а повтор шага
досылает только недостающее.

Модуль импортируется из ``repository.py`` после объявления общих типов.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

import asyncpg

from ..maintenance import (
    KIND_CANCELLED,
    KIND_COMPLETED,
    KIND_RESCHEDULED,
    KIND_STARTED,
    KIND_WARNING,
    MAINTENANCE_KINDS,
    MAX_DESCRIPTION_LENGTH,
    MAX_TITLE_LENGTH,
    OPEN_STATUSES,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_SCHEDULED,
    AUDIENCES,
    MaintenanceConflictError,
    MaintenanceNotFoundError,
    MaintenanceWindow,
    normalize_text,
    normalize_warning_minutes,
    notification_payload,
    validate_period,
    warning_decision,
    warning_stage,
)
from .repository import is_staff_admin

logger = logging.getLogger("bot.database")

_COLUMNS = """
    id, status, title, description, starts_at, ends_at, warning_minutes,
    notify_users, audience, revision, started_at, finished_at, created_by,
    created_at, updated_at
"""

# Сколько после завершения или отмены фоновая задача ещё досылает
# последние уведомления окна (если постановка сразу после действия не
# удалась). Дальше окно — только история.
FINISHED_FOLLOW_UP = timedelta(days=1)


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


async def _require_admin(connection: Any, actor_telegram_id: int) -> None:
    if not await is_staff_admin(connection, actor_telegram_id):
        raise PermissionError("Действие доступно только администратору")


async def _event(
    connection: Any,
    maintenance_id: int,
    event: str,
    actor_telegram_id: Optional[int],
    details: Optional[Mapping[str, Any]] = None,
) -> None:
    await connection.execute(
        """
        INSERT INTO site_maintenance_events (
            maintenance_id, event, actor_telegram_id, details
        )
        VALUES ($1, $2, $3, $4::JSONB)
        """,
        maintenance_id,
        event,
        actor_telegram_id,
        json.dumps(dict(details or {}), ensure_ascii=False, default=str),
    )


async def _lock_window(connection: Any, maintenance_id: int) -> MaintenanceWindow:
    record = await connection.fetchrow(
        "SELECT " + _COLUMNS + " FROM site_maintenance WHERE id = $1 FOR UPDATE",
        maintenance_id,
    )
    if record is None:
        raise MaintenanceNotFoundError("Обслуживание не найдено")
    return MaintenanceWindow.from_record(record)


def _audience_query(audience: str) -> str:
    if audience == "staff":
        return (
            "SELECT staff.telegram_id FROM staff_members AS staff "
            "JOIN bot_users AS profile ON profile.telegram_id = staff.telegram_id"
        )
    return "SELECT telegram_id FROM bot_users"


# Кто уже получил (или прямо сейчас получает) уведомления окна указанных
# типов: перенос, отмена и завершение — только им.
_NOTIFIED_QUERY = """
    SELECT DISTINCT telegram_id FROM user_notifications
    WHERE payload ->> 'maintenance_id' = $1::TEXT
      AND kind = ANY($2::TEXT[])
      AND delivery_status IN ('sent', 'sending')
"""


class PostgresMaintenanceMixin:
    _pool: Any

    # ---------- чтение ----------

    async def get_open_maintenance(self) -> Optional[MaintenanceWindow]:
        """Текущее незавершённое окно (запланированное или активное)."""
        async with self._pool.acquire() as connection:
            record = await connection.fetchrow(
                "SELECT " + _COLUMNS + " FROM site_maintenance "
                "WHERE status = ANY($1::TEXT[])",
                list(OPEN_STATUSES),
            )
        return MaintenanceWindow.from_record(record) if record else None

    async def get_maintenance(self, maintenance_id: int) -> Optional[MaintenanceWindow]:
        async with self._pool.acquire() as connection:
            record = await connection.fetchrow(
                "SELECT " + _COLUMNS + " FROM site_maintenance WHERE id = $1",
                maintenance_id,
            )
        return MaintenanceWindow.from_record(record) if record else None

    async def list_maintenance(self, limit: int = 20) -> Sequence[MaintenanceWindow]:
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                "SELECT " + _COLUMNS + " FROM site_maintenance "
                "ORDER BY created_at DESC, id DESC LIMIT $1",
                limit,
            )
        return [MaintenanceWindow.from_record(record) for record in records]

    async def list_maintenance_events(self, maintenance_id: int) -> Sequence[dict]:
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT event.id, event.event, event.actor_telegram_id,
                       event.details, event.created_at,
                       COALESCE(staff.display_name, profile.user_name) AS actor_name
                FROM site_maintenance_events AS event
                LEFT JOIN staff_members AS staff
                    ON staff.telegram_id = event.actor_telegram_id
                LEFT JOIN bot_users AS profile
                    ON profile.telegram_id = event.actor_telegram_id
                WHERE event.maintenance_id = $1
                ORDER BY event.id
                """,
                maintenance_id,
            )
        return [
            {
                "id": record["id"],
                "event": record["event"],
                "actor_telegram_id": record["actor_telegram_id"],
                "actor_name": record["actor_name"],
                "details": json.loads(record["details"])
                if isinstance(record["details"], str)
                else dict(record["details"] or {}),
                "created_at": record["created_at"],
            }
            for record in records
        ]

    async def list_maintenance_dispatches(self, maintenance_id: int) -> Sequence[dict]:
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT revision, stage, recipients, skipped_reason, created_at
                FROM site_maintenance_dispatches
                WHERE maintenance_id = $1
                ORDER BY created_at, stage
                """,
                maintenance_id,
            )
        return [dict(record) for record in records]

    async def maintenance_delivery_stats(
        self, maintenance_id: int
    ) -> Dict[str, Dict[str, int]]:
        """Доставка уведомлений окна: тип → статус → количество."""
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT kind, delivery_status, COUNT(*) AS total
                FROM user_notifications
                WHERE payload ->> 'maintenance_id' = $1::TEXT
                  AND kind = ANY($2::TEXT[])
                GROUP BY kind, delivery_status
                """,
                str(maintenance_id),
                list(MAINTENANCE_KINDS),
            )
        stats: Dict[str, Dict[str, int]] = {}
        for record in records:
            stats.setdefault(record["kind"], {})[record["delivery_status"]] = record[
                "total"
            ]
        return stats

    async def count_maintenance_audience(self, audience: str) -> int:
        if audience not in AUDIENCES:
            raise ValueError("Неизвестная аудитория")
        async with self._pool.acquire() as connection:
            return await connection.fetchval(
                "SELECT COUNT(*) FROM ({}) AS recipients".format(
                    _audience_query(audience)
                )
            )

    # ---------- действия администратора ----------

    async def create_maintenance(
        self,
        actor_telegram_id: int,
        title: str,
        description: str,
        starts_at: Optional[datetime],
        ends_at: Optional[datetime],
        warning_minutes: Sequence[int],
        notify_users: bool = True,
        audience: str = "all",
        start_now: bool = False,
        now: Optional[datetime] = None,
    ) -> MaintenanceWindow:
        """Новое окно: запланированное или (``start_now``) сразу активное."""
        current = _now(now)
        title = normalize_text(title, MAX_TITLE_LENGTH, "Заголовок")
        description = normalize_text(description, MAX_DESCRIPTION_LENGTH, "Описание")
        minutes = normalize_warning_minutes(warning_minutes)
        if audience not in AUDIENCES:
            raise ValueError("Неизвестная аудитория уведомлений")
        if start_now:
            starts_at = current
        if starts_at is None:
            raise ValueError("Укажите время начала")
        validate_period(starts_at, ends_at, current, future_start=not start_now)
        status = STATUS_ACTIVE if start_now else STATUS_SCHEDULED
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await _require_admin(connection, actor_telegram_id)
                try:
                    record = await connection.fetchrow(
                        """
                        INSERT INTO site_maintenance (
                            status, title, description, starts_at, ends_at,
                            warning_minutes, notify_users, audience, started_at,
                            created_by, created_at, updated_at
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $11)
                        RETURNING """
                        + _COLUMNS,
                        status,
                        title,
                        description,
                        starts_at,
                        ends_at,
                        list(minutes),
                        notify_users,
                        audience,
                        starts_at if start_now else None,
                        actor_telegram_id,
                        current,
                    )
                except asyncpg.UniqueViolationError as error:
                    raise MaintenanceConflictError(
                        "Уже есть запланированное или идущее обслуживание — "
                        "измените его, завершите или отмените"
                    ) from error
                window = MaintenanceWindow.from_record(record)
                await _event(
                    connection,
                    window.id,
                    "created",
                    actor_telegram_id,
                    {
                        "starts_at": starts_at,
                        "ends_at": ends_at,
                        "warning_minutes": list(minutes),
                        "notify_users": notify_users,
                        "audience": audience,
                    },
                )
                if start_now:
                    await _event(
                        connection,
                        window.id,
                        "started",
                        actor_telegram_id,
                        {"manual": True},
                    )
        logger.info(
            "Maintenance created id=%s status=%s actor=%s",
            window.id,
            window.status,
            actor_telegram_id,
        )
        return window

    async def update_maintenance(
        self,
        maintenance_id: int,
        actor_telegram_id: int,
        changes: Mapping[str, Any],
        now: Optional[datetime] = None,
    ) -> MaintenanceWindow:
        """Изменение окна. ``changes`` — только переданные поля.

        Запланированное: всё; активное — заголовок, описание и окончание.
        Изменение начала или окончания — новая редакция времени
        (``revision``): старые предупреждения устаревают, получившим их
        уходит уведомление о переносе.
        """
        current = _now(now)
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await _require_admin(connection, actor_telegram_id)
                window = await _lock_window(connection, maintenance_id)
                state = window.effective_status(current)
                if state not in OPEN_STATUSES:
                    raise MaintenanceConflictError(
                        "Обслуживание уже завершено или отменено"
                    )
                values: Dict[str, Any] = {}
                if "title" in changes:
                    values["title"] = normalize_text(
                        changes["title"], MAX_TITLE_LENGTH, "Заголовок"
                    )
                if "description" in changes:
                    values["description"] = normalize_text(
                        changes["description"], MAX_DESCRIPTION_LENGTH, "Описание"
                    )
                if state == STATUS_ACTIVE and (
                    {"warning_minutes", "notify_users", "audience"} & set(changes)
                    or changes.get("starts_at", window.starts_at) != window.starts_at
                ):
                    raise MaintenanceConflictError(
                        "Обслуживание уже идёт: можно изменить только тексты и "
                        "время окончания"
                    )
                if "warning_minutes" in changes:
                    values["warning_minutes"] = list(
                        normalize_warning_minutes(changes["warning_minutes"])
                    )
                if "notify_users" in changes:
                    values["notify_users"] = bool(changes["notify_users"])
                if "audience" in changes:
                    if changes["audience"] not in AUDIENCES:
                        raise ValueError("Неизвестная аудитория уведомлений")
                    values["audience"] = changes["audience"]
                starts_at = window.starts_at
                ends_at = window.ends_at
                if state == STATUS_SCHEDULED and "starts_at" in changes:
                    starts_at = changes["starts_at"]
                if "ends_at" in changes:
                    ends_at = changes["ends_at"]
                time_changed = (
                    starts_at != window.starts_at or ends_at != window.ends_at
                )
                if time_changed:
                    validate_period(
                        starts_at,
                        ends_at,
                        current,
                        future_start=state == STATUS_SCHEDULED,
                    )
                    values.update(
                        starts_at=starts_at,
                        ends_at=ends_at,
                        revision=window.revision + 1,
                    )
                if not values:
                    return window
                if state == STATUS_ACTIVE and window.status == STATUS_SCHEDULED:
                    # Начало наступило, но фоновая задача ещё не отметила
                    # старт — фиксируем его здесь.
                    await self._mark_started(connection, window, None, current)
                assignments = ", ".join(
                    "{} = ${}".format(column, index + 2)
                    for index, column in enumerate(values)
                )
                record = await connection.fetchrow(
                    "UPDATE site_maintenance SET {}, updated_at = ${} "
                    "WHERE id = $1 RETURNING {}".format(
                        assignments, len(values) + 2, _COLUMNS
                    ),
                    maintenance_id,
                    *values.values(),
                    current,
                )
                updated = MaintenanceWindow.from_record(record)
                details = {
                    key: (list(value) if isinstance(value, tuple) else value)
                    for key, value in values.items()
                    if key != "revision"
                }
                if time_changed:
                    details.update(
                        old_starts_at=window.starts_at,
                        old_ends_at=window.ends_at,
                        revision=updated.revision,
                    )
                await _event(
                    connection,
                    maintenance_id,
                    "rescheduled" if time_changed else "updated",
                    actor_telegram_id,
                    details,
                )
        logger.info(
            "Maintenance updated id=%s rescheduled=%s actor=%s",
            maintenance_id,
            time_changed,
            actor_telegram_id,
        )
        return updated

    async def start_maintenance(
        self,
        maintenance_id: int,
        actor_telegram_id: int,
        now: Optional[datetime] = None,
    ) -> MaintenanceWindow:
        """Включить запланированное обслуживание сейчас (досрочно)."""
        current = _now(now)
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await _require_admin(connection, actor_telegram_id)
                window = await _lock_window(connection, maintenance_id)
                if window.status != STATUS_SCHEDULED:
                    raise MaintenanceConflictError(
                        "Включить можно только запланированное обслуживание"
                    )
                if window.ends_at is not None and window.ends_at <= current:
                    raise MaintenanceConflictError(
                        "Предполагаемое окончание уже прошло — измените его"
                    )
                window = await self._mark_started(
                    connection, window, actor_telegram_id, current
                )
        logger.info(
            "Maintenance started manually id=%s actor=%s",
            maintenance_id,
            actor_telegram_id,
        )
        return window

    async def complete_maintenance(
        self,
        maintenance_id: int,
        actor_telegram_id: int,
        now: Optional[datetime] = None,
    ) -> MaintenanceWindow:
        """Работы завершены: сайт возвращается к обычной работе."""
        current = _now(now)
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await _require_admin(connection, actor_telegram_id)
                window = await _lock_window(connection, maintenance_id)
                if window.effective_status(current) != STATUS_ACTIVE:
                    raise MaintenanceConflictError(
                        "Завершить можно только идущее обслуживание; "
                        "запланированное — отменить"
                    )
                if window.status == STATUS_SCHEDULED:
                    await self._mark_started(connection, window, None, current)
                record = await connection.fetchrow(
                    "UPDATE site_maintenance SET status = $2, finished_at = $3, "
                    "updated_at = $3 WHERE id = $1 RETURNING " + _COLUMNS,
                    maintenance_id,
                    STATUS_COMPLETED,
                    current,
                )
                await _event(connection, maintenance_id, "completed", actor_telegram_id)
        logger.info(
            "Maintenance completed id=%s actor=%s", maintenance_id, actor_telegram_id
        )
        return MaintenanceWindow.from_record(record)

    async def cancel_maintenance(
        self,
        maintenance_id: int,
        actor_telegram_id: int,
        now: Optional[datetime] = None,
    ) -> MaintenanceWindow:
        """Отмена запланированного (ещё не начавшегося) обслуживания."""
        current = _now(now)
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await _require_admin(connection, actor_telegram_id)
                window = await _lock_window(connection, maintenance_id)
                state = window.effective_status(current)
                if state == STATUS_ACTIVE:
                    raise MaintenanceConflictError(
                        "Обслуживание уже идёт — его можно только завершить"
                    )
                if state != STATUS_SCHEDULED:
                    raise MaintenanceConflictError(
                        "Обслуживание уже завершено или отменено"
                    )
                record = await connection.fetchrow(
                    "UPDATE site_maintenance SET status = $2, finished_at = $3, "
                    "updated_at = $3 WHERE id = $1 RETURNING " + _COLUMNS,
                    maintenance_id,
                    STATUS_CANCELLED,
                    current,
                )
                await _event(connection, maintenance_id, "cancelled", actor_telegram_id)
        logger.info(
            "Maintenance cancelled id=%s actor=%s", maintenance_id, actor_telegram_id
        )
        return MaintenanceWindow.from_record(record)

    @staticmethod
    async def _mark_started(
        connection: Any,
        window: MaintenanceWindow,
        actor_telegram_id: Optional[int],
        now: datetime,
    ) -> MaintenanceWindow:
        """scheduled → active внутри транзакции (строка уже заблокирована).

        Досрочное включение переносит начало на «сейчас»; автоматическое
        считает началом плановое время — с него сайт уже показывал
        страницу обслуживания.
        """
        starts_at = min(window.starts_at, now)
        record = await connection.fetchrow(
            "UPDATE site_maintenance SET status = $2, starts_at = $3, "
            "started_at = $3, updated_at = $4 WHERE id = $1 RETURNING " + _COLUMNS,
            window.id,
            STATUS_ACTIVE,
            starts_at,
            now,
        )
        await _event(
            connection,
            window.id,
            "started",
            actor_telegram_id,
            {"manual": actor_telegram_id is not None},
        )
        return MaintenanceWindow.from_record(record)

    # ---------- фоновая обработка и рассылка ----------

    async def process_maintenance(self, now: Optional[datetime] = None) -> dict:
        """Автоматический старт и постановка недостающих уведомлений.

        Идемпотентно: можно вызывать сколько угодно раз и из нескольких
        процессов. Возвращает сводку (для логов и тестов).
        """
        current = _now(now)
        summary: dict = {"started": [], "queued": {}, "skipped": []}
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                record = await connection.fetchrow(
                    "SELECT " + _COLUMNS + " FROM site_maintenance "
                    "WHERE status = $1 AND starts_at <= $2 FOR UPDATE SKIP LOCKED",
                    STATUS_SCHEDULED,
                    current,
                )
                if record is not None:
                    window = await self._mark_started(
                        connection, MaintenanceWindow.from_record(record), None, current
                    )
                    summary["started"].append(window.id)
                    logger.info("Maintenance started automatically id=%s", window.id)

        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                "SELECT " + _COLUMNS + " FROM site_maintenance "
                "WHERE notify_users AND (status = ANY($1::TEXT[]) "
                "OR finished_at > $2)",
                list(OPEN_STATUSES),
                current - FINISHED_FOLLOW_UP,
            )
            done = {
                (record["maintenance_id"], record["revision"], record["stage"])
                for record in await connection.fetch(
                    "SELECT maintenance_id, revision, stage "
                    "FROM site_maintenance_dispatches "
                    "WHERE maintenance_id = ANY($1::BIGINT[])",
                    [record["id"] for record in records],
                )
            }
        for window in (MaintenanceWindow.from_record(record) for record in records):
            for stage, revision, kind, recipients_kinds, extra in self._due_stages(
                window, current
            ):
                if (window.id, revision, stage.split("late:")[-1]) in done:
                    continue
                if stage.startswith("late:"):
                    if await self._skip_stage(window, stage[len("late:") :]):
                        summary["skipped"].append(stage[len("late:") :])
                    continue
                queued = await self._queue_stage(
                    window, revision, stage, kind, recipients_kinds, extra
                )
                if queued is not None:
                    summary["queued"][stage] = queued
        return summary

    @staticmethod
    def _due_stages(window: MaintenanceWindow, now: datetime) -> List[tuple]:
        """Этапы рассылки, которые сейчас положено поставить (или пропустить).

        Элемент — (этап, редакция, тип уведомления, типы «уже уведомлённых»
        или None для всей аудитории, дополнительные данные payload).
        """
        stages: List[tuple] = []
        if window.status == STATUS_SCHEDULED:
            if window.revision > 1:
                stages.append(
                    (
                        "rescheduled",
                        window.revision,
                        KIND_RESCHEDULED,
                        [KIND_WARNING, KIND_RESCHEDULED],
                        {"phase": "scheduled"},
                    )
                )
            for minutes in window.warning_minutes:
                decision = warning_decision(minutes, window.starts_at, now)
                if decision == "send":
                    stages.append(
                        (
                            warning_stage(minutes),
                            window.revision,
                            KIND_WARNING,
                            None,
                            {"lead_minutes": minutes},
                        )
                    )
                elif decision == "late":
                    stages.append(
                        (
                            "late:" + warning_stage(minutes),
                            window.revision,
                            None,
                            None,
                            {},
                        )
                    )
        elif window.status == STATUS_ACTIVE:
            stages.append(("started", 0, KIND_STARTED, None, {}))
            if window.started_at is not None and window.revision > 1:
                # Окончание изменили уже во время работ — тем, кто знает о
                # начале.
                stages.append(
                    (
                        "rescheduled",
                        window.revision,
                        KIND_RESCHEDULED,
                        [KIND_STARTED, KIND_RESCHEDULED],
                        {"phase": "active"},
                    )
                )
        elif window.status == STATUS_CANCELLED:
            # Об отмене — только тем, кого уже предупредили.
            stages.append(
                ("cancelled", 0, KIND_CANCELLED, [KIND_WARNING, KIND_RESCHEDULED], {})
            )
        elif window.status == STATUS_COMPLETED:
            # О завершении — тем, кто получил хоть одно уведомление окна.
            stages.append(
                (
                    "completed",
                    0,
                    KIND_COMPLETED,
                    [KIND_WARNING, KIND_STARTED, KIND_RESCHEDULED],
                    {},
                )
            )
        return stages

    async def _skip_stage(self, window: MaintenanceWindow, stage: str) -> bool:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                inserted = await connection.fetchval(
                    """
                    INSERT INTO site_maintenance_dispatches (
                        maintenance_id, revision, stage, skipped_reason
                    )
                    VALUES ($1, $2, $3, 'late')
                    ON CONFLICT DO NOTHING
                    RETURNING 1
                    """,
                    window.id,
                    window.revision,
                    stage,
                )
                if inserted:
                    await _event(
                        connection,
                        window.id,
                        "warning_skipped",
                        None,
                        {"stage": stage, "revision": window.revision, "reason": "late"},
                    )
        return bool(inserted)

    async def _queue_stage(
        self,
        window: MaintenanceWindow,
        revision: int,
        stage: str,
        kind: str,
        notified_kinds: Optional[Sequence[str]],
        extra: Mapping[str, Any],
    ) -> Optional[int]:
        """Ставит этап рассылки один раз; ``None`` — уже был поставлен."""
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                # Окно блокируется, чтобы этап не поставился по устаревшему
                # состоянию (например, предупреждение после отмены).
                locked = await _lock_window(connection, window.id)
                if (
                    locked.status != window.status
                    or locked.revision != window.revision
                    or not locked.notify_users
                ):
                    return None
                claimed = await connection.fetchval(
                    """
                    INSERT INTO site_maintenance_dispatches (
                        maintenance_id, revision, stage
                    )
                    VALUES ($1, $2, $3)
                    ON CONFLICT DO NOTHING
                    RETURNING 1
                    """,
                    window.id,
                    revision,
                    stage,
                )
                if not claimed:
                    return None
                if notified_kinds is None:
                    targets, args = _audience_query(locked.audience), []
                else:
                    targets = _NOTIFIED_QUERY
                    args = [str(window.id), list(notified_kinds)]
                    if stage == "rescheduled":
                        # Получившие уведомления прежних редакций.
                        targets += " AND (payload ->> 'revision')::INT < {}".format(
                            int(revision)
                        )
                payload = notification_payload(locked, title=locked.title, **extra)
                offset = len(args)
                recipients = await connection.fetchval(
                    """
                    WITH targets AS ({targets}),
                    queued AS (
                        INSERT INTO user_notifications (
                            telegram_id, kind, payload, dedupe_key, expires_at
                        )
                        SELECT targets.telegram_id, ${kind}, ${payload}::JSONB,
                               ${prefix} || targets.telegram_id, ${expires}
                        FROM targets
                        ON CONFLICT (dedupe_key) DO NOTHING
                        RETURNING 1
                    )
                    SELECT COUNT(*) FROM queued
                    """.format(
                        targets=targets,
                        kind=offset + 1,
                        payload=offset + 2,
                        prefix=offset + 3,
                        expires=offset + 4,
                    ),
                    *args,
                    kind,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    "maintenance:{}:{}:{}:".format(window.id, revision, stage),
                    # Предупреждение после начала работ уже не нужно.
                    locked.starts_at if kind == KIND_WARNING else None,
                )
                await connection.execute(
                    "UPDATE site_maintenance_dispatches SET recipients = $4 "
                    "WHERE maintenance_id = $1 AND revision = $2 AND stage = $3",
                    window.id,
                    revision,
                    stage,
                    recipients,
                )
                if recipients:
                    await _event(
                        connection,
                        window.id,
                        "notifications_queued",
                        None,
                        {
                            "stage": stage,
                            "revision": revision,
                            "recipients": recipients,
                        },
                    )
        logger.info(
            "Maintenance notifications queued id=%s stage=%s recipients=%s",
            window.id,
            stage,
            recipients,
        )
        return recipients

    async def is_maintenance_notification_current(
        self, kind: str, payload: Mapping[str, Any], now: Optional[datetime] = None
    ) -> bool:
        """Не устарело ли уведомление к моменту отправки.

        Предупреждение — только пока окно запланировано в той же редакции
        времени; «началось» — пока работы идут; перенос — пока редакция не
        сменилась. Отмена и завершение — всегда актуальны.
        """
        if kind in (KIND_CANCELLED, KIND_COMPLETED):
            return True
        try:
            maintenance_id = int(payload["maintenance_id"])
            revision = int(payload.get("revision") or 0)
        except (KeyError, TypeError, ValueError):
            return False
        window = await self.get_maintenance(maintenance_id)
        if window is None:
            return False
        state = window.effective_status(_now(now))
        if kind == KIND_WARNING:
            return state == STATUS_SCHEDULED and window.revision == revision
        if kind == KIND_STARTED:
            return state == STATUS_ACTIVE
        if kind == KIND_RESCHEDULED:
            return state in OPEN_STATUSES and window.revision == revision
        return False
