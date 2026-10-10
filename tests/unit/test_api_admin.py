import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from flamenco_bot.api.app import create_app
from flamenco_bot.payments import YooKassaClient
from flamenco_bot.runtime.security import AuthRateLimiter, SupportRateLimiter
from flamenco_bot.services import AuthService

from tests.pg import PostgresApiTestCase, PostgresTestCase, sql
from .test_api_payments import BOT_TOKEN, sign_telegram_payload


PASSWORD = "correct-horse-battery-staple"


class ApiTestBase(PostgresApiTestCase):
    def setUp(self):
        super().setUp()
        app = create_app()
        app.state.repository = self.repository
        app.state.auth_service = AuthService(self.repository, BOT_TOKEN)
        app.state.bot = AsyncMock()
        app.state.support_limiter = SupportRateLimiter()
        app.state.auth_limiter = AuthRateLimiter()
        app.state.payment_gateway = YooKassaClient("", "", "")
        self.bot = app.state.bot
        self.client = self.make_client(app)

    def register(self, email="admin@example.com", client=None):
        response = (client or self.client).post(
            "/api/auth/register",
            json={"email": email, "password": PASSWORD, "display_name": "Админ"},
        )
        self.assertEqual(response.status_code, 201)
        return response.json()

    def make_admin(self, user_id=None, telegram_id=900, role="admin", client=None):
        """Привязывает Telegram к текущему аккаунту и даёт ему роль
        (права — только роль привязанного Telegram в staff_members)."""
        self.link_telegram(telegram_id, client=client)
        self.run_async(
            sql(
                self.repository,
                "INSERT INTO staff_members (telegram_id, role) VALUES ($1, $2) "
                "ON CONFLICT (telegram_id) DO UPDATE SET role = EXCLUDED.role",
                telegram_id,
                role,
            )
        )

    def link_telegram(self, telegram_id=900, client=None):
        payload = sign_telegram_payload(
            {"id": telegram_id, "first_name": "Админ", "auth_date": int(time.time())}
        )
        response = (client or self.client).post("/api/auth/me/telegram", json=payload)
        self.assertEqual(response.status_code, 200)

    def future(self, days=3):
        return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


