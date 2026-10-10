"""Правила профиля участника, общие для бота и веб-API."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence, Tuple

from ..database.repository import UserBooking, UserProfile
from ..database.studio_models import NotificationSettings, PackageSummary

MAX_USER_NAME_LENGTH = 64


class InvalidUserNameError(ValueError):
    pass


def normalize_user_name(value: str) -> str:
    """Имя без пробелов по краям, от 1 до ``MAX_USER_NAME_LENGTH`` символов."""
    user_name = value.strip()
    if not user_name or len(user_name) > MAX_USER_NAME_LENGTH:
        raise InvalidUserNameError(
            "Имя должно содержать от 1 до {} символов".format(MAX_USER_NAME_LENGTH)
        )
    return user_name


_PHONE = re.compile(r"\+?[0-9][0-9 ()-]{5,30}")


def normalize_phone(value: str) -> str:
    """Телефон: цифры, пробелы, скобки и дефисы, от 7 до 32 символов."""
    phone = value.strip()
    if not _PHONE.fullmatch(phone):
        raise ValueError(
            "Введите номер телефона: цифры, пробелы, скобки и дефисы "
            "(от 7 до 32 символов)"
        )
    return phone


@dataclass(frozen=True)
class ClientOverview:
    """Сводка участника: профиль, баланс по абонементам, ближайшие занятия.

    Одна и та же для «👤 Профиль» в боте, ``/api/users/me/overview`` и
    карточки участника у администратора.
    """

    profile: UserProfile
    packages: PackageSummary
    upcoming: Tuple[UserBooking, ...]
    unread_notifications: int
    settings: NotificationSettings


def upcoming_bookings(
    bookings: Sequence[UserBooking],
    now: Optional[datetime] = None,
) -> list:
    """Действующие будущие записи от ближайшей."""
    current = now or datetime.now(timezone.utc)
    return sorted(
        (
            booking
            for booking in bookings
            if booking.booking_status == "confirmed"
            and booking.slot_status != "cancelled"
            and booking.starts_at > current
        ),
        key=lambda booking: booking.starts_at,
    )


class ProfileService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    async def overview(
        self, telegram_id: int, upcoming_limit: int = 3
    ) -> ClientOverview:
        """``LookupError`` — профиля нет."""
        profile = await self.repository.get_profile(telegram_id)
        if profile is None:
            raise LookupError(
                "Профиль не найден для Telegram ID {}".format(telegram_id)
            )
        packages = await self.repository.get_package_summary(telegram_id)
        bookings = await self.repository.list_bookings_for_telegram_id(telegram_id)
        return ClientOverview(
            profile=profile,
            packages=packages,
            upcoming=tuple(upcoming_bookings(bookings)[:upcoming_limit]),
            unread_notifications=await self.repository.count_unread_notifications(
                telegram_id
            ),
            settings=await self.repository.get_notification_settings(telegram_id),
        )
