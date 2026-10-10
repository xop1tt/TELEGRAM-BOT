"""Серверные веб-сессии: генерация токена и реальный отзыв по logout.

Раньше сессия была самоподписанным JWT — logout только удалял cookie у
клиента, а похищенный токен оставался действителен ещё 7 дней (см. аудит
безопасности). Теперь сессия хранится в БД (``web_sessions``), и logout
должен реально сделать cookie бесполезной, даже если её значение переслать
отдельным запросом без участия браузера — это проверяется здесь сквозным
HTTP-тестом, а не только проверкой функции.
"""

import hashlib
import hmac
import time
import unittest
from unittest.mock import AsyncMock


from flamenco_bot.api.app import create_app
from flamenco_bot.api.security import SESSION_COOKIE_NAME, generate_session_token
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


class SessionTokenGenerationTests(unittest.TestCase):
    def test_tokens_are_non_empty_and_unique(self):
        first = generate_session_token()
        second = generate_session_token()

        self.assertTrue(first)
        self.assertNotEqual(first, second)

    def test_token_has_enough_entropy_to_resist_guessing(self):
        token = generate_session_token()

        # secrets.token_urlsafe(32) кодирует 32 байта в base64url — длина
        # печатного представления не должна давать повод усомниться в этом.
        self.assertGreaterEqual(len(token), 32)


class SessionRevocationApiTests(PostgresApiTestCase):
    """Сквозной тест через настоящий HTTP-слой, а не вызов функции напрямую."""

    def setUp(self):
        super().setUp()
        app = create_app()
        app.state.repository = self.repository
        app.state.auth_service = AuthService(self.repository, BOT_TOKEN)
        app.state.bot = AsyncMock()
        app.state.support_limiter = SupportRateLimiter()
        app.state.auth_limiter = AuthRateLimiter()
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

    def test_unknown_session_cookie_is_rejected(self):
        self.client.cookies.set(SESSION_COOKIE_NAME, "this-token-was-never-issued")

        response = self.client.get("/api/auth/me")

        self.assertEqual(response.status_code, 401)

    def test_logout_revokes_the_session_server_side(self):
        """Главная гарантия, которой не было у самоподписанного JWT.

        Снимаем cookie, выставленную при входе, ДО вызова logout — чтобы
        убедиться, что отзыв происходит в БД, а не только путём удаления
        cookie в ответе (которое клиент мог бы и не применить).
        """
        self._login()
        issued_cookie = self.client.cookies.get(SESSION_COOKIE_NAME)
        self.assertIsNotNone(issued_cookie)

        logout_response = self.client.post("/api/auth/logout")
        self.assertEqual(logout_response.status_code, 204)

        replay_client = self.make_client(self.client.app)
        replay_client.cookies.set(SESSION_COOKIE_NAME, issued_cookie)
        replayed = replay_client.get("/api/auth/me")

        self.assertEqual(replayed.status_code, 401)

    def test_logout_without_a_session_cookie_is_a_no_op(self):
        response = self.client.post("/api/auth/logout")

        self.assertEqual(response.status_code, 204)

    def test_session_is_scoped_to_the_issuing_user(self):
        self._login(telegram_id=111)
        first_user_me = self.client.get("/api/auth/me")
        self.assertEqual(first_user_me.json()["telegram_id"], 111)

        self.client.post("/api/auth/logout")
        self._login(telegram_id=222)
        second_user_me = self.client.get("/api/auth/me")

        self.assertEqual(second_user_me.json()["telegram_id"], 222)


if __name__ == "__main__":
    unittest.main()
