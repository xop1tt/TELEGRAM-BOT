"""Вход на сайт по email/паролю или через привязанный Telegram.

Используется только веб-интерфейсом: Telegram-бот продолжает опознавать
пользователей по telegram_id через ``bot_users``/``get_or_create_profile``
и не зависит от этого модуля.

Веб-аккаунт (``users``) не владеет бизнес-данными бота — он лишь хранит,
какой telegram_id сейчас привязан. Смена привязки — это обновление одной
строки в ``users``; история платежей/записей остаётся там, где её ведёт бот
(под тем telegram_id, который привязан в данный момент).
"""

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from ..database.repository import (
    EmailAlreadyRegisteredError,
    TelegramAlreadyLinkedError,
    WebUserRecord,
)
from ..database.studio_models import (
    TELEGRAM_CONNECT_TTL,
    TelegramConnectError,
    TelegramConnectRequest,
)


logger = logging.getLogger("bot.services.auth")

MIN_PASSWORD_LENGTH = 8
# Верхняя граница — чтобы PBKDF2 не считал хеш от мегабайтной строки.
MAX_PASSWORD_LENGTH = 256
_TELEGRAM_AUTH_MAX_AGE_SECONDS = 24 * 60 * 60
# PBKDF2-HMAC-SHA256: доступен в любой сборке hashlib (в отличие от scrypt,
# который на некоторых платформах требует OpenSSL с поддержкой scrypt).
# Итерации — по рекомендации OWASP (2023) для PBKDF2-SHA256.
_PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16
_DERIVED_KEY_LENGTH = 32


class WeakPasswordError(ValueError):
    pass


class InvalidCredentialsError(ValueError):
    pass


class InvalidTelegramAuthError(ValueError):
    """Подпись или срок действия данных Telegram Login Widget не прошли проверку."""


class CannotUnlinkOnlyLoginMethodError(ValueError):
    """Нельзя отвязать Telegram, если это единственный способ входа в аккаунт."""


class CredentialsAlreadySetError(ValueError):
    """У аккаунта уже есть email и пароль."""


# Токен в deep link: t.me/<бот>?start=c_<token>. Telegram допускает в
# параметре start до 64 символов [A-Za-z0-9_-].
CONNECT_START_PREFIX = "c_"
CONNECT_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32}$")


@dataclass(frozen=True)
class TelegramConnectStart:
    """Новый запрос входа/привязки через бота.

    ``browser_secret`` уходит только в httpOnly-cookie браузера, ``token`` —
    в ссылку на бота; в БД хранятся их хеши.
    """

    token: str
    browser_secret: str
    deep_link: str
    expires_at: datetime


@dataclass(frozen=True)
class TelegramConnectCompletion:
    # pending — ждём подтверждения в боте; completed — можно выдать сессию
    # (только один раз); rejected — участник отказался; expired — ссылка
    # устарела; used — запрос уже использован; unknown — нет такого запроса.
    status: str
    user: Optional[WebUserRecord] = None


def _normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    local, _, domain = normalized.partition("@")
    if not local or "." not in domain or len(normalized) > 254 or " " in normalized:
        raise ValueError("Укажите корректный email")
    return normalized


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(
            "Пароль должен содержать минимум {} символов".format(MIN_PASSWORD_LENGTH)
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPasswordError(
            "Пароль должен быть не длиннее {} символов".format(MAX_PASSWORD_LENGTH)
        )


def mask_email(email: Optional[str]) -> str:
    """«a***@example.org» — бот показывает, к какому аккаунту привязка."""
    if not email or "@" not in email:
        return "аккаунт сайта"
    name, domain = email.split("@", 1)
    return "{}***@{}".format(name[:1], domain)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
        dklen=_DERIVED_KEY_LENGTH,
    )
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS, salt.hex(), derived.hex()
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations, salt_hex, derived_hex = stored_hash.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(derived_hex)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        int(iterations),
        dklen=len(expected),
    )
    return hmac.compare_digest(candidate, expected)


