"""Настоящий FastAPI + PostgreSQL для сквозных тестов сайта (WEBSITE).

Запускает то же приложение, что и production (create_app), на изолированной
схеме тестовой БД (TEST_DATABASE_URL, схема — миграции из DATABASE_DIR) с
заранее созданными данными, и печатает JSON с данными для входа:

    python tests/e2e/serve_api.py [порт]      # порт 0 — свободный

Первая строка stdout после запуска: ``READY {...json...}``. Никаких реальных
платежей и сети: ЮKassa не настроена (checkout отвечает 503, как до подключения
магазина), бот-клиент заменён заглушкой. Production-БД использовать нельзя.
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from flamenco_bot.api.app import create_app  # noqa: E402
from flamenco_bot.payments import YooKassaClient  # noqa: E402
from flamenco_bot.runtime.security import (  # noqa: E402
    AuthRateLimiter,
    SupportRateLimiter,
)
from flamenco_bot.services import AuthService  # noqa: E402
from tests.pg import fresh_repository, make_admin, set_credits, sql  # noqa: E402

PASSWORD = "e2e-password-1"
USER_ID = 1001
OTHER_ID = 1002
ADMIN_ID = 77
NO_TELEGRAM_EMAIL = "guest-no-telegram@example.com"


async def seed(repository, auth):
    await make_admin(repository, ADMIN_ID, "Админ")
    await repository.get_or_create_profile(USER_ID, "Анна")
    await set_credits(repository, USER_ID, 3)
    await repository.get_or_create_profile(OTHER_ID, "Мария")
    await set_credits(repository, OTHER_ID, 1)
    accounts = {}
    for email, telegram_id, name in (
        ("user@example.com", USER_ID, "Анна"),
        ("other@example.com", OTHER_ID, "Мария"),
        ("admin@example.com", ADMIN_ID, "Админ"),
        (NO_TELEGRAM_EMAIL, None, "Без Telegram"),
    ):
        account = await auth.register_with_email(email, PASSWORD, name)
        if telegram_id:
            await sql(
                repository,
                "UPDATE users SET telegram_id = $2 WHERE id = $1",
                account.id,
                telegram_id,
            )
        accounts[email] = account.id
    now = datetime.now(timezone.utc)
    slots = {}
    for key, days, capacity in (("roomy", 3, 5), ("single_seat", 4, 1)):
        slot = await repository.create_class_slot(
            "beginner", now + timedelta(days=days), capacity, ADMIN_ID
        )
        slots[key] = slot.id
    # Занятие, которое началось бы менее чем через срок отмены (24 ч).
    soon = await repository.create_class_slot(
        "beginner", now + timedelta(hours=3), 5, ADMIN_ID
    )
    slots["soon"] = soon.id
    return {"accounts": accounts, "slots": slots}


async def main(port: int) -> None:
    repository = await fresh_repository()
    app = create_app()
    auth = AuthService(repository, "123456:e2e-token")
    app.state.repository = repository
    app.state.auth_service = auth
    app.state.bot = AsyncMock()
    app.state.support_limiter = SupportRateLimiter()
    app.state.auth_limiter = AuthRateLimiter()
    app.state.payment_gateway = YooKassaClient("", "", "")
    app.state.admin_payment_gateway = YooKassaClient("", "", "")
    seeded = await seed(repository, auth)

    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.05)
    actual = server.servers[0].sockets[0].getsockname()[1]
    info = {
        "port": actual,
        "password": None,
        "user_id": USER_ID,
        "other_id": OTHER_ID,
        "admin_id": ADMIN_ID,
        **seeded,
    }
    print("READY " + json.dumps(info), flush=True)
    try:
        await task
    finally:
        await repository.close()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 0))