class AdminApiTests(ApiTestBase):
    # ---------- доступ ----------

    def test_admin_endpoints_require_login(self):
        for path in ("/api/admin/dashboard", "/api/admin/metrics", "/api/admin/users"):
            self.assertEqual(self.client.get(path).status_code, 401, path)

    def test_regular_user_gets_403(self):
        self.register("user@example.com")
        for path in (
            "/api/admin/dashboard",
            "/api/admin/metrics",
            "/api/admin/slots",
            "/api/admin/users",
            "/api/admin/payments",
            "/api/admin/support",
        ):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        response = self.client.post(
            "/api/admin/slots",
            json={"class_key": "beginner", "starts_at": self.future(), "capacity": 5},
        )
        self.assertEqual(response.status_code, 403)

    def test_me_reports_role(self):
        user = self.register()
        me = self.client.get("/api/auth/me").json()
        self.assertFalse(me["is_admin"])
        self.assertIsNone(me["role"])
        self.make_admin(user["id"])
        me = self.client.get("/api/auth/me").json()
        self.assertTrue(me["is_admin"])
        self.assertEqual(me["role"], "admin")

    def test_account_without_linked_telegram_has_no_rights(self):
        # Роль привязана к Telegram ID: аккаунт по email без Telegram —
        # не сотрудник, даже если когда-то был users.is_admin.
        user = self.register()
        self.run_async(
            sql(
                self.repository,
                "UPDATE users SET is_admin = TRUE WHERE id = $1",
                user["id"],
            )
        )
        self.assertFalse(self.client.get("/api/auth/me").json()["is_admin"])
        self.assertEqual(self.client.get("/api/admin/dashboard").status_code, 403)

    # ---------- роли ----------

    def test_teacher_sees_only_own_slots_and_no_admin_sections(self):
        self.register("admin@example.com")
        self.make_admin()
        teacher_client = self.make_client(self.client.app)
        self.register("teacher@example.com", client=teacher_client)
        self.make_admin(telegram_id=901, role="teacher", client=teacher_client)
        own = self.client.post(
            "/api/admin/slots",
            json={
                "class_key": "beginner",
                "starts_at": self.future(),
                "capacity": 5,
                "teacher_telegram_id": 901,
            },
        ).json()
        other = self.client.post(
            "/api/admin/slots",
            json={"class_key": "beginner", "starts_at": self.future(4), "capacity": 5},
        ).json()
        self.assertEqual(own["teacher_telegram_id"], 901)
        self.assertEqual(own["teacher_name"], "Админ")

        listed = teacher_client.get("/api/admin/slots").json()
        self.assertEqual([slot["id"] for slot in listed], [own["id"]])
        self.assertEqual(
            teacher_client.post(f"/api/admin/slots/{other['id']}/close").status_code,
            404,
        )
        self.assertEqual(
            teacher_client.post(f"/api/admin/slots/{own['id']}/close").json()["status"],
            "closed",
        )
        for path in (
            "/api/admin/dashboard",
            "/api/admin/users",
            "/api/admin/payments",
            "/api/admin/staff",
        ):
            self.assertEqual(teacher_client.get(path).status_code, 403, path)
        self.assertEqual(
            teacher_client.post(
                "/api/admin/slots",
                json={
                    "class_key": "beginner",
                    "starts_at": self.future(),
                    "capacity": 5,
                },
            ).status_code,
            403,
        )
        self.assertEqual(teacher_client.get("/api/admin/support").status_code, 200)
        # Публичное расписание показывает преподавателя.
        public = self.client.get(f"/api/schedule/{own['id']}").json()
        self.assertEqual(public["teacher_name"], "Админ")

    def test_staff_management_and_last_admin_guard(self):
        self.register()
        self.make_admin()
        self.run_async(self.repository.get_or_create_profile(902, "Мария"))

        created = self.client.put(
            "/api/admin/staff/902",
            json={"role": "teacher", "display_name": "  Мария  Гарсия "},
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["public_name"], "Мария Гарсия")
        roles = {
            m["telegram_id"]: m["role"]
            for m in self.client.get("/api/admin/staff").json()
        }
        self.assertEqual(roles, {900: "admin", 902: "teacher"})

        self.assertEqual(
            self.client.put(
                "/api/admin/staff/903", json={"role": "teacher"}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.put("/api/admin/staff/902", json={"role": "boss"}).status_code,
            422,
        )
        # Себя — последнего администратора — понизить или удалить нельзя.
        self.assertEqual(
            self.client.put(
                "/api/admin/staff/900", json={"role": "teacher"}
            ).status_code,
            409,
        )
        self.assertEqual(self.client.delete("/api/admin/staff/900").status_code, 409)
        self.assertEqual(self.client.delete("/api/admin/staff/902").status_code, 204)
        self.assertEqual(self.client.delete("/api/admin/staff/902").status_code, 404)
        audit = self.run_async(self.repository.list_staff_audit())
        self.assertEqual(
            [entry.action for entry in audit], ["staff_removed", "staff_role_set"]
        )

    # ---------- каталог и правила ----------

    def test_catalog_is_editable_by_admin_and_shown_publicly(self):
        self.register()
        self.make_admin()
        created = self.client.post(
            "/api/admin/packages",
            json={
                "key": "pack_12",
                "title": "Абонемент на 12 занятий",
                "lessons": 12,
                "price_rub": 9600,
                "validity_days": 90,
                "sort_order": 40,
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        public = {p["key"]: p for p in self.client.get("/api/packages").json()}
        self.assertEqual(public["pack_12"]["validity_days"], 90)
        self.assertEqual(public["pack_12"]["price_rub"], 9600)

        # Скрытый абонемент не продаётся, но остаётся в админке.
        hidden = dict(created.json(), is_active=False)
        self.assertEqual(
            self.client.put("/api/admin/packages/pack_12", json=hidden).status_code,
            200,
        )
        self.assertNotIn(
            "pack_12", {p["key"] for p in self.client.get("/api/packages").json()}
        )
        self.assertIn(
            "pack_12",
            {p["key"] for p in self.client.get("/api/admin/packages").json()},
        )
        self.assertEqual(
            self.client.post("/api/admin/packages", json=hidden).status_code, 409
        )
        renamed = dict(hidden, key="other")
        self.assertEqual(
            self.client.put("/api/admin/packages/pack_12", json=renamed).status_code,
            422,
        )

        # Новое направление — в /api/classes и доступно для занятий;
        # скрытое — нет.
        direction = {
            "key": "tablao",
            "title": "Табло-класс",
            "short_title": "Табло",
            "description": "Подготовка номера",
            "level": "Для продолжающих",
            "sort_order": 5,
        }
        self.assertEqual(
            self.client.post("/api/admin/classes", json=direction).status_code, 201
        )
        classes = [c["key"] for c in self.client.get("/api/classes").json()]
        self.assertEqual(classes[0], "tablao")
        slot = self.client.post(
            "/api/admin/slots",
            json={"class_key": "tablao", "starts_at": self.future(), "capacity": 6},
        )
        self.assertEqual(slot.status_code, 201, slot.text)
        self.assertEqual(slot.json()["class_label"], "Табло-класс")
        self.client.put(
            "/api/admin/classes/tablao", json=dict(direction, is_active=False)
        )
        self.assertNotIn(
            "tablao", [c["key"] for c in self.client.get("/api/classes").json()]
        )
        self.assertEqual(
            self.client.post(
                "/api/admin/slots",
                json={"class_key": "tablao", "starts_at": self.future(), "capacity": 6},
            ).status_code,
            422,
        )
        audit = [e.action for e in self.run_async(self.repository.list_staff_audit())]
        self.assertIn("lesson_package_created", audit)
        self.assertIn("class_format_updated", audit)

    def test_booking_rules_are_set_by_admin(self):
        self.register()
        self.make_admin()
        self.assertEqual(
            self.client.get("/api/admin/settings").json(),
            {"cancellation_deadline_hours": 24, "rebook_cooldown_hours": 12},
        )
        updated = self.client.put(
            "/api/admin/settings",
            json={"cancellation_deadline_hours": 6, "rebook_cooldown_hours": 0},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(
            self.client.get("/api/bookings/rules").json(),
            {"cancellation_deadline_hours": 6, "rebook_cooldown_hours": 0},
        )
        self.assertEqual(
            self.client.put(
                "/api/admin/settings",
                json={"cancellation_deadline_hours": 500, "rebook_cooldown_hours": 0},
            ).status_code,
            422,
        )

    def test_catalog_and_settings_are_admin_only(self):
        self.register()
        self.make_admin(role="teacher")
        for path in (
            "/api/admin/classes",
            "/api/admin/packages",
            "/api/admin/settings",
        ):
            self.assertEqual(self.client.get(path).status_code, 403, path)

    # ---------- сводка и нагрузка ----------

    def test_metrics_report_requests_process_and_database(self):
        user = self.register()
        self.make_admin(user["id"])
        self.client.get("/api/schedule")
        body = self.client.get("/api/admin/metrics").json()
        self.assertGreaterEqual(body["requests"]["total_since_start"], 2)
        self.assertGreater(body["process"]["uptime_seconds"], -1)
        self.assertEqual(body["database"]["backend"], "postgres")
        self.assertGreaterEqual(body["database"]["active_web_sessions"], 1)

    def test_dashboard_counts_today_and_week(self):
        user = self.register()
        self.make_admin(user["id"])
        for days in (1, 2):
            response = self.client.post(
                "/api/admin/slots",
                json={
                    "class_key": "beginner",
                    "starts_at": self.future(days),
                    "capacity": 4,
                },
            )
            self.assertEqual(response.status_code, 201)
        body = self.client.get("/api/admin/dashboard").json()
        self.assertEqual(body["slots_week"], 2)
        self.assertEqual(body["free_seats_week"], 8)
        self.assertEqual(body["total_web_users"], 1)

    # ---------- расписание ----------

    def test_schedule_lifecycle(self):
        user = self.register()
        self.make_admin(user["id"])
        created = self.client.post(
            "/api/admin/slots",
            json={
                "class_key": "intermediate",
                "starts_at": self.future(),
                "capacity": 6,
            },
        )
        self.assertEqual(created.status_code, 201)
        slot_id = created.json()["id"]
        self.assertEqual(created.json()["booked"], 0)

        updated = self.client.patch(f"/api/admin/slots/{slot_id}", json={"capacity": 8})
        self.assertEqual(updated.json()["capacity"], 8)

        closed = self.client.post(f"/api/admin/slots/{slot_id}/close")
        self.assertEqual(closed.json()["status"], "closed")
        self.assertEqual(
            self.client.post(f"/api/admin/slots/{slot_id}/close").status_code, 409
        )
        reopened = self.client.post(f"/api/admin/slots/{slot_id}/reopen")
        self.assertEqual(reopened.json()["status"], "open")

        self.assertEqual(
            self.client.get(f"/api/admin/slots/{slot_id}/participants").json(), []
        )
        cancelled = self.client.post(
            f"/api/admin/slots/{slot_id}/cancel", json={"reason": "Болезнь педагога"}
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["slot"]["status"], "cancelled")
        listed = self.client.get("/api/admin/slots").json()
        self.assertEqual([slot["status"] for slot in listed], ["cancelled"])

    def test_create_slot_validates_input(self):
        user = self.register()
        self.make_admin(user["id"])
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        for payload in (
            {"class_key": "unknown", "starts_at": self.future(), "capacity": 5},
            {"class_key": "beginner", "starts_at": past, "capacity": 5},
            {"class_key": "beginner", "starts_at": self.future(), "capacity": 0},
        ):
            self.assertEqual(
                self.client.post("/api/admin/slots", json=payload).status_code,
                422,
                payload,
            )
        self.assertEqual(
            self.client.post("/api/admin/slots/999/close").status_code, 404
        )

    # ---------- пользователи ----------

    def test_users_search_shows_one_row_per_person(self):
        admin = self.register()
        self.make_admin(admin["id"])

        # Участник: сначала вошёл только через Telegram (в боте/на сайте),
        # потом зарегистрировался по email и привязал тот же Telegram.
        other = self.make_client(self.client.app)
        telegram_login = sign_telegram_payload(
            {"id": 555, "first_name": "Анна", "auth_date": int(time.time())}
        )
        self.assertEqual(
            other.post("/api/auth/telegram", json=telegram_login).status_code, 200
        )
        other.post("/api/auth/logout")
        self.register("anna@example.com", client=other)
        self.link_telegram(555, client=other)

        rows = self.client.get("/api/admin/users", params={"q": "анна"}).json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "anna@example.com")
        self.assertEqual(rows[0]["telegram_id"], 555)
        by_email = self.client.get("/api/admin/users", params={"q": "anna@"}).json()
        self.assertEqual([row["telegram_id"] for row in by_email], [555])

    # ---------- поддержка ----------

    def test_support_reply_and_close(self):
        member = self.make_client(self.client.app)
        member.post(
            "/api/auth/telegram",
            json=sign_telegram_payload(
                {"id": 777, "first_name": "Ира", "auth_date": int(time.time())}
            ),
        )
        submitted = member.post("/api/support", json={"body": "Не могу записаться"})
        self.assertEqual(submitted.status_code, 201)

        admin = self.register()
        self.make_admin(admin["id"])
        self.link_telegram(900)
        tickets = self.client.get("/api/admin/support").json()
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["user_name"], "Ира")
        ticket_id = tickets[0]["id"]

        self.bot.send_message.reset_mock()
        reply = self.client.post(
            f"/api/admin/support/{ticket_id}/reply", json={"body": "Уже исправили"}
        )
        self.assertEqual(reply.json()["status"], "delivered")
        self.assertEqual(self.bot.send_message.await_args.args[0], 777)

        thread = self.client.get(f"/api/admin/support/{ticket_id}").json()
        self.assertEqual(
            [message["sender_role"] for message in thread["messages"]],
            ["user", "admin"],
        )
        self.assertEqual(
            self.client.post(f"/api/admin/support/{ticket_id}/close").json()["status"],
            "closed",
        )
        self.assertEqual(
            self.client.post(
                f"/api/admin/support/{ticket_id}/reply", json={"body": "Ещё"}
            ).status_code,
            404,
        )
        self.assertEqual(self.client.get("/api/admin/support").json(), [])


class CredentialsApiTests(ApiTestBase):
    """Email и пароль для аккаунта, созданного через Telegram."""

    def login_with_telegram(self, telegram_id=555, client=None):
        payload = sign_telegram_payload(
            {"id": telegram_id, "first_name": "Анна", "auth_date": int(time.time())}
        )
        response = (client or self.client).post("/api/auth/telegram", json=payload)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_telegram_account_gets_email_login_without_duplicate(self):
        account = self.login_with_telegram()
        response = self.client.post(
            "/api/auth/me/credentials",
            json={"email": "Anna@Example.com", "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["email"], "anna@example.com")

        fresh = self.make_client(self.client.app)
        login = fresh.post(
            "/api/auth/login", json={"email": "anna@example.com", "password": PASSWORD}
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["id"], account["id"])
        self.assertEqual(login.json()["telegram_id"], 555)

        again = self.client.post(
            "/api/auth/me/credentials",
            json={"email": "other@example.com", "password": PASSWORD},
        )
        self.assertEqual(again.status_code, 409)

    def test_email_taken_by_other_account_returns_409(self):
        self.register("anna@example.com")
        other = self.make_client(self.client.app)
        self.login_with_telegram(client=other)
        response = other.post(
            "/api/auth/me/credentials",
            json={"email": "anna@example.com", "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("объединятся", response.json()["detail"])

    def test_credentials_validate_email_and_password(self):
        self.login_with_telegram()
        for payload in (
            {"email": "not-an-email", "password": PASSWORD},
            {"email": "anna@example.com", "password": "short"},
        ):
            response = self.client.post("/api/auth/me/credentials", json=payload)
            self.assertEqual(response.status_code, 422, payload)


class BootstrapAdminTests(PostgresTestCase):
    async def test_server_command_creates_profile_and_admin_role(self):
        member = await self.repository.bootstrap_admin(4242, "Владелец")
        self.assertEqual((member.role, member.user_name), ("admin", "Владелец"))
        profile = await self.repository.get_profile(4242)
        self.assertTrue(profile.is_admin)
        # Повтор не меняет имя уже существующего профиля.
        again = await self.repository.bootstrap_admin(4242, "Другое имя")
        self.assertEqual(again.user_name, "Владелец")
        self.assertFalse(await self.repository.revoke_staff_from_server(4243))


if __name__ == "__main__":
    unittest.main()
