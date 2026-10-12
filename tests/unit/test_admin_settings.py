"""Настройки студии в боте (handlers/admin_settings.py) на настоящем PostgreSQL.

Каталог (абонементы, направления), правила записи, сотрудники, сбои
доставки и заметки об участнике: права на каждое нажатие, ввод значений,
подтверждение «было → станет» и его срок, запись в базу.
"""

import unittest
from unittest.mock import patch

from flamenco_bot.handlers import admin_settings
from flamenco_bot.handlers.states import AdminForm
from flamenco_bot.payments.catalog import ALL_PACKAGES
from tests.pg import PostgresTestCase, fetchval, make_admin
from tests.support import FakeCallback, FakeMessage, FakeState, callback_data

ADMIN_ID = 77
USER_ID = 1001
TEACHER_ID = 55


class AdminSettingsTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await make_admin(self.repository, ADMIN_ID)
        await self.repository.get_or_create_profile(USER_ID, "Анна")
        self.state = FakeState()
        patches = [
            patch("flamenco_bot.handlers.admin_panel.actions_logger"),
            patch("flamenco_bot.handlers.admin_settings.actions_logger"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def press(self, data, telegram_id=ADMIN_ID):
        return FakeCallback(data, telegram_id=telegram_id)

    def say(self, text, telegram_id=ADMIN_ID):
        return FakeMessage(text=text, telegram_id=telegram_id)

    async def _confirm(self, data):
        callback = self.press(data)
        handler = {
            "s:okc": admin_settings.confirm_catalog_save,
            "s:rok": admin_settings.confirm_rule_save,
            "s:oks": admin_settings.confirm_staff_set,
            "s:okx": admin_settings.confirm_staff_remove,
            "s:okn": admin_settings.confirm_note_delete,
        }[data]
        await handler(callback, self.repository, self.state)
        return callback

    # --- права -------------------------------------------------------------

    async def test_every_settings_callback_rejects_non_admin(self):
        cases = (
            ("s:m", admin_settings.show_settings, True),
            ("s:l:pk", admin_settings.show_catalog, True),
            ("s:c:pk:single", admin_settings.show_catalog_item, False),
            ("s:e:pk:single:2", admin_settings.start_catalog_edit, True),
            ("s:t:pk:single", admin_settings.confirm_toggle_screen, True),
            ("s:n:pk", admin_settings.start_catalog_create, True),
            ("s:okc", admin_settings.confirm_catalog_save, True),
            ("s:ru", admin_settings.show_rules, True),
            ("s:re:0", admin_settings.start_rule_edit, True),
            ("s:rok", admin_settings.confirm_rule_save, True),
            ("s:st", admin_settings.show_staff, True),
            ("s:sc:{}".format(ADMIN_ID), admin_settings.show_staff_member, False),
            ("s:oks", admin_settings.confirm_staff_set, True),
            ("s:okx", admin_settings.confirm_staff_remove, True),
            ("s:nf", admin_settings.show_failed_notifications, False),
            ("s:nl:{}".format(USER_ID), admin_settings.show_notes, True),
            ("s:na:{}".format(USER_ID), admin_settings.start_note, True),
            ("s:okn", admin_settings.confirm_note_delete, True),
        )
        for data, handler, with_state in cases:
            with self.subTest(data=data):
                callback = self.press(data, telegram_id=USER_ID)
                if with_state:
                    await handler(callback, self.repository, self.state)
                else:
                    await handler(callback, self.repository)
                self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
                callback.message.edit_text.assert_not_awaited()

    async def test_teacher_has_no_settings(self):
        await self.repository.get_or_create_profile(TEACHER_ID, "Мария")
        await self.repository.set_staff_role(TEACHER_ID, "teacher", ADMIN_ID)
        message = self.say("/settings", telegram_id=TEACHER_ID)
        await admin_settings.open_settings(message, self.state, self.repository)
        self.assertIn("только администратору", message.last_answer.args[0])

    async def test_settings_menu_for_admin(self):
        message = self.say("/settings")
        await admin_settings.open_settings(message, self.state, self.repository)
        data = callback_data(message.last_answer.kwargs["reply_markup"])
        self.assertEqual(data, ["s:l:pk", "s:l:cl", "s:ru", "s:st", "s:nf"])

    # --- каталог -------------------------------------------------------------

    async def test_package_price_edit_needs_confirmation_and_refreshes_cache(self):
        card = self.press("s:c:pk:pack_4")
        await admin_settings.show_catalog_item(card, self.repository)
        self.assertIn("s:e:pk:pack_4:2", callback_data(card.screen[1]))

        start = self.press("s:e:pk:pack_4:2")
        await admin_settings.start_catalog_edit(start, self.repository, self.state)
        self.assertEqual(self.state.current_state, AdminForm.waiting_for_catalog_value)

        message = self.say("4200")
        await admin_settings.save_catalog_value(message, self.state, self.repository)
        self.assertIn("3600", message.last_answer.args[0])
        self.assertIn("4200", message.last_answer.args[0])
        stored = await fetchval(
            self.repository,
            "SELECT price_minor FROM lesson_packages WHERE key='pack_4'",
        )
        self.assertEqual(stored, 360000)

        done = await self._confirm("s:okc")
        self.assertIn("Сохранено", done.screen[0])
        stored = await fetchval(
            self.repository,
            "SELECT price_minor FROM lesson_packages WHERE key='pack_4'",
        )
        self.assertEqual(stored, 420000)
        self.assertEqual(ALL_PACKAGES["pack_4"].price_rub, 4200)

    async def test_invalid_value_is_rejected_with_reason(self):
        await admin_settings.start_catalog_edit(
            self.press("s:e:pk:single:1"), self.repository, self.state
        )
        for bad in ("0", "101", "abc", "1.5"):
            with self.subTest(bad=bad):
                message = self.say(bad)
                await admin_settings.save_catalog_value(
                    message, self.state, self.repository
                )
                self.assertIn("от 1 до 100", message.last_answer.args[0])

    async def test_validity_zero_means_no_expiry(self):
        await admin_settings.start_catalog_edit(
            self.press("s:e:pk:pack_8:3"), self.repository, self.state
        )
        await admin_settings.save_catalog_value(
            self.say("0"), self.state, self.repository
        )
        await self._confirm("s:okc")
        self.assertIsNone(
            await fetchval(
                self.repository,
                "SELECT validity_days FROM lesson_packages WHERE key='pack_8'",
            )
        )

    async def test_hide_and_show_package(self):
        screen = self.press("s:t:pk:single")
        await admin_settings.confirm_toggle_screen(screen, self.repository, self.state)
        self.assertIn("Скрыть", screen.screen[0])
        await self._confirm("s:okc")
        self.assertFalse(
            await fetchval(
                self.repository,
                "SELECT is_active FROM lesson_packages WHERE key='single'",
            )
        )
        listing = self.press("s:l:pk")
        await admin_settings.show_catalog(listing, self.repository, self.state)
        self.assertIn("🙈", listing.screen[1].inline_keyboard[0][0].text)

    async def test_create_package_flow(self):
        await admin_settings.start_catalog_create(
            self.press("s:n:pk"), self.repository, self.state
        )
        taken = self.say("single")
        await admin_settings.save_catalog_value(taken, self.state, self.repository)
        self.assertIn("уже есть", taken.last_answer.args[0])
        bad = self.say("Bad Key")
        await admin_settings.save_catalog_value(bad, self.state, self.repository)
        self.assertIn("латиница", bad.last_answer.args[0])

        for answer in ("pack_12", "Абонемент на 12", "12", "9000", "90", "40"):
            message = self.say(answer)
            await admin_settings.save_catalog_value(
                message, self.state, self.repository
            )
        self.assertIn("Создать абонемент?", message.last_answer.args[0])

        done = await self._confirm("s:okc")
        self.assertIn("Сохранено", done.screen[0])
        row = await self.repository._pool.fetchrow(
            "SELECT lessons, price_minor, validity_days, is_active "
            "FROM lesson_packages WHERE key='pack_12'"
        )
        self.assertEqual(tuple(row), (12, 900000, 90, True))

    async def test_class_format_edit(self):
        await admin_settings.start_catalog_edit(
            self.press("s:e:cl:beginner:2"), self.repository, self.state
        )
        await admin_settings.save_catalog_value(
            self.say("Новое описание"), self.state, self.repository
        )
        await self._confirm("s:okc")
        self.assertEqual(
            await fetchval(
                self.repository,
                "SELECT description FROM class_formats WHERE key='beginner'",
            ),
            "Новое описание",
        )

    async def test_confirmation_expires_and_is_not_reusable(self):
        callback = await self._confirm("s:okc")
        self.assertIn("устарело", callback.answer.await_args.args[0])

    # --- правила -------------------------------------------------------------

    async def test_rule_edit_flow(self):
        screen = self.press("s:ru")
        await admin_settings.show_rules(screen, self.repository, self.state)
        self.assertIn("Окно отмены записи", screen.screen[0])

        await admin_settings.start_rule_edit(
            self.press("s:re:0"), self.repository, self.state
        )
        bad = self.say("500")
        await admin_settings.save_rule_value(bad, self.state, self.repository)
        self.assertIn("от 0 до 168", bad.last_answer.args[0])

        ok = self.say("36")
        await admin_settings.save_rule_value(ok, self.state, self.repository)
        self.assertIn("24 → 36", ok.last_answer.args[0])
        done = await self._confirm("s:rok")
        self.assertIn("Сохранено", done.screen[0])
        self.assertEqual(
            (await self.repository.get_studio_rules()).cancellation_deadline_hours, 36
        )

    # --- сотрудники ------------------------------------------------------------

    async def test_staff_assign_change_role_and_remove(self):
        await admin_settings.start_staff_add(
            self.press("s:sa"), self.repository, self.state
        )
        unknown = self.say("999999")
        await admin_settings.save_staff_id(unknown, self.state, self.repository)
        self.assertIn("не найден", unknown.last_answer.args[0])

        found = self.say(str(USER_ID))
        await admin_settings.save_staff_id(found, self.state, self.repository)
        pick = self.press("s:sar:teacher")
        await admin_settings.confirm_staff_add_screen(pick, self.repository, self.state)
        self.assertIn("преподаватель", pick.screen[0])
        await self._confirm("s:oks")
        self.assertEqual(await self.repository.get_staff_role(USER_ID), "teacher")

        rename = self.press("s:sn:{}".format(USER_ID))
        await admin_settings.start_staff_name(rename, self.repository, self.state)
        await admin_settings.save_staff_name(
            self.say("Анна К."), self.state, self.repository
        )
        await self._confirm("s:oks")
        member = await self.repository.get_staff_member(USER_ID)
        self.assertEqual(member.display_name, "Анна К.")

        promote = self.press("s:sr:{}:admin".format(USER_ID))
        await admin_settings.confirm_staff_role_screen(
            promote, self.repository, self.state
        )
        await self._confirm("s:oks")
        self.assertEqual(await self.repository.get_staff_role(USER_ID), "admin")

        remove = self.press("s:sd:{}".format(USER_ID))
        await admin_settings.confirm_staff_remove_screen(
            remove, self.repository, self.state
        )
        await self._confirm("s:okx")
        self.assertIsNone(await self.repository.get_staff_role(USER_ID))

    async def test_last_admin_cannot_be_demoted_or_removed(self):
        demote = self.press("s:sr:{}:teacher".format(ADMIN_ID))
        await admin_settings.confirm_staff_role_screen(
            demote, self.repository, self.state
        )
        refused = await self._confirm("s:oks")
        self.assertIn("последний администратор", refused.answer.await_args.args[0])

        remove = self.press("s:sd:{}".format(ADMIN_ID))
        await admin_settings.confirm_staff_remove_screen(
            remove, self.repository, self.state
        )
        refused = await self._confirm("s:okx")
        self.assertIn("последний администратор", refused.answer.await_args.args[0])
        self.assertEqual(await self.repository.get_staff_role(ADMIN_ID), "admin")

    # --- сбои доставки ------------------------------------------------------------

    async def test_failed_notifications_screen(self):
        empty = self.press("s:nf")
        await admin_settings.show_failed_notifications(empty, self.repository)
        self.assertIn("Сбоев нет", empty.screen[0])

        await self.repository._pool.execute(
            "INSERT INTO user_notifications "
            "(telegram_id, kind, dedupe_key, delivery_status, attempts, last_error) "
            "VALUES ($1, 'lesson_reminder', 'k1', 'failed', 6, 'bot was blocked')",
            USER_ID,
        )
        screen = self.press("s:nf")
        await admin_settings.show_failed_notifications(screen, self.repository)
        self.assertIn("lesson_reminder", screen.screen[0])
        self.assertIn("bot was blocked", screen.screen[0])
        self.assertIn("не доставлено: 1", screen.screen[0])

    # --- заметки -------------------------------------------------------------------

    async def test_notes_add_and_delete(self):
        await admin_settings.start_note(
            self.press("s:na:{}".format(USER_ID)), self.repository, self.state
        )
        empty = self.say("   ")
        await admin_settings.save_note(empty, self.state, self.repository)
        self.assertIn("от 1 до", empty.last_answer.args[0])

        message = self.say("Просила не звонить до 12")
        await admin_settings.save_note(message, self.state, self.repository)
        self.assertIn("Заметка добавлена", message.last_answer.args[0])
        notes = await self.repository.list_client_notes(USER_ID)
        self.assertEqual([note.body for note in notes], ["Просила не звонить до 12"])

        screen = self.press("s:nl:{}".format(USER_ID))
        await admin_settings.show_notes(screen, self.repository, self.state)
        self.assertIn("Просила не звонить", screen.screen[0])
        ask = self.press("s:nd:{}:{}".format(USER_ID, notes[0].id))
        await admin_settings.confirm_note_delete_screen(
            ask, self.repository, self.state
        )
        done = await self._confirm("s:okn")
        self.assertIn("Заметка удалена", done.screen[0])
        self.assertEqual(await self.repository.list_client_notes(USER_ID), [])


if __name__ == "__main__":
    unittest.main()
