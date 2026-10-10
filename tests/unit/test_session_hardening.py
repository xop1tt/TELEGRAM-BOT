"""Хранение веб-сессий (только SHA-256 токена) и PBKDF2 вне event loop."""

import hashlib
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from flamenco_bot.database.repository import hash_session_token
from flamenco_bot.services import AuthService
from flamenco_bot.services import auth as auth_module
from tests.pg import PostgresTestCase, fetch_all


class SessionTokenHashingTests(PostgresTestCase):
    async def test_raw_token_is_not_stored_but_lookup_and_revocation_work(self):
        repository = self.repository
        await repository.get_or_create_profile(555, "Анна")
        user = await repository.get_or_create_web_user_from_telegram(555, "Анна")
        token = "cookie-token-value"
        expires = datetime.now(timezone.utc) + timedelta(days=7)

        await repository.create_web_session(user.id, token, expires)

        stored = [
            record["token_hash"]
            for record in await fetch_all(
                repository, "SELECT token_hash FROM web_sessions"
            )
        ]
        self.assertEqual(stored, [hashlib.sha256(token.encode()).hexdigest()])
        self.assertNotIn(token, stored)
        self.assertEqual(await repository.get_web_session_user_id(token), user.id)
        self.assertIsNone(await repository.get_web_session_user_id(stored[0]))

        await repository.delete_web_session(token)
        self.assertIsNone(await repository.get_web_session_user_id(token))

    def test_hash_is_sha256_hex(self):
        self.assertEqual(
            hash_session_token("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )


class PasswordHashingOffEventLoopTests(PostgresTestCase):
    async def test_hash_and_verify_run_outside_event_loop_thread(self):
        loop_thread = threading.get_ident()
        seen = {}
        real_hash, real_verify = auth_module.hash_password, auth_module.verify_password

        def tracking_hash(password):
            seen["hash"] = threading.get_ident()
            return real_hash(password)

        def tracking_verify(password, stored):
            seen["verify"] = threading.get_ident()
            return real_verify(password, stored)

        service = AuthService(self.repository, "123456:test-token")
        with (
            patch.object(auth_module, "hash_password", tracking_hash),
            patch.object(auth_module, "verify_password", tracking_verify),
        ):
            await service.register_with_email("a@example.com", "long-password", "Анна")
            user = await service.authenticate_with_email(
                "a@example.com", "long-password"
            )

        self.assertEqual(user.email, "a@example.com")
        self.assertNotEqual(seen["hash"], loop_thread)
        self.assertNotEqual(seen["verify"], loop_thread)

    async def test_security_parameters_are_unchanged(self):
        stored = auth_module.hash_password("long-password")
        algorithm, iterations, salt_hex, derived_hex = stored.split("$")
        self.assertEqual(algorithm, "pbkdf2_sha256")
        self.assertEqual(int(iterations), 600_000)
        self.assertEqual(len(bytes.fromhex(salt_hex)), 16)
        self.assertEqual(len(bytes.fromhex(derived_hex)), 32)


if __name__ == "__main__":
    unittest.main()
