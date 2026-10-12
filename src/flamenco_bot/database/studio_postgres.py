"""PostgreSQL: отмена и перенос занятий, абонементы, история, уведомления.

Методы подмешиваются в ``PostgresRepository`` (см. ``repository.py``) и
следуют его правилам: баланс меняет только ``_change_credits`` внутри
транзакции, состояние слота меняется под ``SELECT ... FOR UPDATE`` на его
строке (как запись и отмена записи — значит, перенос/отмена студией и
запись участника выполняются строго по очереди), уведомления ставятся в
outbox ``user_notifications`` в той же транзакции, что и изменение.

Модуль импортируется из ``repository.py`` после объявления общих типов —
напрямую его не импортируют.
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence, Tuple

import asyncpg

from .repository import (
    SLOT_SELECT,
    ClassSlot,
    TelegramAlreadyLinkedError,
    is_staff_admin,
)
from .studio_models import (
    LOW_BALANCE_THRESHOLD,
    NOTIFICATION_KINDS,
    AuditEvent,
    CreditSource,
    LedgerEntry,
    NotificationSettings,
    PackageGrant,
    PackageGrantNotFoundError,
    PackageGrantResult,
    PackageRevokeResult,
    PackageSummary,
    SlotCancellation,
    SlotEvent,
    SlotNotFoundError,
    SlotParticipant,
    SlotRefund,
    SlotReschedule,
    SlotStateError,
    SupportThreadMessage,
    SupportTicketStateError,
    SupportTicketThread,
    TelegramConnectError,
    TelegramConnectRequest,
    UserNotification,
    UserPackage,
    effective_remaining,
    low_balance_dedupe_key,
    normalize_reason,
    pick_credit_source_index,
    source_order_key,
)


logger = logging.getLogger("bot.database")

# Подтверждённый в боте вход сайт должен забрать за это время.
CONNECT_CONSUME_WINDOW = timedelta(minutes=5)

_SLOT_SELECT = SLOT_SELECT

# Активные источники занятий участника (успешные платежи и действующие
# выдачи) с суммой ledger по каждому — в порядке списания (FIFO).
_ACTIVE_SOURCES_SQL = """
    SELECT kind, id, title, lessons, acquired_at, expires_at, remaining
    FROM (
        SELECT 'payment' AS kind, payment.id,
               payment.package_title AS title, payment.lessons,
               COALESCE(payment.completed_at, payment.created_at) AS acquired_at,
               payment.expires_at,
               SUM(ledger.delta)::INTEGER AS remaining
        FROM lesson_payments AS payment
        JOIN lesson_credit_ledger AS ledger ON ledger.payment_id = payment.id
        WHERE payment.telegram_id = $1 AND payment.status = 'succeeded'
        GROUP BY payment.id
        UNION ALL
        SELECT 'grant' AS kind, grant_row.id,
               grant_row.package_title AS title, grant_row.lessons,
               grant_row.created_at AS acquired_at,
               grant_row.expires_at,
               SUM(ledger.delta)::INTEGER AS remaining
        FROM lesson_package_grants AS grant_row
        JOIN lesson_credit_ledger AS ledger ON ledger.grant_id = grant_row.id
        WHERE grant_row.telegram_id = $1 AND grant_row.status = 'active'
        GROUP BY grant_row.id
    ) AS sources
    WHERE remaining > 0
    -- Порядок списания: сначала сгорающие раньше (миграция 018).
    ORDER BY expires_at NULLS LAST, acquired_at, kind, id
"""

_LEDGER_SELECT = """
    SELECT ledger.id, ledger.telegram_id, ledger.entry_type, ledger.delta,
           ledger.created_at, ledger.reason, ledger.actor_telegram_id,
           ledger.payment_id, ledger.grant_id, ledger.booking_id,
           COALESCE(payment.package_title, grant_row.package_title)
               AS package_title,
           slot.class_key, slot.starts_at, profile.user_name
    FROM lesson_credit_ledger AS ledger
    JOIN bot_users AS profile ON profile.telegram_id = ledger.telegram_id
    LEFT JOIN lesson_payments AS payment ON payment.id = ledger.payment_id
    LEFT JOIN lesson_package_grants AS grant_row
        ON grant_row.id = ledger.grant_id
    LEFT JOIN lesson_bookings AS booking ON booking.id = ledger.booking_id
    LEFT JOIN lesson_slots AS slot ON slot.id = booking.slot_id
"""

_NOTIFICATION_COLUMNS = """
    id, telegram_id, kind, payload, created_at, delivery_status, attempts,
    read_at, expires_at, sent_at
"""


def secret_hash(value: str) -> str:
    """SHA-256 (hex) одноразового токена — в БД хранится только он."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_new_start(starts_at: datetime) -> None:
    if starts_at.tzinfo is None or starts_at.utcoffset() is None:
        raise ValueError("Время занятия должно содержать часовой пояс")
    if starts_at <= datetime.now(timezone.utc):
        raise ValueError("Новое время занятия должно быть в будущем")


