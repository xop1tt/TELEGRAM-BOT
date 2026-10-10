"""FastAPI-приложение сайта.

Второй интерфейс к той же системе, что и Telegram-бот: тот же PostgreSQL,
те же сервисы (``..services``). Бот и сайт не держат отдельных копий
бизнес-логики — см. ``CLAUDE.md``, принцип "один backend, одна БД".
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

from aiogram import Bot
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, Response

from ..database import (
    PostgresRepository,
    is_database_configured,
    is_database_unavailable,
)
from ..database.errors import DATABASE_UNAVAILABLE_ERRORS
from ..payments import YooKassaClient
from ..keyboards.user.screens import payment_confirmed_notifier
from ..runtime.notifications import start_notification_worker
from ..runtime.payment_reconciliation import start_reconciliation_task
from ..runtime.security import AuthRateLimiter, SupportRateLimiter
from ..services import AuthService, PaymentService
from .config import WebConfig
from .logging_config import configure_api_logging
from .metrics import ProcessMetrics, RequestMetrics
from .routers.admin import router as admin_router
from .routers.admin import staff_router as admin_staff_router
from .routers.auth import router as auth_router
from .routers.bookings import router as bookings_router
from .routers.classes import router as classes_router
from .routers.history import router as history_router
from .routers.notifications import router as notifications_router
from .routers.packages import router as packages_router
from .routers.payments import router as payments_router
from .routers.schedule import router as schedule_router
from .routers.support import router as support_router
from .routers.users import router as users_router


logger = logging.getLogger("bot.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_api_logging()
    if not is_database_configured(WebConfig.DATABASE_URL):
        raise RuntimeError("DATABASE_URL must point to PostgreSQL")

    repository = await PostgresRepository.connect(
        WebConfig.DATABASE_URL,
        ssl_ca_path=WebConfig.DATABASE_SSL_CA,
        pool_min_size=WebConfig.DATABASE_POOL_MIN_SIZE,
        pool_max_size=WebConfig.DATABASE_POOL_MAX_SIZE,
        ssl_mode=WebConfig.DATABASE_SSL_MODE,
        allow_insecure_local=WebConfig.ENV == "development",
    )
    logger.info("API storage backend=postgres")
    await repository.initialize()

    # Тот же бот, что и в main.py: нужен только чтобы рассылать уведомления
    # администраторам (AdminNotifier), не для приёма обновлений/polling.
    bot = Bot(token=WebConfig.BOT_TOKEN)

    # Тот же shop_id/secret_key, что у бота (один магазин ЮKassa), но
    # отдельный return_url — оплата, начатая на сайте, должна вернуть
    # пользователя на сайт, а не в Telegram-чат бота. Пока
    # WEB_YOOKASSA_RETURN_URL не задан, YooKassaClient.is_configured — False
    # и checkout на сайте недоступен (503), что ожидаемо на этом этапе.
    payment_gateway = YooKassaClient(
        WebConfig.YOOKASSA_SHOP_ID,
        WebConfig.YOOKASSA_SECRET_KEY,
        WebConfig.WEB_YOOKASSA_RETURN_URL,
    )

    app.state.repository = repository
    app.state.auth_service = AuthService(repository, WebConfig.BOT_TOKEN)
    app.state.bot = bot
    app.state.support_limiter = SupportRateLimiter()
    app.state.auth_limiter = AuthRateLimiter()
    app.state.payment_gateway = payment_gateway

    # Фоновая сверка "забытых" платежей — та же задача, что в боте
    # (main.py), чтобы оплата зачислялась и при остановленном боте. Сверку
    # в каждый момент выполняет один процесс (advisory-блокировка, см.
    # runtime/payment_reconciliation.py). Шлюз — с return_url бота: сверка
    # только запрашивает статус платежа, return_url ей не нужен, а
    # WEB_YOOKASSA_RETURN_URL может быть ещё не задан.
    reconciliation_gateway = YooKassaClient(
        WebConfig.YOOKASSA_SHOP_ID,
        WebConfig.YOOKASSA_SECRET_KEY,
        WebConfig.YOOKASSA_RETURN_URL,
    )
    reconciliation_task = start_reconciliation_task(
        PaymentService(repository, reconciliation_gateway),
        repository,
        logger,
        on_confirmed=payment_confirmed_notifier(bot, repository),
    )
    app.state.reconciliation_task = reconciliation_task
    # Та же отправка уведомлений, что в боте: одновременно работает один
    # процесс (advisory-блокировка), уведомления с сайта уходят и без бота.
    notification_task = start_notification_worker(
        repository, bot, logger, WebConfig.LESSON_REMINDER_HOURS
    )
    app.state.notification_task = notification_task
    try:
        yield
    finally:
        background = [notification_task]
        if reconciliation_task is not None:
            background.append(reconciliation_task)
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await bot.session.close()
        await repository.close()


DATABASE_UNAVAILABLE_DETAIL = "Database temporarily unavailable"


async def database_unavailable_handler(
    request: Request, error: Exception
) -> JSONResponse:
    """503 вместо 500, когда PostgreSQL недоступна.

    Сайт отличает "сервис временно недоступен" от пустых данных именно по
    статусу. Подробности (тип ошибки, traceback) — только в логах сервера,
    в ответе нет ни SQL, ни адреса БД.
    """
    if not is_database_unavailable(error):
        # Обычный OSError/таймаут не из БД — пусть обработается как 500.
        raise error
    logger.error(
        "Database unavailable method=%s path=%s error_type=%s",
        request.method,
        request.url.path,
        type(error).__name__,
        exc_info=error,
    )
    return JSONResponse(
        {"detail": DATABASE_UNAVAILABLE_DETAIL},
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


async def internal_error_handler(request: Request, error: Exception) -> JSONResponse:
    # Traceback уже пишет сервер (uvicorn); клиенту — только общий JSON.
    return JSONResponse(
        {"detail": "Internal Server Error"},
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def create_app() -> FastAPI:
    app = FastAPI(title="Flamenco Studio API", lifespan=lifespan)
    # Показатели нагрузки для окна администратора (/api/admin/metrics):
    # создаются вместе с приложением, а не в lifespan — их видят и тесты.
    app.state.request_metrics = RequestMetrics()
    app.state.process_metrics = ProcessMetrics()

    @app.middleware("http")
    async def track_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_metrics: RequestMetrics = app.state.request_metrics
        request_metrics.started()
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            request_metrics.finished(
                (time.perf_counter() - started) * 1000, status_code
            )

    for error_type in (*DATABASE_UNAVAILABLE_ERRORS, OSError):
        app.add_exception_handler(error_type, database_unavailable_handler)
    app.add_exception_handler(Exception, internal_error_handler)
    app.include_router(auth_router)
    app.include_router(schedule_router)
    app.include_router(classes_router)
    app.include_router(packages_router)
    app.include_router(bookings_router)
    app.include_router(support_router)
    app.include_router(users_router)
    app.include_router(payments_router)
    app.include_router(history_router)
    app.include_router(notifications_router)
    app.include_router(admin_staff_router)
    app.include_router(admin_router)

    @app.get("/api/health")
    async def health() -> dict:
        health_status = await app.state.repository.health_check()
        return {
            "backend": health_status.backend,
            "pool_size": health_status.pool_size,
            "idle_connections": health_status.idle_connections,
        }

    return app


app = create_app()
