"""Полный жизненный цикл технического обслуживания через API, фоновый шаг
и отправку в (фейковый) Telegram — включая «перезапуск» приложения.

Время планировщика задаётся явно (``process_maintenance(now=...)``), чтобы
не ждать реальных часов; публичное состояние сайта читается через API.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg

from flamenco_bot.api.app import create_app
from flamenco_bot.database.repository import PostgresRepository
from flamenco_bot.runtime.notifications import dispatch_due_notifications
from tests import pg
from tests.unit.test_api_admin import ApiTestBase

UTC = timezone.utc
MEMBERS = (2001, 2002)
LOGGER = SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
    exception=lambda *a, **k: None,
)


class MaintenanceLifecycleTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.register()
        self.make_admin()  # Telegram 900
        for telegram_id in MEMBERS:
            self.run_async(
                self.repository.get_or_create_profile(telegram_id, "Участник")
            )
        self.sent = []

        async def send(chat_id, text, reply_markup=None):
            self.sent.append((chat_id, text))

        self.fake_bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))

    def deliver(self, repository=None):
        return self.run_async(
            dispatch_due_notifications(
                repository or self.repository, self.fake_bot, LOGGER
            )
        )

    def site_mode(self):
        return self.client.get("/api/maintenance").json()["mode"]

    def received(self, fragment):
        return sorted(chat for chat, text in self.sent if fragment in text)

    def test_full_lifecycle(self):
        everyone = sorted((900, *MEMBERS))
        now = datetime.now(UTC)
        starts_at = now + timedelta(minutes=70)
        ends_at = starts_at + timedelta(hours=1)

        # 1–2. Администратор планирует работы; расписание сохранено.
        created = self.client.post(
            "/api/admin/maintenance",
            json={
                "title": "Небольшая пауза для больших улучшений",
                "description": "Скоро вернёмся!",
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
                "warning_minutes": [1440, 60, 10],
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        window_id = created.json()["window"]["id"]
        stored = self.run_async(self.repository.get_maintenance(window_id))
        self.assertEqual(stored.starts_at, starts_at)

        # 3. До начала сайт работает (режим «запланировано»).
        self.assertEqual(self.site_mode(), "scheduled")

        # 4–5. Планировщик ставит часовое предупреждение, бот доставляет.
        # (Суточное запоздало — окно создано за 70 минут — и пропущено.)
        summary = self.run_async(
            self.repository.process_maintenance(now=starts_at - timedelta(minutes=60))
        )
        self.assertEqual(summary["queued"], {"warning:60": 3})
        dispatches = self.run_async(
            self.repository.list_maintenance_dispatches(window_id)
        )
        self.assertIn(
            ("warning:1440", "late"),
            [(item["stage"], item["skipped_reason"]) for item in dispatches],
        )
        self.deliver()
        self.assertEqual(self.received("плановое обслуживание"), everyone)

        # 10-минутное — вовремя.
        self.run_async(
            self.repository.process_maintenance(now=starts_at - timedelta(minutes=10))
        )
        self.deliver()
        self.assertEqual(len(self.received("плановое обслуживание")), 6)

        # 6–8. Начало: сайт переходит в обслуживание, бот сообщает о начале.
        self.sent.clear()
        summary = self.run_async(
            self.repository.process_maintenance(now=starts_at + timedelta(seconds=2))
        )
        self.assertEqual(summary["started"], [window_id])
        self.assertEqual(self.site_mode(), "active")
        self.deliver()
        self.assertEqual(self.received("обслуживание началось"), everyone)

        # 11–12. Плановое окончание прошло, работы не закончены — обслуживание
        # продолжается, сообщения о завершении нет.
        summary = self.run_async(
            self.repository.process_maintenance(now=ends_at + timedelta(minutes=30))
        )
        self.assertEqual(summary, {"started": [], "queued": {}, "skipped": []})
        self.assertEqual(self.site_mode(), "active")
        self.deliver()
        self.assertEqual(self.received("снова работает"), [])

        # 13–15. Администратор подтверждает завершение: сайт снова работает,
        # бот сообщает о завершении.
        done = self.client.post("/api/admin/maintenance/{}/complete".format(window_id))
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(self.site_mode(), "normal")
        self.deliver()
        self.assertEqual(self.received("снова работает"), everyone)

        # 16. «Перезапуск»: новое приложение и новый пул к той же базе —
        # повторная обработка не шлёт ничего второй раз.
        total = len(self.sent)

        async def restart():
            pool = await asyncpg.create_pool(
                pg.DATABASE_URL,
                min_size=1,
                max_size=2,
                server_settings={"search_path": pg._schema},
            )
            repository = PostgresRepository(pool)
            await repository.initialize()
            return repository

        restarted = self.run_async(restart())
        try:
            app = create_app()
            app.state.repository = restarted
            client = self.make_client(app)
            self.assertEqual(client.get("/api/maintenance").json()["mode"], "normal")
            for _ in range(3):
                self.run_async(restarted.process_maintenance())
                self.deliver(restarted)
            self.assertEqual(len(self.sent), total)
        finally:
            self.run_async(restarted.close())

        # Итог по каждому участнику: 2 предупреждения, начало, завершение.
        for telegram_id in everyone:
            texts = [text for chat, text in self.sent if chat == telegram_id]
            self.assertEqual(len(texts), 2, telegram_id)  # после очистки: начало+конец
        detail = self.client.get("/api/admin/maintenance/{}".format(window_id)).json()
        delivery = {item["kind"]: item for item in detail["delivery"]}
        self.assertEqual(delivery["maintenance_warning"]["sent"], 6)
        self.assertEqual(delivery["maintenance_started"]["sent"], 3)
        self.assertEqual(delivery["maintenance_completed"]["sent"], 3)
        self.assertEqual(
            sum(item["failed"] + item["pending"] for item in delivery.values()), 0
        )
