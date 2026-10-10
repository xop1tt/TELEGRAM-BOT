"""FastAPI-зависимости: репозиторий, сервисы, текущий пользователь."""

import hmac
import ipaddress
import logging
from dataclasses import dataclass
from typing import Any, Optional

from aiogram.exceptions import TelegramAPIError
from fastapi import Cookie, Depends, HTTPException, Request, status

from ..database.repository import STAFF_ADMIN, WebUserRecord
from ..runtime.security import AuthRateLimiter, SupportRateLimiter
from ..services import (
    AdminNotifier,
    AdminService,
    AuthService,
    BookingService,
    HistoryService,
    NotificationService,
    PackageService,
    PaymentService,
    ProfileService,
    ScheduleService,
    SupportService,
)
from .config import WebConfig
from .security import SESSION_COOKIE_NAME


logger = logging.getLogger("bot.api.dependencies")


def get_repository(request: Request) -> Any:
    return request.app.state.repository


def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


def get_bot(request: Request) -> Any:
    return request.app.state.bot


def get_support_limiter(request: Request) -> SupportRateLimiter:
    return request.app.state.support_limiter


def get_auth_limiter(request: Request) -> AuthRateLimiter:
    return request.app.state.auth_limiter


FRONTEND_SECRET_HEADER = "x-flamenco-frontend-secret"
FRONTEND_CLIENT_IP_HEADER = "x-flamenco-client-ip"


def client_ip(request: Request) -> str:
    # IP посетителя от сервера сайта принимается только с верным общим
    # секретом (WebConfig.FRONTEND_PROXY_SECRET): без него клиент подставил
    # бы любой адрес и обошёл rate limit входа.
    secret = WebConfig.FRONTEND_PROXY_SECRET
    provided = request.headers.get(FRONTEND_SECRET_HEADER, "")
    if secret and provided and hmac.compare_digest(provided.encode(), secret.encode()):
        try:
            return str(
                ipaddress.ip_address(
                    request.headers.get(FRONTEND_CLIENT_IP_HEADER, "").strip()
                )
            )
        except ValueError:
            pass
    return request.client.host if request.client else "unknown"


def get_payment_gateway(request: Request) -> Any:
    return request.app.state.payment_gateway


async def get_current_user(
    request: Request,
    session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> WebUserRecord:
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется вход")
    repository = get_repository(request)
    user_id = await repository.get_web_session_user_id(session)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Сессия недействительна")
    user = await repository.get_web_user_by_id(user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Аккаунт не найден")
    return user


async def get_optional_user(
    request: Request,
    session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Optional[WebUserRecord]:
    """Текущий пользователь или ``None`` (для эндпоинтов без обязательного входа)."""
    if session is None:
        return None
    repository = get_repository(request)
    user_id = await repository.get_web_session_user_id(session)
    if user_id is None:
        return None
    return await repository.get_web_user_by_id(user_id)


async def require_telegram_linked_user(
    current_user: WebUserRecord = Depends(get_current_user),
) -> WebUserRecord:
    """Бронирование, баланс занятий и поддержка в БД привязаны к telegram_id

    (``bot_users``), а не к веб-аккаунту напрямую — так уже устроена схема,
    которой пользуется и бот. Поэтому эти действия на сайте требуют
    привязанного Telegram, а не только входа по email.
    """
    if current_user.telegram_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Привяжите Telegram к аккаунту, чтобы использовать эту функцию",
        )
    return current_user


@dataclass(frozen=True)
class StaffActor:
    """Сотрудник студии, выполняющий запрос: Telegram ID и роль."""

    telegram_id: int
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == STAFF_ADMIN


async def require_staff(
    current_user: WebUserRecord = Depends(get_current_user),
) -> StaffActor:
    """Сотрудник (администратор или преподаватель): роль привязанного
    Telegram в staff_members. Проверка — здесь, на каждом ``/api/admin/*``;
    скрытая ссылка во фронтенде защитой не считается."""
    if current_user.role is None or current_user.telegram_id is None:
        logger.warning("Staff access denied user_id=%s", current_user.id)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав")
    return StaffActor(current_user.telegram_id, current_user.role)


async def require_admin(staff: StaffActor = Depends(require_staff)) -> StaffActor:
    """Только администратор: финансы, участники, настройки, сотрудники."""
    if not staff.is_admin:
        logger.warning("Admin access denied telegram_id=%s", staff.telegram_id)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав")
    return staff


async def require_admin_actor(admin: StaffActor = Depends(require_admin)) -> int:
    """Telegram ID администратора — журналы в БД ведутся по нему."""
    return admin.telegram_id


async def require_staff_actor(staff: StaffActor = Depends(require_staff)) -> int:
    """Telegram ID сотрудника (поддержка: отвечает и преподаватель)."""
    return staff.telegram_id


def get_admin_notifier(request: Request) -> AdminNotifier:
    return AdminNotifier(get_bot(request), get_repository(request))


def get_booking_service(request: Request) -> BookingService:
    return BookingService(get_repository(request), get_admin_notifier(request))


def get_support_service(request: Request) -> SupportService:
    return SupportService(
        get_repository(request),
        get_support_limiter(request),
        get_admin_notifier(request),
    )


def get_payment_service(request: Request) -> PaymentService:
    return PaymentService(get_repository(request), get_payment_gateway(request))


def get_profile_service(request: Request) -> ProfileService:
    return ProfileService(get_repository(request))


def get_package_service(request: Request) -> PackageService:
    return PackageService(get_repository(request))


def get_history_service(request: Request) -> HistoryService:
    return HistoryService(get_repository(request))


def get_notification_service(request: Request) -> NotificationService:
    return NotificationService(get_repository(request))


def get_schedule_service(request: Request) -> ScheduleService:
    return ScheduleService(get_repository(request))


def get_admin_service(request: Request) -> AdminService:
    return AdminService(get_repository(request))


async def get_bot_username(request: Request) -> str:
    """Имя бота для ссылки t.me/<бот>?start=… (BOT_USERNAME или getMe)."""
    if WebConfig.BOT_USERNAME:
        return WebConfig.BOT_USERNAME
    cached = getattr(request.app.state, "bot_username", None)
    if cached:
        return cached
    try:
        me = await get_bot(request).get_me()
    except (TelegramAPIError, OSError) as error:
        logger.error("Bot username lookup failed error_type=%s", type(error).__name__)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Вход через Telegram временно недоступен",
        ) from error
    if not me.username:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Вход через Telegram временно недоступен",
        )
    request.app.state.bot_username = me.username
    return me.username
