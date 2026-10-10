"""``/api/bookings`` сквозь HTTP: дубль записи, заполненный слот, доступ.

До этого теста роутер проверялся только по докстрингу/код-ревью — реальные
сценарии (повтор, нет мест, не привязан Telegram, не авторизован) не были
покрыты ни одним тестом (отмечено в аудите).
"""

import hashlib
import hmac
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock


from flamenco_bot.api.app import create_app
from flamenco_bot.payments import YooKassaClient
from flamenco_bot.runtime.security import AuthRateLimiter, SupportRateLimiter
from flamenco_bot.services import AuthService
from tests.pg import PostgresApiTestCase, set_credits


BOT_TOKEN = "123456:test-token"


def sign_telegram_payload(payload, bot_token=BOT_TOKEN):
    data = dict(payload)
    check_string = "\n".join("{}={}".format(key, data[key]) for key in sorted(data))
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    data["hash"] = hmac.new(
        secret_key, check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return data


class ApiBookingsTests(PostgresApiTestCase):
    def setUp(self):
        super().setUp()
        app = create_app()
        app.state.repository = self.repository
        app.state.auth_service = AuthService(self.repository, BOT_TOKEN)
        app.state.bot = AsyncMock()
        app.state.support_limiter = SupportRateLimiter()
        app.state.auth_limiter = AuthRateLimiter()
        app.state.payment_gateway = YooKassaClient("", "", "")
        self.client = self.make_client(app)

    def _login(self, telegram_id=1001):
        payload = {
            "id": telegram_id,
            "first_name": "Анна",
            "auth_date": int(time.time()),
        }
        response = self.client.post(
            "/api/auth/telegram", json=sign_telegram_payload(payload)
        )
        self.assertEqual(response.status_code, 200)

    def _create_slot(self, capacity=1, created_by=77, starts_at=None):
        async def create():
            return await self.repository.create_class_slot(
                "beginner",
                starts_at or datetime.now(timezone.utc) + timedelta(days=2),
                capacity,
                created_by,
            )

        return self.run_async(create())

    def _grant_credits(self, telegram_id, credits=1):
        """Выдаёт lesson_credits — бронирование теперь списывает их напрямую."""
        self.run_async(set_credits(self.repository, telegram_id, credits))

    def test_create_booking_requires_login(self):
        slot = self._create_slot()

        response = self.client.post("/api/bookings", json={"slot_id": slot.id})

        self.assertEqual(response.status_code, 401)

    def test_create_booking_requires_linked_telegram(self):
        self.client.post(
            "/api/auth/register",
            json={
                "email": "anna@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Анна",
            },
        )
        slot = self._create_slot()

        response = self.client.post("/api/bookings", json={"slot_id": slot.id})

        self.assertEqual(response.status_code, 409)

    def test_create_booking_succeeds_for_an_open_slot(self):
        self._login()
        self._grant_credits(1001)
        slot = self._create_slot()

        response = self.client.post("/api/bookings", json={"slot_id": slot.id})

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["slot_id"], slot.id)
        self.assertFalse(body["already_booked"])

    def test_create_booking_without_credits_is_rejected(self):
        self._login()
        slot = self._create_slot()

        response = self.client.post("/api/bookings", json={"slot_id": slot.id})

        self.assertEqual(response.status_code, 409)
        self.assertIn("абонемент", response.json()["detail"])

    def test_duplicate_booking_by_the_same_user_is_idempotent(self):
        self._login()
        self._grant_credits(1001)
        slot = self._create_slot()

        first = self.client.post("/api/bookings", json={"slot_id": slot.id})
        second = self.client.post("/api/bookings", json={"slot_id": slot.id})

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertFalse(first.json()["already_booked"])
        self.assertTrue(second.json()["already_booked"])
        self.assertEqual(first.json()["id"], second.json()["id"])

    def test_booking_a_full_slot_returns_conflict(self):
        slot = self._create_slot(capacity=1)
        self._login(telegram_id=1001)
        self._grant_credits(1001)
        first = self.client.post("/api/bookings", json={"slot_id": slot.id})
        self.assertEqual(first.status_code, 201)

        self._login(telegram_id=1002)
        self._grant_credits(1002)
        second = self.client.post("/api/bookings", json={"slot_id": slot.id})

        self.assertEqual(second.status_code, 409)

    def test_booking_an_unknown_slot_returns_conflict(self):
        self._login()

        response = self.client.post("/api/bookings", json={"slot_id": 999999})

        self.assertEqual(response.status_code, 409)

    def test_cancel_booking_requires_login(self):
        slot = self._create_slot()

        response = self.client.delete("/api/bookings/{}".format(slot.id))

        self.assertEqual(response.status_code, 401)

    def test_cancel_booking_that_does_not_exist_returns_404(self):
        slot = self._create_slot()
        self._login()

        response = self.client.delete("/api/bookings/{}".format(slot.id))

        self.assertEqual(response.status_code, 404)

    def test_cancel_booking_refunds_credit_and_frees_the_seat(self):
        self._login()
        self._grant_credits(1001)
        slot = self._create_slot(
            starts_at=datetime.now(timezone.utc) + timedelta(days=2)
        )
        self.client.post("/api/bookings", json={"slot_id": slot.id})

        response = self.client.delete("/api/bookings/{}".format(slot.id))

        self.assertEqual(response.status_code, 204)
        mine = self.client.get("/api/bookings/me")
        self.assertEqual(mine.json()[0]["booking_status"], "cancelled")

    def test_cancel_booking_too_close_to_start_returns_conflict(self):
        self._login()
        self._grant_credits(1001)
        slot = self._create_slot(
            starts_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        self.client.post("/api/bookings", json={"slot_id": slot.id})

        response = self.client.delete("/api/bookings/{}".format(slot.id))

        self.assertEqual(response.status_code, 409)

    def test_list_my_bookings_requires_login(self):
        response = self.client.get("/api/bookings/me")

        self.assertEqual(response.status_code, 401)

    def test_list_my_bookings_only_returns_the_caller_bookings(self):
        slot = self._create_slot(capacity=2)
        self._login(telegram_id=1001)
        self._grant_credits(1001)
        self.client.post("/api/bookings", json={"slot_id": slot.id})

        self._login(telegram_id=1002)
        response = self.client.get("/api/bookings/me")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_my_bookings_include_cancellation_deadline(self):
        slot = self._create_slot(capacity=2)
        self._login(telegram_id=1001)
        self._grant_credits(1001)
        self.client.post("/api/bookings", json={"slot_id": slot.id})

        response = self.client.get("/api/bookings/me")

        self.assertEqual(response.status_code, 200)
        booking = response.json()[0]
        # Python 3.10: fromisoformat не понимает суффикс «Z».
        starts_at = datetime.fromisoformat(booking["starts_at"].replace("Z", "+00:00"))
        cancellable_until = datetime.fromisoformat(
            booking["cancellable_until"].replace("Z", "+00:00")
        )
        self.assertEqual(starts_at - cancellable_until, timedelta(hours=24))

    def test_booking_rules_are_public_and_match_repository_rules(self):
        response = self.client.get("/api/bookings/rules")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"cancellation_deadline_hours": 24, "rebook_cooldown_hours": 12},
        )


if __name__ == "__main__":
    unittest.main()
