"""Общая бизнес-логика для Telegram-бота и веб-интерфейса.

Модули пакета не импортируют обработчики aiogram и не формируют ответы
пользователю: они возвращают результат, а интерфейс решает, как его показать.
"""

from .admin import AdminService
from .auth import (
    AuthService,
    CannotUnlinkOnlyLoginMethodError,
    CredentialsAlreadySetError,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidTelegramAuthError,
    TelegramAlreadyLinkedError,
    TelegramConnectCompletion,
    TelegramConnectStart,
    WeakPasswordError,
)
from .booking import BookingService
from .catalog import CatalogService, start_catalog_refresh
from .credits import CreditService
from .history import HistoryOperation, HistoryService
from .notifications import AdminNotifier, NotificationReport, NotificationService
from .packages import PackageService, catalog_package
from .profile import (
    MAX_USER_NAME_LENGTH,
    ClientOverview,
    InvalidUserNameError,
    ProfileService,
    normalize_user_name,
)
from .schedule import ScheduleService
from .payments import (
    CheckoutFailedError,
    CheckoutResult,
    CheckoutUnavailableError,
    PaymentCheckResult,
    PaymentCheckStatus,
    PaymentService,
    RefundResult,
    RefundStatus,
)
from .support import (
    SupportMessageInvalidError,
    SupportRateLimitedError,
    SupportService,
    SupportSubmission,
)

__all__ = [
    "start_catalog_refresh",
    "AdminNotifier",
    "AdminService",
    "AuthService",
    "BookingService",
    "CatalogService",
    "CannotUnlinkOnlyLoginMethodError",
    "CheckoutFailedError",
    "CheckoutResult",
    "CheckoutUnavailableError",
    "ClientOverview",
    "CreditService",
    "CredentialsAlreadySetError",
    "EmailAlreadyRegisteredError",
    "HistoryOperation",
    "HistoryService",
    "InvalidCredentialsError",
    "InvalidTelegramAuthError",
    "InvalidUserNameError",
    "MAX_USER_NAME_LENGTH",
    "NotificationReport",
    "NotificationService",
    "PackageService",
    "PaymentCheckResult",
    "PaymentCheckStatus",
    "PaymentService",
    "ProfileService",
    "RefundResult",
    "RefundStatus",
    "ScheduleService",
    "SupportMessageInvalidError",
    "SupportRateLimitedError",
    "SupportService",
    "SupportSubmission",
    "TelegramAlreadyLinkedError",
    "TelegramConnectCompletion",
    "TelegramConnectStart",
    "WeakPasswordError",
    "catalog_package",
    "normalize_user_name",
]
