from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
import uuid
from unittest.mock import AsyncMock, Mock

from flamenco_bot.database.repository import (
    DatabaseHealth,
    LessonPayment,
    UserStatistics,
    UserProfile,
)
from flamenco_bot.database.studio_models import NotificationSettings, PackageSummary


class FakeMessage:
    def __init__(
        self,
        text="",
        telegram_id=1001,
        full_name="Анна",
        contact=None,
    ):
        self.text = text
        self.from_user = SimpleNamespace(
            id=telegram_id,
            full_name=full_name,
            is_bot=False,
        )
        self.chat = SimpleNamespace(type="private")
        self.contact = contact
        self.answer = AsyncMock()
        self.edit_text = AsyncMock()
        self.bot: Any = None

    @property
    def last_answer(self):
        call = self.answer.await_args
        if call is None:
            raise AssertionError("Message.answer was not called")
        return call

    @property
    def last_edit(self):
        call = self.edit_text.await_args
        if call is None:
            raise AssertionError("Message.edit_text was not called")
        return call


class FakeCallback:
    """CallbackQuery: нажатие inline-кнопки под сообщением бота."""

    def __init__(self, data, telegram_id=1001, full_name="Анна", message=None):
        self.data = data
        self.from_user = SimpleNamespace(
            id=telegram_id,
            full_name=full_name,
            is_bot=False,
        )
        self.message = message or FakeMessage(telegram_id=telegram_id)
        self.answer = AsyncMock()
        self.bot = SimpleNamespace(send_message=AsyncMock())

    @property
    def screen(self):
        """Текст и разметка экрана, показанного в ответ на нажатие."""
        call = self.message.edit_text.await_args
        if call is None:
            raise AssertionError("Screen was not shown")
        return call.args[0], call.kwargs.get("reply_markup")


def inline_buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def callback_data(markup):
    return [button.callback_data for button in inline_buttons(markup)]


class FakeState:
    def __init__(self, data=None):
        self.data = data or {}
        self.current_state = None
        self.clear = AsyncMock(side_effect=self._clear)
        self.set_state = AsyncMock(side_effect=self._set_state)
        self.update_data = AsyncMock(side_effect=self._update_data)
        self.get_data = AsyncMock(side_effect=self._get_data)
        self.get_state = AsyncMock(side_effect=self._get_state)

    async def _clear(self):
        self.current_state = None
        self.data.clear()

    async def _set_state(self, state):
        self.current_state = state.state if hasattr(state, "state") else state

    async def _update_data(self, **kwargs):
        self.data.update(kwargs)
        return self.data

    async def _get_data(self):
        return self.data

    async def _get_state(self):
        return self.current_state


class FakeRepository:
    def __init__(self):
        self.profile = UserProfile(
            telegram_id=1001,
            phone=None,
            user_name="Анна",
            registered_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
            is_admin=False,
        )
        self.get_or_create_profile = AsyncMock(return_value=self.profile)
        self.initialize = AsyncMock()
        self.close = AsyncMock()
        self.health_check = AsyncMock(
            return_value=DatabaseHealth(
                backend="memory",
                pool_size=0,
                idle_connections=0,
            )
        )
        self.update_phone = AsyncMock()
        self.update_user_name = AsyncMock()
        self.search_profiles = AsyncMock(return_value=[self.profile])
        self.get_profile = AsyncMock(return_value=self.profile)
        self.list_class_slots = AsyncMock(return_value=[])
        self.list_available_class_slots = AsyncMock(return_value=[])
        self.create_class_slot = AsyncMock()
        self.update_class_slot_capacity = AsyncMock(return_value=True)
        self.close_class_slot = AsyncMock(return_value=True)
        self.book_class_slot = AsyncMock()
        self.cancel_class_slot_booking = AsyncMock(return_value=True)
        self.list_bookings_for_telegram_id = AsyncMock(return_value=[])
        self.list_admin_ids = AsyncMock(return_value=[])
        self.list_staff = AsyncMock(return_value=[])
        self.get_staff_role = AsyncMock(return_value=None)
        self.is_slot_teacher = AsyncMock(return_value=False)
        self.create_support_message = AsyncMock(return_value=(1, True))
        self.reply_support_ticket = AsyncMock(return_value=1001)
        self.list_open_support_tickets = AsyncMock(return_value=[])
        self.close_support_ticket = AsyncMock(return_value=1001)
        self.record_activity = AsyncMock()
        self.get_user_statistics = AsyncMock(
            return_value=UserStatistics(total_users=10, online_users=2)
        )
        self.create_lesson_payment = AsyncMock(
            return_value=LessonPayment(
                id=8,
                telegram_id=1001,
                package_key="single",
                lessons=1,
                amount_minor=100000,
                provider_payment_id="provider-payment-1",
                confirmation_url="https://pay.example.test/confirm/1",
                status="pending",
            )
        )
        self.begin_lesson_payment_attempt = AsyncMock(
            return_value=SimpleNamespace(
                idempotence_key=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                telegram_id=1001,
                package_key="single",
                package_title="Разовое занятие",
                lessons=1,
                amount_minor=100000,
                status="creating",
                provider_payment_id=None,
                confirmation_url=None,
            )
        )
        self.get_lesson_payment = AsyncMock()
        self.complete_lesson_payment = AsyncMock(return_value=True)
        self.cancel_lesson_payment = AsyncMock()
        self.get_lesson_credits = AsyncMock(return_value=1)
        self.list_lesson_payments_for_telegram_id = AsyncMock(return_value=[])
        self.get_scheduled_restart = AsyncMock(return_value=None)
        self.set_scheduled_restart = AsyncMock()
        self.clear_scheduled_restart = AsyncMock()
        self.prepare_lesson_refund = AsyncMock()
        self.record_provider_refund = AsyncMock()
        self.complete_lesson_refund = AsyncMock(return_value=True)
        # Жизненный цикл занятий, абонементы, история, уведомления.
        self.get_package_summary = AsyncMock(side_effect=self._package_summary)
        self.get_package_grant = AsyncMock(return_value=None)
        self.grant_lesson_package = AsyncMock()
        self.revoke_lesson_package_grant = AsyncMock()
        self.get_class_slot = AsyncMock(return_value=None)
        self.list_slot_participants = AsyncMock(return_value=[])
        self.list_slot_events = AsyncMock(return_value=[])
        self.reschedule_class_slot = AsyncMock()
        self.cancel_class_slot = AsyncMock()
        self.reopen_class_slot = AsyncMock(return_value=True)
        self.list_credit_history = AsyncMock(return_value=[])
        self.list_audit_events = AsyncMock(return_value=[])
        self.enqueue_notification = AsyncMock(return_value=True)
        self.list_notifications = AsyncMock(return_value=[])
        self.count_unread_notifications = AsyncMock(return_value=0)
        self.mark_notifications_read = AsyncMock(return_value=0)
        self.get_notification_settings = AsyncMock(return_value=NotificationSettings())
        self.update_notification_settings = AsyncMock(
            return_value=NotificationSettings()
        )
        self.list_support_tickets_for_telegram_id = AsyncMock(return_value=[])
        self.get_support_ticket_thread = AsyncMock(return_value=None)
        self.reopen_support_ticket = AsyncMock(return_value=1001)

    async def _package_summary(self, telegram_id):
        """Баланс — из профиля, без абонементов (как у нового участника)."""
        credits = self.profile.lesson_credits
        return PackageSummary(balance=credits, packages=(), unallocated=credits)


