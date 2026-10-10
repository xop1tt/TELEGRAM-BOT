"""PostgreSQL: направления, абонементы и правила записи (миграция 017).

Меняет их только администратор (проверка роли в той же транзакции);
каждое изменение пишется в ``staff_audit_log``. Ключи неизменяемы, удаления
нет — направление или абонемент скрывается (``is_active = FALSE``).

Модуль импортируется из ``repository.py`` после объявления общих типов.
"""

import logging
from typing import Any, Optional, Sequence

from ..class_catalog import ClassFormat
from ..payments.catalog import LessonPackage
from ..studio_rules import StudioRules
from .repository import is_staff_admin
from .staff_postgres import record_staff_action

logger = logging.getLogger("bot.database")

_FORMAT_COLUMNS = "key, title, short_title, description, level, sort_order, is_active"
_RULE_COLUMNS = (
    "cancellation_deadline_hours, rebook_cooldown_hours, "
    "manual_credit_validity_days, expiry_warning_days"
)
_PACKAGE_COLUMNS = (
    "key, title, lessons, price_minor, validity_days, sort_order, is_active"
)


def _format(record: Any) -> ClassFormat:
    return ClassFormat(**dict(record))


def _package(record: Any) -> LessonPackage:
    data = dict(record)
    return LessonPackage(
        key=data["key"],
        title=data["title"],
        lessons=data["lessons"],
        price_rub=data["price_minor"] // 100,
        validity_days=data["validity_days"],
        sort_order=data["sort_order"],
        is_active=data["is_active"],
    )


async def _require_admin(connection: Any, actor_telegram_id: int) -> None:
    if not await is_staff_admin(connection, actor_telegram_id):
        raise PermissionError("Менять каталог и правила может только админ")


class PostgresCatalogMixin:
    _pool: Any

    # ---------- направления ----------

    async def list_class_formats(self) -> Sequence[ClassFormat]:
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                "SELECT {} FROM class_formats ORDER BY sort_order, key".format(
                    _FORMAT_COLUMNS
                )
            )
        return [_format(record) for record in records]

    async def save_class_format(
        self, item: ClassFormat, actor_telegram_id: int, create: bool
    ) -> ClassFormat:
        """Создаёт (``create``) или изменяет направление.

        ``ValueError`` — ключ занят (при создании) или поля не проходят
        ограничения; ``LookupError`` — изменяемого направления нет.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await _require_admin(connection, actor_telegram_id)
                values = (
                    item.key,
                    item.title.strip(),
                    item.short_title.strip(),
                    item.description.strip(),
                    item.level.strip(),
                    item.sort_order,
                    item.is_active,
                )
                if create:
                    record = await connection.fetchrow(
                        """
                        INSERT INTO class_formats ({})
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (key) DO NOTHING
                        RETURNING {}
                        """.format(_FORMAT_COLUMNS, _FORMAT_COLUMNS),
                        *values,
                    )
                    if record is None:
                        raise ValueError("Направление с таким ключом уже есть")
                else:
                    record = await connection.fetchrow(
                        """
                        UPDATE class_formats
                        SET title = $2, short_title = $3, description = $4,
                            level = $5, sort_order = $6, is_active = $7,
                            updated_at = NOW()
                        WHERE key = $1
                        RETURNING {}
                        """.format(_FORMAT_COLUMNS),
                        *values,
                    )
                    if record is None:
                        raise LookupError("Направление не найдено")
                await record_staff_action(
                    connection,
                    actor_telegram_id,
                    "class_format_created" if create else "class_format_updated",
                    None,
                    dict(record),
                )
        return _format(record)

    # ---------- абонементы ----------

    async def list_lesson_packages(self) -> Sequence[LessonPackage]:
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                "SELECT {} FROM lesson_packages ORDER BY sort_order, key".format(
                    _PACKAGE_COLUMNS
                )
            )
        return [_package(record) for record in records]

    async def save_lesson_package(
        self, package: LessonPackage, actor_telegram_id: int, create: bool
    ) -> LessonPackage:
        """Создаёт или изменяет абонемент (цена — в рублях, целое).

        Новые цена, состав и срок действуют для следующих покупок и выдач;
        уже купленные абонементы сохраняют свои условия.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await _require_admin(connection, actor_telegram_id)
                values = (
                    package.key,
                    package.title.strip(),
                    package.lessons,
                    package.price_rub * 100,
                    package.validity_days,
                    package.sort_order,
                    package.is_active,
                )
                if create:
                    record = await connection.fetchrow(
                        """
                        INSERT INTO lesson_packages ({})
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (key) DO NOTHING
                        RETURNING {}
                        """.format(_PACKAGE_COLUMNS, _PACKAGE_COLUMNS),
                        *values,
                    )
                    if record is None:
                        raise ValueError("Абонемент с таким ключом уже есть")
                else:
                    record = await connection.fetchrow(
                        """
                        UPDATE lesson_packages
                        SET title = $2, lessons = $3, price_minor = $4,
                            validity_days = $5, sort_order = $6, is_active = $7,
                            updated_at = NOW()
                        WHERE key = $1
                        RETURNING {}
                        """.format(_PACKAGE_COLUMNS),
                        *values,
                    )
                    if record is None:
                        raise LookupError("Абонемент не найден")
                await record_staff_action(
                    connection,
                    actor_telegram_id,
                    "lesson_package_created" if create else "lesson_package_updated",
                    None,
                    dict(record),
                )
        return _package(record)

    # ---------- правила записи ----------

    async def get_studio_rules(self) -> StudioRules:
        async with self._pool.acquire() as connection:
            record = await connection.fetchrow(
                "SELECT {} FROM studio_settings WHERE id".format(_RULE_COLUMNS)
            )
        return StudioRules(**dict(record)) if record is not None else StudioRules()

    async def update_studio_rules(
        self,
        actor_telegram_id: int,
        cancellation_deadline_hours: Optional[int] = None,
        rebook_cooldown_hours: Optional[int] = None,
        manual_credit_validity_days: Optional[int] = None,
        expiry_warning_days: Optional[int] = None,
    ) -> StudioRules:
        """Меняет правила; ``None`` — оставить как есть. Новые значения
        действуют сразу для всех записей и отмен; срок ручных начислений —
        для следующих начислений."""
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await _require_admin(connection, actor_telegram_id)
                previous = await connection.fetchrow(
                    "SELECT {} FROM studio_settings WHERE id FOR UPDATE".format(
                        _RULE_COLUMNS
                    )
                )
                record = await connection.fetchrow(
                    """
                    UPDATE studio_settings
                    SET cancellation_deadline_hours = COALESCE(
                            $1, cancellation_deadline_hours
                        ),
                        rebook_cooldown_hours = COALESCE($2, rebook_cooldown_hours),
                        manual_credit_validity_days = COALESCE(
                            $3, manual_credit_validity_days
                        ),
                        expiry_warning_days = COALESCE($4, expiry_warning_days),
                        updated_at = NOW(),
                        updated_by = $5
                    WHERE id
                    RETURNING {}
                    """.format(_RULE_COLUMNS),
                    cancellation_deadline_hours,
                    rebook_cooldown_hours,
                    manual_credit_validity_days,
                    expiry_warning_days,
                    actor_telegram_id,
                )
                await record_staff_action(
                    connection,
                    actor_telegram_id,
                    "studio_rules_updated",
                    None,
                    {"before": dict(previous), "after": dict(record)},
                )
        logger.warning(
            "Studio rules updated admin_id=%s values=%s",
            actor_telegram_id,
            dict(record),
        )
        return StudioRules(**dict(record))
