"""Каталог направлений занятий: ``/api/classes``.

Источник истины — ``class_catalog`` (тот же каталог, которым пользуется бот).
Фронтенд получает название, описание и уровень отсюда вместо того, чтобы
хардкодить их отдельно (см. ``src/lib/directions.ts`` в проекте сайта).
"""

from typing import List

from fastapi import APIRouter

from ...class_catalog import (
    ACTIVE_CLASS_KEYS,
    CLASS_DESCRIPTIONS,
    CLASS_LABELS,
    CLASS_LEVELS,
)
from ..schemas import ClassFormatResponse


router = APIRouter(prefix="/api/classes", tags=["classes"])


@router.get("", response_model=List[ClassFormatResponse])
async def list_classes() -> List[ClassFormatResponse]:
    return [
        ClassFormatResponse(
            key=key,
            label=CLASS_LABELS[key],
            description=CLASS_DESCRIPTIONS[key],
            level=CLASS_LEVELS[key],
        )
        # Только действующие направления; скрытые — лишь в админке.
        for key in ACTIVE_CLASS_KEYS
    ]