def make_record(
    telegram_id=1001,
    phone=None,
    user_name="Анна",
    staff_role=None,
):
    return {
        "telegram_id": telegram_id,
        "phone": phone,
        "user_name": user_name,
        "registered_at": datetime(2025, 1, 2, tzinfo=timezone.utc),
        "staff_role": staff_role,
        "lesson_credits": 0,
        "id": 7,
        "kind": "booking",
        "details": "тест",
        "status": "pending",
        "created_at": datetime(2025, 1, 2, tzinfo=timezone.utc),
    }


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.applied_migrations = set()
        self.profile_admin_status = {}

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "UPDATE 1"

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        if "COUNT(*) FILTER (WHERE last_seen_at >= $1)" in query:
            return {"total_users": 1, "online_users": 1}
        # Роль — из staff_members (profile_admin_status: Telegram ID → админ).
        return make_record(
            telegram_id=args[0],
            staff_role="admin" if self.profile_admin_status.get(args[0]) else None,
        )

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        if "FROM schema_migrations" in query:
            return [{"version": version} for version in sorted(self.applied_migrations)]
        return [make_record()]

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        if "to_regclass('schema_migrations')" in query:
            return "schema_migrations" if self.applied_migrations else None
        return 0

    def transaction(self):
        return FakeTransaction()


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        _ = (exc_type, exc, traceback)
        return False


class FakeAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        _ = (exc_type, exc, traceback)
        return False


class FakePool:
    def __init__(self):
        self.connection = FakeConnection()

    def acquire(self):
        return FakeAcquire(self.connection)

    async def close(self):
        return None

    def get_size(self):
        return 1

    def get_idle_size(self):
        return 1


class FakeDispatcher:
    def __init__(self):
        self.update = SimpleNamespace(outer_middleware=Mock())
        self.message = SimpleNamespace(
            middleware=Mock(),
            outer_middleware=Mock(),
        )
        self.callback_query = SimpleNamespace(
            middleware=Mock(),
            outer_middleware=Mock(),
        )
        self.include_router = Mock()
        self.start_polling = AsyncMock()
        self.stop_polling = AsyncMock()

    def __setitem__(self, key, value):
        setattr(self, key, value)


# Миграции живут в репозитории базы данных (DATABASE): локально — соседняя
# папка «DATABASE», в CI — путь из DATABASE_DIR. Интеграционные тесты
# создают схему той же функцией migrate.apply_migrations, что и production.
DB_REPOSITORY = Path(
    os.getenv("DATABASE_DIR") or Path(__file__).resolve().parents[2] / "DATABASE"
)
_db_migrate = None


def db_migrate():
    """Модуль migrate.py репозитория базы данных."""
    global _db_migrate
    if _db_migrate is None:
        path = DB_REPOSITORY / "migrate.py"
        if not path.is_file():
            raise RuntimeError(
                "Не найден {} — склонируйте DATABASE рядом с проектом или "
                "задайте DATABASE_DIR".format(path)
            )
        spec = importlib.util.spec_from_file_location("flamenco_db_migrate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _db_migrate = module
    return _db_migrate


async def apply_migrations(pool: Any, directory: Optional[Path] = None) -> list:
    """Применяет миграции DATABASE в схему из search_path пула."""
    migrate = db_migrate()
    async with pool.acquire() as connection:
        return await migrate.apply_migrations(
            connection, directory or migrate.MIGRATIONS_DIRECTORY
        )
