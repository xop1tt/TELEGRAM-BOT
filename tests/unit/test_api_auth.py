import hashlib
import hmac
import time
import unittest


from flamenco_bot.api.app import create_app
from flamenco_bot.runtime.security import AuthRateLimiter
from flamenco_bot.services import AuthService
from tests.pg import PostgresApiTestCase


BOT_TOKEN = "123456:test-token"


def sign_telegram_payload(payload, bot_token=BOT_TOKEN):
    data = dict(payload)
    check_string = "\n".join("{}={}".format(key, data[key]) for key in sorted(data))
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    data["hash"] = hmac.new(
        secret_key, check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return data


def make_telegram_payload(telegram_id=555, first_name="Анна", auth_date=None):
    return {
        "id": telegram_id,
        "first_name": first_name,
        "auth_date": int(auth_date if auth_date is not None else time.time()),
    }


class ApiAuthTests(PostgresApiTestCase):
    def setUp(self):
        super().setUp()
        app = create_app()
        app.state.repository = self.repository
        app.state.auth_service = AuthService(self.repository, BOT_TOKEN)
        app.state.auth_limiter = AuthRateLimiter()
        # https-схема нужна, чтобы httpx-клиент сохранял Secure-cookie сессии
        # между запросами — так же, как это делает настоящий браузер на сайте.
        self.client = self.make_client(app)

    def test_register_sets_session_cookie_and_returns_user(self):
        response = self.client.post(
            "/api/auth/register",
            json={
                "email": "anna@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Анна",
            },
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["email"], "anna@example.com")
        self.assertIsNone(body["telegram_id"])
        self.assertNotIn("password", body)
        self.assertNotIn("password_hash", body)
        self.assertIn("session", response.cookies)

    def test_duplicate_registration_returns_409(self):
        payload = {
            "email": "anna@example.com",
            "password": "correct-horse-battery-staple",
            "display_name": "Анна",
        }
        self.client.post("/api/auth/register", json=payload)
        response = self.client.post("/api/auth/register", json=payload)
        self.assertEqual(response.status_code, 409)

    def test_login_with_correct_and_incorrect_password(self):
        self.client.post(
            "/api/auth/register",
            json={
                "email": "anna@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Анна",
            },
        )
        ok = self.client.post(
            "/api/auth/login",
            json={
                "email": "anna@example.com",
                "password": "correct-horse-battery-staple",
            },
        )
        self.assertEqual(ok.status_code, 200)

        bad = self.client.post(
            "/api/auth/login",
            json={"email": "anna@example.com", "password": "wrong"},
        )
        self.assertEqual(bad.status_code, 401)

    def test_me_requires_session_cookie(self):
        response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 401)

    def test_me_returns_current_user_after_login(self):
        self.client.post(
            "/api/auth/register",
            json={
                "email": "anna@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Анна",
            },
        )
        response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["email"], "anna@example.com")

    def test_logout_clears_session(self):
        self.client.post(
            "/api/auth/register",
            json={
                "email": "anna@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Анна",
            },
        )
        logout_response = self.client.post("/api/auth/logout")
        self.assertEqual(logout_response.status_code, 204)
        me_response = self.client.get("/api/auth/me")
        self.assertEqual(me_response.status_code, 401)

    def test_telegram_login_rejects_invalid_signature(self):
        payload = make_telegram_payload()
        payload["hash"] = "not-a-real-signature"
        response = self.client.post("/api/auth/telegram", json=payload)
        self.assertEqual(response.status_code, 401)

    def test_telegram_login_creates_account(self):
        payload = sign_telegram_payload(make_telegram_payload(telegram_id=1001))
        response = self.client.post("/api/auth/telegram", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["telegram_id"], 1001)

    def test_link_and_unlink_telegram_on_own_account(self):
        self.client.post(
            "/api/auth/register",
            json={
                "email": "anna@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Анна",
            },
        )
        link_response = self.client.post(
            "/api/auth/me/telegram",
            json=sign_telegram_payload(make_telegram_payload(telegram_id=2002)),
        )
        self.assertEqual(link_response.status_code, 200)
        self.assertEqual(link_response.json()["telegram_id"], 2002)

        unlink_response = self.client.delete("/api/auth/me/telegram")
        self.assertEqual(unlink_response.status_code, 200)
        self.assertIsNone(unlink_response.json()["telegram_id"])

    def test_cannot_unlink_telegram_only_login_account(self):
        self.client.post(
            "/api/auth/telegram",
            json=sign_telegram_payload(make_telegram_payload(telegram_id=3003)),
        )
        response = self.client.delete("/api/auth/me/telegram")
        self.assertEqual(response.status_code, 409)

    def test_link_telegram_already_linked_to_other_account_returns_409(self):
        self.client.post(
            "/api/auth/register",
            json={
                "email": "anna@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Анна",
            },
        )
        self.client.post(
            "/api/auth/me/telegram",
            json=sign_telegram_payload(make_telegram_payload(telegram_id=4004)),
        )
        self.client.post("/api/auth/logout")
        self.client.post(
            "/api/auth/register",
            json={
                "email": "bob@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Боб",
            },
        )
        response = self.client.post(
            "/api/auth/me/telegram",
            json=sign_telegram_payload(make_telegram_payload(telegram_id=4004)),
        )
        self.assertEqual(response.status_code, 409)

    def test_link_telegram_merges_telegram_only_account(self):
        self.client.post(
            "/api/auth/telegram",
            json=sign_telegram_payload(make_telegram_payload(telegram_id=4004)),
        )
        self.client.post("/api/auth/logout")
        self.client.post(
            "/api/auth/register",
            json={
                "email": "bob@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Боб",
            },
        )
        response = self.client.post(
            "/api/auth/me/telegram",
            json=sign_telegram_payload(make_telegram_payload(telegram_id=4004)),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["email"], "bob@example.com")
        self.assertEqual(response.json()["telegram_id"], 4004)

    def test_login_is_rate_limited_per_ip_after_too_many_attempts(self):
        self.client.post(
            "/api/auth/register",
            json={
                "email": "anna@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Анна",
            },
        )
        self.client.post("/api/auth/logout")

        # Регистрация выше уже заняла одну попытку из общего лимита на IP.
        for _ in range(9):
            response = self.client.post(
                "/api/auth/login",
                json={"email": "anna@example.com", "password": "wrong"},
            )
            self.assertEqual(response.status_code, 401)

        blocked = self.client.post(
            "/api/auth/login",
            json={"email": "anna@example.com", "password": "wrong"},
        )
        self.assertEqual(blocked.status_code, 429)

        # Лимит общий на IP, не на email — другой email тоже блокируется.
        blocked_other_identity = self.client.post(
            "/api/auth/login",
            json={"email": "someone-else@example.com", "password": "wrong"},
        )
        self.assertEqual(blocked_other_identity.status_code, 429)

    def test_register_is_rate_limited_per_ip_after_too_many_attempts(self):
        for index in range(10):
            self.client.post(
                "/api/auth/register",
                json={
                    "email": "user{}@example.com".format(index),
                    "password": "correct-horse-battery-staple",
                    "display_name": "Анна",
                },
            )

        blocked = self.client.post(
            "/api/auth/register",
            json={
                "email": "one-more@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Анна",
            },
        )
        self.assertEqual(blocked.status_code, 429)

    def test_health_endpoint_reports_backend(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["backend"], "postgres")


if __name__ == "__main__":
    unittest.main()
