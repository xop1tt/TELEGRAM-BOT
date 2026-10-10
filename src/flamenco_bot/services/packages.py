"""Абонементы участника: остатки, выдача и отзыв администратором.

Источник истины для движения занятий — ledger (``lesson_credit_ledger``):
остаток абонемента — сумма ledger по нему, а распределение баланса по
абонементам считается функцией ``effective_remaining`` (см.
``database/studio_models.py``). Купленные абонементы — успешные платежи,
выданные студией — ``lesson_package_grants``; второй системы баланса нет.
"""

import logging
import uuid
from typing import Any, Optional

from ..database.studio_models import (
    PackageGrantResult,
    PackageRevokeResult,
    PackageSummary,
)
from ..payments import PURCHASE_PACKAGES, LessonPackage


logger = logging.getLogger("bot.services.packages")


def catalog_package(package_key: str) -> Optional[LessonPackage]:
    return next(
        (
            package
            for package in PURCHASE_PACKAGES.values()
            if package.key == package_key
        ),
        None,
    )


class PackageService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    async def summary(self, telegram_id: int) -> PackageSummary:
        return await self.repository.get_package_summary(telegram_id)

    async def grant(
        self,
        telegram_id: int,
        package_key: str,
        reason: str,
        admin_telegram_id: int,
        idempotence_key: uuid.UUID,
    ) -> PackageGrantResult:
        """Выдаёт абонемент из каталога без онлайн-оплаты (например, наличные).

        ``ValueError`` — неизвестный абонемент или пустая причина;
        ``PermissionError`` — не администратор; ``LookupError`` — нет профиля.
        """
        package = catalog_package(package_key)
        if package is None:
            raise ValueError("Неизвестный абонемент")
        result = await self.repository.grant_lesson_package(
            telegram_id,
            package.key,
            package.title,
            package.lessons,
            reason,
            admin_telegram_id,
            idempotence_key,
            validity_days=package.validity_days,
        )
        logger.info(
            "Package grant processed grant_id=%s applied=%s",
            result.grant.id,
            result.applied,
        )
        return result

    async def revoke(
        self,
        grant_id: int,
        reason: str,
        admin_telegram_id: int,
    ) -> PackageRevokeResult:
        return await self.repository.revoke_lesson_package_grant(
            grant_id, reason, admin_telegram_id
        )
