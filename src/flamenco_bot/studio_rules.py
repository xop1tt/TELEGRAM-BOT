"""Правила записи студии — кэш строки ``studio_settings`` (миграция 017).

Правила меняет администратор в админке сайта. Здесь — значения для
отображения (сроки отмены в боте и на сайте); сами проверки при записи и
отмене выполняются в транзакции по значениям, прочитанным из базы там же
(``PostgresRepository``), так что устаревший кэш не может пропустить
запрещённое действие.

Значения по умолчанию — прежние правила студии (до первой загрузки из базы).
"""

from dataclasses import dataclass, replace
from datetime import timedelta


@dataclass(frozen=True)
class StudioRules:
    # Отменить запись можно не позднее чем за столько часов до начала.
    cancellation_deadline_hours: int = 24
    # После своей отмены повторная запись на то же занятие — через.
    rebook_cooldown_hours: int = 12
    # Срок ручных начислений «± баланс», дней (миграция 018).
    manual_credit_validity_days: int = 60
    # За сколько дней предупредить, что абонемент сгорает (0 — не надо).
    expiry_warning_days: int = 7

    @property
    def cancellation_deadline(self) -> timedelta:
        return timedelta(hours=self.cancellation_deadline_hours)

    @property
    def rebook_cooldown(self) -> timedelta:
        return timedelta(hours=self.rebook_cooldown_hours)


_current = StudioRules()


def rules() -> StudioRules:
    return _current


def apply_rules(new_rules: StudioRules) -> None:
    global _current
    _current = replace(new_rules)
