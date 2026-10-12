"""ЮKassa: ошибки транспорта, разбор ответов и расчёт сумм.

Сеть не используется: ``urlopen`` подменён. Проверяются деньги (копейки ↔
рубли без потерь), заголовки идемпотентности и то, что любой сбой превращается
в понятное ``PaymentProviderError`` без утечки секретов.
"""

import json
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from flamenco_bot.payments.client import PaymentProviderError, YooKassaClient

SECRET = "live_super_secret_key"


class Response:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def payment_body(**override):
    body = {
        "id": "pay-1",
        "status": "pending",
        "amount": {"value": "1000.00", "currency": "RUB"},
        "confirmation": {"confirmation_url": "https://pay.example/c"},
        "metadata": {"telegram_id": "1"},
    }
    body.update(override)
    return body


def refund_body(**override):
    body = {
        "id": "ref-1",
        "payment_id": "pay-1",
        "status": "succeeded",
        "amount": {"value": "250.50", "currency": "RUB"},
    }
    body.update(override)
    return body


class YooKassaErrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = YooKassaClient("shop", SECRET, "https://site.example/back")

    async def _call_payment(self, urlopen_result):
        with patch(
            "flamenco_bot.payments.client.urlopen", side_effect=urlopen_result
        ) as urlopen:
            try:
                return await self.client.get_payment("pay-1"), urlopen
            finally:
                self.urlopen = urlopen

    async def test_http_errors_become_provider_errors_with_status_code(self):
        for code in (400, 401, 403, 404, 429, 500, 503):
            error = HTTPError("u", code, "msg", {}, BytesIO(b'{"secret":"x"}'))
            with self.subTest(code=code):
                with self.assertRaises(PaymentProviderError) as raised:
                    await self._call_payment(error)
                self.assertIn(str(code), str(raised.exception))
                self.assertNotIn(SECRET, str(raised.exception))

    async def test_network_failures_become_provider_errors(self):
        for failure in (
            URLError("dns"),
            TimeoutError("slow"),
            ConnectionResetError("reset"),
        ):
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaisesRegex(PaymentProviderError, "связаться"):
                    await self._call_payment(failure)

    async def test_garbage_bodies_are_rejected(self):
        for body in (
            b"",
            b"<html>502</html>",
            b"\xff\xfe",
            b"[1, 2]",
            b'"text"',
            b"null",
        ):
            with self.subTest(body=body):
                with self.assertRaises(PaymentProviderError):
                    await self._call_payment([Response(body)])

    async def test_unexpected_payment_shapes_are_rejected(self):
        broken = (
            {},
            payment_body(id=5),
            payment_body(status=None),
            payment_body(amount=None),
            payment_body(amount={"value": "abc", "currency": "RUB"}),
            payment_body(amount={"currency": "RUB"}),
        )
        for body in broken:
            with self.subTest(body=body):
                with self.assertRaisesRegex(PaymentProviderError, "Некорректный"):
                    await self._call_payment([Response(body)])

    async def test_missing_confirmation_and_metadata_are_tolerated(self):
        body = payment_body(confirmation=None, metadata=None)
        payment, _ = await self._call_payment([Response(body)])
        self.assertIsNone(payment.confirmation_url)
        self.assertEqual(payment.metadata, {})

    async def test_amounts_convert_to_minor_units_exactly(self):
        for value, minor in (
            ("1000.00", 100000),
            ("0.01", 1),
            ("1999.99", 199999),
            ("0.1", 10),
            ("12", 1200),
        ):
            with self.subTest(value=value):
                body = payment_body(amount={"value": value, "currency": "RUB"})
                payment, _ = await self._call_payment([Response(body)])
                self.assertEqual(payment.amount_minor, minor)

    async def test_request_carries_basic_auth_and_no_body_for_get(self):
        _, urlopen = await self._call_payment([Response(payment_body())])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))
        self.assertIsNone(request.get_header("Idempotence-key"))
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 10.0)

    async def test_create_payment_sends_exact_money_and_idempotence_key(self):
        with patch(
            "flamenco_bot.payments.client.urlopen",
            return_value=Response(payment_body()),
        ) as urlopen:
            await self.client.create_payment(199999, "Д" * 300, 7, "pack_4", "key-123")
        request = urlopen.call_args.args[0]
        sent = json.loads(request.data)
        self.assertEqual(sent["amount"], {"value": "1999.99", "currency": "RUB"})
        self.assertEqual(len(sent["description"]), 128)
        self.assertEqual(
            sent["metadata"], {"telegram_id": "7", "package_key": "pack_4"}
        )
        self.assertTrue(sent["capture"])
        self.assertEqual(request.get_header("Idempotence-key"), "key-123")
        self.assertEqual(request.get_header("Content-type"), "application/json")

    async def test_invalid_amounts_never_reach_the_provider(self):
        with patch("flamenco_bot.payments.client.urlopen") as urlopen:
            for amount in (0, -100):
                with self.assertRaises(ValueError):
                    await self.client.create_payment(amount, "d", 1, "single", "k")
                with self.assertRaises(ValueError):
                    await self.client.create_refund("pay-1", amount, "d", "k")
            urlopen.assert_not_called()

    async def test_unconfigured_client_refuses_every_operation(self):
        empty = YooKassaClient("", "", "")
        self.assertFalse(empty.is_configured)
        with patch("flamenco_bot.payments.client.urlopen") as urlopen:
            for call in (
                empty.create_payment(100, "d", 1, "single", "k"),
                empty.get_payment("p"),
                empty.create_refund("p", 100, "d", "k"),
                empty.get_refund("r"),
            ):
                with self.assertRaisesRegex(PaymentProviderError, "не настроена"):
                    await call
            urlopen.assert_not_called()

    async def test_refund_roundtrip_and_validation(self):
        with patch(
            "flamenco_bot.payments.client.urlopen",
            return_value=Response(refund_body()),
        ) as urlopen:
            refund = await self.client.create_refund("pay/1", 25050, "возврат", "rk")
            fetched = await self.client.get_refund("ref/1?x")
        self.assertEqual((refund.refund_id, refund.amount_minor), ("ref-1", 25050))
        self.assertEqual(fetched.status, "succeeded")
        first, second = (c.args[0] for c in urlopen.call_args_list)
        self.assertEqual(json.loads(first.data)["amount"]["value"], "250.50")
        self.assertEqual(first.get_header("Idempotence-key"), "rk")
        # Идентификатор в пути экранируется: нельзя подменить ресурс.
        self.assertTrue(second.full_url.endswith("/refunds/ref%2F1%3Fx"))

        for body in (
            {},
            refund_body(id=None),
            refund_body(amount={"value": "1", "currency": None}),
            refund_body(amount={"value": "x", "currency": "RUB"}),
        ):
            with self.subTest(body=body):
                with (
                    patch(
                        "flamenco_bot.payments.client.urlopen",
                        return_value=Response(body),
                    ),
                    self.assertRaisesRegex(PaymentProviderError, "Некорректный"),
                ):
                    await self.client.get_refund("ref-1")

    async def test_errors_are_logged_without_credentials(self):
        error = HTTPError("u", 500, "x", {}, BytesIO(b"{}"))
        with self.assertLogs("bot.payments", level="ERROR") as logs:
            with self.assertRaises(PaymentProviderError):
                await self._call_payment(error)
        joined = "\n".join(logs.output)
        self.assertNotIn(SECRET, joined)
        self.assertNotIn("shop", joined.replace("YooKassa", ""))


if __name__ == "__main__":
    unittest.main()
