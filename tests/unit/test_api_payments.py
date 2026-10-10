import hashlib
import hmac
import time
import unittest
import uuid
from unittest.mock import AsyncMock


from flamenco_bot.api.app import create_app
from flamenco_bot.payments import YooKassaClient
from flamenco_bot.runtime.security import AuthRateLimiter, SupportRateLimiter
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


class ApiPaymentsTests(PostgresApiTestCase):
    def setUp(self):
        super().setUp()
        app = create_app()
        app.state.repository = self.repository
        app.state.auth_service = AuthService(self.repository, BOT_TOKEN)
        app.state.bot = AsyncMock()
        app.state.support_limiter = SupportRateLimiter()
        app.state.auth_limiter = AuthRateLimiter()
        # Незаполненный return_url => YooKassaClient.is_configured == False,
        # тот же способ, которым checkout остаётся недоступен без реального
        # подключения ЮKassa в продакшене (см. WebConfig.WEB_YOOKASSA_RETURN_URL).
        app.state.payment_gateway = YooKassaClient("", "", "")
        self.client = self.make_client(app)

    def _login(self, telegram_id=555):
        payload = {
            "id": telegram_id,
            "first_name": "Анна",
            "auth_date": int(time.time()),
        }
        response = self.client.post(
            "/api/auth/telegram", json=sign_telegram_payload(payload)
        )
        self.assertEqual(response.status_code, 200)

    def test_checkout_requires_login(self):
        response = self.client.post(
            "/api/payments/checkout", json={"package_key": "single"}
        )
        self.assertEqual(response.status_code, 401)

    def test_checkout_requires_linked_telegram(self):
        self.client.post(
            "/api/auth/register",
            json={
                "email": "anna@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Анна",
            },
        )
        response = self.client.post(
            "/api/payments/checkout", json={"package_key": "single"}
        )
        self.assertEqual(response.status_code, 409)

    def test_checkout_rejects_unknown_package(self):
        self._login()
        response = self.client.post(
            "/api/payments/checkout", json={"package_key": "does-not-exist"}
        )
        self.assertEqual(response.status_code, 404)

    def test_checkout_unavailable_without_yookassa_configured(self):
        self._login()
        response = self.client.post(
            "/api/payments/checkout", json={"package_key": "single"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("ЮKassa", response.json()["detail"])

    def test_check_payment_status_not_found(self):
        self._login()
        response = self.client.get("/api/payments/999/check")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "not_found")

    def test_payment_history_is_empty_for_new_user(self):
        self._login()
        response = self.client.get("/api/payments/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_payment_history_lists_seeded_payment(self):
        self._login(telegram_id=777)

        async def seed():
            await self.repository.create_lesson_payment(
                telegram_id=777,
                package_key="single",
                package_title="Разовое занятие",
                lessons=1,
                amount_minor=100000,
                provider_payment_id="provider-payment-1",
                confirmation_url="https://pay.example.test/confirm/1",
                idempotence_key=uuid.uuid4(),
            )

        self.run_async(seed())

        response = self.client.get("/api/payments/me")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["package_key"], "single")
        self.assertEqual(body[0]["status"], "pending")
        self.assertEqual(body[0]["amount_minor"], 100000)
        self.assertNotIn("provider_payment_id", body[0])


if __name__ == "__main__":
    unittest.main()
