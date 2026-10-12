"""Модуль конфигурации"""

import os
from urllib.parse import urlsplit
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


load_dotenv()


def parse_database_pool_sizes(min_size: str, max_size: str) -> tuple[int, int]:
    try:
        minimum = int(min_size)
        maximum = int(max_size)
    except ValueError as error:
        raise ValueError(
            "DATABASE_POOL_MIN_SIZE и DATABASE_POOL_MAX_SIZE должны быть целыми числами"
        ) from error
    if minimum < 1 or maximum < minimum:
        raise ValueError(
            "Размер пула PostgreSQL должен удовлетворять "
            "1 <= DATABASE_POOL_MIN_SIZE <= DATABASE_POOL_MAX_SIZE"
        )
    return minimum, maximum


def parse_credit_adjustment_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as error:
        raise ValueError(
            "CREDIT_ADJUSTMENT_MAX_DELTA должен быть целым числом"
        ) from error
    # Начисление становится выдачей со сроком (миграция 018), а в одной
    # выдаче — не больше 100 занятий.
    if not 1 <= limit <= 100:
        raise ValueError("CREDIT_ADJUSTMENT_MAX_DELTA должен быть от 1 до 100")
    return limit


def parse_studio_timezone(value: str) -> ZoneInfo:
    name = value.strip() or "Europe/Moscow"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(
            "STUDIO_TIMEZONE должен быть IANA-именем часового пояса, "
            "например Europe/Moscow"
        ) from error


def parse_reminder_hours(value: str) -> int:
    """За сколько часов до занятия напомнить участнику; 0 — не напоминать."""
    try:
        hours = int(value)
    except ValueError as error:
        raise ValueError("LESSON_REMINDER_HOURS должен быть целым числом") from error
    if not 0 <= hours <= 72:
        raise ValueError("LESSON_REMINDER_HOURS должен быть от 0 до 72")
    return hours


def parse_bot_username(value: str) -> Optional[str]:
    username = value.strip().lstrip("@")
    if not username:
        return None
    if not username.replace("_", "").isalnum() or not 5 <= len(username) <= 32:
        raise ValueError("BOT_USERNAME должен быть именем бота, например mirada_bot")
    return username


def parse_website_url(value: str, env: str) -> Optional[str]:
    url = value.strip().rstrip("/")
    if not url:
        return None
    parsed = urlsplit(url)
    local_http = (
        env.strip().lower() == "development"
        and parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1"}
    )
    if (parsed.scheme != "https" and not local_http) or not parsed.hostname:
        raise ValueError("WEBSITE_URL должен быть https-адресом сайта")
    return url


def parse_flag(name: str, value: str, default: bool) -> bool:
    text = value.strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError("{} должен быть 1/0 (true/false)".format(name))


class Config:
    """Конфигурация бота"""

    # Токен бота
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()

    # Окружение
    ENV: str = os.getenv("ENV", "development").strip().lower()
    if ENV not in {"development", "production"}:
        raise ValueError("ENV должен быть development или production")

    # PostgreSQL: строка подключения передается только через окружение.
    DATABASE_URL: str = os.getenv("DATABASE_URL", "").strip()
    DATABASE_SSL_CA: Optional[str] = os.getenv("DATABASE_SSL_CA", "").strip() or None
    DATABASE_SSL_MODE: str = (
        os.getenv("DATABASE_SSL_MODE", "verify-full").strip().lower()
    )
    if DATABASE_SSL_MODE not in {"verify-full", "disable"}:
        raise ValueError("DATABASE_SSL_MODE должен быть verify-full или disable")
    if ENV == "production" and DATABASE_SSL_MODE != "verify-full":
        raise ValueError("В production DATABASE_SSL_MODE должен быть verify-full")
    DATABASE_POOL_MIN_SIZE, DATABASE_POOL_MAX_SIZE = parse_database_pool_sizes(
        os.getenv("DATABASE_POOL_MIN_SIZE", "1"),
        os.getenv("DATABASE_POOL_MAX_SIZE", "10"),
    )
    YOOKASSA_SHOP_ID: str = os.getenv("YOOKASSA_SHOP_ID", "").strip()
    YOOKASSA_SECRET_KEY: str = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
    YOOKASSA_RETURN_URL: str = os.getenv(
        "YOOKASSA_RETURN_URL",
        "https://t.me/",
    ).strip()

    # Максимум занятий за одну ручную корректировку баланса (в любую сторону).
    # Защита от опечатки администратора, а не бизнес-правило студии.
    CREDIT_ADJUSTMENT_MAX_DELTA: int = parse_credit_adjustment_limit(
        os.getenv("CREDIT_ADJUSTMENT_MAX_DELTA", "50")
    )

    # Часовой пояс студии: в нём бот показывает время занятий, сроки отмены
    # и уведомления. Сайт читает ту же переменную (WEBSITE,
    # src/lib/format.ts) с тем же значением по умолчанию — один и тот же
    # момент из PostgreSQL выглядит одинаково в обоих интерфейсах.
    STUDIO_TIMEZONE: ZoneInfo = parse_studio_timezone(
        os.getenv("STUDIO_TIMEZONE", "Europe/Moscow")
    )

    # Публичный адрес сайта студии — бот ссылается на него в «О студии»,
    # «Профиле» и «Абонементах». Необязателен: без него бот просто не
    # показывает ссылку.
    WEBSITE_URL: Optional[str] = parse_website_url(
        os.getenv("WEBSITE_URL", ""), os.getenv("ENV", "development")
    )

    # Напоминание о занятии: за столько часов до начала (0 — выключено).
    # Приходит только тем, кто записался раньше этого срока.
    # Ежедневный плановый перезапуск бота в полночь по времени студии (за час
    # админам уходит предупреждение). В development перезапуск делает сам
    # процесс (exec), в production процесс завершается, а контейнер поднимает
    # Docker (restart: unless-stopped) или платформа (Render).
    DAILY_RESTART: bool = parse_flag(
        "BOT_DAILY_RESTART", os.getenv("BOT_DAILY_RESTART", ""), True
    )
    LESSON_REMINDER_HOURS: int = parse_reminder_hours(
        os.getenv("LESSON_REMINDER_HOURS", "3")
    )

    # Имя бота для ссылки входа на сайт через бота (t.me/<имя>?start=…).
    # Необязательно: без него API один раз спрашивает имя у Telegram (getMe).
    BOT_USERNAME: Optional[str] = parse_bot_username(os.getenv("BOT_USERNAME", ""))

    # Если токен не задан - ошибка
    if not BOT_TOKEN:
        raise ValueError("⚠️ BOT_TOKEN не указан! Получи его от @BotFather")
