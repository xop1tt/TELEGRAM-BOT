import hashlib
import hmac
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock


from flamenco_bot.api.app import create_app
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


def make_telegram_payload(telegram_id=555, first_name="Анна", auth_date=None):
    return {
        "id": telegram_id,
        "first_name": first_name,
        "auth_date": int(auth_date if auth_date is not None else time.time()),
    }


class ClientAreaApiTests(PostgresApiTestCase):
    def setUp(self):
        super().setUp()
        app = create_app()
        app.state.repository = self.repository
        app.state.auth_service = AuthService(self.repository, BOT_TOKEN)
        app.state.bot = AsyncMock()
        app.state.support_limiter = SupportRateLimiter()
        app.state.auth_limiter = AuthRateLimiter()
        self.client = self.make_client(app)

    def _login_with_telegram(self, telegram_id=555):
        response = self.client.post(
            "/api/auth/telegram",
            json=sign_telegram_payload(make_telegram_payload(telegram_id=telegram_id)),
        )
        self.assertEqual(response.status_code, 200)

    def _register_email_only(self):
        response = self.client.post(
            "/api/auth/register",
            json={
                "email": "anna@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Анна",
            },
        )
        self.assertEqual(response.status_code, 201)

    async def _create_open_slot(self, class_key="beginner", capacity=1):
        return await self.repository.create_class_slot(
            class_key,
            datetime.now(timezone.utc) + timedelta(days=1),
            capacity,
            admin_telegram_id=1,
        )

    def _grant_credits(self, telegram_id, credits=1):
        """Выдаёт lesson_credits — бронирование теперь списывает их напрямую."""
        self.run_async(set_credits(self.repository, telegram_id, credits))

    def test_schedule_is_public_and_lists_upcoming_slots(self):
        slot = self.run_async(self._create_open_slot())
        response = self.client.get("/api/schedule")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["id"], slot.id)
        self.assertEqual(body[0]["remaining"], 1)

    def test_schedule_rejects_unknown_class_key(self):
        response = self.client.get("/api/schedule", params={"class_key": "nope"})
        self.assertEqual(response.status_code, 422)

    def test_packages_lists_catalog_without_auth(self):
        response = self.client.get("/api/packages")
        self.assertEqual(response.status_code, 200)
        keys = {package["key"] for package in response.json()}
        self.assertIn("single", keys)

    def test_classes_lists_catalog_without_auth(self):
        response = self.client.get("/api/classes")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            {item["key"] for item in body},
            {"beginner", "intermediate", "individual"},
        )
        beginner = next(item for item in body if item["key"] == "beginner")
        self.assertEqual(beginner["label"], "Фламенко для начинающих")
        # Описание и уровень — из того же каталога, что и в боте.
        self.assertIn("без предварительной подготовки", beginner["description"])
        self.assertEqual(beginner["level"], "Для начинающих, без подготовки")

    def test_booking_requires_login(self):
        response = self.client.post("/api/bookings", json={"slot_id": 1})
        self.assertEqual(response.status_code, 401)

    def test_booking_requires_linked_telegram(self):
        self._register_email_only()
        response = self.client.post("/api/bookings", json={"slot_id": 1})
        self.assertEqual(response.status_code, 409)

    def test_booking_confirms_slot_and_lists_it_under_me(self):
        slot = self.run_async(self._create_open_slot())
        self._login_with_telegram(telegram_id=777)
        self._grant_credits(777)

        response = self.client.post("/api/bookings", json={"slot_id": slot.id})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["slot_id"], slot.id)
        self.assertFalse(body["already_booked"])

        mine = self.client.get("/api/bookings/me")
        self.assertEqual(mine.status_code, 200)
        self.assertEqual(len(mine.json()), 1)
        self.assertEqual(mine.json()[0]["slot_id"], slot.id)

    def test_booking_rejects_full_slot(self):
        slot = self.run_async(self._create_open_slot(capacity=1))
        self._login_with_telegram(telegram_id=111)
        self._grant_credits(111)
        self.client.post("/api/bookings", json={"slot_id": slot.id})
        self.client.post("/api/auth/logout")

        self._login_with_telegram(telegram_id=222)
        self._grant_credits(222)
        response = self.client.post("/api/bookings", json={"slot_id": slot.id})
        self.assertEqual(response.status_code, 409)

    def test_support_requires_linked_telegram(self):
        self._register_email_only()
        response = self.client.post("/api/support", json={"body": "Вопрос"})
        self.assertEqual(response.status_code, 409)

    def test_support_submission_is_listed_under_me(self):
        self._login_with_telegram(telegram_id=999)
        response = self.client.post("/api/support", json={"body": "Не могу записаться"})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["created"])

        mine = self.client.get("/api/support/me")
        self.assertEqual(mine.status_code, 200)
        self.assertEqual(len(mine.json()), 1)
        self.assertEqual(mine.json()[0]["last_message"], "Не могу записаться")

    def test_support_rejects_empty_message(self):
        self._login_with_telegram(telegram_id=888)
        response = self.client.post("/api/support", json={"body": "   "})
        self.assertEqual(response.status_code, 422)

    def test_profile_requires_linked_telegram(self):
        self._register_email_only()
        response = self.client.get("/api/users/me/profile")
        self.assertEqual(response.status_code, 409)

    def test_profile_reports_bot_user_data(self):
        self._login_with_telegram(telegram_id=321)
        response = self.client.get("/api/users/me/profile")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["telegram_id"], 321)
        self.assertEqual(body["lesson_credits"], 0)
        self.assertIsNone(body["phone"])
        self.assertFalse(body["is_admin"])

    def test_profile_name_can_be_updated(self):
        self._login_with_telegram(telegram_id=654)
        response = self.client.patch(
            "/api/users/me/profile", json={"user_name": "Новое имя"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user_name"], "Новое имя")

    def test_renamed_profile_is_the_name_of_the_web_account(self):
        """Единое имя: /api/auth/me показывает имя профиля, а не виджета."""
        self._login_with_telegram(telegram_id=655)
        self.client.patch("/api/users/me/profile", json={"user_name": "Новое имя"})
        self.assertEqual(
            self.client.get("/api/auth/me").json()["display_name"], "Новое имя"
        )

    def test_profile_name_update_rejects_empty_value(self):
        self._login_with_telegram(telegram_id=987)
        response = self.client.patch("/api/users/me/profile", json={"user_name": ""})
        self.assertEqual(response.status_code, 422)

    def test_profile_name_follows_the_same_rule_as_the_bot(self):
        """strip + 1–64 символа — services.profile.normalize_user_name."""
        self._login_with_telegram(telegram_id=988)
        for rejected in ("   ", "я" * 65):
            with self.subTest(user_name=rejected):
                response = self.client.patch(
                    "/api/users/me/profile", json={"user_name": rejected}
                )
                self.assertEqual(response.status_code, 422)

        response = self.client.patch(
            "/api/users/me/profile", json={"user_name": "  Анна  "}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user_name"], "Анна")

        long_name_with_spaces = " " + "я" * 64 + " "
        response = self.client.patch(
            "/api/users/me/profile", json={"user_name": long_name_with_spaces}
        )
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
