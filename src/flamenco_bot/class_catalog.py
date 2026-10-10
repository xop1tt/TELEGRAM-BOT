"""Направления занятий — кэш таблицы ``class_formats`` (миграция 017).

Источник истины — база: направления меняет администратор в админке сайта.
Здесь — словари, которыми пользуются бот и API; ``apply_class_formats``
обновляет их **на месте**, поэтому модули, импортировавшие словари
(``from ..class_catalog import CLASS_LABELS``), видят новые значения без
перезапуска. Обновляет кэш ``services.catalog.CatalogService`` при старте,
по таймеру и сразу после правки в админке.

Значения ниже — только начальные, до первой загрузки из базы (совпадают с
тем, что кладёт миграция).

Словари содержат и скрытые направления (``is_active = FALSE``): прошлым
занятиям нужно название. Для выбора при создании и записи —
``ACTIVE_CLASS_KEYS``.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, List


class _Labels(dict):
    """Словарь подписей: неизвестный ключ возвращает сам ключ.

    Направление, созданное в админке, другой процесс (бот или API) узнает
    при следующем обновлении кэша; до тех пор вместо названия — ключ, а не
    ``KeyError``.
    """

    def __missing__(self, key: str) -> str:
        return key


@dataclass(frozen=True)
class ClassFormat:
    key: str
    title: str
    short_title: str
    description: str
    level: str
    sort_order: int = 0
    is_active: bool = True


CLASS_LABELS: Dict[str, str] = _Labels(
    {
        "beginner": "Фламенко для начинающих",
        "intermediate": "Продолжающая группа",
        "individual": "Индивидуальное занятие",
    }
)

CLASS_KEYS_BY_LABEL: Dict[str, str] = {
    label: key for key, label in CLASS_LABELS.items()
}

# Короткие названия направлений — для кнопок, где полное не помещается.
CLASS_SHORT_LABELS: Dict[str, str] = _Labels(
    {
        "beginner": "Начинающие",
        "intermediate": "Продолжающие",
        "individual": "Индивидуально",
    }
)

# Описание и уровень направления — для «О студии» в боте и карточек
# направлений на сайте (отдаются через /api/classes).
CLASS_DESCRIPTIONS: Dict[str, str] = _Labels(
    {
        "beginner": (
            "Группа для тех, кто никогда не танцевал фламенко или делает первые "
            "шаги. Базовая техника, ритм, работа с телом — без предварительной "
            "подготовки."
        ),
        "intermediate": (
            "Для тех, кто уже освоил основы и хочет двигаться дальше: усложнённые "
            "связки, работа с партнёром по классу, более быстрый темп."
        ),
        "individual": (
            "Персональный разбор техники и подготовка к выступлению в удобное "
            "время, в своём темпе."
        ),
    }
)

CLASS_LEVELS: Dict[str, str] = _Labels(
    {
        "beginner": "Для начинающих, без подготовки",
        "intermediate": "Для продолжающих",
        "individual": "Индивидуально, любой уровень",
    }
)

# Ключи всех направлений (и скрытых) и активных — в порядке сортировки.
CLASS_KEYS: List[str] = list(CLASS_LABELS)
ACTIVE_CLASS_KEYS: List[str] = list(CLASS_LABELS)


def apply_class_formats(formats: Iterable[ClassFormat]) -> None:
    """Заменяет содержимое кэша направлениями из базы (на месте)."""
    ordered = sorted(formats, key=lambda item: (item.sort_order, item.key))
    if not ordered:
        # Пустая выборка — сбой, а не «направлений нет»: кэш не трогаем.
        return
    for mapping, value in (
        (CLASS_LABELS, lambda item: item.title),
        (CLASS_SHORT_LABELS, lambda item: item.short_title),
        (CLASS_DESCRIPTIONS, lambda item: item.description),
        (CLASS_LEVELS, lambda item: item.level),
    ):
        mapping.clear()
        mapping.update({item.key: value(item) for item in ordered})
    CLASS_KEYS_BY_LABEL.clear()
    CLASS_KEYS_BY_LABEL.update({item.title: item.key for item in ordered})
    CLASS_KEYS[:] = [item.key for item in ordered]
    ACTIVE_CLASS_KEYS[:] = [item.key for item in ordered if item.is_active]


def class_label(key: str) -> str:
    """Название направления; неизвестный ключ — сам ключ (не KeyError)."""
    return CLASS_LABELS.get(key, key)
