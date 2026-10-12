"""Серверные команды прав (python -m flamenco_bot.api.manage).

Через эти команды выдаётся и снимается доступ администратора, поэтому важны
защита последнего администратора, проверка аргументов и то, что соединение
с БД закрывается при любом исходе. Работают на настоящем PostgreSQL.
"""

import contextlib
import io
import unittest
from unittest.mock import AsyncMock, patch

from flamenco_bot.api import manage
from tests.pg import PostgresTestCase, fetchval


class ManageCommandTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        patcher = patch.object(
            manage, "_connect", AsyncMock(return_value=self.repository)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # close() вызывает сама команда; тестовый репозиторий закроет базовый класс.
        self.close = patch.object(self.repository, "close", AsyncMock())
        self.close_mock = self.close.start()
        self.addCleanup(self.close.stop)

    async def _role(self, telegram_id):
        return await self.repository.get_staff_role(telegram_id)

    async def test_grant_admin_creates_profile_and_role(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await manage.grant_admin(555, "Ольга")
        self.assertEqual(await self._role(555), "admin")
        profile = await self.repository.get_profile(555)
        self.assertEqual(profile.user_name, "Ольга")
        self.assertIn("555", out.getvalue())
        self.close_mock.assert_awaited_once()

    async def test_grant_admin_is_idempotent_and_keeps_existing_name(self):
        await self.repository.get_or_create_profile(555, "Настоящее имя")
        for _ in range(2):
            with contextlib.redirect_stdout(io.StringIO()):
                await manage.grant_admin(555, "Другое")
        self.assertEqual(
            (await self.repository.get_profile(555)).user_name, "Настоящее имя"
        )
        self.assertEqual(await self._role(555), "admin")

    async def test_grant_admin_promotes_a_teacher(self):
        with contextlib.redirect_stdout(io.StringIO()):
            await manage.grant_admin(1, "Админ")
        await self.repository.get_or_create_profile(2, "Учитель")
        await self.repository.set_staff_role(2, "teacher", 1)
        with contextlib.redirect_stdout(io.StringIO()):
            await manage.grant_admin(2, "Учитель")
        self.assertEqual(await self._role(2), "admin")

    async def test_last_admin_cannot_be_revoked(self):
        with contextlib.redirect_stdout(io.StringIO()):
            await manage.grant_admin(1, "Единственный")
        with self.assertRaises(SystemExit) as raised:
            await manage.revoke_staff(1)
        self.assertIn("посл", str(raised.exception).lower())
        self.assertEqual(await self._role(1), "admin")
        self.close_mock.assert_awaited()

    async def test_revoke_removes_role_when_another_admin_remains(self):
        with contextlib.redirect_stdout(io.StringIO()):
            await manage.grant_admin(1, "Первый")
            await manage.grant_admin(2, "Второй")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await manage.revoke_staff(2)
        self.assertIsNone(await self._role(2))
        self.assertEqual(await self._role(1), "admin")
        self.assertIn("снята", out.getvalue())

    async def test_revoking_unknown_person_reports_it(self):
        with contextlib.redirect_stdout(io.StringIO()):
            await manage.grant_admin(1, "Админ")
        with self.assertRaises(SystemExit) as raised:
            await manage.revoke_staff(999)
        self.assertIn("нет", str(raised.exception))
        self.assertEqual(
            await fetchval(self.repository, "SELECT COUNT(*) FROM staff_members"), 1
        )


class ManageArgumentsTests(unittest.TestCase):
    def _run(self, argv):
        with contextlib.redirect_stderr(io.StringIO()):
            return manage.main(argv)

    def test_telegram_id_must_be_a_positive_integer(self):
        for value in ("0", "-5", "abc", "1.5", "", "+7"):
            with self.subTest(value=value), self.assertRaises(SystemExit) as raised:
                self._run(["grant-admin", "--telegram-id", value])
            self.assertEqual(raised.exception.code, 2)

    def test_command_and_id_are_required(self):
        for argv in ([], ["grant-admin"], ["revoke-staff"], ["delete-everything"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as raised:
                self._run(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_main_dispatches_to_the_right_command(self):
        with (
            patch.object(manage, "grant_admin", AsyncMock()) as grant,
            patch.object(manage, "revoke_staff", AsyncMock()) as revoke,
        ):
            manage.main(["grant-admin", "--telegram-id", "42", "--name", "Оля"])
            grant.assert_awaited_once_with(42, "Оля")
            revoke.assert_not_awaited()
            manage.main(["revoke-staff", "--telegram-id", "42"])
            revoke.assert_awaited_once_with(42)

    def test_default_name_is_used_when_not_given(self):
        with patch.object(manage, "grant_admin", AsyncMock()) as grant:
            manage.main(["grant-admin", "--telegram-id", "7"])
        grant.assert_awaited_once_with(7, "Администратор")

    def test_connect_refuses_without_database_url(self):
        with patch.object(manage.WebConfig, "DATABASE_URL", ""):
            with self.assertRaises(SystemExit) as raised:
                import asyncio

                asyncio.run(manage._connect())
        self.assertIn("DATABASE_URL", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
