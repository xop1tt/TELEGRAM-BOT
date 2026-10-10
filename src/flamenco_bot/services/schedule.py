"""Управление расписанием: создание, перенос, отмена, закрытие занятий.

Общая логика для любого интерфейса администратора (сейчас — Telegram-бот).
Атомарность и правила (терминальный статус 'cancelled', возврат занятий
ровно один раз, запрет после начала) обеспечивает репозиторий в одной
транзакции под блокировкой строки слота; уведомления участникам он ставит
в outbox той же транзакцией, а отправляет их диспетчер после commit.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from ..class_catalog import ACTIVE_CLASS_KEYS
from ..database.repository import ClassSlot
from ..database.studio_models import (
    SlotCancellation,
    SlotEvent,
    SlotParticipant,
    SlotReschedule,
)


logger = logging.getLogger("bot.services.schedule")

# Админ-расписание показывает и начавшиеся сегодня занятия — чтобы открыть
# список участников идущего занятия.
ADMIN_SCHEDULE_LOOKBACK = timedelta(hours=12)


class ScheduleService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    async def admin_schedule(
        self,
        class_key: Optional[str] = None,
        limit: int = 30,
        teacher_telegram_id: Optional[int] = None,
    ) -> Sequence[ClassSlot]:
        """Расписание админки; ``teacher_telegram_id`` — только занятия
        этого преподавателя (так его видит преподаватель)."""
        return await self.repository.list_class_slots(
            class_key=class_key,
            limit=limit,
            include_cancelled=True,
            starts_after=datetime.now(timezone.utc) - ADMIN_SCHEDULE_LOOKBACK,
            teacher_telegram_id=teacher_telegram_id,
        )

    async def get(self, slot_id: int) -> Optional[ClassSlot]:
        return await self.repository.get_class_slot(slot_id)

    async def create(
        self,
        class_key: str,
        starts_at: datetime,
        capacity: int,
        admin_telegram_id: int,
        teacher_telegram_id: Optional[int] = None,
    ) -> ClassSlot:
        """``ValueError`` — неизвестный формат, время без пояса или в прошлом,
        вместимость вне 1–100 (проверяет репозиторий); ``LookupError`` —
        преподаватель не сотрудник."""
        if class_key not in ACTIVE_CLASS_KEYS:
            raise ValueError("Неизвестный или скрытый формат занятия")
        slot = await self.repository.create_class_slot(
            class_key,
            starts_at,
            capacity,
            admin_telegram_id,
            teacher_telegram_id=teacher_telegram_id,
        )
        logger.info(
            "Class slot created slot_id=%s admin_id=%s", slot.id, admin_telegram_id
        )
        return slot

    async def reschedule(
        self,
        slot_id: int,
        new_starts_at: datetime,
        admin_telegram_id: int,
        reason: Optional[str] = None,
    ) -> SlotReschedule:
        return await self.repository.reschedule_class_slot(
            slot_id, new_starts_at, admin_telegram_id, reason
        )

    async def cancel(
        self,
        slot_id: int,
        admin_telegram_id: int,
        reason: Optional[str] = None,
    ) -> SlotCancellation:
        return await self.repository.cancel_class_slot(
            slot_id, admin_telegram_id, reason
        )

    async def close(self, slot_id: int, admin_telegram_id: int) -> bool:
        return await self.repository.close_class_slot(slot_id, admin_telegram_id)

    async def reopen(self, slot_id: int, admin_telegram_id: int) -> bool:
        return await self.repository.reopen_class_slot(slot_id, admin_telegram_id)

    async def set_capacity(
        self, slot_id: int, capacity: int, admin_telegram_id: int
    ) -> bool:
        return await self.repository.update_class_slot_capacity(
            slot_id, capacity, admin_telegram_id
        )

    async def participants(
        self, slot_id: int, include_cancelled: bool = False
    ) -> Sequence[SlotParticipant]:
        return await self.repository.list_slot_participants(slot_id, include_cancelled)

    async def events(self, slot_id: int, limit: int = 10) -> Sequence[SlotEvent]:
        return await self.repository.list_slot_events(slot_id, limit)
