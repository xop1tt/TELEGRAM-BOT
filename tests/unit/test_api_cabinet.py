"""Клиентский Web API личного кабинета: обзор, абонементы, история,
уведомления, расписание, переписка поддержки, вход через бота."""

import hashlib
import hmac
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock


from flamenco_bot.api.app import create_app
from flamenco_bot.payments import YooKassaClient
from flamenco_bot.runtime.security import AuthRateLimiter, SupportRateLimiter
from flamenco_bot.services import AuthService
from tests.pg import PostgresApiTestCase, make_admin

BOT_TOKEN = "123456:test-token"
USER_ID = 1001
ADMIN_ID = 77


def signed(telegram_id):
    data = {"id": telegram_id, "first_name": "Анна", "auth_date": int(time.time())}
    check_string = "\n".join("{}={}".format(k, data[k]) for k in sorted(data))
    secret = hashlib.sha256(BOT_TOKEN.encode("utf-8")).digest()
    data["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return data


def parse_api_time(value):
    """ISO 8601 из API (UTC с суффиксом Z — Python 3.10 его не принимает)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ApiCabinetTests(PostgresApiTestCase):
    def setUp(self):
        super().setUp()
        app = create_app()
        app.state.repository = self.repository
        app.state.auth_service = AuthService(self.repository, BOT_TOKEN)
        app.state.bot = AsyncMock()
        app.state.bot.get_me.return_value = SimpleNamespace(username="mirada_bot")
        app.state.support_limiter = SupportRateLimiter()
        app.state.auth_limiter = AuthRateLimiter(max_attempts=100)
        app.state.payment_gateway = YooKassaClient("", "", "")
        self.client = self.make_client(app)
        self.call(self.repository.get_or_create_profile(ADMIN_ID, "Админ"))
        self.run_async(make_admin(self.repository, ADMIN_ID))

    def call(self, coroutine):
        return self.run_async(coroutine)

    def login(self, telegram_id=USER_ID):
        response = self.client.post("/api/auth/telegram", json=signed(telegram_id))
        self.assertEqual(response.status_code, 200)

    def slot(self, days=3, capacity=5):
        return self.call(
            self.repository.create_class_slot(
                "beginner",
                datetime.now(timezone.utc) + timedelta(days=days),
                capacity,
                ADMIN_ID,
            )
        )

    def grant(self, telegram_id=USER_ID, lessons=4):
        return self.call(
            self.repository.grant_lesson_package(
                telegram_id,
                "pack_4",
                "Абонемент на 4 занятия",
                lessons,
                "наличные",
                ADMIN_ID,
                uuid.uuid4(),
            )
        )

    # --- доступ ------------------------------------------------------------

    def test_cabinet_endpoints_require_login_and_linked_telegram(self):
        paths = (
            "/api/users/me/overview",
            "/api/users/me/notification-settings",
            "/api/packages/me",
            "/api/history/operations",
            "/api/history/classes",
            "/api/notifications/me",
            "/api/notifications/me/unread-count",
            "/api/support/me/1",
        )
        for path in paths:
            with self.subTest(path=path, auth="none"):
                self.assertEqual(self.client.get(path).status_code, 401)
        self.client.post(
            "/api/auth/register",
            json={
                "email": "bob@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Боб",
            },
        )
        for path in paths:
            with self.subTest(path=path, auth="email-only"):
                self.assertEqual(self.client.get(path).status_code, 409)

    def test_ids_outside_bigint_are_rejected_as_validation_errors(self):
        # Раньше такие id доходили до asyncpg и давали 500 вместо 4xx.
        self.login()
        too_big = 2**63
        requests = (
            ("get", "/api/schedule/{}".format(too_big), None),
            ("get", "/api/support/me/{}".format(too_big), None),
            ("delete", "/api/bookings/{}".format(too_big), None),
            ("get", "/api/payments/{}/check".format(too_big), None),
            ("get", "/api/history/operations?before_id={}".format(too_big), None),
            ("get", "/api/notifications/me?before_id={}".format(too_big), None),
            ("post", "/api/bookings", {"slot_id": too_big}),
            ("post", "/api/notifications/me/read", {"ids": [too_big]}),
        )
        for method, path, body in requests:
            with self.subTest(method=method, path=path):
                kwargs = {"json": body} if body is not None else {}
                response = getattr(self.client, method)(path, **kwargs)
                self.assertEqual(response.status_code, 422)
        # Границы не меняют обычные ответы.
        self.assertEqual(self.client.get("/api/schedule/0").status_code, 404)
        self.assertEqual(
            self.client.get("/api/schedule/{}".format(2**63 - 1)).status_code, 404
        )

    # --- обзор, абонементы, история ---------------------------------------

    def test_overview_packages_and_history_reflect_ledger(self):
        self.login()
        self.grant()
        slot = self.slot()
        booked = self.client.post("/api/bookings", json={"slot_id": slot.id})
        self.assertEqual(booked.status_code, 201)
        self.assertEqual(booked.json()["balance"], 3)
        self.assertEqual(booked.json()["source"]["remaining"], 3)

        overview = self.client.get("/api/users/me/overview").json()
        self.assertEqual(overview["profile"]["lesson_credits"], 3)
        self.assertEqual(overview["packages"]["balance"], 3)
        self.assertEqual(overview["packages"]["packages"][0]["remaining"], 3)
        self.assertEqual(overview["upcoming"][0]["slot_id"], slot.id)
        self.assertTrue(overview["upcoming"][0]["can_cancel"])
        self.assertGreaterEqual(overview["unread_notifications"], 1)

        packages = self.client.get("/api/packages/me").json()
        self.assertEqual(
            sum(p["remaining"] for p in packages["packages"]) + packages["unallocated"],
            packages["balance"],
        )
        self.assertEqual(packages["next_source_title"], "Абонемент на 4 занятия")

        history = self.client.get("/api/history/operations?limit=1").json()
        self.assertEqual(history["items"][0]["operation"], "lesson_use")
        self.assertEqual(history["items"][0]["class_label"], "Фламенко для начинающих")
        older = self.client.get(
            "/api/history/operations?before_id={}".format(history["next_before_id"])
        ).json()
        self.assertEqual(older["items"][0]["operation"], "package_grant")
        self.assertEqual(older["items"][0]["reason"], "наличные")

        classes = self.client.get("/api/history/classes").json()
        self.assertEqual(classes["items"][0]["status"], "upcoming")

    # --- уведомления --------------------------------------------------------

    def test_web_booking_and_cancellation_queue_notifications(self):
        self.login()
        self.grant()
        slot = self.slot()
        self.client.post("/api/bookings", json={"slot_id": slot.id})
        self.assertEqual(
            self.client.delete("/api/bookings/{}".format(slot.id)).status_code, 204
        )

        feed = self.client.get("/api/notifications/me").json()
        kinds = [item["kind"] for item in feed["items"]]
        self.assertEqual(kinds[:2], ["booking_cancelled", "booking_confirmed"])
        self.assertEqual(feed["items"][0]["title"], "Запись отменена")
        self.assertEqual(feed["unread_count"], len(feed["items"]))

        first_id = feed["items"][0]["id"]
        marked = self.client.post(
            "/api/notifications/me/read", json={"ids": [first_id]}
        )
        self.assertEqual(marked.json(), {"updated": 1})
        self.assertEqual(
            self.client.get("/api/notifications/me/unread-count").json()[
                "unread_count"
            ],
            len(feed["items"]) - 1,
        )
        all_read = self.client.post("/api/notifications/me/read", json={})
        self.assertEqual(all_read.json()["updated"], len(feed["items"]) - 1)

    def test_cannot_mark_other_users_notifications(self):
        self.call(self.repository.get_or_create_profile(2002, "Мария"))
        self.call(self.repository.enqueue_notification(2002, "low_balance", {}, "x"))
        other_id = self.call(self.repository.list_notifications(2002))[0].id
        self.login()

        response = self.client.post(
            "/api/notifications/me/read", json={"ids": [other_id]}
        )

        self.assertEqual(response.json(), {"updated": 0})
        self.assertEqual(self.call(self.repository.count_unread_notifications(2002)), 1)

    def test_notification_settings(self):
        self.login()
        self.assertEqual(
            self.client.get("/api/users/me/notification-settings").json(),
            {"reminders": True, "low_balance": True},
        )
        updated = self.client.patch(
            "/api/users/me/notification-settings", json={"reminders": False}
        )
        self.assertEqual(updated.json(), {"reminders": False, "low_balance": True})

    # --- расписание и записи ---------------------------------------------

    def test_schedule_hides_cancelled_slots_but_card_shows_status(self):
        open_slot = self.slot(days=2, capacity=1)
        cancelled = self.slot(days=3)
        self.call(self.repository.cancel_class_slot(cancelled.id, ADMIN_ID, "ремонт"))

        listing = self.client.get("/api/schedule").json()
        self.assertEqual([item["id"] for item in listing], [open_slot.id])
        self.assertTrue(listing[0]["bookable"])

        card = self.client.get("/api/schedule/{}".format(cancelled.id)).json()
        self.assertEqual(card["status"], "cancelled")
        self.assertEqual(card["cancel_reason"], "ремонт")
        self.assertFalse(card["bookable"])
        self.assertEqual(self.client.get("/api/schedule/999999").status_code, 404)

        self.login()
        self.grant()
        self.client.post("/api/bookings", json={"slot_id": open_slot.id})
        available = self.client.get("/api/schedule?available_only=true").json()
        self.assertEqual(available, [])

    def test_bookings_scope_and_relaxed_deadline_after_reschedule(self):
        self.login()
        self.grant()
        slot = self.slot(days=3)
        self.client.post("/api/bookings", json={"slot_id": slot.id})
        new_time = datetime.now(timezone.utc) + timedelta(hours=6)
        self.call(self.repository.reschedule_class_slot(slot.id, new_time, ADMIN_ID))

        upcoming = self.client.get("/api/bookings/me?scope=upcoming").json()
        self.assertEqual(len(upcoming), 1)
        booking = upcoming[0]
        self.assertTrue(booking["can_cancel"])
        self.assertEqual(
            parse_api_time(booking["cancellable_until"]),
            parse_api_time(booking["starts_at"]),
        )
        self.assertIsNotNone(booking["previous_starts_at"])
        self.assertEqual(self.client.get("/api/bookings/me?scope=past").json(), [])
        self.assertEqual(
            self.client.delete("/api/bookings/{}".format(slot.id)).status_code, 204
        )
        past = self.client.get("/api/bookings/me?scope=past").json()
        self.assertEqual(past[0]["status"], "cancelled_by_user")
        self.assertEqual(
            self.client.get("/api/bookings/me?scope=bogus").status_code, 422
        )

    # --- поддержка ----------------------------------------------------------

    def test_support_thread_is_visible_only_to_owner(self):
        self.login()
        created = self.client.post("/api/support", json={"body": "Вопрос"}).json()
        self.call(
            self.repository.reply_support_ticket(
                created["ticket_id"], ADMIN_ID, "Ответ"
            )
        )

        thread = self.client.get("/api/support/me/{}".format(created["ticket_id"]))
        self.assertEqual(thread.status_code, 200)
        self.assertEqual(
            [m["sender_role"] for m in thread.json()["messages"]], ["user", "admin"]
        )

        self.client.post("/api/auth/logout")
        self.login(2002)
        foreign = self.client.get("/api/support/me/{}".format(created["ticket_id"]))
        self.assertEqual(foreign.status_code, 404)

    # --- вход через бота ----------------------------------------------------

    def test_connect_login_flow_issues_session_once(self):
        started = self.client.post("/api/auth/telegram/connect", json={})
        self.assertEqual(started.status_code, 201)
        body = started.json()
        self.assertTrue(
            body["deep_link"].startswith("https://t.me/mirada_bot?start=c_")
        )
        self.assertIn("tg_connect", started.headers["set-cookie"])
        self.assertIn("HttpOnly", started.headers["set-cookie"])

        pending = self.client.post("/api/auth/telegram/connect/complete")
        self.assertEqual(pending.json()["status"], "pending")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

        token = body["deep_link"].split("start=c_", 1)[1]
        self.call(
            AuthService(self.repository, BOT_TOKEN).confirm_telegram_connect(
                token, USER_ID, "Анна"
            )
        )
        completed = self.client.post("/api/auth/telegram/connect/complete")
        self.assertEqual(completed.json()["status"], "completed")
        self.assertEqual(completed.json()["user"]["telegram_id"], USER_ID)
        self.assertEqual(self.client.get("/api/auth/me").json()["telegram_id"], USER_ID)

        # Cookie запроса удалена — повторно сессию не получить.
        again = self.client.post("/api/auth/telegram/connect/complete")
        self.assertEqual(again.status_code, 400)

    def test_connect_link_requires_login_and_links_current_account(self):
        self.assertEqual(
            self.client.post(
                "/api/auth/telegram/connect", json={"purpose": "link"}
            ).status_code,
            401,
        )
        self.client.post(
            "/api/auth/register",
            json={
                "email": "bob@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Боб",
            },
        )
        started = self.client.post(
            "/api/auth/telegram/connect", json={"purpose": "link"}
        ).json()
        token = started["deep_link"].split("start=c_", 1)[1]
        self.call(
            AuthService(self.repository, BOT_TOKEN).confirm_telegram_connect(
                token, USER_ID, "Боб"
            )
        )
        completed = self.client.post("/api/auth/telegram/connect/complete").json()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["user"]["email"], "bob@example.com")
        self.assertEqual(completed["user"]["telegram_id"], USER_ID)

    def test_connect_rejected_in_bot(self):
        started = self.client.post("/api/auth/telegram/connect", json={}).json()
        token = started["deep_link"].split("start=c_", 1)[1]
        self.call(
            AuthService(self.repository, BOT_TOKEN).reject_telegram_connect(token)
        )
        response = self.client.post("/api/auth/telegram/connect/complete")
        self.assertEqual(response.json(), {"status": "rejected", "user": None})


if __name__ == "__main__":
    unittest.main()