class PostgresStudioMixin:
    _pool: Any

    # ---------- общие шаги внутри транзакции ----------

    async def _pick_credit_source(
        self, connection: Any, telegram_id: int
    ) -> Tuple[Optional[CreditSource], bool]:
        """Абонемент для следующего списания и включено ли уведомление о
        малом остатке. Блокирует строку профиля до конца транзакции, чтобы
        параллельные записи одного участника выбирали источник по очереди.

        ``FOR NO KEY UPDATE``, а не ``FOR UPDATE``: вставка брони и строки
        ledger уже держит на профиле ``KEY SHARE`` (проверка внешнего
        ключа), а ``FOR UPDATE`` с ним конфликтует — две параллельные записи
        одного участника взаимоблокировались бы. Этот режим тот же, что у
        ``UPDATE`` баланса, и между собой транзакции он сериализует.
        """
        profile = await connection.fetchrow(
            """
            SELECT lesson_credits, notify_low_balance FROM bot_users
            WHERE telegram_id = $1 FOR NO KEY UPDATE
            """,
            telegram_id,
        )
        if profile is None:
            raise LookupError(
                "Профиль не найден для Telegram ID {}".format(telegram_id)
            )
        sources = await connection.fetch(_ACTIVE_SOURCES_SQL, telegram_id)
        raw = [row["remaining"] for row in sources]
        index = pick_credit_source_index(profile["lesson_credits"], raw)
        if index is None:
            return None, profile["notify_low_balance"]
        effective, _ = effective_remaining(profile["lesson_credits"], raw)
        row = sources[index]
        return (
            CreditSource(
                kind=row["kind"],
                id=row["id"],
                title=row["title"],
                lessons=row["lessons"],
                remaining=effective[index],
                expires_at=row["expires_at"],
            ),
            profile["notify_low_balance"],
        )

    async def _expire_lots(
        self, connection: Any, telegram_id: int, now: Optional[datetime] = None
    ) -> int:
        """Сжигает остаток истёкших партий участника; возвращает число занятий.

        Внутри транзакции вызывающего; блокирует строку профиля (как и
        списание), так что запись и сгорание идут по очереди. Остаток
        партии — тот же «эффективный», по которому идёт списание
        (``effective_remaining``), поэтому сумма ledger по-прежнему равна
        балансу. Каждая партия сгорает одной строкой ``expiration``;
        обработанная партия помечается ``expiry_processed_at``.
        """
        current = now or datetime.now(timezone.utc)
        balance = await connection.fetchval(
            "SELECT lesson_credits FROM bot_users WHERE telegram_id = $1 "
            "FOR NO KEY UPDATE",
            telegram_id,
        )
        if balance is None:
            return 0
        sources = await connection.fetch(_ACTIVE_SOURCES_SQL, telegram_id)
        effective, _ = effective_remaining(
            balance, [row["remaining"] for row in sources]
        )
        burned = 0
        for row, remaining in zip(sources, effective):
            if row["expires_at"] is None or row["expires_at"] > current:
                continue
            if remaining <= 0:
                continue
            changed = await self._change_credits(
                connection,
                telegram_id,
                -remaining,
                "expiration",
                "expire:{}:{}:{}".format(row["kind"], row["id"], uuid.uuid4()),
                payment_id=row["id"] if row["kind"] == "payment" else None,
                grant_id=row["id"] if row["kind"] == "grant" else None,
                reason="Истёк срок действия: {}".format(row["title"])[:300],
            )
            burned += remaining
            await self._enqueue_notification(
                connection,
                telegram_id,
                "credits_expired",
                {
                    "title": row["title"],
                    "lessons": remaining,
                    "expired_at": row["expires_at"].isoformat(),
                    "balance": changed[1] if changed else balance - burned,
                },
                "credits_expired:{}:{}:{}".format(
                    row["kind"], row["id"], changed[0] if changed else uuid.uuid4()
                ),
            )
        await connection.execute(
            """
            UPDATE lesson_payments SET expiry_processed_at = $2
            WHERE telegram_id = $1 AND expires_at <= $2
              AND expiry_processed_at IS NULL
            """,
            telegram_id,
            current,
        )
        await connection.execute(
            """
            UPDATE lesson_package_grants SET expiry_processed_at = $2
            WHERE telegram_id = $1 AND expires_at <= $2
              AND expiry_processed_at IS NULL
            """,
            telegram_id,
            current,
        )
        if burned:
            logger.warning(
                "Expired lesson credits burned telegram_id=%s lessons=%s",
                telegram_id,
                burned,
            )
        return burned

    @staticmethod
    async def _source_expired(
        connection: Any,
        payment_id: Optional[int],
        grant_id: Optional[int],
        now: Optional[datetime] = None,
    ) -> bool:
        """Истёк ли срок партии, с которой было списано занятие."""
        if payment_id is None and grant_id is None:
            return False
        expires_at = await connection.fetchval(
            "SELECT expires_at FROM lesson_payments WHERE id = $1"
            if payment_id is not None
            else "SELECT expires_at FROM lesson_package_grants WHERE id = $1",
            payment_id if payment_id is not None else grant_id,
        )
        return expires_at is not None and expires_at <= (
            now or datetime.now(timezone.utc)
        )

    @staticmethod
    async def _enqueue_notification(
        connection: Any,
        telegram_id: int,
        kind: str,
        payload: dict,
        dedupe_key: str,
        expires_at: Optional[datetime] = None,
    ) -> bool:
        """Ставит уведомление в outbox; ``False`` — такое уже стоит (dedupe)."""
        if kind not in NOTIFICATION_KINDS:
            raise ValueError("Неизвестный тип уведомления")
        notification_id = await connection.fetchval(
            """
            INSERT INTO user_notifications (
                telegram_id, kind, payload, dedupe_key, expires_at
            )
            VALUES ($1, $2, $3::JSONB, $4, $5)
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING id
            """,
            telegram_id,
            kind,
            json.dumps(payload, ensure_ascii=False, default=str),
            dedupe_key,
            expires_at,
        )
        return notification_id is not None

    async def _enqueue_low_balance(
        self, connection: Any, telegram_id: int, balance: int
    ) -> None:
        if balance > LOW_BALANCE_THRESHOLD:
            return
        cycle_marker = await connection.fetchval(
            """
            SELECT COALESCE(MAX(id), 0) FROM lesson_credit_ledger
            WHERE telegram_id = $1 AND delta > 0
              AND entry_type IN (
                  'purchase', 'package_grant', 'admin_adjustment', 'refund_release'
              )
            """,
            telegram_id,
        )
        await self._enqueue_notification(
            connection,
            telegram_id,
            "low_balance",
            {"balance": balance},
            low_balance_dedupe_key(telegram_id, cycle_marker),
        )

    @staticmethod
    async def _require_admin(connection: Any, actor_telegram_id: int) -> None:
        if not await is_staff_admin(connection, actor_telegram_id):
            raise PermissionError("Действие доступно только администратору")

    @staticmethod
    async def _slot_in_transaction(connection: Any, slot_id: int) -> ClassSlot:
        record = await connection.fetchrow(_SLOT_SELECT + "WHERE slot.id = $1", slot_id)
        return ClassSlot.from_record(record)

    # ---------- слоты ----------

    async def get_class_slot(self, slot_id: int) -> Optional[ClassSlot]:
        async with self._pool.acquire() as connection:
            record = await connection.fetchrow(
                _SLOT_SELECT + "WHERE slot.id = $1", slot_id
            )
        return ClassSlot.from_record(record) if record is not None else None

    async def list_slot_participants(
        self,
        slot_id: int,
        include_cancelled: bool = False,
    ) -> Sequence[SlotParticipant]:
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT booking.id AS booking_id, booking.telegram_id,
                       profile.user_name, profile.phone, booking.booked_at,
                       booking.status
                FROM lesson_bookings AS booking
                JOIN bot_users AS profile
                    ON profile.telegram_id = booking.telegram_id
                WHERE booking.slot_id = $1
                  AND ($2::BOOLEAN OR booking.status = 'confirmed')
                ORDER BY booking.status DESC, booking.booked_at, booking.id
                """,
                slot_id,
                include_cancelled,
            )
        return [
            SlotParticipant(
                booking_id=record["booking_id"],
                telegram_id=record["telegram_id"],
                user_name=record["user_name"],
                phone=record["phone"],
                booked_at=record["booked_at"],
                status=record["status"],
            )
            for record in records
        ]

    async def list_slot_events(
        self,
        slot_id: Optional[int] = None,
        limit: int = 30,
    ) -> Sequence[SlotEvent]:
        if not 1 <= limit <= 100:
            raise ValueError("Количество событий должно быть от 1 до 100")
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT event.id, event.slot_id, event.event_type,
                       event.actor_telegram_id, event.old_starts_at,
                       event.new_starts_at, event.old_capacity,
                       event.new_capacity, event.reason, event.created_at,
                       slot.class_key
                FROM lesson_slot_events AS event
                JOIN lesson_slots AS slot ON slot.id = event.slot_id
                WHERE $1::BIGINT IS NULL OR event.slot_id = $1
                ORDER BY event.id DESC
                LIMIT $2
                """,
                slot_id,
                limit,
            )
        return [SlotEvent.from_record(record) for record in records]

    async def reschedule_class_slot(
        self,
        slot_id: int,
        new_starts_at: datetime,
        admin_telegram_id: int,
        reason: Optional[str] = None,
    ) -> SlotReschedule:
        """Переносит существующий слот на новое время.

        Брони сохраняются, баланс не меняется; факт переноса пишется в
        журнал, каждому записанному ставится уведомление со старым и новым
        временем. ``SlotNotFoundError`` / ``SlotStateError`` (отменён или уже
        начался) / ``ValueError`` (время в прошлом, без пояса, то же самое).
        """
        _validate_new_start(new_starts_at)
        normalized_reason = normalize_reason(reason, required=False)
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                slot = await connection.fetchrow(
                    """
                    SELECT id, class_key, starts_at, status FROM lesson_slots
                    WHERE id = $1 FOR UPDATE
                    """,
                    slot_id,
                )
                if slot is None:
                    raise SlotNotFoundError("Занятие не найдено")
                if slot["status"] == "cancelled":
                    raise SlotStateError("Занятие отменено — перенести его нельзя")
                if slot["starts_at"] <= datetime.now(timezone.utc):
                    raise SlotStateError("Занятие уже началось — перенести его нельзя")
                if slot["starts_at"] == new_starts_at:
                    raise ValueError("Новое время совпадает с текущим")
                await connection.execute(
                    """
                    UPDATE lesson_slots
                    SET starts_at = $2, rescheduled_at = NOW(), updated_at = NOW()
                    WHERE id = $1
                    """,
                    slot_id,
                    new_starts_at,
                )
                event_id = await connection.fetchval(
                    """
                    INSERT INTO lesson_slot_events (
                        slot_id, event_type, actor_telegram_id, old_starts_at,
                        new_starts_at, reason
                    )
                    VALUES ($1, 'rescheduled', $2, $3, $4, $5)
                    RETURNING id
                    """,
                    slot_id,
                    admin_telegram_id,
                    slot["starts_at"],
                    new_starts_at,
                    normalized_reason,
                )
                participants = await connection.fetch(
                    """
                    SELECT id, telegram_id FROM lesson_bookings
                    WHERE slot_id = $1 AND status = 'confirmed'
                    ORDER BY telegram_id
                    """,
                    slot_id,
                )
                for participant in participants:
                    await self._enqueue_notification(
                        connection,
                        participant["telegram_id"],
                        "slot_rescheduled",
                        {
                            "slot_id": slot_id,
                            "class_key": slot["class_key"],
                            "old_starts_at": slot["starts_at"].isoformat(),
                            "new_starts_at": new_starts_at.isoformat(),
                            "reason": normalized_reason,
                        },
                        "slot_rescheduled:{}:{}".format(event_id, participant["id"]),
                    )
                updated = await self._slot_in_transaction(connection, slot_id)
        logger.warning(
            "Class slot rescheduled slot_id=%s admin_id=%s participants=%s",
            slot_id,
            admin_telegram_id,
            len(participants),
        )
        return SlotReschedule(
            slot=updated,
            old_starts_at=slot["starts_at"],
            new_starts_at=new_starts_at,
            participants=tuple(row["telegram_id"] for row in participants),
        )

    async def cancel_class_slot(
        self,
        slot_id: int,
        admin_telegram_id: int,
        reason: Optional[str] = None,
    ) -> SlotCancellation:
        """Отмена занятия студией: терминальный статус и возврат занятий.

        Каждому подтверждённому участнику возвращается +1 занятие (в тот
        же абонемент, с которого было списано) с ledger-записью
        ``slot_cancellation`` и детерминированным ключом
        ``slot_cancellation:<слот>:<бронь>`` — повторная обработка не начислит
        занятие второй раз. Повторный вызов для отменённого слота ничего не
        меняет (``already_cancelled=True``). После начала занятия —
        ``SlotStateError``.
        """
        normalized_reason = normalize_reason(reason, required=False)
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                slot = await connection.fetchrow(
                    """
                    SELECT id, class_key, starts_at, status FROM lesson_slots
                    WHERE id = $1 FOR UPDATE
                    """,
                    slot_id,
                )
                if slot is None:
                    raise SlotNotFoundError("Занятие не найдено")
                if slot["status"] == "cancelled":
                    return SlotCancellation(
                        slot=await self._slot_in_transaction(connection, slot_id),
                        already_cancelled=True,
                    )
                if slot["starts_at"] <= datetime.now(timezone.utc):
                    raise SlotStateError("Занятие уже началось — отменить его нельзя")
                await connection.execute(
                    """
                    UPDATE lesson_slots
                    SET status = 'cancelled', cancelled_at = NOW(),
                        cancelled_by = $2, cancel_reason = $3, updated_at = NOW()
                    WHERE id = $1
                    """,
                    slot_id,
                    admin_telegram_id,
                    normalized_reason,
                )
                await connection.execute(
                    """
                    INSERT INTO lesson_slot_events (
                        slot_id, event_type, actor_telegram_id, reason
                    )
                    VALUES ($1, 'cancelled', $2, $3)
                    """,
                    slot_id,
                    admin_telegram_id,
                    normalized_reason,
                )
                bookings = await connection.fetch(
                    """
                    UPDATE lesson_bookings
                    SET status = 'cancelled', updated_at = NOW()
                    WHERE slot_id = $1 AND status = 'confirmed'
                    RETURNING id, telegram_id
                    """,
                    slot_id,
                )
                # Занятия больше нет — очередь на него тоже.
                await connection.execute(
                    "DELETE FROM lesson_waitlist WHERE slot_id = $1", slot_id
                )
                refunds = []
                # Строки профилей блокируются в порядке telegram_id: две
                # одновременные отмены с общими участниками не взаимоблокируются.
                for booking in sorted(bookings, key=lambda row: row["telegram_id"]):
                    source = await connection.fetchrow(
                        """
                        SELECT payment_id, grant_id FROM lesson_credit_ledger
                        WHERE booking_id = $1 AND entry_type = 'lesson_use'
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        booking["id"],
                    )
                    payment_id = source["payment_id"] if source else None
                    grant_id = source["grant_id"] if source else None
                    # Студия отменила занятие, а срок абонемента уже вышел:
                    # возвращённое занятие не сгорает — оно становится
                    # бессрочным (без партии), так решил владелец студии.
                    if await self._source_expired(connection, payment_id, grant_id):
                        payment_id = grant_id = None
                    changed = await self._change_credits(
                        connection,
                        booking["telegram_id"],
                        1,
                        "slot_cancellation",
                        "slot_cancellation:{}:{}".format(slot_id, booking["id"]),
                        payment_id=payment_id,
                        grant_id=grant_id,
                        booking_id=booking["id"],
                        actor_telegram_id=admin_telegram_id,
                        reason=normalized_reason,
                    )
                    if changed is not None:
                        balance = changed[1]
                    else:
                        balance = await connection.fetchval(
                            """
                            SELECT lesson_credits FROM bot_users
                            WHERE telegram_id = $1
                            """,
                            booking["telegram_id"],
                        )
                    await self._enqueue_notification(
                        connection,
                        booking["telegram_id"],
                        "slot_cancelled",
                        {
                            "slot_id": slot_id,
                            "class_key": slot["class_key"],
                            "starts_at": slot["starts_at"].isoformat(),
                            "reason": normalized_reason,
                            "refunded": True,
                            "balance": balance,
                        },
                        "slot_cancelled:{}:{}".format(slot_id, booking["id"]),
                    )
                    refunds.append(
                        SlotRefund(
                            booking_id=booking["id"],
                            telegram_id=booking["telegram_id"],
                            balance=balance,
                        )
                    )
                updated = await self._slot_in_transaction(connection, slot_id)
        logger.warning(
            "Class slot cancelled by studio slot_id=%s admin_id=%s refunds=%s",
            slot_id,
            admin_telegram_id,
            len(refunds),
        )
        return SlotCancellation(
            slot=updated, already_cancelled=False, refunds=tuple(refunds)
        )

    async def reopen_class_slot(
        self,
        slot_id: int,
        admin_telegram_id: Optional[int] = None,
    ) -> bool:
        """Снова открывает закрытый будущий слот для записи."""
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                reopened = await connection.fetchval(
                    """
                    UPDATE lesson_slots SET status = 'open', updated_at = NOW()
                    WHERE id = $1 AND status = 'closed' AND starts_at > NOW()
                    RETURNING id
                    """,
                    slot_id,
                )
                if reopened is not None:
                    await connection.execute(
                        """
                        INSERT INTO lesson_slot_events (
                            slot_id, event_type, actor_telegram_id
                        )
                        VALUES ($1, 'reopened', $2)
                        """,
                        slot_id,
                        admin_telegram_id,
                    )
        return reopened is not None

    # ---------- абонементы ----------

    async def get_package_summary(self, telegram_id: int) -> PackageSummary:
        async with self._pool.acquire() as connection:
            # Баланс и суммы ledger — из одного снимка: параллельная запись
            # не должна выглядеть расхождением.
            async with connection.transaction(
                isolation="repeatable_read", readonly=True
            ):
                balance = await connection.fetchval(
                    "SELECT lesson_credits FROM bot_users WHERE telegram_id = $1",
                    telegram_id,
                )
                if balance is None:
                    raise LookupError(
                        "Профиль не найден для Telegram ID {}".format(telegram_id)
                    )
                records = await connection.fetch(
                    """
                    SELECT 'payment' AS kind, payment.id, payment.package_key,
                           payment.package_title AS title, payment.lessons,
                           payment.status AS source_status,
                           COALESCE(payment.completed_at, payment.created_at)
                               AS acquired_at,
                           payment.amount_minor, NULL::TEXT AS reason,
                           payment.expires_at,
                           COALESCE((
                               SELECT SUM(ledger.delta) FROM lesson_credit_ledger
                                   AS ledger
                               WHERE ledger.payment_id = payment.id
                           ), 0)::INTEGER AS raw_remaining
                    FROM lesson_payments AS payment
                    WHERE payment.telegram_id = $1
                      AND payment.status IN (
                          'succeeded', 'refund_pending', 'refunded'
                      )
                    UNION ALL
                    SELECT 'grant', grant_row.id, grant_row.package_key,
                           grant_row.package_title, grant_row.lessons,
                           grant_row.status, grant_row.created_at, NULL::BIGINT,
                           grant_row.reason, grant_row.expires_at,
                           COALESCE((
                               SELECT SUM(ledger.delta) FROM lesson_credit_ledger
                                   AS ledger
                               WHERE ledger.grant_id = grant_row.id
                           ), 0)::INTEGER
                    FROM lesson_package_grants AS grant_row
                    WHERE grant_row.telegram_id = $1
                    ORDER BY acquired_at, kind, id
                    """,
                    telegram_id,
                )
        return build_package_summary(balance, records)

    async def get_package_grant(self, grant_id: int) -> Optional[PackageGrant]:
        async with self._pool.acquire() as connection:
            record = await connection.fetchrow(
                "SELECT * FROM lesson_package_grants WHERE id = $1", grant_id
            )
        return PackageGrant.from_record(record) if record is not None else None

    async def grant_lesson_package(
        self,
        telegram_id: int,
        package_key: str,
        title: str,
        lessons: int,
        reason: str,
        admin_telegram_id: int,
        idempotence_key: uuid.UUID,
        validity_days: Optional[int] = None,
    ) -> PackageGrantResult:
        """Выдача абонемента администратором без оплаты через ЮKassa.

        ``validity_days`` — срок действия с момента выдачи (из каталога);
        ``None`` — бессрочно.

        Занятия начисляются через ``_change_credits`` (тип ``package_grant``,
        актор и причина обязательны и на уровне БД). Повтор с тем же ключом
        ничего не начисляет и возвращает прежнюю выдачу (``applied=False``).
        """
        normalized_reason = normalize_reason(reason, required=True)
        normalized_title = title.strip()
        if not 1 <= len(normalized_title) <= 100:
            raise ValueError(
                "Название абонемента должно содержать от 1 до 100 символов"
            )
        if not 1 <= lessons <= 100:
            raise ValueError("Количество занятий должно быть от 1 до 100")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._require_admin(connection, admin_telegram_id)
                try:
                    record = await connection.fetchrow(
                        """
                        INSERT INTO lesson_package_grants (
                            telegram_id, package_key, package_title, lessons,
                            granted_by, reason, idempotence_key, expires_at
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6, $7,
                            NOW() + make_interval(days => $8::INTEGER)
                        )
                        ON CONFLICT (idempotence_key) DO NOTHING
                        RETURNING *
                        """,
                        telegram_id,
                        package_key,
                        normalized_title,
                        lessons,
                        admin_telegram_id,
                        normalized_reason,
                        idempotence_key,
                        validity_days,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    raise LookupError(
                        "Профиль не найден для Telegram ID {}".format(telegram_id)
                    ) from error
                if record is None:
                    existing = await connection.fetchrow(
                        """
                        SELECT * FROM lesson_package_grants
                        WHERE idempotence_key = $1
                        """,
                        idempotence_key,
                    )
                    if (
                        existing["telegram_id"] != telegram_id
                        or existing["lessons"] != lessons
                    ):
                        raise ValueError(
                            "Ключ идемпотентности уже использован для другой выдачи"
                        )
                    balance = await connection.fetchval(
                        "SELECT lesson_credits FROM bot_users WHERE telegram_id = $1",
                        telegram_id,
                    )
                    return PackageGrantResult(
                        grant=PackageGrant.from_record(existing),
                        ledger_id=None,
                        balance=balance,
                        applied=False,
                    )
                ledger_id, balance = await self._change_credits(
                    connection,
                    telegram_id,
                    lessons,
                    "package_grant",
                    "package-grant:{}".format(record["id"]),
                    grant_id=record["id"],
                    actor_telegram_id=admin_telegram_id,
                    reason=normalized_reason,
                )
                await self._enqueue_notification(
                    connection,
                    telegram_id,
                    "package_granted",
                    {
                        "grant_id": record["id"],
                        "title": normalized_title,
                        "lessons": lessons,
                        "balance": balance,
                    },
                    "package_granted:{}".format(record["id"]),
                )
        logger.warning(
            "Lesson package granted grant_id=%s telegram_id=%s lessons=%s admin_id=%s",
            record["id"],
            telegram_id,
            lessons,
            admin_telegram_id,
        )
        return PackageGrantResult(
            grant=PackageGrant.from_record(record),
            ledger_id=ledger_id,
            balance=balance,
            applied=True,
        )

    async def revoke_lesson_package_grant(
        self,
        grant_id: int,
        reason: str,
        admin_telegram_id: int,
    ) -> PackageRevokeResult:
        """Отзыв выданного абонемента: списываются только неиспользованные
        занятия. Использованные занятия и записи не трогаются; повторный
        вызов ничего не меняет (``applied=False``).
        """
        normalized_reason = normalize_reason(reason, required=True)
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._require_admin(connection, admin_telegram_id)
                record = await connection.fetchrow(
                    "SELECT * FROM lesson_package_grants WHERE id = $1 FOR UPDATE",
                    grant_id,
                )
                if record is None:
                    raise PackageGrantNotFoundError("Выданный абонемент не найден")
                telegram_id = record["telegram_id"]
                balance = await connection.fetchval(
                    """
                    SELECT lesson_credits FROM bot_users
                    WHERE telegram_id = $1 FOR NO KEY UPDATE
                    """,
                    telegram_id,
                )
                if record["status"] == "revoked":
                    return PackageRevokeResult(
                        grant=PackageGrant.from_record(record),
                        revoked_lessons=0,
                        balance=balance,
                        applied=False,
                    )
                sources = await connection.fetch(_ACTIVE_SOURCES_SQL, telegram_id)
                effective, _ = effective_remaining(
                    balance, [row["remaining"] for row in sources]
                )
                revoked_lessons = next(
                    (
                        effective[index]
                        for index, row in enumerate(sources)
                        if row["kind"] == "grant" and row["id"] == grant_id
                    ),
                    0,
                )
                revoked = await connection.fetchrow(
                    """
                    UPDATE lesson_package_grants
                    SET status = 'revoked', revoked_at = NOW(), revoked_by = $2,
                        revoke_reason = $3
                    WHERE id = $1
                    RETURNING *
                    """,
                    grant_id,
                    admin_telegram_id,
                    normalized_reason,
                )
                if revoked_lessons > 0:
                    _, balance = await self._change_credits(
                        connection,
                        telegram_id,
                        -revoked_lessons,
                        "package_revoke",
                        "package-revoke:{}".format(grant_id),
                        grant_id=grant_id,
                        actor_telegram_id=admin_telegram_id,
                        reason=normalized_reason,
                    )
                await self._enqueue_notification(
                    connection,
                    telegram_id,
                    "package_revoked",
                    {
                        "grant_id": grant_id,
                        "title": record["package_title"],
                        "revoked_lessons": revoked_lessons,
                        "balance": balance,
                        "reason": normalized_reason,
                    },
                    "package_revoked:{}".format(grant_id),
                )
        logger.warning(
            "Lesson package grant revoked grant_id=%s lessons=%s admin_id=%s",
            grant_id,
            revoked_lessons,
            admin_telegram_id,
        )
        return PackageRevokeResult(
            grant=PackageGrant.from_record(revoked),
            revoked_lessons=revoked_lessons,
            balance=balance,
            applied=True,
        )

    # ---------- история и аудит ----------

    async def list_credit_history(
        self,
        telegram_id: Optional[int],
        limit: int = 20,
        before_id: Optional[int] = None,
        offset: int = 0,
    ) -> Sequence[LedgerEntry]:
        """Движения занятий из ledger, новые сверху; ``before_id`` — курсор,
        ``offset`` — сколько последних пропустить (страницы в боте).

        ``telegram_id=None`` — по всем участникам (для администратора).
        """
        if not 1 <= limit <= 100:
            raise ValueError("Количество операций должно быть от 1 до 100")
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                _LEDGER_SELECT
                + """
                WHERE ($1::BIGINT IS NULL OR ledger.telegram_id = $1)
                  AND ($2::BIGINT IS NULL OR ledger.id < $2)
                ORDER BY ledger.id DESC
                LIMIT $3 OFFSET $4
                """,
                telegram_id,
                before_id,
                limit,
                offset,
            )
        return [LedgerEntry.from_record(record) for record in records]

    async def list_audit_events(
        self,
        limit: int = 30,
        before: Optional[datetime] = None,
        offset: int = 0,
    ) -> Sequence[AuditEvent]:
        """Журнал действий от новых к старым; ``before`` — для следующей
        страницы (время последней показанной записи), ``offset`` — сколько
        последних записей пропустить (страницы в боте)."""
        if not 1 <= limit <= 100:
            raise ValueError("Количество событий должно быть от 1 до 100")
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT audit.*, actor.user_name AS actor_name,
                       target.user_name AS target_name
                FROM (
                    SELECT 'ledger' AS source, ledger.entry_type AS action,
                           ledger.created_at, ledger.actor_telegram_id,
                           ledger.telegram_id AS target_telegram_id,
                           NULL::BIGINT AS slot_id, ledger.payment_id,
                           ledger.delta, ledger.reason
                    FROM lesson_credit_ledger AS ledger
                    WHERE ledger.actor_telegram_id IS NOT NULL
                      AND ledger.entry_type <> 'slot_cancellation'
                    UNION ALL
                    SELECT 'slot', event.event_type, event.created_at,
                           event.actor_telegram_id, NULL, event.slot_id, NULL,
                           NULL, event.reason
                    FROM lesson_slot_events AS event
                    UNION ALL
                    SELECT 'payment', event.event_type, event.created_at,
                           event.actor_telegram_id, payment.telegram_id, NULL,
                           event.payment_id, NULL, event.reason
                    FROM lesson_payment_events AS event
                    JOIN lesson_payments AS payment ON payment.id = event.payment_id
                    WHERE event.event_type IN (
                        'refund_requested', 'refunded', 'refund_failed'
                    )
                    UNION ALL
                    SELECT 'staff', entry.action, entry.created_at,
                           entry.actor_telegram_id, entry.target_telegram_id,
                           CASE WHEN entry.details ? 'slot_id'
                                THEN (entry.details->>'slot_id')::BIGINT END,
                           NULL, NULL, NULL
                    FROM staff_audit_log AS entry
                ) AS audit
                LEFT JOIN bot_users AS actor
                    ON actor.telegram_id = audit.actor_telegram_id
                LEFT JOIN bot_users AS target
                    ON target.telegram_id = audit.target_telegram_id
                WHERE ($2::TIMESTAMPTZ IS NULL OR audit.created_at < $2)
                ORDER BY audit.created_at DESC
                LIMIT $1 OFFSET $3
                """,
                limit,
                before,
                offset,
            )
        return [
            AuditEvent(
                source=record["source"],
                action=record["action"],
                created_at=record["created_at"],
                actor_telegram_id=record["actor_telegram_id"],
                target_telegram_id=record["target_telegram_id"],
                slot_id=record["slot_id"],
                payment_id=record["payment_id"],
                delta=record["delta"],
                reason=record["reason"],
                actor_name=record["actor_name"],
                target_name=record["target_name"],
            )
            for record in records
        ]

    # ---------- уведомления ----------

    async def enqueue_notification(
        self,
        telegram_id: int,
        kind: str,
        payload: dict,
        dedupe_key: str,
        expires_at: Optional[datetime] = None,
    ) -> bool:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                return await self._enqueue_notification(
                    connection, telegram_id, kind, payload, dedupe_key, expires_at
                )

    async def claim_due_notifications(
        self,
        limit: int = 20,
        lease: timedelta = timedelta(minutes=2),
    ) -> Sequence[UserNotification]:
        """Забирает уведомления к отправке и продлевает их «аренду».

        Строка получает статус 'sending' и следующий срок попытки через
        ``lease``: если процесс упадёт, не отметив результат, уведомление
        вернётся в очередь после этого срока. ``SKIP LOCKED`` — два
        диспетчера (бот и API) не возьмут одну строку.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE user_notifications
                    SET delivery_status = 'skipped', last_error = 'expired'
                    WHERE delivery_status IN ('pending', 'sending')
                      AND expires_at IS NOT NULL AND expires_at <= NOW()
                    """
                )
                records = await connection.fetch(
                    """
                    UPDATE user_notifications
                    SET delivery_status = 'sending', attempts = attempts + 1,
                        next_attempt_at = NOW() + $2::INTERVAL
                    WHERE id IN (
                        SELECT id FROM user_notifications
                        WHERE delivery_status IN ('pending', 'sending')
                          AND next_attempt_at <= NOW()
                        ORDER BY next_attempt_at, id
                        LIMIT $1
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING """
                    + _NOTIFICATION_COLUMNS,
                    limit,
                    lease,
                )
        return sorted(
            (UserNotification.from_record(record) for record in records),
            key=lambda item: item.id,
        )

    async def mark_notification_sent(self, notification_id: int) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE user_notifications
                SET delivery_status = 'sent', sent_at = NOW(), last_error = NULL
                WHERE id = $1
                """,
                notification_id,
            )

    async def mark_notification_retry(
        self,
        notification_id: int,
        error: str,
        retry_at: datetime,
    ) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE user_notifications
                SET delivery_status = 'pending', next_attempt_at = $3,
                    last_error = $2
                WHERE id = $1 AND delivery_status = 'sending'
                """,
                notification_id,
                error[:300],
                retry_at,
            )

    async def mark_notification_finished(
        self,
        notification_id: int,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        """Окончательный статус без отправки: failed или skipped."""
        if status not in {"failed", "skipped"}:
            raise ValueError("Недопустимый итоговый статус уведомления")
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE user_notifications
                SET delivery_status = $2, last_error = $3
                WHERE id = $1
                """,
                notification_id,
                status,
                error[:300] if error else None,
            )

    async def list_notifications(
        self,
        telegram_id: int,
        limit: int = 20,
        unread_only: bool = False,
        before_id: Optional[int] = None,
    ) -> Sequence[UserNotification]:
        if not 1 <= limit <= 100:
            raise ValueError("Количество уведомлений должно быть от 1 до 100")
        async with self._pool.acquire() as connection:
            records = await connection.fetch(
                "SELECT "
                + _NOTIFICATION_COLUMNS
                + """
                FROM user_notifications
                WHERE telegram_id = $1
                  AND (NOT $2::BOOLEAN OR read_at IS NULL)
                  AND ($3::BIGINT IS NULL OR id < $3)
                ORDER BY id DESC
                LIMIT $4
                """,
                telegram_id,
                unread_only,
                before_id,
                limit,
            )
        return [UserNotification.from_record(record) for record in records]

    async def count_unread_notifications(self, telegram_id: int) -> int:
        async with self._pool.acquire() as connection:
            return await connection.fetchval(
                """
                SELECT COUNT(*) FROM user_notifications
                WHERE telegram_id = $1 AND read_at IS NULL
                """,
                telegram_id,
            )

    async def mark_notifications_read(
        self,
        telegram_id: int,
        notification_ids: Optional[Sequence[int]] = None,
    ) -> int:
        """Отмечает прочитанными свои уведомления (все или перечисленные)."""
        async with self._pool.acquire() as connection:
            result = await connection.execute(
                """
                UPDATE user_notifications SET read_at = NOW()
                WHERE telegram_id = $1 AND read_at IS NULL
                  AND ($2::BIGINT[] IS NULL OR id = ANY($2::BIGINT[]))
                """,
                telegram_id,
                list(notification_ids) if notification_ids is not None else None,
            )
        return int(result.split()[-1])

    async def get_notification_settings(self, telegram_id: int) -> NotificationSettings:
        async with self._pool.acquire() as connection:
            record = await connection.fetchrow(
                """
                SELECT notify_reminders, notify_low_balance FROM bot_users
                WHERE telegram_id = $1
                """,
                telegram_id,
            )
        if record is None:
            raise LookupError(
                "Профиль не найден для Telegram ID {}".format(telegram_id)
            )
        return NotificationSettings(
            reminders=record["notify_reminders"],
            low_balance=record["notify_low_balance"],
        )

    async def update_notification_settings(
        self,
        telegram_id: int,
        reminders: Optional[bool] = None,
        low_balance: Optional[bool] = None,
    ) -> NotificationSettings:
        async with self._pool.acquire() as connection:
            record = await connection.fetchrow(
                """
                UPDATE bot_users
                SET notify_reminders = COALESCE($2, notify_reminders),
                    notify_low_balance = COALESCE($3, notify_low_balance)
                WHERE telegram_id = $1
                RETURNING notify_reminders, notify_low_balance
                """,
                telegram_id,
                reminders,
                low_balance,
            )
        if record is None:
            raise LookupError(
                "Профиль не найден для Telegram ID {}".format(telegram_id)
            )
        return NotificationSettings(
            reminders=record["notify_reminders"],
            low_balance=record["notify_low_balance"],
        )

    async def enqueue_due_reminders(self, lead: timedelta) -> int:
        """Ставит напоминания о занятиях, до начала которых осталось ``lead``.

        Только участникам, записавшимся раньше, чем за ``lead`` до начала
        (иначе напоминание пришло бы сразу после записи), и не отключившим
        напоминания. Ключ включает время начала — после переноса
        напоминание ставится заново; истекает в момент начала занятия.
        """
        async with self._pool.acquire() as connection:
            result = await connection.execute(
                """
                INSERT INTO user_notifications (
                    telegram_id, kind, payload, dedupe_key, expires_at
                )
                SELECT booking.telegram_id, 'lesson_reminder',
                       jsonb_build_object(
                           'slot_id', slot.id,
                           'booking_id', booking.id,
                           'class_key', slot.class_key,
                           'starts_at', to_char(
                               slot.starts_at AT TIME ZONE 'UTC',
                               'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'
                           )
                       ),
                       'reminder:' || booking.id || ':'
                           || FLOOR(EXTRACT(EPOCH FROM slot.starts_at))::BIGINT,
                       slot.starts_at
                FROM lesson_bookings AS booking
                JOIN lesson_slots AS slot ON slot.id = booking.slot_id
                JOIN bot_users AS profile
                    ON profile.telegram_id = booking.telegram_id
                WHERE booking.status = 'confirmed'
                  AND slot.status <> 'cancelled'
                  AND slot.starts_at > NOW()
                  AND slot.starts_at <= NOW() + $1::INTERVAL
                  AND booking.booked_at < slot.starts_at - $1::INTERVAL
                  AND profile.notify_reminders
                ON CONFLICT (dedupe_key) DO NOTHING
                """,
                lead,
            )
        return int(result.split()[-1])

    # ---------- срок действия абонементов ----------

    async def expire_due_lots(self, limit: int = 200) -> int:
        """Сжигает остатки истёкших партий (фоновая задача); число занятий.

        Каждый участник — своей транзакцией; повтор безопасен: обработанные
        партии помечаются, а сгоревший остаток больше не числится.
        """
        async with self._pool.acquire() as connection:
            owners = await connection.fetch(
                """
                SELECT telegram_id FROM lesson_payments
                WHERE expires_at <= NOW() AND expiry_processed_at IS NULL
                UNION
                SELECT telegram_id FROM lesson_package_grants
                WHERE expires_at <= NOW() AND expiry_processed_at IS NULL
                LIMIT $1
                """,
                limit,
            )
        burned = 0
        for owner in owners:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    burned += await self._expire_lots(connection, owner["telegram_id"])
        return burned

    async def enqueue_expiry_warnings(self, warning_days: int) -> int:
        """Предупреждения «абонемент скоро сгорит» за ``warning_days`` дней.

        Одно на партию и её срок (dedupe); только если в ней ещё есть
        занятия. ``0`` — предупреждения выключены.
        """
        if warning_days <= 0:
            return 0
        async with self._pool.acquire() as connection:
            owners = await connection.fetch(
                """
                SELECT DISTINCT telegram_id FROM (
                    SELECT telegram_id FROM lesson_payments
                    WHERE status = 'succeeded'
                      AND expires_at > NOW()
                      AND expires_at <= NOW() + make_interval(days => $1)
                    UNION
                    SELECT telegram_id FROM lesson_package_grants
                    WHERE status = 'active'
                      AND expires_at > NOW()
                      AND expires_at <= NOW() + make_interval(days => $1)
                ) AS due
                """,
                warning_days,
            )
            created = 0
            horizon = datetime.now(timezone.utc) + timedelta(days=warning_days)
            for owner in owners:
                telegram_id = owner["telegram_id"]
                async with connection.transaction():
                    balance = await connection.fetchval(
                        "SELECT lesson_credits FROM bot_users WHERE telegram_id = $1",
                        telegram_id,
                    )
                    sources = await connection.fetch(_ACTIVE_SOURCES_SQL, telegram_id)
                    effective, _ = effective_remaining(
                        balance, [row["remaining"] for row in sources]
                    )
                    for row, remaining in zip(sources, effective):
                        expires_at = row["expires_at"]
                        if remaining <= 0 or expires_at is None or expires_at > horizon:
                            continue
                        if await self._enqueue_notification(
                            connection,
                            telegram_id,
                            "package_expiring",
                            {
                                "title": row["title"],
                                "lessons": remaining,
                                "expires_at": expires_at.isoformat(),
                            },
                            "package_expiring:{}:{}:{}".format(
                                row["kind"], row["id"], int(expires_at.timestamp())
                            ),
                            expires_at=expires_at,
                        ):
                            created += 1
        return created

    async def is_reminder_current(self, booking_id: int, starts_at: datetime) -> bool:
        """Запись ещё действует и занятие не перенесено/не отменено."""
        async with self._pool.acquire() as connection:
            found = await connection.fetchval(
                """
                SELECT 1 FROM lesson_bookings AS booking
                JOIN lesson_slots AS slot ON slot.id = booking.slot_id
                WHERE booking.id = $1 AND booking.status = 'confirmed'
                  AND slot.status <> 'cancelled' AND slot.starts_at = $2
                  AND slot.starts_at > NOW()
                """,
                booking_id,
                starts_at,
            )
        return found is not None

    # ---------- вход на сайт через бота ----------

    async def create_telegram_connect_request(
        self,
        token: str,
        browser_secret: str,
        purpose: str,
        web_user_id: Optional[int],
        expires_at: datetime,
    ) -> int:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                # Простая уборка без отдельной фоновой задачи (как в web_sessions).
                await connection.execute(
                    """
                    DELETE FROM telegram_connect_requests
                    WHERE expires_at < NOW() - INTERVAL '1 day'
                    """
                )
                return await connection.fetchval(
                    """
                    INSERT INTO telegram_connect_requests (
                        token_hash, browser_secret_hash, purpose, web_user_id,
                        expires_at
                    )
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id
                    """,
                    secret_hash(token),
                    secret_hash(browser_secret),
                    purpose,
                    web_user_id,
                    expires_at,
                )

    async def get_telegram_connect_request(
        self, token: str
    ) -> Optional[TelegramConnectRequest]:
        async with self._pool.acquire() as connection:
            record = await connection.fetchrow(
                """
                SELECT request.*, account.email AS web_user_email
                FROM telegram_connect_requests AS request
                LEFT JOIN users AS account ON account.id = request.web_user_id
                WHERE request.token_hash = $1
                """,
                secret_hash(token),
            )
        return TelegramConnectRequest.from_record(record) if record else None

    async def confirm_telegram_connect_request(
        self, token: str, telegram_id: int
    ) -> TelegramConnectRequest:
        """Подтверждение в боте: фиксирует проверенный Telegram ID.

        Для привязки (``purpose='link'``) сразу привязывает Telegram к
        веб-аккаунту в той же транзакции. ``TelegramConnectError`` — запрос
        не найден, истёк или уже обработан; ``TelegramAlreadyLinkedError``.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                record = await connection.fetchrow(
                    """
                    SELECT * FROM telegram_connect_requests
                    WHERE token_hash = $1 FOR UPDATE
                    """,
                    secret_hash(token),
                )
                if (
                    record is None
                    or record["status"] != "pending"
                    or record["expires_at"] <= datetime.now(timezone.utc)
                ):
                    raise TelegramConnectError(
                        "Ссылка для входа недействительна или устарела"
                    )
                if record["purpose"] == "link":
                    await self._link_telegram_in_transaction(
                        connection, record["web_user_id"], telegram_id
                    )
                updated = await connection.fetchrow(
                    """
                    UPDATE telegram_connect_requests
                    SET status = 'confirmed', telegram_id = $2, confirmed_at = NOW()
                    WHERE id = $1
                    RETURNING *
                    """,
                    record["id"],
                    telegram_id,
                )
        logger.info(
            "Telegram connect confirmed request_id=%s purpose=%s",
            record["id"],
            record["purpose"],
        )
        return TelegramConnectRequest.from_record(updated)

    async def reject_telegram_connect_request(self, token: str) -> bool:
        async with self._pool.acquire() as connection:
            result = await connection.execute(
                """
                UPDATE telegram_connect_requests SET status = 'rejected'
                WHERE token_hash = $1 AND status = 'pending'
                """,
                secret_hash(token),
            )
        return result == "UPDATE 1"

    async def consume_telegram_connect_request(
        self, browser_secret: str
    ) -> Tuple[Optional[TelegramConnectRequest], bool]:
        """Браузер забирает подтверждённый запрос — ровно один раз.

        Возвращает ``(запрос, забран_сейчас)``. Сессию можно выдать только при
        ``True``: повторный опрос того же браузера получает ``False`` и
        статус 'consumed', ожидание — статус 'pending', отказ — 'rejected'.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                record = await connection.fetchrow(
                    """
                    SELECT * FROM telegram_connect_requests
                    WHERE browser_secret_hash = $1 FOR UPDATE
                    """,
                    secret_hash(browser_secret),
                )
                if record is None:
                    return None, False
                now = datetime.now(timezone.utc)
                consumed_now = (
                    record["status"] == "confirmed"
                    and record["confirmed_at"] + CONNECT_CONSUME_WINDOW > now
                )
                if consumed_now:
                    record = await connection.fetchrow(
                        """
                        UPDATE telegram_connect_requests
                        SET status = 'consumed', consumed_at = NOW()
                        WHERE id = $1
                        RETURNING *
                        """,
                        record["id"],
                    )
        return TelegramConnectRequest.from_record(record), consumed_now

    @staticmethod
    async def _link_telegram_in_transaction(
        connection: Any, user_id: int, telegram_id: int
    ) -> None:
        """Привязывает Telegram к веб-аккаунту, не создавая дублей.

        Если этот Telegram уже у другого аккаунта без email (аккаунт,
        созданный только входом через Telegram, — у него нет своих данных,
        кроме сессий), он объединяется с текущим: строка удаляется, привязка
        переходит сюда. Аккаунт с email так не забирается —
        ``TelegramAlreadyLinkedError``.
        """
        holder = await connection.fetchrow(
            "SELECT id, email FROM users WHERE telegram_id = $1 FOR UPDATE",
            telegram_id,
        )
        if holder is not None and holder["id"] != user_id:
            if holder["email"] is not None:
                raise TelegramAlreadyLinkedError(
                    "Этот Telegram уже привязан к другому аккаунту"
                )
            await connection.execute("DELETE FROM users WHERE id = $1", holder["id"])
            logger.info(
                "Merged Telegram-only web account user_id=%s into user_id=%s",
                holder["id"],
                user_id,
            )
        updated = await connection.execute(
            "UPDATE users SET telegram_id = $2, updated_at = NOW() WHERE id = $1",
            user_id,
            telegram_id,
        )
        if updated != "UPDATE 1":
            raise LookupError("Аккаунт не найден")

    async def link_telegram_to_web_user(self, user_id: int, telegram_id: int) -> None:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._link_telegram_in_transaction(
                    connection, user_id, telegram_id
                )
        logger.info("Linked Telegram to web user user_id=%s", user_id)

    # ---------- поддержка ----------

    async def get_support_ticket_thread(
        self,
        ticket_id: int,
        telegram_id: Optional[int] = None,
        message_limit: int = 50,
    ) -> Optional[SupportTicketThread]:
        """Обращение с последними сообщениями; ``telegram_id`` — только своё."""
        async with self._pool.acquire() as connection:
            ticket = await connection.fetchrow(
                """
                SELECT ticket.id, ticket.telegram_id, ticket.status,
                       ticket.created_at, ticket.updated_at, profile.user_name
                FROM support_tickets AS ticket
                JOIN bot_users AS profile ON profile.telegram_id = ticket.telegram_id
                WHERE ticket.id = $1
                  AND ($2::BIGINT IS NULL OR ticket.telegram_id = $2)
                """,
                ticket_id,
                telegram_id,
            )
            if ticket is None:
                return None
            messages = await connection.fetch(
                """
                SELECT id, sender_role, body, created_at FROM support_messages
                WHERE ticket_id = $1
                ORDER BY id DESC
                LIMIT $2
                """,
                ticket_id,
                message_limit,
            )
        return SupportTicketThread(
            id=ticket["id"],
            telegram_id=ticket["telegram_id"],
            status=ticket["status"],
            created_at=ticket["created_at"],
            updated_at=ticket["updated_at"],
            user_name=ticket["user_name"],
            messages=tuple(
                SupportThreadMessage(
                    id=message["id"],
                    sender_role=message["sender_role"],
                    body=message["body"],
                    created_at=message["created_at"],
                )
                for message in reversed(messages)
            ),
        )

    async def reopen_support_ticket(
        self,
        ticket_id: int,
        admin_telegram_id: int,
    ) -> Optional[int]:
        """Снова открывает закрытое обращение; возвращает Telegram ID автора.

        У участника может быть только одно открытое обращение (уникальный
        индекс) — если есть другое, ``SupportTicketStateError``.
        """
        async with self._pool.acquire() as connection:
            try:
                async with connection.transaction():
                    ticket = await connection.fetchrow(
                        """
                        SELECT telegram_id, status FROM support_tickets
                        WHERE id = $1 FOR UPDATE
                        """,
                        ticket_id,
                    )
                    if ticket is None:
                        return None
                    if ticket["status"] == "open":
                        raise SupportTicketStateError("Обращение уже открыто")
                    other_id = await connection.fetchval(
                        """
                        SELECT id FROM support_tickets
                        WHERE telegram_id = $1 AND status = 'open'
                        """,
                        ticket["telegram_id"],
                    )
                    if other_id is not None:
                        raise SupportTicketStateError(
                            "У участника уже есть открытое обращение №{}".format(
                                other_id
                            )
                        )
                    await connection.execute(
                        """
                        UPDATE support_tickets
                        SET status = 'open', updated_at = NOW()
                        WHERE id = $1
                        """,
                        ticket_id,
                    )
                    await connection.execute(
                        """
                        INSERT INTO support_messages (
                            ticket_id, sender_telegram_id, sender_role, body
                        )
                        VALUES ($1, $2, 'admin', 'Обращение снова открыто.')
                        """,
                        ticket_id,
                        admin_telegram_id,
                    )
            except asyncpg.UniqueViolationError as error:
                raise SupportTicketStateError(
                    "У участника уже есть открытое обращение"
                ) from error
        return ticket["telegram_id"]


def build_package_summary(
    balance: int, records: Sequence[Any], now: Optional[datetime] = None
) -> PackageSummary:
    """Абонементы участника из строк источников.

    ``records`` — в порядке получения (так они и показываются); поля: kind,
    id, package_key, title, lessons, source_status, acquired_at,
    amount_minor, reason, expires_at, raw_remaining. Остатки считаются в
    порядке списания (сначала сгорающие раньше) — так же, как при записи.
    Партия с истёкшим сроком — ``expired``: её остаток сгорает (фоновой
    задачей), записаться на него уже нельзя.
    """
    current = now or datetime.now(timezone.utc)
    active_indexes = sorted(
        (
            index
            for index, record in enumerate(records)
            if (record["kind"] == "payment" and record["source_status"] == "succeeded")
            or (record["kind"] == "grant" and record["source_status"] == "active")
        ),
        key=lambda index: source_order_key(records[index]),
    )
    effective, unallocated = effective_remaining(
        balance, [records[index]["raw_remaining"] for index in active_indexes]
    )
    effective_by_index = dict(zip(active_indexes, effective))
    packages = []
    for index, record in enumerate(records):
        expires_at = record["expires_at"]
        if index in effective_by_index:
            remaining = effective_by_index[index]
            if expires_at is not None and expires_at <= current:
                status = "expired"
                remaining = 0
            else:
                status = "active" if remaining > 0 else "used"
        else:
            remaining = 0
            status = record["source_status"]
        packages.append(
            UserPackage(
                kind=record["kind"],
                id=record["id"],
                package_key=record["package_key"],
                title=record["title"],
                lessons=record["lessons"],
                remaining=remaining,
                status=status,
                acquired_at=record["acquired_at"],
                amount_minor=record["amount_minor"],
                reason=record["reason"],
                expires_at=expires_at,
            )
        )
    return PackageSummary(
        balance=balance, packages=tuple(packages), unallocated=unallocated
    )
