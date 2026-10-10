"""Настоящий PostgreSQL для unit-тестов вместо хранилища в памяти.

Схема с миграциями DATABASE создаётся один раз на процесс; перед каждым
тестом все таблицы очищаются, а справочные строки, которые кладут сами
миграции (направления, пакеты, настройки), восстанавливаются из снимка.
Так тест получает ту же свежую базу, что после миграций, за десятки
миллисекунд, а не за полное применение миграций.

Нужен ``TEST_DATABASE_URL`` (в CI — сервис postgres, см.
.github/workflows); без него такие тесты пропускаются.
"""

import os
import time
import unittest
import uuid
from typing import Any, Optional

import anyio.from_thread
import asyncpg
from fastapi.testclient import TestClient

from flamenco_bot.database.repository import PostgresRepository
from flamenco_bot.services.catalog import CatalogService
from tests.support import apply_migrations

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
SKIP_REASON = "TEST_DATABASE_URL is not set; configure a dedicated test PostgreSQL"

_schema: Optional[str] = None
_tables: list[str] = []
_seeded: list[str] = []


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _drop_stale_schemas(connection: Any) -> None:
    """Схемы прошлых прогонов, чей процесс уже завершился.

    Убирать свою схему при выходе ненадёжно: в atexit asyncio уже не может
    резолвить адрес базы. Поэтому каждый прогон убирает чужие осиротевшие.
    """
    for record in await connection.fetch(
        "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'test\\_unit\\_%'"
    ):
        name = record["nspname"]
        parts = name.split("_")
        if len(parts) >= 3 and parts[2].isdigit() and not _process_alive(int(parts[2])):
            await connection.execute('DROP SCHEMA IF EXISTS "{}" CASCADE'.format(name))


async def _prepare_schema() -> None:
    global _schema, _tables, _seeded
    schema = "test_unit_{}_{}".format(os.getpid(), time.time_ns())
    connection = await asyncpg.connect(DATABASE_URL)
    try:
        await _drop_stale_schemas(connection)
        await connection.execute('CREATE SCHEMA "{}"'.format(schema))
        await connection.execute('SET search_path TO "{}"'.format(schema))
        pool = _SingleConnectionPool(connection)
        await apply_migrations(pool)
        tables = [
            record["tablename"]
            for record in await connection.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = $1 "
                "AND tablename <> 'schema_migrations' "
                "AND tablename NOT LIKE '\\_seed\\_%'",
                schema,
            )
        ]
        seeded = []
        for table in tables:
            if await connection.fetchval(
                'SELECT EXISTS (SELECT 1 FROM "{}")'.format(table)
            ):
                await connection.execute(
                    'CREATE TABLE "_seed_{0}" AS SELECT * FROM "{0}"'.format(table)
                )
                seeded.append(table)
    finally:
        await connection.close()
    _schema, _tables, _seeded = schema, tables, seeded


class _SingleConnectionPool:
    """Минимальный «пул» для apply_migrations поверх одного соединения."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def acquire(self) -> "_SingleConnectionPool":
        return self

    async def __aenter__(self) -> Any:
        return self._connection

    async def __aexit__(self, *exc: Any) -> None:
        return None


async def fresh_repository() -> PostgresRepository:
    """Репозиторий на пустой (только что мигрированной) базе.

    Закрывать — ``await repository.close()`` (делает ``PostgresTestCase``).
    """
    if _schema is None:
        await _prepare_schema()
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        server_settings={"search_path": _schema},
    )
    async with pool.acquire() as connection:
        await connection.execute(
            "TRUNCATE {} RESTART IDENTITY CASCADE".format(
                ", ".join('"{}"'.format(table) for table in _tables)
            )
        )
        for table in _seeded:
            await connection.execute(
                'INSERT INTO "{0}" SELECT * FROM "_seed_{0}"'.format(table)
            )
    repository = PostgresRepository(pool)
    await repository.initialize()
    # Кэш каталога и правил общий на процесс: возвращаем его к состоянию
    # свежей базы, чтобы тест, менявший каталог, не влиял на следующие.
    await CatalogService(repository).refresh()
    return repository


async def sql(repository: PostgresRepository, query: str, *args: Any) -> str:
    async with repository._pool.acquire() as connection:
        return await connection.execute(query, *args)


async def fetchval(repository: PostgresRepository, query: str, *args: Any) -> Any:
    async with repository._pool.acquire() as connection:
        return await connection.fetchval(query, *args)


async def fetch_all(repository: PostgresRepository, query: str, *args: Any) -> list:
    async with repository._pool.acquire() as connection:
        return await connection.fetch(query, *args)


async def fetchrow(repository: PostgresRepository, query: str, *args: Any) -> Any:
    async with repository._pool.acquire() as connection:
        return await connection.fetchrow(query, *args)


async def make_admin(
    repository: PostgresRepository, telegram_id: int, name: str = "Админ"
) -> None:
    """Профиль бота с правами администратора студии."""
    await repository.get_or_create_profile(telegram_id, name)
    await sql(
        repository,
        "INSERT INTO staff_members (telegram_id, role) VALUES ($1, 'admin') "
        "ON CONFLICT (telegram_id) DO UPDATE SET role = 'admin'",
        telegram_id,
    )


async def set_credits(
    repository: PostgresRepository, telegram_id: int, credits: int
) -> None:
    """Выставляет баланс занятий через журнал (сумма журнала = баланс)."""
    current = await repository.get_lesson_credits(telegram_id)
    delta = credits - current
    if delta == 0:
        return
    async with repository._pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "INSERT INTO lesson_credit_ledger "
                "(telegram_id, entry_type, delta, reference_key) "
                "VALUES ($1, 'adjustment', $2, $3)",
                telegram_id,
                delta,
                "test:{}".format(uuid.uuid4()),
            )
            await connection.execute(
                "UPDATE bot_users SET lesson_credits = $2 WHERE telegram_id = $1",
                telegram_id,
                credits,
            )


@unittest.skipUnless(DATABASE_URL, SKIP_REASON)
class PostgresTestCase(unittest.IsolatedAsyncioTestCase):
    """``self.repository`` — свежая база на каждый тест."""

    async def asyncSetUp(self) -> None:
        self.repository = await fresh_repository()

    async def asyncTearDown(self) -> None:
        await self.repository.close()


@unittest.skipUnless(DATABASE_URL, SKIP_REASON)
class PostgresApiTestCase(unittest.TestCase):
    """Синхронные тесты API через ``TestClient`` поверх свежей базы.

    Пул asyncpg привязан к циклу событий, а ``TestClient`` без контекстного
    менеджера заводит новый цикл на каждый запрос. Поэтому репозиторий и все
    клиенты теста работают в одном общем портале anyio: клиентов создавать
    через ``make_client``, корутины выполнять через ``run_async``.
    """

    def setUp(self) -> None:
        self._portal_context = anyio.from_thread.start_blocking_portal()
        self.portal = self._portal_context.__enter__()
        self.repository = self.portal.call(fresh_repository)

    def tearDown(self) -> None:
        try:
            self.portal.call(self.repository.close)
        finally:
            self._portal_context.__exit__(None, None, None)

    def run_async(self, coroutine: Any) -> Any:
        async def runner() -> Any:
            return await coroutine

        return self.portal.call(runner)

    def make_client(self, app: Any, **kwargs: Any) -> TestClient:
        kwargs.setdefault("base_url", "https://testserver")
        client = TestClient(app, **kwargs)
        client.portal = self.portal
        return client