def verify_telegram_login(
    payload: Mapping[str, Any],
    bot_token: str,
    max_age_seconds: int = _TELEGRAM_AUTH_MAX_AGE_SECONDS,
) -> int:
    """Проверяет подпись Telegram Login Widget и возвращает telegram_id.

    См. https://core.telegram.org/widgets/login#checking-authorization
    """
    data = dict(payload)
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise InvalidTelegramAuthError("Отсутствует подпись Telegram")

    check_string = "\n".join(
        "{}={}".format(key, data[key]) for key in sorted(data) if data[key] is not None
    )
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    computed_hash = hmac.new(
        secret_key, check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(computed_hash, str(received_hash)):
        raise InvalidTelegramAuthError("Подпись Telegram не прошла проверку")

    try:
        auth_timestamp = int(data["auth_date"])
    except (KeyError, TypeError, ValueError) as error:
        raise InvalidTelegramAuthError(
            "Некорректная дата авторизации Telegram"
        ) from error
    if time.time() - auth_timestamp > max_age_seconds:
        raise InvalidTelegramAuthError("Срок действия входа через Telegram истёк")

    try:
        telegram_id = int(data["id"])
    except (KeyError, TypeError, ValueError) as error:
        raise InvalidTelegramAuthError("Отсутствует Telegram ID") from error
    if telegram_id <= 0:
        raise InvalidTelegramAuthError("Некорректный Telegram ID")
    return telegram_id


def _telegram_display_name(payload: Mapping[str, Any]) -> str:
    first_name = str(payload.get("first_name") or "").strip()
    last_name = str(payload.get("last_name") or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part)
    return full_name or "Участник студии"


class AuthService:
    def __init__(self, repository: Any, bot_token: str) -> None:
        self.repository = repository
        self.bot_token = bot_token

    async def register_with_email(
        self,
        email: str,
        password: str,
        display_name: str,
    ) -> WebUserRecord:
        normalized_email = _normalize_email(email)
        _check_password(password)
        normalized_name = display_name.strip() or "Участник студии"
        # PBKDF2 (600k итераций) — сотни миллисекунд CPU: в отдельном потоке,
        # чтобы не останавливать event loop всего API.
        password_hash = await asyncio.to_thread(hash_password, password)
        user = await self.repository.create_web_user(
            normalized_email, password_hash, normalized_name
        )
        logger.info("Registered web user user_id=%s", user.id)
        return user

    async def set_email_credentials(
        self,
        user_id: int,
        email: str,
        password: str,
    ) -> WebUserRecord:
        """Email и пароль для аккаунта, созданного входом через Telegram:
        дальше можно входить и так, и так — это тот же аккаунт.

        ``EmailAlreadyRegisteredError`` — email занят другим аккаунтом;
        ``CredentialsAlreadySetError`` — у аккаунта уже есть email.
        """
        normalized_email = _normalize_email(email)
        _check_password(password)
        password_hash = await asyncio.to_thread(hash_password, password)
        if not await self.repository.set_web_user_credentials(
            user_id, normalized_email, password_hash
        ):
            raise CredentialsAlreadySetError("У аккаунта уже есть email и пароль")
        logger.info("Set email credentials user_id=%s", user_id)
        updated = await self.repository.get_web_user_by_id(user_id)
        if updated is None:
            raise LookupError("Аккаунт не найден")
        return updated

    async def authenticate_with_email(
        self,
        email: str,
        password: str,
    ) -> WebUserRecord:
        user = await self.repository.get_web_user_by_email(email.strip().lower())
        if (
            user is None
            or user.password_hash is None
            or not await asyncio.to_thread(
                verify_password, password, user.password_hash
            )
        ):
            logger.warning("Email authentication failed email_present=%s", bool(email))
            raise InvalidCredentialsError("Неверный email или пароль")
        return user

    async def login_with_telegram(self, payload: Mapping[str, Any]) -> WebUserRecord:
        telegram_id = verify_telegram_login(payload, self.bot_token)
        display_name = _telegram_display_name(payload)
        # Гарантируем, что у Telegram-идентичности есть профиль бота: та же
        # запись bot_users, которую видит и использует сам бот.
        await self.repository.get_or_create_profile(
            telegram_id=telegram_id,
            user_name=display_name,
        )
        user = await self.repository.get_or_create_web_user_from_telegram(
            telegram_id, display_name
        )
        logger.info("Telegram login succeeded user_id=%s", user.id)
        # Перечитываем: имя участника — из профиля бота (его могли изменить
        # в боте после первого входа), а не из payload виджета.
        return await self.repository.get_web_user_by_id(user.id) or user

    async def link_telegram(
        self,
        user_id: int,
        payload: Mapping[str, Any],
    ) -> WebUserRecord:
        telegram_id = verify_telegram_login(payload, self.bot_token)
        display_name = _telegram_display_name(payload)
        await self.repository.get_or_create_profile(
            telegram_id=telegram_id,
            user_name=display_name,
        )
        try:
            # Аккаунт, созданный только входом через Telegram (без email),
            # объединяется с этим — у участника остаётся один аккаунт.
            await self.repository.link_telegram_to_web_user(user_id, telegram_id)
        except TelegramAlreadyLinkedError:
            logger.warning("Telegram link rejected: already linked user_id=%s", user_id)
            raise
        logger.info("Linked Telegram to web account user_id=%s", user_id)
        updated = await self.repository.get_web_user_by_id(user_id)
        if updated is None:
            raise LookupError("Аккаунт не найден")
        return updated

    async def unlink_telegram(self, user_id: int) -> WebUserRecord:
        user = await self.repository.get_web_user_by_id(user_id)
        if user is None:
            raise LookupError("Аккаунт не найден")
        if user.email is None:
            raise CannotUnlinkOnlyLoginMethodError(
                "Нельзя отвязать Telegram: это единственный способ входа "
                "в аккаунт. Сначала задайте email и пароль."
            )
        await self.repository.set_web_user_telegram_id(user_id, None)
        logger.info("Unlinked Telegram from web account user_id=%s", user_id)
        updated = await self.repository.get_web_user_by_id(user_id)
        if updated is None:
            raise LookupError("Аккаунт не найден")
        return updated

    # ---------- вход и привязка через бота (deep link) ----------

    async def start_telegram_connect(
        self,
        bot_username: str,
        web_user_id: Optional[int] = None,
    ) -> TelegramConnectStart:
        """Создаёт запрос: вход (без аккаунта) или привязка (``web_user_id``)."""
        token = secrets.token_urlsafe(24)
        browser_secret = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + TELEGRAM_CONNECT_TTL
        purpose = "link" if web_user_id is not None else "login"
        await self.repository.create_telegram_connect_request(
            token, browser_secret, purpose, web_user_id, expires_at
        )
        logger.info(
            "Telegram connect started purpose=%s user_id=%s", purpose, web_user_id
        )
        return TelegramConnectStart(
            token=token,
            browser_secret=browser_secret,
            deep_link="https://t.me/{}?start={}{}".format(
                bot_username, CONNECT_START_PREFIX, token
            ),
            expires_at=expires_at,
        )

    async def describe_telegram_connect(self, token: str) -> TelegramConnectRequest:
        """Запрос для экрана подтверждения в боте; ``TelegramConnectError``."""
        if not CONNECT_TOKEN_PATTERN.fullmatch(token):
            raise TelegramConnectError("Ссылка для входа недействительна")
        request = await self.repository.get_telegram_connect_request(token)
        if (
            request is None
            or request.status != "pending"
            or request.expires_at <= datetime.now(timezone.utc)
        ):
            raise TelegramConnectError("Ссылка для входа недействительна или устарела")
        return request

    async def confirm_telegram_connect(
        self,
        token: str,
        telegram_id: int,
        display_name: str,
    ) -> TelegramConnectRequest:
        """Подтверждение кнопкой в боте: Telegram ID берётся из апдейта
        Telegram (его подлинность гарантирует сам Telegram), а не из сайта."""
        if not CONNECT_TOKEN_PATTERN.fullmatch(token):
            raise TelegramConnectError("Ссылка для входа недействительна")
        await self.repository.get_or_create_profile(
            telegram_id=telegram_id,
            user_name=display_name.strip() or "Участник студии",
        )
        request = await self.repository.confirm_telegram_connect_request(
            token, telegram_id
        )
        logger.info(
            "Telegram connect confirmed purpose=%s telegram_id=%s",
            request.purpose,
            telegram_id,
        )
        return request

    async def reject_telegram_connect(self, token: str) -> bool:
        if not CONNECT_TOKEN_PATTERN.fullmatch(token):
            return False
        return await self.repository.reject_telegram_connect_request(token)

    async def complete_telegram_connect(
        self, browser_secret: str
    ) -> TelegramConnectCompletion:
        """Браузер с секретом из cookie забирает подтверждённый запрос."""
        request, consumed_now = await self.repository.consume_telegram_connect_request(
            browser_secret
        )
        if request is None:
            return TelegramConnectCompletion("unknown")
        if not consumed_now:
            if request.status == "pending":
                expired = request.expires_at <= datetime.now(timezone.utc)
                return TelegramConnectCompletion("expired" if expired else "pending")
            if request.status == "confirmed":
                return TelegramConnectCompletion("expired")
            if request.status == "rejected":
                return TelegramConnectCompletion("rejected")
            return TelegramConnectCompletion("used")
        if request.purpose == "link":
            user = await self.repository.get_web_user_by_id(request.web_user_id)
        else:
            profile = await self.repository.get_profile(request.telegram_id)
            user = await self.repository.get_or_create_web_user_from_telegram(
                request.telegram_id,
                profile.user_name if profile else "Участник студии",
            )
            user = await self.repository.get_web_user_by_id(user.id) or user
        if user is None:
            return TelegramConnectCompletion("unknown")
        logger.info(
            "Telegram connect completed purpose=%s user_id=%s",
            request.purpose,
            user.id,
        )
        return TelegramConnectCompletion("completed", user)


__all__ = [
    "AuthService",
    "CannotUnlinkOnlyLoginMethodError",
    "CredentialsAlreadySetError",
    "EmailAlreadyRegisteredError",
    "InvalidCredentialsError",
    "InvalidTelegramAuthError",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "TelegramAlreadyLinkedError",
    "TelegramConnectCompletion",
    "TelegramConnectStart",
    "mask_email",
    "WeakPasswordError",
    "hash_password",
    "verify_password",
    "verify_telegram_login",
]
