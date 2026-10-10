"""Личный кабинет в боте и вход на сайт через бота (временное хранилище)."""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from flamenco_bot.handlers import connect
from flamenco_bot.keyboards.user import cabinet, lessons
from flamenco_bot.keyboards.user.account import open_profile
from flamenco_bot.keyboards.user.main_menu import open_packages, open_purchases
from flamenco_bot.services import AuthService
from tests.support import FakeCallback, FakeMessage, FakeState, callback_data
from tests.pg import PostgresTestCase, make_admin, set_credits

USER_ID = 1001
ADMIN_ID = 77


class CabinetHandlerTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.repository.get_or_create_profile(USER_ID, "Анна", False)
        await self.repository.get_or_create_profile(ADMIN_ID, "Админ", False)
        await make_admin(self.repository, ADMIN_ID)
        self.state = FakeState()

    async def _grant(self, lessons_count=4, reason="наличные"):
        return await self.repository.grant_lesson_package(
            USER_ID,
            "pack_4",
            "Абонемент на 4 занятия",
            lessons_count,
            reason,
            ADMIN_ID,
            uuid.uuid4(),
        )

    async def _slot(self, days=3):
        return await self.repository.create_class_slot(
            "beginner", datetime.now(timezone.utc) + timedelta(days=days), 5, ADMIN_ID
        )

    async def test_profile_shows_balance_packages_upcoming_and_actions(self):
        await self._grant()
        slot = await self._slot()
        await self.repository.book_class_slot(slot.id, USER_ID)
        message = FakeMessage(text="👤 Профиль")

        await open_profile(message, self.state, self.repository)

        text = message.last_answer.args[0]
        self.assertIn("Баланс: 3 занятия", text)
        self.assertIn("«Абонемент на 4 занятия» (от студии) — осталось 3 из 4", text)
        self.assertIn("Ближайшие занятия", text)
        self.assertIn("напоминания — вкл", text)
        data = callback_data(message.last_answer.kwargs["reply_markup"])
        for expected in ("prof:name", "mypacks", "hist:o", "hist:c:0", "notif"):
            self.assertIn(expected, data)

    async def test_packages_screen_explains_next_source(self):
        await self._grant()
        message = FakeMessage(text="🎟 Абонементы")

        await open_packages(message, self.state, self.repository)

        text = message.last_answer.args[0]
        self.assertIn("Действующие", text)
        self.assertIn(
            "Следующая запись спишет занятие из «Абонемент на 4 занятия»", text
        )

    async def test_purchases_and_purchase_history(self):
        message = FakeMessage(text="💳 Покупки")
        await open_purchases(message, self.state, self.repository)
        self.assertIn(
            "purch:hist", callback_data(message.last_answer.kwargs["reply_markup"])
        )

        callback = FakeCallback("purch:hist")
        await cabinet.show_purchase_history(callback, self.repository)
        self.assertIn("Покупок пока нет", callback.screen[0])

    async def test_booking_confirmation_shows_package_source(self):
        await self._grant(lessons_count=2)
        slot = await self._slot()
        callback = FakeCallback("book:beginner:{}".format(slot.id))

        await lessons.book_class_slot(callback, self.repository, self.state)

        text, _ = callback.screen
        self.assertIn(
            "Списано 1 занятие — «Абонемент на 4 занятия», осталось 1 из 2", text
        )
        self.assertIn("Баланс: 1 занятие", text)

    async def test_operations_history_with_reasons_and_paging(self):
        # Занятия на 12 записей — раньше абонемента, чтобы он оказался между
        # списаниями и этим начислением, на второй странице истории.
        await set_credits(self.repository, USER_ID, 12)
        await self._grant(reason="оплата наличными")
        for days in range(2, 14):
            slot = await self._slot(days=days)
            await self.repository.book_class_slot(slot.id, USER_ID)

        callback = FakeCallback("hist:o")
        await cabinet.show_operations(callback, self.repository)
        text, markup = callback.screen
        self.assertIn("История операций", text)
        self.assertIn("Запись на занятие", text)
        older = [d for d in callback_data(markup) if d.startswith("hist:o:")]
        self.assertEqual(len(older), 1)

        page = FakeCallback(older[0])
        await cabinet.show_operations(page, self.repository)
        self.assertIn("Абонемент от студии", page.screen[0])
        self.assertIn("оплата наличными", page.screen[0])

    async def test_class_history_marks_studio_cancellation_and_reschedule(self):
        cancelled = await self._slot(days=2)
        moved = await self._slot(days=4)
        await set_credits(self.repository, USER_ID, 2)
        await self.repository.book_class_slot(cancelled.id, USER_ID)
        await self.repository.book_class_slot(moved.id, USER_ID)
        await self.repository.cancel_class_slot(cancelled.id, ADMIN_ID, "ремонт")
        await self.repository.reschedule_class_slot(
            moved.id, datetime.now(timezone.utc) + timedelta(days=6), ADMIN_ID
        )

        callback = FakeCallback("hist:c:0")
        await cabinet.show_class_history(callback, self.repository)

        text = callback.screen[0]
        self.assertIn("Отменено студией — ремонт", text)
        self.assertIn("перенесено с", text)

    async def test_notification_settings_toggle(self):
        callback = FakeCallback("notif:rem:0")
        await cabinet.toggle_notification_setting(callback, self.repository)
        settings = await self.repository.get_notification_settings(USER_ID)
        self.assertFalse(settings.reminders)
        self.assertIn("Напоминание о занятии — выкл", callback.screen[0])

        invalid = FakeCallback("notif:xxx:1")
        await cabinet.toggle_notification_setting(invalid, self.repository)
        self.assertTrue(invalid.answer.await_args.kwargs.get("show_alert"))

    async def test_tickets_show_only_own_threads(self):
        ticket_id, _ = await self.repository.create_support_message(USER_ID, "Вопрос")
        await self.repository.reply_support_ticket(ticket_id, ADMIN_ID, "Ответ")
        own = FakeCallback("ticket:{}".format(ticket_id))
        await cabinet.show_ticket(own, self.repository)
        self.assertIn("Студия", own.screen[0])
        self.assertIn("Ответ", own.screen[0])

        stranger = FakeCallback("ticket:{}".format(ticket_id), telegram_id=2002)
        await cabinet.show_ticket(stranger, self.repository)
        self.assertEqual(stranger.answer.await_args.args[0], "Обращение не найдено.")

    async def test_rescheduled_booking_can_be_cancelled_inside_24_hours(self):
        slot = await self._slot(days=3)
        await set_credits(self.repository, USER_ID, 1)
        await self.repository.book_class_slot(slot.id, USER_ID)
        await self.repository.reschedule_class_slot(
            slot.id, datetime.now(timezone.utc) + timedelta(hours=5), ADMIN_ID
        )

        request = FakeCallback("cancel_booking:{}".format(slot.id))
        await lessons.request_booking_cancel(request, self.repository)
        self.assertIn("Перенесено студией", request.screen[0])

        confirm = FakeCallback("cancel_ok:{}".format(slot.id))
        confirm.bot = SimpleNamespace(send_message=None)
        await lessons.cancel_booking_callback(confirm, self.repository)
        self.assertIn("Запись отменена", confirm.screen[0])
        self.assertEqual(await self.repository.get_lesson_credits(USER_ID), 1)


class TelegramConnectHandlerTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.auth = AuthService(self.repository, "123:token")

    def command(self, args):
        return SimpleNamespace(args=args)

    async def test_login_link_confirmed_in_bot_then_completed_once_on_site(self):
        started = await self.auth.start_telegram_connect("mirada_bot")
        self.assertTrue(
            started.deep_link.startswith("https://t.me/mirada_bot?start=c_")
        )
        message = FakeMessage(text="/start c_" + started.token)

        await connect.start_connect(
            message, self.command("c_" + started.token), self.repository
        )
        self.assertIn("Вход на сайт студии", message.last_answer.args[0])
        data = callback_data(message.last_answer.kwargs["reply_markup"])
        self.assertEqual(data, ["tgc:" + started.token, "tgx:" + started.token])
        self.assertTrue(all(len(item.encode()) <= 64 for item in data))

        self.assertEqual(
            (await self.auth.complete_telegram_connect(started.browser_secret)).status,
            "pending",
        )
        confirm = FakeCallback("tgc:" + started.token)
        await connect.confirm_connect(confirm, self.repository)
        self.assertIn("Вернитесь на сайт", confirm.screen[0])

        completion = await self.auth.complete_telegram_connect(started.browser_secret)
        self.assertEqual(completion.status, "completed")
        self.assertEqual(completion.user.telegram_id, USER_ID)
        repeat = await self.auth.complete_telegram_connect(started.browser_secret)
        self.assertEqual(repeat.status, "used")
        # Чужой секрет (другой браузер) входа не получает.
        other = await self.auth.complete_telegram_connect("other-browser-secret")
        self.assertEqual(other.status, "unknown")

    async def test_link_shows_masked_email_and_links_account(self):
        user = await self.repository.create_web_user("anna@example.org", "hash", "Анна")
        started = await self.auth.start_telegram_connect("mirada_bot", user.id)
        message = FakeMessage(text="/start c_" + started.token)
        await connect.start_connect(
            message, self.command("c_" + started.token), self.repository
        )
        self.assertIn("a***@example.org", message.last_answer.args[0])

        await connect.confirm_connect(
            FakeCallback("tgc:" + started.token), self.repository
        )

        linked = await self.repository.get_web_user_by_id(user.id)
        self.assertEqual(linked.telegram_id, USER_ID)

    async def test_rejected_and_invalid_links(self):
        started = await self.auth.start_telegram_connect("mirada_bot")
        reject = FakeCallback("tgx:" + started.token)
        await connect.reject_connect(reject, self.repository)
        self.assertIn("Вход отменён", reject.screen[0])
        self.assertEqual(
            (await self.auth.complete_telegram_connect(started.browser_secret)).status,
            "rejected",
        )

        message = FakeMessage(text="/start c_bad")
        await connect.start_connect(message, self.command("c_bad"), self.repository)
        self.assertIn("недействительна", message.last_answer.args[0])

        late = FakeCallback("tgc:" + started.token)
        await connect.confirm_connect(late, self.repository)
        self.assertIn("уже использована или устарела", late.screen[0])


class TelegramConnectConflictTests(PostgresTestCase):
    async def test_link_to_telegram_owned_by_email_account_is_rejected(self):
        repository = self.repository
        auth = AuthService(repository, "123:token")
        owner = await repository.create_web_user(
            "owner@example.org", "hash", "Владелец"
        )
        await repository.get_or_create_profile(USER_ID, "Анна", False)
        await repository.link_telegram_to_web_user(owner.id, USER_ID)
        other = await repository.create_web_user("bob@example.org", "hash", "Боб")
        started = await auth.start_telegram_connect("mirada_bot", other.id)

        confirm = FakeCallback("tgc:" + started.token)
        await connect.confirm_connect(confirm, repository)

        self.assertIn("уже привязан к другому аккаунту", confirm.screen[0])
        completion = await auth.complete_telegram_connect(started.browser_secret)
        self.assertEqual(completion.status, "rejected")
        self.assertIsNone((await repository.get_web_user_by_id(other.id)).telegram_id)


if __name__ == "__main__":
    unittest.main()
