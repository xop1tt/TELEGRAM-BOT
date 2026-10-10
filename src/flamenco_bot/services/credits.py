"""Баланс занятий: ручная корректировка администратором и сверка с ledger.

Сам баланс меняется только в репозитории (``PostgresRepository._change_credits``);
сервис добавляет проверки ввода, общие для любого интерфейса администратора.
"""

import logging
import uuid
from typing import Any

from ..database.repository import CreditAdjustment, CreditReconciliation


logger = logging.getLogger("bot.services.credits")

MAX_REASON_LENGTH = 300


class CreditService:
    def __init__(self, repository: Any, max_adjustment: int) -> None:
        if max_adjustment < 1:
            raise ValueError("Максимальная корректировка должна быть не меньше 1")
        self.repository = repository
        self.max_adjustment = max_adjustment

    def validate_adjustment(self, delta: int, reason: str) -> str:
        """Возвращает нормализованную причину или бросает ``ValueError``.

        Верхняя граница ``max_adjustment`` — защита от опечатки («100» вместо
        «1»), а не бизнес-правило студии; настраивается в окружении.
        """
        if delta == 0:
            raise ValueError("Изменение баланса не может быть нулевым")
        if abs(delta) > self.max_adjustment:
            raise ValueError(
                "За одну корректировку можно изменить баланс не более чем на {}".format(
                    self.max_adjustment
                )
            )
        normalized_reason = reason.strip()
        if not normalized_reason or len(normalized_reason) > MAX_REASON_LENGTH:
            raise ValueError(
                "Причина должна содержать от 1 до {} символов".format(MAX_REASON_LENGTH)
            )
        return normalized_reason

    async def adjust(
        self,
        telegram_id: int,
        delta: int,
        reason: str,
        actor_telegram_id: int,
        idempotence_key: uuid.UUID,
    ) -> CreditAdjustment:
        """Ошибки репозитория передаются как есть: ``PermissionError`` (актор
        не администратор), ``LookupError`` (нет профиля),
        ``InsufficientLessonCreditsError`` (баланс ушёл бы ниже нуля).
        """
        normalized_reason = self.validate_adjustment(delta, reason)
        result = await self.repository.adjust_lesson_credits(
            telegram_id,
            delta,
            normalized_reason,
            actor_telegram_id,
            idempotence_key,
        )
        if result.applied:
            # Корректировка уже зафиксирована; уведомление участнику ставится
            # отдельно и идемпотентно (ключ — строка ledger): его сбой не
            # влияет на баланс.
            try:
                await self.repository.enqueue_notification(
                    telegram_id,
                    "credits_adjusted",
                    {
                        "delta": result.delta,
                        "balance": result.balance,
                        "reason": normalized_reason,
                    },
                    "credits_adjusted:{}".format(result.ledger_id),
                )
            except Exception:
                logger.exception(
                    "Credit adjustment notification not queued ledger_id=%s",
                    result.ledger_id,
                )
        return result

    async def reconcile(self, limit: int = 20) -> CreditReconciliation:
        """Находит расхождения ``lesson_credits`` и суммы ledger; ничего не чинит."""
        result = await self.repository.get_credit_reconciliation(limit)
        if result.mismatched_users:
            logger.warning(
                "Credit balance mismatch found users=%s checked=%s",
                result.mismatched_users,
                result.checked_users,
            )
        return result
