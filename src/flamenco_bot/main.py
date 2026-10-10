import asyncio
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.utils.token import TokenValidationError

from .runtime.bot_lifecycle import RestartController, restart_process
from .runtime.bot_logging import (
    UpdateLoggingMiddleware,
    UpdateMetrics,
    configure_logging,
)
from .healthcheck import heartbeat_path_from_env, write_heartbeat
from .runtime.monitoring import monitor_health
from .runtime.notifications import start_notification_worker
from .runtime.payment_reconciliation import start_reconciliation_task
from .runtime.security import SecurityMiddleware, SupportRateLimiter
from .commands import register_commands
from .config import Config
from .database import PostgresRepository, is_database_configured
from .database.repository import DatabaseUnavailableError, SchemaVersionError
from .payments import YooKassaClient
from .services import PaymentService
from .handlers import router
from .keyboards.user.screens import payment_confirmed_notifier


async def main():
    """Запуск бота"""
    logger = configure_logging()
    logger.info("Starting bot env=%s", Config.ENV)
    if not is_database_configured(Config.DATABASE_URL):
        # Хранилища в памяти больше нет: и для разработки нужен PostgreSQL
        # (docker compose или TEST_DATABASE_URL, см. README).
        logger.critical("DATABASE_URL must point to PostgreSQL")
        print(
            "DATABASE_URL не задан: боту нужен PostgreSQL (см. README).",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1)

    bot = None
    repository = None
    lifecycle = None
    lifecycle_tasks = []
    restart_requested = False
    try:
        try:
            bot = Bot(token=Config.BOT_TOKEN)
        except TokenValidationError:
            message = (
                "BOT_TOKEN некорректен. В .env укажите токен BotFather "
                "в формате <числовой_id>:<секрет>."
            )
            logger.error(message)
            print(message, file=sys.stderr, flush=True)
            raise SystemExit(1)

        lifecycle = RestartController(
            bot=bot,
            dispatcher=None,
            admins=Config.ADMINS,
            logger=logger,
            source_root=Path(__file__).resolve().parent,
        )

        repository = await PostgresRepository.connect(
            Config.DATABASE_URL,
            ssl_ca_path=Config.DATABASE_SSL_CA,
            pool_min_size=Config.DATABASE_POOL_MIN_SIZE,
            pool_max_size=Config.DATABASE_POOL_MAX_SIZE,
            ssl_mode=Config.DATABASE_SSL_MODE,
            allow_insecure_local=Config.ENV == "development",
        )
        logger.info(
            "Storage backend=postgres pool_min_size=%d pool_max_size=%d",
            Config.DATABASE_POOL_MIN_SIZE,
            Config.DATABASE_POOL_MAX_SIZE,
        )

        dp = Dispatcher()
        lifecycle.dispatcher = dp
        dp["repository"] = repository
        dp["support_limiter"] = SupportRateLimiter()
        dp["restart_controller"] = lifecycle
        payment_gateway = YooKassaClient(
            Config.YOOKASSA_SHOP_ID,
            Config.YOOKASSA_SECRET_KEY,
            Config.YOOKASSA_RETURN_URL,
        )
        dp["payment_gateway"] = payment_gateway
        update_metrics = UpdateMetrics()
        dp["update_metrics"] = update_metrics
        security_middleware = SecurityMiddleware()
        dp.message.outer_middleware(security_middleware)
        dp.callback_query.outer_middleware(security_middleware)
        update_logger = UpdateLoggingMiddleware(logger, metrics=update_metrics)
        dp.message.middleware(update_logger)
        dp.callback_query.middleware(update_logger)
        dp.include_router(router)

        await repository.initialize()
        heartbeat_path = heartbeat_path_from_env()
        await register_commands(bot, repository, logger)
        lifecycle_tasks = [
            asyncio.create_task(
                monitor_health(
                    repository,
                    update_metrics,
                    logger,
                    heartbeat_path=heartbeat_path,
                )
            )
        ]
        reconciliation_task = start_reconciliation_task(
            PaymentService(repository, payment_gateway),
            repository,
            logger,
            on_confirmed=payment_confirmed_notifier(bot, repository),
        )
        if reconciliation_task is not None:
            lifecycle_tasks.append(reconciliation_task)
        # Уведомления участникам из outbox (запись с сайта, отмена/перенос
        # студией, напоминания) — отправляются после commit операции.
        lifecycle_tasks.append(
            start_notification_worker(
                repository, bot, logger, Config.LESSON_REMINDER_HOURS
            )
        )
        if Config.ENV == "development":
            lifecycle_tasks.extend(
                [
                    asyncio.create_task(
                        lifecycle.run_lifecycle_task(
                            "code_watcher",
                            lifecycle.watch_code_changes,
                        )
                    ),
                    asyncio.create_task(
                        lifecycle.run_lifecycle_task(
                            "midnight_scheduler",
                            lifecycle.schedule_midnight_restart,
                        )
                    ),
                ]
            )
        # Первый heartbeat — запуск завершён (БД, миграции, команды); дальше
        # его обновляет monitor_health после каждой проверки БД.
        write_heartbeat(heartbeat_path)
        logger.info("Bot polling started")
        lifecycle.polling_started = True
        await dp.start_polling(bot, tasks_concurrency_limit=64)
        logger.info("Bot polling stopped")
    except Exception as error:
        if lifecycle is None or not lifecycle.polling_started:
            logger.exception(
                "Bot startup failed before polling error_type=%s",
                type(error).__name__,
            )
            if isinstance(error, DatabaseUnavailableError):
                print(
                    "Не удалось запустить бота: "
                    "сервер PostgreSQL недоступен. Запустите сервер БД "
                    "и проверьте DATABASE_URL в .env.",
                    file=sys.stderr,
                    flush=True,
                )
            elif isinstance(error, SchemaVersionError):
                print(
                    "Не удалось запустить бота: {}".format(error),
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    "Не удалось запустить бота "
                    f"({type(error).__name__}). Подробности — в logs/bot.log.",
                    file=sys.stderr,
                    flush=True,
                )
            raise SystemExit(1)

        logger.exception("Bot startup or polling failed")
        await lifecycle.notify_critical_error(error)
        restart_requested = True
    finally:
        for task in lifecycle_tasks:
            task.cancel()
        if lifecycle_tasks:
            await asyncio.gather(*lifecycle_tasks, return_exceptions=True)
        if lifecycle is not None:
            await lifecycle.close()
        try:
            if bot is not None:
                await bot.session.close()
        finally:
            if repository is not None:
                await repository.close()

    if lifecycle is not None and lifecycle.restart_event.is_set():
        if Config.ENV == "development":
            restart_requested = True
        else:
            raise SystemExit(1)
    if restart_requested:
        if Config.ENV == "development":
            restart_process()
        else:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
