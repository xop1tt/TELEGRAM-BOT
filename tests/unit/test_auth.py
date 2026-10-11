import hashlib
import hmac
import time
import unittest

from flamenco_bot.services.auth import (
    AuthService,
    CannotUnlinkOnlyLoginMethodError,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidTelegramAuthError,
    TelegramAlreadyLinkedError,
    WeakPasswordError,
    hash_password,
    verify_password,
    verify_telegram_login,
)
from tests.pg import PostgresTestCase


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


class PasswordHashingTests(unittest.TestCase):
    def test_correct_password_verifies_and_wrong_password_is_rejected(self):
        stored = hash_password("correct-horse-battery-staple")
        self.assertTrue(verify_password("correct-horse-battery-staple", stored))
        self.assertFalse(verify_password("wrong-password", stored))

    def test_malformed_stored_hash_is_rejected_without_raising(self):
        self.assertFalse(verify_password("anything", "not-a-valid-hash"))


class TelegramLoginVerificationTests(unittest.TestCase):
    def test_valid_signature_returns_telegram_id(self):
        payload = sign_telegram_payload(make_telegram_payload(telegram_id=777))
        self.assertEqual(verify_telegram_login(payload, BOT_TOKEN), 777)

    def test_tampered_payload_is_rejected(self):
        payload = sign_telegram_payload(make_telegram_payload(telegram_id=777))
        payload["id"] = 999
        with self.assertRaises(InvalidTelegramAuthError):
            verify_telegram_login(payload, BOT_TOKEN)

    def test_expired_auth_date_is_rejected(self):
        payload = sign_telegram_payload(
            make_telegram_payload(auth_date=time.time() - 999999)
        )
        with self.assertRaises(InvalidTelegramAuthError):
            verify_telegram_login(payload, BOT_TOKEN)

    def test_auth_date_older_than_an_hour_is_rejected(self):
        payload = sign_telegram_payload(
            make_telegram_payload(auth_date=time.time() - 61 * 60)
        )
        with self.assertRaises(InvalidTelegramAuthError):
            verify_telegram_login(payload, BOT_TOKEN)

    def test_auth_date_within_an_hour_is_accepted(self):
        payload = sign_telegram_payload(
            make_telegram_payload(auth_date=time.time() - 50 * 60)
        )
        self.assertEqual(verify_telegram_login(payload, BOT_TOKEN), 555)

    def test_missing_hash_is_rejected(self):
        with self.assertRaises(InvalidTelegramAuthError):
            verify_telegram_login(make_telegram_payload(), BOT_TOKEN)


class AuthServiceTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.service = AuthService(self.repository, BOT_TOKEN)

    async def test_register_and_authenticate_with_email(self):
        user = await self.service.register_with_email(
            "Anna@Example.com", "correct-horse-battery-staple", "Анна"
        )
        self.assertIsNone(user.telegram_id)

        authenticated = await self.service.authenticate_with_email(
            "anna@example.com", "correct-horse-battery-staple"
        )
        self.assertEqual(authenticated.id, user.id)

    async def test_duplicate_email_registration_is_rejected(self):
        await self.service.register_with_email(
            "anna@example.com", "correct-horse-battery-staple", "Анна"
        )
        with self.assertRaises(EmailAlreadyRegisteredError):
            await self.service.register_with_email(
                "ANNA@example.com", "another-strong-password", "Анна 2"
            )

    async def test_weak_password_is_rejected(self):
        with self.assertRaises(WeakPasswordError):
            await self.service.register_with_email("anna@example.com", "short", "Анна")

    async def test_wrong_password_is_rejected(self):
        await self.service.register_with_email(
            "anna@example.com", "correct-horse-battery-staple", "Анна"
        )
        with self.assertRaises(InvalidCredentialsError):
            await self.service.authenticate_with_email("anna@example.com", "wrong")

    async def test_unknown_email_is_rejected(self):
        with self.assertRaises(InvalidCredentialsError):
            await self.service.authenticate_with_email("ghost@example.com", "whatever")

    async def test_telegram_login_creates_profile_and_web_user_once(self):
        payload = sign_telegram_payload(make_telegram_payload(telegram_id=1001))
        user = await self.service.login_with_telegram(payload)
        self.assertEqual(user.telegram_id, 1001)
        self.assertIsNotNone(await self.repository.get_profile(1001))

        again = await self.service.login_with_telegram(payload)
        self.assertEqual(again.id, user.id)

    async def test_link_telegram_to_existing_email_account(self):
        user = await self.service.register_with_email(
            "anna@example.com", "correct-horse-battery-staple", "Анна"
        )
        payload = sign_telegram_payload(make_telegram_payload(telegram_id=2002))
        linked = await self.service.link_telegram(user.id, payload)
        self.assertEqual(linked.telegram_id, 2002)
        self.assertEqual(linked.email, "anna@example.com")

    async def test_linking_merges_account_created_only_by_telegram_login(self):
        """Вход через Telegram создал аккаунт без email; привязка того же
        Telegram к email-аккаунту объединяет их — у участника один аккаунт."""
        shell = await self.service.login_with_telegram(
            sign_telegram_payload(make_telegram_payload(telegram_id=3003))
        )
        other = await self.service.register_with_email(
            "bob@example.com", "correct-horse-battery-staple", "Боб"
        )
        linked = await self.service.link_telegram(
            other.id,
            sign_telegram_payload(make_telegram_payload(telegram_id=3003)),
        )
        self.assertEqual(linked.id, other.id)
        self.assertIsNone(await self.repository.get_web_user_by_id(shell.id))
        again = await self.service.login_with_telegram(
            sign_telegram_payload(make_telegram_payload(telegram_id=3003))
        )
        self.assertEqual(again.id, other.id)

    async def test_cannot_link_telegram_already_linked_to_another_account(self):
        owner = await self.service.register_with_email(
            "anna@example.com", "correct-horse-battery-staple", "Анна"
        )
        await self.service.link_telegram(
            owner.id, sign_telegram_payload(make_telegram_payload(telegram_id=3003))
        )
        other = await self.service.register_with_email(
            "bob@example.com", "correct-horse-battery-staple", "Боб"
        )
        with self.assertRaises(TelegramAlreadyLinkedError):
            await self.service.link_telegram(
                other.id,
                sign_telegram_payload(make_telegram_payload(telegram_id=3003)),
            )

    async def test_changing_linked_telegram_updates_existing_account(self):
        user = await self.service.register_with_email(
            "anna@example.com", "correct-horse-battery-staple", "Анна"
        )
        await self.service.link_telegram(
            user.id, sign_telegram_payload(make_telegram_payload(telegram_id=4004))
        )
        switched = await self.service.link_telegram(
            user.id, sign_telegram_payload(make_telegram_payload(telegram_id=5005))
        )
        self.assertEqual(switched.id, user.id)
        self.assertEqual(switched.telegram_id, 5005)
        self.assertIsNone(await self.repository.get_web_user_by_telegram_id(4004))

    async def test_cannot_unlink_only_login_method(self):
        payload = sign_telegram_payload(make_telegram_payload(telegram_id=6006))
        user = await self.service.login_with_telegram(payload)
        with self.assertRaises(CannotUnlinkOnlyLoginMethodError):
            await self.service.unlink_telegram(user.id)

    async def test_unlink_telegram_when_email_password_also_set(self):
        user = await self.service.register_with_email(
            "anna@example.com", "correct-horse-battery-staple", "Анна"
        )
        await self.service.link_telegram(
            user.id, sign_telegram_payload(make_telegram_payload(telegram_id=7007))
        )
        unlinked = await self.service.unlink_telegram(user.id)
        self.assertIsNone(unlinked.telegram_id)


if __name__ == "__main__":
    unittest.main()
