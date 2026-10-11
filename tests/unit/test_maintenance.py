"""Техническое обслуживание сайта: правила, состояние, рассылки, API.

Telegram во всех тестах — фейковый объект (``send_message`` — AsyncMock):
реальные сообщения не отправляются.
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
)
from aiogram.methods import SendMessage

from flamenco_bot import maintenance
from flamenco_bot.database.repository import PostgresRepository
from flamenco_bot.maintenance import (
    MaintenanceConflictError,
    MaintenanceWindow,
    warning_decision,
)
from flamenco_bot.notification_texts import render_notification
from flamenco_bot.runtime.notifications import (
    dispatch_due_notifications,
    process_maintenance,
    run_notification_worker,
)
from tests.pg import PostgresTestCase, fetch_all, fetchval, make_admin, sql

from .test_api_admin import ApiTestBase

UTC = timezone.utc
ADMIN = 900
USERS = (1001, 1002, 1003)
LOGGER = SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
    exception=lambda *a, **k: None,
)
METHOD = SendMessage(chat_id=1, text="x")


def window(**overrides):
    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    values = dict(
        id=1,
        status="scheduled",
        title="Работы",
        description="Описание",
        starts_at=now + timedelta(hours=2),
        ends_at=None,
        warning_minutes=(1440, 60, 10),
        notify_users=True,
        audience="all",
        revision=1,
        started_at=None,
        finished_at=None,
        created_by=ADMIN,
        created_at=now,
        updated_at=now,
    )
    values.update(overrides)
    return MaintenanceWindow(**values)


class MaintenanceRulesTests(unittest.TestCase):
    def test_warning_is_sent_only_while_on_time(self):
        start = datetime(2026, 10, 13, 0, 0, tzinfo=UTC)
        hour = 60
        self.assertEqual(
            warning_decision(hour, start, start - timedelta(hours=2)), "wait"
        )
        self.assertEqual(
            warning_decision(hour, start, start - timedelta(minutes=60)), "send"
        )
        # Допуск для часа — 15 минут.
        self.assertEqual(
            warning_decision(hour, start, start - timedelta(minutes=46)), "send"
        )
        self.assertEqual(
            warning_decision(hour, start, start - timedelta(minutes=44)), "late"
        )
        # За 10 минут — допуск 2,5 минуты; за сутки — не больше 30 минут.
        self.assertEqual(
            warning_decision(10, start, start - timedelta(minutes=8)), "send"
        )
        self.assertEqual(
            warning_decision(10, start, start - timedelta(minutes=7)), "late"
        )
        self.assertEqual(
            warning_decision(1440, start, start - timedelta(hours=23)), "late"
        )
        # После начала предупреждать поздно.
        self.assertEqual(warning_decision(10, start, start), "late")

    def test_effective_status_and_site_mode(self):
        item = window()
        before = item.starts_at - timedelta(seconds=1)
        self.assertEqual(maintenance.site_mode(None, before), "normal")
        self.assertEqual(maintenance.site_mode(item, before), "scheduled")
        # Начало наступило — активно, даже если фоновая задача не успела.
        self.assertEqual(maintenance.site_mode(item, item.starts_at), "active")
        # Плановое окончание прошло — всё ещё активно.
        late = window(
            status="active",
            started_at=item.starts_at,
            ends_at=item.starts_at + timedelta(hours=1),
        )
        self.assertEqual(
            maintenance.site_mode(late, item.starts_at + timedelta(hours=5)), "active"
        )
        done = window(status="completed", finished_at=item.starts_at)
        self.assertEqual(maintenance.site_mode(done, item.starts_at), "normal")

    def test_validation(self):
        now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
        with self.assertRaises(ValueError):
            maintenance.validate_period(
                now - timedelta(minutes=1), None, now, future_start=True
            )
        with self.assertRaises(ValueError):
            maintenance.validate_period(
                now + timedelta(hours=1),
                now + timedelta(minutes=30),
                now,
                future_start=True,
            )
        with self.assertRaises(ValueError):
            maintenance.validate_period(
                datetime(2026, 10, 13, 1, 0), None, now, future_start=True
            )
        maintenance.validate_period(
            now + timedelta(hours=1), now + timedelta(hours=2), now, future_start=True
        )
        self.assertEqual(
            maintenance.normalize_warning_minutes([10, 1440, 60, 10]), (1440, 60, 10)
        )
        for bad in ([0], [20000], [1, 2, 3, 4, 5, 6, 7]):
            with self.assertRaises(ValueError):
                maintenance.normalize_warning_minutes(bad)

    def test_retry_after(self):
        now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
        self.assertEqual(maintenance.retry_after_seconds(None, now), 600)
        item = window(ends_at=now + timedelta(minutes=30))
        self.assertEqual(maintenance.retry_after_seconds(item, now), 1800)
        self.assertEqual(
            maintenance.retry_after_seconds(item, now + timedelta(hours=1)), 600
        )


class MaintenanceTextsTests(unittest.TestCase):
    PAYLOAD = {
        "maintenance_id": 1,
        "revision": 1,
        # 23:30 UTC — уже следующий день в Москве.
        "starts_at": "2026-10-12T23:30:00+00:00",
        "ends_at": "2026-10-13T01:00:00+00:00",
    }

    def test_times_in_studio_timezone_across_midnight(self):
        text = render_notification("maintenance_warning", self.PAYLOAD).text
        self.assertIn("Начало: 13.10.2026 02:30", text)
        self.assertIn("Ожидаемое завершение: 13.10.2026 04:00", text)
        self.assertIn("плановое обслуживание", text)

    def test_every_kind_has_its_message(self):
        expected = {
            "maintenance_warning": "Мы сообщим, когда работы начнутся",
            "maintenance_started": "техническое обслуживание началось",
            "maintenance_rescheduled": "обслуживание перенесено",
            "maintenance_cancelled": "обслуживание отменено",
            "maintenance_completed": "снова работает",
        }
        for kind, fragment in expected.items():
            with self.subTest(kind=kind):
                self.assertIn(fragment, render_notification(kind, self.PAYLOAD).text)
        active = render_notification(
            "maintenance_rescheduled", {**self.PAYLOAD, "phase": "active"}
        ).text
        self.assertIn("время завершения работ изменилось", active)

    def test_unknown_end_is_not_promised(self):
        payload = {**self.PAYLOAD, "ends_at": None}
        started = render_notification("maintenance_started", payload).text
        self.assertIn("Время завершения пока неизвестно", started)
        self.assertNotIn("Ожидаемое завершение", started)
        warning = render_notification("maintenance_warning", payload).text
        self.assertNotIn("Ожидаемое завершение", warning)


class MaintenanceRepositoryBase(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await make_admin(self.repository, ADMIN)
        for telegram_id in USERS:
            await self.repository.get_or_create_profile(telegram_id, "Участник")
        self.sent = []
        self.bot = SimpleNamespace(send_message=AsyncMock(side_effect=self._send))
        self.now = datetime.now(UTC)

    async def _send(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))

    async def create(self, hours=2, **overrides):
        values = dict(
            title="Небольшая пауза",
            description="Обновляем сайт",
            starts_at=self.now + timedelta(hours=hours),
            ends_at=self.now + timedelta(hours=hours + 1),
            warning_minutes=[1440, 60, 10],
        )
        values.update(overrides)
        return await self.repository.create_maintenance(ADMIN, **values)

    async def notifications(self, kind=None):
        rows = await fetch_all(
            self.repository,
            "SELECT telegram_id, kind, delivery_status, "
            "payload ->> 'revision' AS revision FROM user_notifications "
            "WHERE kind LIKE 'maintenance%' ORDER BY id",
        )
        return [row for row in rows if kind is None or row["kind"] == kind]

    async def deliver(self):
        total = 0
        while True:
            sent = await dispatch_due_notifications(self.repository, self.bot, LOGGER)
            pending = await fetchval(
                self.repository,
                "SELECT COUNT(*) FROM user_notifications "
                "WHERE delivery_status = 'pending' AND next_attempt_at <= NOW()",
            )
            total += sent
            if not pending:
                return total

    def texts_for(self, telegram_id):
        return [text for chat_id, text in self.sent if chat_id == telegram_id]


class MaintenanceStateTests(MaintenanceRepositoryBase):
    async def test_create_and_single_open_window(self):
        created = await self.create()
        self.assertEqual(created.status, "scheduled")
        self.assertEqual(created.warning_minutes, (1440, 60, 10))
        self.assertEqual(created.starts_at.utcoffset(), timedelta(0))
        with self.assertRaises(MaintenanceConflictError):
            await self.create(hours=5)
        with self.assertRaises(ValueError):
            await self.repository.create_maintenance(
                ADMIN, "Т", "О", self.now - timedelta(minutes=1), None, []
            )

    async def test_regular_user_cannot_change_state(self):
        with self.assertRaises(PermissionError):
            await self.repository.create_maintenance(
                USERS[0], "Т", "О", self.now + timedelta(hours=1), None, []
            )
        created = await self.create()
        for action in (
            self.repository.start_maintenance,
            self.repository.cancel_maintenance,
            self.repository.complete_maintenance,
        ):
            with self.assertRaises(PermissionError):
                await action(created.id, USERS[0])

    async def test_timezone_input_is_stored_as_the_same_instant(self):
        moscow = ZoneInfo("Europe/Moscow")
        local_start = datetime(2027, 1, 1, 0, 30, tzinfo=moscow)  # полночь МСК
        created = await self.repository.create_maintenance(
            ADMIN, "Т", "О", local_start, local_start + timedelta(hours=2), []
        )
        stored = await self.repository.get_maintenance(created.id)
        self.assertEqual(stored.starts_at, datetime(2026, 12, 31, 21, 30, tzinfo=UTC))
        self.assertEqual(stored.ends_at - stored.starts_at, timedelta(hours=2))

    async def test_update_reschedule_and_text_change(self):
        created = await self.create()
        renamed = await self.repository.update_maintenance(
            created.id, ADMIN, {"title": "Новый заголовок"}
        )
        self.assertEqual(renamed.revision, 1)
        moved = await self.repository.update_maintenance(
            created.id,
            ADMIN,
            {"starts_at": self.now + timedelta(days=1), "ends_at": None},
        )
        self.assertEqual(moved.revision, 2)
        self.assertIsNone(moved.ends_at)
        events = [
            e["event"]
            for e in await self.repository.list_maintenance_events(created.id)
        ]
        self.assertEqual(events, ["created", "updated", "rescheduled"])
        with self.assertRaises(ValueError):
            await self.repository.update_maintenance(
                created.id, ADMIN, {"starts_at": self.now - timedelta(hours=1)}
            )

    async def test_cancel_and_manual_start_and_complete(self):
        created = await self.create()
        cancelled = await self.repository.cancel_maintenance(created.id, ADMIN)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNone(await self.repository.get_open_maintenance())
        with self.assertRaises(MaintenanceConflictError):
            await self.repository.cancel_maintenance(created.id, ADMIN)

        second = await self.create()
        started = await self.repository.start_maintenance(second.id, ADMIN)
        self.assertEqual(started.status, "active")
        self.assertLessEqual(started.starts_at, datetime.now(UTC))
        with self.assertRaises(MaintenanceConflictError):
            await self.repository.cancel_maintenance(second.id, ADMIN)
        with self.assertRaises(MaintenanceConflictError):
            await self.repository.update_maintenance(
                second.id, ADMIN, {"warning_minutes": [5]}
            )
        # Во время работ можно продлить окончание и поменять тексты.
        extended = await self.repository.update_maintenance(
            second.id,
            ADMIN,
            {"ends_at": self.now + timedelta(hours=6), "description": "Чуть дольше"},
        )
        self.assertEqual(extended.revision, 2)
        done = await self.repository.complete_maintenance(second.id, ADMIN)
        self.assertEqual(done.status, "completed")
        self.assertIsNotNone(done.finished_at)
        with self.assertRaises(MaintenanceConflictError):
            await self.repository.complete_maintenance(second.id, ADMIN)

    async def test_start_now_without_end(self):
        created = await self.repository.create_maintenance(
            ADMIN, "Т", "О", None, None, [], start_now=True
        )
        self.assertEqual(created.status, "active")
        self.assertEqual(maintenance.site_mode(created, datetime.now(UTC)), "active")
        self.assertIsNone(created.ends_at)


class MaintenanceSchedulerTests(MaintenanceRepositoryBase):
    async def test_auto_start_is_idempotent_and_survives_restart(self):
        created = await self.create(hours=1, warning_minutes=[])
        after = created.starts_at + timedelta(seconds=5)
        # Время ещё не пришло — ничего не происходит.
        summary = await self.repository.process_maintenance(now=self.now)
        self.assertEqual(summary["started"], [])
        summary = await self.repository.process_maintenance(now=after)
        self.assertEqual(summary["started"], [created.id])
        self.assertEqual(summary["queued"], {"started": len(USERS) + 1})
        # Повтор и «перезапуск» (новый пул к той же базе) не дублируют.
        self.assertEqual(
            await self.repository.process_maintenance(now=after),
            {"started": [], "queued": {}, "skipped": []},
        )
        restarted = PostgresRepository(self.repository._pool)
        self.assertEqual((await restarted.process_maintenance(now=after))["queued"], {})
        self.assertEqual(
            len(await self.notifications("maintenance_started")), len(USERS) + 1
        )
        window_ = await self.repository.get_maintenance(created.id)
        self.assertEqual(window_.status, "active")
        self.assertEqual(window_.started_at, created.starts_at)

    async def test_concurrent_processing_queues_once(self):
        created = await self.create(hours=1, warning_minutes=[])
        after = created.starts_at + timedelta(seconds=5)
        await asyncio.gather(
            *(self.repository.process_maintenance(now=after) for _ in range(4))
        )
        self.assertEqual(
            len(await self.notifications("maintenance_started")), len(USERS) + 1
        )
        dispatches = await self.repository.list_maintenance_dispatches(created.id)
        self.assertEqual([d["stage"] for d in dispatches], ["started"])

    async def test_warnings_on_time_late_ones_skipped(self):
        created = await self.create(hours=2)
        start = created.starts_at
        # Окно создано за 2 часа: суточное предупреждение уже запоздало.
        summary = await self.repository.process_maintenance(now=self.now)
        self.assertEqual(summary["skipped"], ["warning:1440"])
        self.assertEqual(summary["queued"], {})
        # Время часового предупреждения.
        at_hour = start - timedelta(minutes=59)
        summary = await self.repository.process_maintenance(now=at_hour)
        self.assertEqual(summary["queued"], {"warning:60": len(USERS) + 1})
        self.assertEqual(
            (await self.repository.process_maintenance(now=at_hour))["queued"], {}
        )
        # Процесс «проспал» момент 10-минутного предупреждения — не шлём.
        summary = await self.repository.process_maintenance(
            now=start - timedelta(minutes=5)
        )
        self.assertEqual(summary["skipped"], ["warning:10"])
        self.assertEqual(
            len(await self.notifications("maintenance_warning")), len(USERS) + 1
        )
        events = [
            e["event"]
            for e in await self.repository.list_maintenance_events(created.id)
        ]
        self.assertEqual(events.count("warning_skipped"), 2)

    async def test_disabled_notifications_and_staff_audience(self):
        silent = await self.create(hours=1, notify_users=False)
        await self.repository.process_maintenance(
            now=silent.starts_at - timedelta(minutes=59)
        )
        await self.repository.process_maintenance(
            now=silent.starts_at + timedelta(seconds=1)
        )
        self.assertEqual(await self.notifications(), [])
        await self.repository.complete_maintenance(silent.id, ADMIN)

        test_run = await self.create(hours=1, audience="staff")
        await self.repository.process_maintenance(
            now=test_run.starts_at - timedelta(minutes=59)
        )
        recipients = {row["telegram_id"] for row in await self.notifications()}
        self.assertEqual(recipients, {ADMIN})

    async def test_reschedule_notifies_only_warned_users(self):
        created = await self.create(hours=2, warning_minutes=[60])
        await self.repository.process_maintenance(
            now=created.starts_at - timedelta(minutes=59)
        )
        # Двум участникам предупреждение ушло, третий заблокировал бота.
        self.bot.send_message.side_effect = None

        async def send(chat_id, text, reply_markup=None):
            if chat_id == USERS[2]:
                raise TelegramForbiddenError(METHOD, "blocked")
            self.sent.append((chat_id, text))

        self.bot.send_message.side_effect = send
        await self.deliver()
        self.assertEqual(len(self.sent), len(USERS))  # 2 участника + админ
        warnings = await self.notifications("maintenance_warning")
        self.assertEqual(
            {row["telegram_id"]: row["delivery_status"] for row in warnings}[USERS[2]],
            "failed",
        )

        self.sent.clear()
        await self.repository.update_maintenance(
            created.id,
            ADMIN,
            {"starts_at": self.now + timedelta(days=2), "ends_at": None},
        )
        await self.repository.process_maintenance()
        rescheduled = await self.notifications("maintenance_rescheduled")
        self.assertEqual(
            {row["telegram_id"] for row in rescheduled}, {ADMIN, USERS[0], USERS[1]}
        )
        self.assertTrue(all(row["revision"] == "2" for row in rescheduled))
        await self.deliver()
        self.assertTrue(all("перенесено" in text for _, text in self.sent))

        self.sent.clear()
        await self.repository.cancel_maintenance(created.id, ADMIN)
        await self.repository.process_maintenance()
        cancelled = await self.notifications("maintenance_cancelled")
        self.assertEqual(
            {row["telegram_id"] for row in cancelled}, {ADMIN, USERS[0], USERS[1]}
        )
        await self.deliver()
        self.assertTrue(all("отменено" in text for _, text in self.sent))

    async def test_outdated_warning_is_not_sent_after_reschedule(self):
        created = await self.create(hours=2, warning_minutes=[60])
        await self.repository.process_maintenance(
            now=created.starts_at - timedelta(minutes=59)
        )
        # Перенос до того, как предупреждения ушли.
        await self.repository.update_maintenance(
            created.id,
            ADMIN,
            {"starts_at": self.now + timedelta(days=1), "ends_at": None},
        )
        await self.repository.process_maintenance()
        await self.deliver()
        self.assertEqual(self.sent, [])
        statuses = {row["delivery_status"] for row in await self.notifications()}
        self.assertEqual(statuses, {"skipped"})
        # Никто не был предупреждён — уведомлять о переносе некого.
        self.assertEqual(await self.notifications("maintenance_rescheduled"), [])

    async def test_completion_only_after_admin_confirms(self):
        created = await self.create(hours=1, warning_minutes=[])
        await self.repository.process_maintenance(
            now=created.starts_at + timedelta(seconds=1)
        )
        await self.deliver()
        self.assertEqual(len(self.sent), len(USERS) + 1)
        # Плановое окончание прошло, работы не подтверждены — всё ещё идут.
        past_end = created.ends_at + timedelta(hours=3)
        summary = await self.repository.process_maintenance(now=past_end)
        self.assertEqual(summary["queued"], {})
        current = await self.repository.get_open_maintenance()
        self.assertEqual(maintenance.site_mode(current, past_end), "active")
        self.assertEqual(await self.notifications("maintenance_completed"), [])

        self.sent.clear()
        await self.repository.complete_maintenance(created.id, ADMIN)
        self.assertIsNone(await self.repository.get_open_maintenance())
        await self.repository.process_maintenance()
        await self.deliver()
        self.assertEqual(len(self.sent), len(USERS) + 1)
        self.assertTrue(all("снова работает" in text for _, text in self.sent))
        # Повтор шага после рестарта не шлёт завершение второй раз.
        await PostgresRepository(self.repository._pool).process_maintenance()
        self.assertEqual(await self.deliver(), 0)

    async def test_started_message_dropped_if_completed_before_delivery(self):
        created = await self.create(hours=1, warning_minutes=[])
        await self.repository.process_maintenance(
            now=created.starts_at + timedelta(seconds=1)
        )
        await self.repository.complete_maintenance(created.id, ADMIN)
        await self.repository.process_maintenance()
        await self.deliver()
        # «Началось» устарело; о завершении — только тем, кто знал о начале.
        self.assertEqual(self.sent, [])
        statuses = {
            row["kind"]: row["delivery_status"] for row in await self.notifications()
        }
        self.assertEqual(statuses, {"maintenance_started": "skipped"})

    async def test_activation_does_not_depend_on_notifications(self):
        created = await self.create(hours=1, warning_minutes=[])
        after = created.starts_at + timedelta(seconds=1)
        with patch.object(
            PostgresRepository, "_queue_stage", side_effect=RuntimeError("queue down")
        ):
            with self.assertRaises(RuntimeError):
                await self.repository.process_maintenance(now=after)
        stored = await self.repository.get_maintenance(created.id)
        self.assertEqual(stored.status, "active")
        self.assertEqual(await self.notifications(), [])
        # Следующая итерация досылает пропущенное.
        summary = await self.repository.process_maintenance(now=after)
        self.assertEqual(summary["queued"], {"started": len(USERS) + 1})

    async def test_telegram_outage_retries_with_limit(self):
        created = await self.create(hours=1, warning_minutes=[])
        await self.repository.process_maintenance(
            now=created.starts_at + timedelta(seconds=1)
        )
        self.bot.send_message.side_effect = TelegramNetworkError(METHOD, "down")
        await dispatch_due_notifications(self.repository, self.bot, LOGGER)
        rows = await fetch_all(
            self.repository,
            "SELECT delivery_status, attempts, next_attempt_at > NOW() AS later "
            "FROM user_notifications",
        )
        self.assertTrue(all(row["delivery_status"] == "pending" for row in rows))
        self.assertTrue(all(row["attempts"] == 1 and row["later"] for row in rows))
        # Обслуживание идёт независимо от сбоя Telegram.
        current = await self.repository.get_open_maintenance()
        self.assertEqual(current.status, "active")

    async def test_worker_keeps_dispatching_when_maintenance_step_fails(self):
        await self.repository.enqueue_notification(
            USERS[0], "credits_adjusted", {"delta": 1, "balance": 2}, "k:1"
        )
        calls = {"n": 0}

        async def sleep(_):
            calls["n"] += 1
            if calls["n"] > 1:
                raise asyncio.CancelledError

        with patch.object(
            PostgresRepository, "process_maintenance", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(asyncio.CancelledError):
                await run_notification_worker(
                    self.repository, self.bot, LOGGER, None, sleep=sleep
                )
        self.assertEqual(len(self.sent), 1)

    async def test_process_maintenance_runtime_helper(self):
        created = await self.create(hours=1, warning_minutes=[])
        await sql(
            self.repository,
            "UPDATE site_maintenance SET starts_at = NOW() - INTERVAL '1 second', "
            "ends_at = NOW() + INTERVAL '1 hour' WHERE id = $1",
            created.id,
        )
        summary = await process_maintenance(self.repository, LOGGER)
        self.assertEqual(summary["started"], [created.id])


class MaintenanceApiTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.register()
        self.make_admin()

    def payload(self, hours=2, **overrides):
        now = datetime.now(UTC)
        body = {
            "title": "Небольшая пауза для больших улучшений",
            "description": "Скоро вернёмся!",
            "starts_at": (now + timedelta(hours=hours)).isoformat(),
            "ends_at": (now + timedelta(hours=hours + 1)).isoformat(),
            "warning_minutes": [60, 10],
        }
        body.update(overrides)
        return body

    def test_public_status_is_minimal(self):
        anonymous = self.make_client(self.client.app)
        response = anonymous.get("/api/maintenance")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        data = response.json()
        self.assertEqual(data["mode"], "normal")
        self.assertIsNone(data["title"])

        created = self.client.post("/api/admin/maintenance", json=self.payload())
        self.assertEqual(created.status_code, 201, created.text)
        data = anonymous.get("/api/maintenance").json()
        self.assertEqual(data["mode"], "scheduled")
        self.assertEqual(data["title"], "Небольшая пауза для больших улучшений")
        self.assertEqual(
            set(data),
            {
                "mode",
                "title",
                "description",
                "starts_at",
                "ends_at",
                "server_time",
                "retry_after",
            },
        )

    def test_admin_endpoints_require_admin(self):
        anonymous = self.make_client(self.client.app)
        self.assertEqual(anonymous.get("/api/admin/maintenance").status_code, 401)
        self.assertEqual(
            anonymous.post("/api/admin/maintenance", json=self.payload()).status_code,
            401,
        )
        member = self.make_client(self.client.app)
        self.register("member@example.com", client=member)
        teacher = self.make_client(self.client.app)
        self.register("teacher@example.com", client=teacher)
        self.make_admin(telegram_id=901, role="teacher", client=teacher)
        created = self.client.post("/api/admin/maintenance", json=self.payload()).json()
        window_id = created["window"]["id"]
        for client in (member, teacher):
            self.assertEqual(client.get("/api/admin/maintenance").status_code, 403)
            self.assertEqual(
                client.post("/api/admin/maintenance", json=self.payload()).status_code,
                403,
            )
            for action in ("start", "complete", "cancel"):
                self.assertEqual(
                    client.post(
                        "/api/admin/maintenance/{}/{}".format(window_id, action)
                    ).status_code,
                    403,
                )
            self.assertEqual(
                client.patch(
                    "/api/admin/maintenance/{}".format(window_id), json={"title": "x"}
                ).status_code,
                403,
            )
        self.assertEqual(
            self.client.get("/api/admin/maintenance").json()["current"]["window"][
                "status"
            ],
            "scheduled",
        )

    def test_lifecycle_through_api(self):
        created = self.client.post("/api/admin/maintenance", json=self.payload())
        self.assertEqual(created.status_code, 201, created.text)
        detail = created.json()
        window_id = detail["window"]["id"]
        self.assertEqual(
            [(w["minutes"], w["state"]) for w in detail["warnings"]],
            [(60, "wait"), (10, "wait")],
        )
        conflict = self.client.post(
            "/api/admin/maintenance", json=self.payload(hours=5)
        )
        self.assertEqual(conflict.status_code, 409)

        patched = self.client.patch(
            "/api/admin/maintenance/{}".format(window_id),
            json={"ends_at": None, "title": "Новый заголовок"},
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertIsNone(patched.json()["window"]["ends_at"])
        self.assertEqual(patched.json()["window"]["revision"], 2)

        started = self.client.post("/api/admin/maintenance/{}/start".format(window_id))
        self.assertEqual(started.json()["window"]["status"], "active")
        self.assertEqual(self.client.get("/api/maintenance").json()["mode"], "active")
        self.assertEqual(
            self.client.post(
                "/api/admin/maintenance/{}/cancel".format(window_id)
            ).status_code,
            409,
        )
        done = self.client.post("/api/admin/maintenance/{}/complete".format(window_id))
        self.assertEqual(done.json()["window"]["status"], "completed")
        self.assertEqual(self.client.get("/api/maintenance").json()["mode"], "normal")
        detail = self.client.get("/api/admin/maintenance/{}".format(window_id)).json()
        self.assertEqual(
            [e["event"] for e in detail["events"]][:4],
            ["created", "rescheduled", "started", "notifications_queued"],
        )
        kinds = {item["kind"]: item for item in detail["delivery"]}
        self.assertEqual(kinds["maintenance_started"]["pending"], 1)  # только админ
        self.assertEqual(self.client.get("/api/admin/maintenance/999").status_code, 404)

    def test_validation_errors(self):
        naive = self.payload(starts_at="2030-01-01T10:00:00")
        self.assertEqual(
            self.client.post("/api/admin/maintenance", json=naive).status_code, 422
        )
        past = self.payload(hours=-1)
        self.assertEqual(
            self.client.post("/api/admin/maintenance", json=past).status_code, 422
        )
        bad_minutes = self.payload(warning_minutes=[0])
        self.assertEqual(
            self.client.post("/api/admin/maintenance", json=bad_minutes).status_code,
            422,
        )
        missing_start = self.payload()
        del missing_start["starts_at"]
        self.assertEqual(
            self.client.post("/api/admin/maintenance", json=missing_start).status_code,
            422,
        )

    def test_preview_writes_and_sends_nothing(self):
        response = self.client.post(
            "/api/admin/maintenance/preview", json=self.payload()
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(
            [m["kind"] for m in data["messages"]], list(maintenance.MAINTENANCE_KINDS)
        )
        self.assertTrue(all(m["text"] for m in data["messages"]))
        self.assertEqual(data["recipients"], 1)
        self.assertEqual(
            self.run_async(
                fetchval(self.repository, "SELECT COUNT(*) FROM site_maintenance")
            ),
            0,
        )
        self.assertEqual(
            self.run_async(
                fetchval(self.repository, "SELECT COUNT(*) FROM user_notifications")
            ),
            0,
        )
        self.bot.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
