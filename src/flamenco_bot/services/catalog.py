"""Каталог студии: направления, абонементы и правила записи.

Источник — база (миграция 017); бот и API держат копию в памяти
(``class_catalog``, ``payments.catalog``, ``studio_rules``), чтобы не ходить
в базу за каждым названием. ``refresh`` перечитывает всё сразу; процесс,
который изменил каталог, обновляет копию сразу, второй процесс (бот или
API) — фоновой задачей не позже чем через ``CATALOG_REFRESH_SECONDS``.

Проверки, от которых зависят деньги и места, кэшем не пользуются: цена
покупки берётся из каталога в момент оплаты, а правила записи и отмены
репозиторий читает из базы в транзакции операции.
"""

import asyncio
import logging
from typing import Any, Optional, Sequence

from ..class_catalog import ClassFormat, apply_class_formats
from ..payments.catalog import LessonPackage, apply_packages
from ..studio_rules import StudioRules, apply_rules

CATALOG_REFRESH_SECONDS = 60

logger = logging.getLogger("bot.services.catalog")


class CatalogService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    async def refresh(self) -> None:
        apply_class_formats(await self.repository.list_class_formats())
        apply_packages(await self.repository.list_lesson_packages())
        apply_rules(await self.repository.get_studio_rules())

    async def class_formats(self) -> Sequence[ClassFormat]:
        return await self.repository.list_class_formats()

    async def packages(self) -> Sequence[LessonPackage]:
        return await self.repository.list_lesson_packages()

    async def save_class_format(
        self, item: ClassFormat, admin_telegram_id: int, create: bool
    ) -> ClassFormat:
        saved = await self.repository.save_class_format(item, admin_telegram_id, create)
        await self.refresh()
        return saved

    async def save_package(
        self, package: LessonPackage, admin_telegram_id: int, create: bool
    ) -> LessonPackage:
        saved = await self.repository.save_lesson_package(
            package, admin_telegram_id, create
        )
        await self.refresh()
        return saved

    async def update_rules(
        self,
        admin_telegram_id: int,
        **values: Optional[int],
    ) -> StudioRules:
        updated = await self.repository.update_studio_rules(admin_telegram_id, **values)
        await self.refresh()
        return updated


async def run_catalog_refresh(
    repository: Any,
    log: logging.Logger,
    interval_seconds: float = CATALOG_REFRESH_SECONDS,
) -> None:
    """Фоновое обновление копии каталога; сбой БД не останавливает задачу."""
    service = CatalogService(repository)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await service.refresh()
        except Exception:  # noqa: BLE001 — следующая попытка через интервал
            log.exception("Catalog refresh failed")


def start_catalog_refresh(repository: Any, log: logging.Logger) -> asyncio.Task:
    return asyncio.create_task(run_catalog_refresh(repository, log))
