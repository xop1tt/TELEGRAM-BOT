"""IP клиента для rate limit входа за прокси.

Приложение оборачивается так же, как его запускает ``python -m
flamenco_bot.api`` (``uvicorn.Config(proxy_headers=True,
forwarded_allow_ips=...)``), а адрес TCP-соединения задаётся явно —
поэтому проверяется настоящая логика доверия X-Forwarded-For, а не мок.
"""

import ipaddress
import os
import re
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import uvicorn

from flamenco_bot.api import __main__ as api_main
from flamenco_bot.api.app import create_app
from flamenco_bot.api.config import WebConfig, parse_frontend_proxy_secret
from flamenco_bot.runtime.security import AuthRateLimiter, SupportRateLimiter
from flamenco_bot.services import AuthService
from tests.pg import PostgresTestCase


FRONTEND_IP = "10.89.250.10"  # адрес контейнера сайта (compose.yaml)
UNTRUSTED_PEER = "198.51.100.7"
LIMIT = AuthRateLimiter().max_attempts


class ClientIpBehindProxyTests(PostgresTestCase):
    def _app(self, forwarded_allow_ips):
        repository = self.repository
        app = create_app()
        app.state.repository = repository
        app.state.auth_service = AuthService(repository, "123456:test-token")
        app.state.bot = AsyncMock()
        app.state.support_limiter = SupportRateLimiter()
        app.state.auth_limiter = AuthRateLimiter()
        config = uvicorn.Config(
            app, proxy_headers=True, forwarded_allow_ips=forwarded_allow_ips
        )
        config.load()
        return config.loaded_app

    def _client(self, app, peer_ip):
        transport = httpx.ASGITransport(app=app, client=(peer_ip, 40000))
        return httpx.AsyncClient(transport=transport, base_url="https://testserver")

    async def _login(self, client, forwarded_for=None):
        headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
        response = await client.post(
            "/api/auth/login",
            json={"email": "nobody@example.com", "password": "wrong-password"},
            headers=headers,
        )
        return response.status_code

    async def test_two_clients_behind_trusted_proxy_have_separate_limits(self):
        app = self._app(FRONTEND_IP)
        async with self._client(app, FRONTEND_IP) as proxy:
            codes_a = [await self._login(proxy, "203.0.113.1") for _ in range(LIMIT)]
            self.assertEqual(codes_a, [401] * LIMIT)
            self.assertEqual(await self._login(proxy, "203.0.113.1"), 429)

            # Другой пользователь сайта за тем же прокси не заблокирован.
            self.assertEqual(await self._login(proxy, "203.0.113.2"), 401)

    async def test_existing_limit_still_applies_to_each_client(self):
        app = self._app(FRONTEND_IP)
        async with self._client(app, FRONTEND_IP) as proxy:
            codes = [await self._login(proxy, "203.0.113.5") for _ in range(LIMIT + 2)]
        self.assertEqual(codes, [401] * LIMIT + [429, 429])

    async def test_spoofed_header_from_untrusted_peer_is_ignored(self):
        """Не-прокси не может менять свой IP, подставляя X-Forwarded-For."""
        app = self._app(FRONTEND_IP)
        async with self._client(app, UNTRUSTED_PEER) as attacker:
            codes = [
                await self._login(attacker, "192.0.2.{}".format(attempt))
                for attempt in range(LIMIT + 1)
            ]
        self.assertEqual(codes[-1], 429)
        self.assertEqual(codes[:LIMIT], [401] * LIMIT)

    async def test_values_prepended_by_client_do_not_bypass_limit(self):
        """Клиент прислал свой X-Forwarded-For, edge-прокси дописал реальный IP.

        Берётся самый правый недоверенный адрес — записанный edge-прокси,
        поэтому смена подставленной части лимит не обходит.
        """
        app = self._app(FRONTEND_IP)
        async with self._client(app, FRONTEND_IP) as proxy:
            codes = [
                await self._login(proxy, "192.0.2.{}, 203.0.113.9".format(attempt))
                for attempt in range(LIMIT + 1)
            ]
        self.assertEqual(codes[-1], 429)

    async def test_trusted_proxy_without_header_is_limited_as_one_client(self):
        """Edge-прокси не записал IP — безопасный отказ: общий лимит, не обход."""
        app = self._app(FRONTEND_IP)
        async with self._client(app, FRONTEND_IP) as proxy:
            codes = [await self._login(proxy) for _ in range(LIMIT + 1)]
        self.assertEqual(codes[-1], 429)

    async def test_local_run_without_proxy_works(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORWARDED_ALLOW_IPS", None)
            app = self._app(api_main.forwarded_allow_ips())
        async with self._client(app, "127.0.0.1") as local:
            self.assertEqual((await local.get("/api/bookings/rules")).status_code, 200)
            codes = [await self._login(local) for _ in range(LIMIT + 1)]
        self.assertEqual(codes, [401] * LIMIT + [429])


class FrontendSecretClientIpTests(ClientIpBehindProxyTests):
    """Netlify → Render: адрес прокси сайта неизвестен, IP посетителя сервер
    сайта передаёт в X-Flamenco-Client-IP вместе с общим секретом."""

    SECRET = "s" * 40
    RENDER_PROXY = "10.20.30.40"

    def setUp(self):
        patcher = patch.object(WebConfig, "FRONTEND_PROXY_SECRET", self.SECRET)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _site_login(self, client, visitor_ip, secret=SECRET):
        response = await client.post(
            "/api/auth/login",
            json={"email": "nobody@example.com", "password": "wrong-password"},
            headers={
                "X-Flamenco-Frontend-Secret": secret,
                "X-Flamenco-Client-IP": visitor_ip,
            },
        )
        return response.status_code

    async def test_visitors_with_secret_have_separate_limits(self):
        app = self._app("127.0.0.1")
        async with self._client(app, self.RENDER_PROXY) as site:
            codes = [await self._site_login(site, "203.0.113.1") for _ in range(LIMIT)]
            self.assertEqual(codes, [401] * LIMIT)
            self.assertEqual(await self._site_login(site, "203.0.113.1"), 429)
            self.assertEqual(await self._site_login(site, "203.0.113.2"), 401)

    async def test_wrong_secret_does_not_change_client_ip(self):
        app = self._app("127.0.0.1")
        async with self._client(app, self.RENDER_PROXY) as attacker:
            codes = [
                await self._site_login(attacker, "192.0.2.{}".format(n), "x" * 40)
                for n in range(LIMIT + 1)
            ]
        self.assertEqual(codes, [401] * LIMIT + [429])

    async def test_invalid_client_ip_falls_back_to_connection(self):
        app = self._app("127.0.0.1")
        async with self._client(app, self.RENDER_PROXY) as site:
            codes = [
                await self._site_login(site, "not-an-ip-{}".format(n))
                for n in range(LIMIT + 1)
            ]
        self.assertEqual(codes[-1], 429)

    async def test_header_is_ignored_when_secret_is_not_configured(self):
        app = self._app("127.0.0.1")
        with patch.object(WebConfig, "FRONTEND_PROXY_SECRET", ""):
            async with self._client(app, self.RENDER_PROXY) as client:
                codes = [
                    await self._site_login(client, "192.0.2.{}".format(n), "")
                    for n in range(LIMIT + 1)
                ]
        self.assertEqual(codes[-1], 429)


class FrontendSecretConfigTests(unittest.TestCase):
    def test_short_secret_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_frontend_proxy_secret("short")
        self.assertEqual(parse_frontend_proxy_secret(""), "")
        self.assertEqual(parse_frontend_proxy_secret(" " + "a" * 32), "a" * 32)


class ForwardedAllowIpsConfigTests(unittest.TestCase):
    def _run_with_env(self, value):
        env = {} if value is None else {"FORWARDED_ALLOW_IPS": value}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(api_main.uvicorn, "run") as run,
        ):
            if value is None:
                os.environ.pop("FORWARDED_ALLOW_IPS", None)
            api_main.run()
        return run.call_args.kwargs

    def test_defaults_to_localhost_only(self):
        kwargs = self._run_with_env(None)
        self.assertTrue(kwargs["proxy_headers"])
        self.assertEqual(kwargs["forwarded_allow_ips"], "127.0.0.1")

    def test_uses_configured_proxy_address(self):
        kwargs = self._run_with_env(FRONTEND_IP)
        self.assertTrue(kwargs["proxy_headers"])
        self.assertEqual(kwargs["forwarded_allow_ips"], FRONTEND_IP)

    def test_accepts_networks_and_ipv6(self):
        kwargs = self._run_with_env(" 10.89.250.0/24 , ::1 ")
        self.assertEqual(kwargs["forwarded_allow_ips"], "10.89.250.0/24,::1")

    def test_rejects_wildcard_and_invalid_entries(self):
        for value in ("*", "10.0.0.1,*", "frontend", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self._run_with_env(value)


class ComposeTrustedProxyTests(unittest.TestCase):
    """Адрес, которому доверяет api, лежит в подсети flamenco-web."""

    def test_default_frontend_ip_is_inside_default_subnet(self):
        compose = (Path(__file__).resolve().parents[2] / "compose.yaml").read_text(
            encoding="utf-8"
        )
        trusted = re.search(
            r"FORWARDED_ALLOW_IPS: \$\{FLAMENCO_FRONTEND_IP:-([^}]+)\}", compose
        )
        subnet = re.search(r"subnet: \$\{FLAMENCO_WEB_SUBNET:-([^}]+)\}", compose)
        self.assertIsNotNone(trusted)
        self.assertIsNotNone(subnet)
        self.assertNotIn("*", trusted.group(1))
        self.assertIn(
            ipaddress.ip_address(trusted.group(1)),
            ipaddress.ip_network(subnet.group(1)),
        )


if __name__ == "__main__":
    unittest.main()
