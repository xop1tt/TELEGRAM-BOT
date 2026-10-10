"""Серверные команды прав сотрудников (не доступны через API).

    python -m flamenco_bot.api.manage grant-admin --telegram-id 123456789
        Делает человека администратором студии (staff_members). Нужна, когда
        администраторов ещё нет или доступ потерян; остальных сотрудников
        назначают в админке сайта («Сотрудники»). Профиль бота создаётся,
        если человек ещё не открывал бота (--name — имя профиля).

    python -m flamenco_bot.api.manage revoke-staff --telegram-id 123456789
        Снимает роль сотрудника. Последнего администратора снять нельзя.

Права действуют и в боте, и на сайте: на сайте — для аккаунта, к которому
привязан этот Telegram. Использует тот же .env и ту же БД, что API и бот
(запускать из папки проекта, PYTHONPATH=src).
"""

import argparse
import asyncio

from ..database import PostgresRepository, is_database_configured
from ..database.staff_postgres import LastAdminError
from .config import WebConfig


async def _connect():
    if not is_database_configured(WebConfig.DATABASE_URL):
        raise SystemExit("DATABASE_URL не задан — права некуда сохранить")
    repository = await PostgresRepository.connect(
        WebConfig.DATABASE_URL,
        ssl_ca_path=WebConfig.DATABASE_SSL_CA,
        pool_min_size=1,
        pool_max_size=2,
        ssl_mode=WebConfig.DATABASE_SSL_MODE,
        allow_insecure_local=WebConfig.ENV == "development",
    )
    # Схема должна быть не старее нужной коду (миграции — в DATABASE).
    await repository.initialize()
    return repository


async def grant_admin(telegram_id: int, name: str) -> None:
    repository = await _connect()
    try:
        member = await repository.bootstrap_admin(telegram_id, name)
    finally:
        await repository.close()
    print(
        "Администратор студии: {} (Telegram ID {})".format(
            member.user_name, member.telegram_id
        )
    )


async def revoke_staff(telegram_id: int) -> None:
    repository = await _connect()
    try:
        removed = await repository.revoke_staff_from_server(telegram_id)
    except LastAdminError as error:
        raise SystemExit(str(error)) from error
    finally:
        await repository.close()
    if not removed:
        raise SystemExit("Сотрудника с таким Telegram ID нет")
    print("Роль сотрудника снята: Telegram ID {}".format(telegram_id))


def _telegram_id(value: str) -> int:
    if not value.isdigit() or int(value) <= 0:
        raise argparse.ArgumentTypeError("Telegram ID — положительное целое число")
    return int(value)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m flamenco_bot.api.manage")
    commands = parser.add_subparsers(dest="command", required=True)
    grant = commands.add_parser("grant-admin", help="назначить администратора")
    grant.add_argument("--telegram-id", type=_telegram_id, required=True)
    grant.add_argument("--name", default="Администратор")
    revoke = commands.add_parser("revoke-staff", help="снять роль сотрудника")
    revoke.add_argument("--telegram-id", type=_telegram_id, required=True)
    args = parser.parse_args(argv)

    if args.command == "grant-admin":
        asyncio.run(grant_admin(args.telegram_id, args.name))
    else:
        asyncio.run(revoke_staff(args.telegram_id))


__all__ = ["main"]

if __name__ == "__main__":
    main()
