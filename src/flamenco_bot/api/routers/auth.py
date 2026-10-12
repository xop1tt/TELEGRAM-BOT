"""Регистрация, вход и управление привязкой Telegram: ``/api/auth/*``.

Эндпоинты — тонкая обёртка над ``services.auth.AuthService``: валидируют
вход, вызывают сервис, превращают его исключения в HTTP-ответы. Вся бизнес-
логика (проверка пароля, подписи Telegram, уникальности) — в сервисе, а не
здесь, чтобы её не продублировать для бота и сайта.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status

from ...runtime.security import AuthRateLimiter
from ...services import (
    AuthService,
    CannotUnlinkOnlyLoginMethodError,
    CredentialsAlreadySetError,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidTelegramAuthError,
    TelegramAlreadyLinkedError,
    WeakPasswordError,
)
from ...database.repository import WebUserRecord
from ...database.studio_models import TELEGRAM_CONNECT_TTL
from ..dependencies import (
    client_ip,
    get_auth_limiter,
    get_auth_service,
    get_bot_username,
    get_current_user,
    get_optional_user,
    get_repository,
)
from ..schemas import (
    LoginRequest,
    RegisterRequest,
    SetCredentialsRequest,
    TelegramAuthRequest,
    TelegramConnectRequestBody,
    TelegramConnectStartResponse,
    TelegramConnectStatusResponse,
    UserResponse,
)
from ..security import (
    SESSION_COOKIE_NAME,
    SESSION_TOKEN_TTL_SECONDS,
    generate_session_token,
)

# Секрет браузера для входа через бота: только в httpOnly-cookie и только
# для эндпоинтов /api/auth/telegram/connect*.
CONNECT_COOKIE_NAME = "tg_connect"
CONNECT_COOKIE_PATH = "/api/auth/telegram/connect"


def _enforce_auth_rate_limit(ip: str, limiter: AuthRateLimiter) -> None:
    if not limiter.allow(ip):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Слишком много попыток, повторите позже",
        )


logger = logging.getLogger("bot.api.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


async def _set_session_cookie(
    response: Response,
    user_id: int,
    repository: Any,
) -> None:
    token = generate_session_token()
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=SESSION_TOKEN_TTL_SECONDS
    )
    await repository.create_web_session(user_id, token, expires_at)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TOKEN_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    payload: RegisterRequest,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
    repository: Any = Depends(get_repository),
    ip: str = Depends(client_ip),
    limiter: AuthRateLimiter = Depends(get_auth_limiter),
) -> UserResponse:
    _enforce_auth_rate_limit(ip, limiter)
    try:
        user = await auth_service.register_with_email(
            payload.email, payload.password, payload.display_name
        )
    except EmailAlreadyRegisteredError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except WeakPasswordError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from error
    except ValueError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from error
    await _set_session_cookie(response, user.id, repository)
    return UserResponse.from_record(user)


@router.post("/login", response_model=UserResponse)
async def login(
    payload: LoginRequest,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
    repository: Any = Depends(get_repository),
    ip: str = Depends(client_ip),
    limiter: AuthRateLimiter = Depends(get_auth_limiter),
) -> UserResponse:
    _enforce_auth_rate_limit(ip, limiter)
    try:
        user = await auth_service.authenticate_with_email(
            payload.email, payload.password
        )
    except InvalidCredentialsError as error:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(error)) from error
    await _set_session_cookie(response, user.id, repository)
    return UserResponse.from_record(user)


@router.post("/telegram", response_model=UserResponse)
async def login_with_telegram(
    payload: TelegramAuthRequest,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
    repository: Any = Depends(get_repository),
    ip: str = Depends(client_ip),
    limiter: AuthRateLimiter = Depends(get_auth_limiter),
) -> UserResponse:
    _enforce_auth_rate_limit(ip, limiter)
    try:
        user = await auth_service.login_with_telegram(payload.to_payload())
    except InvalidTelegramAuthError as error:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(error)) from error
    await _set_session_cookie(response, user.id, repository)
    return UserResponse.from_record(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    repository: Any = Depends(get_repository),
) -> None:
    if session is not None:
        await repository.delete_web_session(session)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


@router.get("/me", response_model=UserResponse)
async def read_current_user(
    current_user: WebUserRecord = Depends(get_current_user),
) -> UserResponse:
    return UserResponse.from_record(current_user)


@router.post("/me/credentials", response_model=UserResponse)
async def set_credentials(
    payload: SetCredentialsRequest,
    current_user: WebUserRecord = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
    ip: str = Depends(client_ip),
    limiter: AuthRateLimiter = Depends(get_auth_limiter),
) -> UserResponse:
    """Email и пароль для аккаунта, созданного входом через Telegram.

    Дальше в тот же аккаунт можно входить и по email — второго аккаунта
    не появляется. Если email уже занят другим аккаунтом — 409: нужно
    войти в тот аккаунт и привязать Telegram там (аккаунты объединятся).
    """
    _enforce_auth_rate_limit(ip, limiter)
    try:
        user = await auth_service.set_email_credentials(
            current_user.id, payload.email, payload.password
        )
    except EmailAlreadyRegisteredError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Этот email уже зарегистрирован. Войдите по нему и привяжите "
            "Telegram в личном кабинете — аккаунты объединятся.",
        ) from error
    except CredentialsAlreadySetError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from error
    return UserResponse.from_record(user)


@router.post("/me/telegram", response_model=UserResponse)
async def link_telegram(
    payload: TelegramAuthRequest,
    current_user: WebUserRecord = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> UserResponse:
    try:
        user = await auth_service.link_telegram(current_user.id, payload.to_payload())
    except InvalidTelegramAuthError as error:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(error)) from error
    except TelegramAlreadyLinkedError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return UserResponse.from_record(user)


@router.delete("/me/telegram", response_model=UserResponse)
async def unlink_telegram(
    current_user: WebUserRecord = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> UserResponse:
    try:
        user = await auth_service.unlink_telegram(current_user.id)
    except CannotUnlinkOnlyLoginMethodError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return UserResponse.from_record(user)


@router.post(
    "/telegram/connect",
    response_model=TelegramConnectStartResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_telegram_connect(
    payload: TelegramConnectRequestBody,
    response: Response,
    current_user: Optional[WebUserRecord] = Depends(get_optional_user),
    auth_service: AuthService = Depends(get_auth_service),
    bot_username: str = Depends(get_bot_username),
    ip: str = Depends(client_ip),
    limiter: AuthRateLimiter = Depends(get_auth_limiter),
) -> TelegramConnectStartResponse:
    """Вход (``login``) или привязка Telegram (``link``) через бота.

    Возвращает ссылку на бота; браузер получает секрет в httpOnly-cookie.
    Участник подтверждает действие кнопкой в боте, а сайт опрашивает
    ``/telegram/connect/complete`` — сессию получит только этот браузер.
    """
    _enforce_auth_rate_limit(ip, limiter)
    if payload.purpose == "link" and current_user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется вход")
    started = await auth_service.start_telegram_connect(
        bot_username,
        web_user_id=current_user.id if payload.purpose == "link" else None,
    )
    # Секрет браузера обязан попасть в браузер — иначе он не докажет, что это
    # тот же браузер, что начал вход. Одноразовое случайное значение в
    # httpOnly+Secure-cookie; в БД хранится только его хэш (secret_hash).
    response.set_cookie(
        CONNECT_COOKIE_NAME,
        started.browser_secret,
        max_age=int(TELEGRAM_CONNECT_TTL.total_seconds()) + 300,
        httponly=True,
        secure=True,
        samesite="lax",
        path=CONNECT_COOKIE_PATH,
    )
    return TelegramConnectStartResponse(
        deep_link=started.deep_link, expires_at=started.expires_at
    )


@router.post("/telegram/connect/complete", response_model=TelegramConnectStatusResponse)
async def complete_telegram_connect(
    response: Response,
    connect_secret: Optional[str] = Cookie(default=None, alias=CONNECT_COOKIE_NAME),
    auth_service: AuthService = Depends(get_auth_service),
    repository: Any = Depends(get_repository),
) -> TelegramConnectStatusResponse:
    """Опрос состояния входа через бота; при подтверждении — выдаёт сессию.

    Сессия выдаётся ровно один раз (повторный опрос получает ``used``).
    """
    if not connect_secret:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Нет активного входа через бота"
        )
    completion = await auth_service.complete_telegram_connect(connect_secret)
    if completion.status == "unknown":
        response.delete_cookie(CONNECT_COOKIE_NAME, path=CONNECT_COOKIE_PATH)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запрос входа не найден")
    if completion.status == "pending":
        return TelegramConnectStatusResponse(status="pending")
    response.delete_cookie(CONNECT_COOKIE_NAME, path=CONNECT_COOKIE_PATH)
    if completion.status != "completed" or completion.user is None:
        return TelegramConnectStatusResponse(status=completion.status)
    await _set_session_cookie(response, completion.user.id, repository)
    return TelegramConnectStatusResponse(
        status="completed", user=UserResponse.from_record(completion.user)
    )
