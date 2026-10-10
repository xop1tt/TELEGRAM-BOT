import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from flamenco_bot.api.app import create_app
from flamenco_bot.payments import YooKassaClient
from flamenco_bot.runtime.security import AuthRateLimiter, SupportRateLimiter
from flamenco_bot.services import AuthService

from tests.pg import PostgresApiTestCase, PostgresTestCase
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

    def make_admin(self, user_id):
        self.assertTrue(
            self.run_async(self.repository.set_web_user_admin(user_id, True))
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

    def test_me_reports_admin_flag(self):
        user = self.register()
        self.assertFalse(self.client.get("/api/auth/me").json()["is_admin"])
        self.make_admin(user["id"])
        self.assertTrue(self.client.get("/api/auth/me").json()["is_admin"])

    def test_bot_admin_with_linked_telegram_is_site_admin(self):
        self.run_async(
            self.repository.get_or_create_profile(
                telegram_id=901, user_name="Админ бота", is_admin=True
            )
        )
        self.register("botadmin@example.com")
        self.link_telegram(901)
        self.assertTrue(self.client.get("/api/auth/me").json()["is_admin"])
        self.assertEqual(self.client.get("/api/admin/dashboard").status_code, 200)

    # ---------- сводка и нагрузка ----------

    def test_admin_without_telegram_can_read_but_not_change(self):
        user = self.register()
        self.make_admin(user["id"])
        self.assertEqual(self.client.get("/api/admin/dashboard").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/metrics").status_code, 200)
        response = self.client.post(
            "/api/admin/slots",
            json={"class_key": "beginner", "starts_at": self.future(), "capacity": 5},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("Telegram", response.json()["detail"])

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
        self.link_telegram(900)
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
        self.link_telegram(900)
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
        self.link_telegram(900)
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


class EnsureAdminAccountTests(PostgresTestCase):
    async def test_creates_and_promotes_admin(self):
        repository = self.repository
        service = AuthService(repository, BOT_TOKEN)
        created = await service.ensure_admin_account("Boss@Example.com", PASSWORD)
        self.assertTrue(created.is_admin)
        self.assertEqual(created.email, "boss@example.com")

        member = await service.register_with_email(
            "member@example.com", PASSWORD, "Участник"
        )
        self.assertFalse(member.is_admin)
        promoted = await service.ensure_admin_account(
            "member@example.com", "new-password-123"
        )
        self.assertEqual(promoted.id, member.id)
        self.assertTrue(promoted.is_admin)
        authenticated = await service.authenticate_with_email(
            "member@example.com", "new-password-123"
        )
        self.assertEqual(authenticated.id, member.id)


if __name__ == "__main__":
    unittest.main()
