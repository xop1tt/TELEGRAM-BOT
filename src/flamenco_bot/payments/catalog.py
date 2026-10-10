"""Абонементы — кэш таблицы ``lesson_packages`` (миграция 017).

Источник истины — база: цены, сроки и состав меняет администратор в
админке сайта. ``apply_packages`` обновляет словари **на месте** (см.
``class_catalog``); значения ниже — только начальные, до первой загрузки
из базы.

- ``ALL_PACKAGES`` — все абонементы по ключу, включая скрытые (прошлые
  покупки и выдачи);
- ``PURCHASE_PACKAGES`` — продаваемые сейчас, по названию (порядок —
  сортировка в админке); ``PURCHASE_OPTIONS`` — то же по подписи кнопки.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, Optional


@dataclass(frozen=True)
class LessonPackage:
    key: str
    title: str
    lessons: int
    price_rub: int
    # Срок действия с даты покупки/выдачи; None — бессрочно.
    validity_days: Optional[int] = None
    sort_order: int = 0
    is_active: bool = True

    @property
    def menu_label(self) -> str:
        return "{} — {} ₽".format(self.title, self.price_rub)


_DEFAULTS = (
    LessonPackage("single", "Разовое занятие", 1, 1000, 30, 10),
    LessonPackage("pack_4", "Абонемент на 4 занятия", 4, 3600, 30, 20),
    LessonPackage("pack_8", "Абонемент на 8 занятий", 8, 6800, 60, 30),
)

ALL_PACKAGES: Dict[str, LessonPackage] = {package.key: package for package in _DEFAULTS}
PURCHASE_PACKAGES: Dict[str, LessonPackage] = {
    package.title: package for package in _DEFAULTS
}
PURCHASE_OPTIONS: Dict[str, LessonPackage] = {
    package.menu_label: package for package in _DEFAULTS
}


def apply_packages(packages: Iterable[LessonPackage]) -> None:
    """Заменяет содержимое кэша абонементами из базы (на месте)."""
    ordered = sorted(packages, key=lambda item: (item.sort_order, item.key))
    if not ordered:
        return
    active = [package for package in ordered if package.is_active]
    ALL_PACKAGES.clear()
    ALL_PACKAGES.update({package.key: package for package in ordered})
    PURCHASE_PACKAGES.clear()
    PURCHASE_PACKAGES.update({package.title: package for package in active})
    PURCHASE_OPTIONS.clear()
    PURCHASE_OPTIONS.update({package.menu_label: package for package in active})
