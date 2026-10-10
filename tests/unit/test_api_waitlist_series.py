"""Лист ожидания, запись за участника, серия занятий и копия недели."""

import time
from datetime import datetime, timedelta, timezone

from tests.pg import set_credits

from .test_api_admin import ApiTestBase
from .test_api_payments import sign_telegram_payload

WAITING_ID = 701
BOOKED_ID = 702


class WaitlistApiTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.register()
        self.make_admin()
        self.waiting = self.login_member(WAITING_ID, "Анна")
        self.booked = self.login_member(BOOKED_ID, "Мария")
        self.slot_id = self.client.post(
            "/api/admin/slots",
            json={"class_key": "beginner", "starts_at": self.future(), "capacity": 1},
        ).json()["id"]

    def login_member(self, telegram_id, name):
        client = self.make_client(self.client.app)
        client.post(
            "/api/auth/telegram",
            json=sign_telegram_payload(
                {"id": telegram_id, "first_name": name, "auth_date": int(time.time())}
            ),
        )
        self.run_async(set_credits(self.repository, telegram_id, 2))
        return client

    def kinds(self, telegram_id):
        return [
            n.kind
            for n in self.run_async(self.repository.list_notifications(telegram_id))
        ]

    def test_full_slot_waitlist_notifies_when_seat_frees(self):
        # Свободное место — в очередь нельзя, нужно просто записаться.
        self.assertEqual(
            self.waiting.post(
                "/api/waitlist", json={"slot_id": self.slot_id}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.booked.post(
                "/api/bookings", json={"slot_id": self.slot_id}
            ).status_code,
            201,
        )
        joined = self.waiting.post("/api/waitlist", json={"slot_id": self.slot_id})
        self.assertEqual(joined.status_code, 201, joined.text)
        self.assertEqual(joined.json()["queue_size"], 1)
        self.assertEqual(
            [e["slot"]["id"] for e in self.waiting.get("/api/waitlist/me").json()],
            [self.slot_id],
        )
        staff_view = self.client.get(f"/api/admin/slots/{self.slot_id}/waitlist").json()
        self.assertEqual([m["telegram_id"] for m in staff_view], [WAITING_ID])

        # Освободилось место — уведомление всем в очереди.
        self.assertEqual(
            self.booked.delete(f"/api/bookings/{self.slot_id}").status_code, 204
        )
        self.assertIn("waitlist_seat", self.kinds(WAITING_ID))

        # Запись убирает из очереди.
        self.assertEqual(
            self.waiting.post(
                "/api/bookings", json={"slot_id": self.slot_id}
            ).status_code,
            201,
        )
        self.assertEqual(self.waiting.get("/api/waitlist/me").json(), [])

    def test_waitlist_requires_credits_and_can_be_left(self):
        self.booked.post("/api/bookings", json={"slot_id": self.slot_id})
        self.run_async(set_credits(self.repository, WAITING_ID, 0))
        refused = self.waiting.post("/api/waitlist", json={"slot_id": self.slot_id})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("баланс", refused.json()["detail"])

        self.run_async(set_credits(self.repository, WAITING_ID, 1))
        self.waiting.post("/api/waitlist", json={"slot_id": self.slot_id})
        self.assertEqual(
            self.waiting.delete(f"/api/waitlist/{self.slot_id}").status_code, 204
        )
        self.assertEqual(
            self.waiting.delete(f"/api/waitlist/{self.slot_id}").status_code, 404
        )

    def test_capacity_increase_notifies_queue(self):
        self.booked.post("/api/bookings", json={"slot_id": self.slot_id})
        self.waiting.post("/api/waitlist", json={"slot_id": self.slot_id})
        self.client.patch(f"/api/admin/slots/{self.slot_id}", json={"capacity": 2})
        self.assertIn("waitlist_seat", self.kinds(WAITING_ID))


class AdminBookingForClientTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.register()
        self.make_admin()
        member = self.make_client(self.client.app)
        member.post(
            "/api/auth/telegram",
            json=sign_telegram_payload(
                {"id": WAITING_ID, "first_name": "Анна", "auth_date": int(time.time())}
            ),
        )

    def test_admin_books_and_cancels_after_window(self):
        # Занятие через 2 часа — участник сам отменить уже не может.
        slot = self.client.post(
            "/api/admin/slots",
            json={
                "class_key": "beginner",
                "starts_at": (
                    datetime.now(timezone.utc) + timedelta(hours=2)
                ).isoformat(),
                "capacity": 3,
            },
        ).json()
        no_credits = self.client.post(
            f"/api/admin/clients/{WAITING_ID}/bookings", json={"slot_id": slot["id"]}
        )
        self.assertEqual(no_credits.status_code, 409)

        self.run_async(set_credits(self.repository, WAITING_ID, 1))
        booked = self.client.post(
            f"/api/admin/clients/{WAITING_ID}/bookings", json={"slot_id": slot["id"]}
        )
        self.assertEqual(booked.status_code, 201, booked.text)
        self.assertEqual(booked.json()["balance"], 0)

        cancelled = self.client.post(
            f"/api/admin/clients/{WAITING_ID}/bookings/{slot['id']}/cancel"
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["balance"], 1)
        actions = [e.action for e in self.run_async(self.repository.list_staff_audit())]
        self.assertIn("booking_created", actions)
        self.assertIn("booking_cancelled", actions)

    def test_admin_booking_respects_closed_slot(self):
        slot = self.client.post(
            "/api/admin/slots",
            json={"class_key": "beginner", "starts_at": self.future(), "capacity": 3},
        ).json()
        self.client.post(f"/api/admin/slots/{slot['id']}/close")
        self.run_async(set_credits(self.repository, WAITING_ID, 1))
        refused = self.client.post(
            f"/api/admin/clients/{WAITING_ID}/bookings", json={"slot_id": slot["id"]}
        )
        self.assertEqual(refused.status_code, 409)


class SeriesAndCopyWeekTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.register()
        self.make_admin()

    def test_series_creates_weekly_slots_and_skips_duplicates(self):
        payload = {
            "class_key": "beginner",
            "starts_at": self.future(2),
            "capacity": 8,
            "weeks": 4,
        }
        first = self.client.post("/api/admin/slots/series", json=payload)
        self.assertEqual(first.status_code, 201, first.text)
        starts = [
            datetime.fromisoformat(s["starts_at"].replace("Z", "+00:00"))
            for s in first.json()["created"]
        ]
        self.assertEqual(len(starts), 4)
        self.assertTrue(
            all(
                b - a
                in (
                    timedelta(days=7),
                    timedelta(days=7, hours=1),
                    timedelta(days=7, hours=-1),
                )
                for a, b in zip(starts, starts[1:])
            )
        )
        again = self.client.post("/api/admin/slots/series", json=payload).json()
        self.assertEqual((len(again["created"]), again["skipped"]), (0, 4))
        self.assertEqual(
            self.client.post(
                "/api/admin/slots/series", json=dict(payload, weeks=13)
            ).status_code,
            422,
        )

    def test_copy_week_copies_to_following_weeks(self):
        start = datetime.now(timezone.utc) + timedelta(days=1)
        self.client.post(
            "/api/admin/slots",
            json={
                "class_key": "beginner",
                "starts_at": start.isoformat(),
                "capacity": 5,
            },
        )
        copied = self.client.post(
            "/api/admin/slots/copy-week",
            json={"week_start": start.date().isoformat(), "weeks": 2},
        )
        self.assertEqual(copied.status_code, 201, copied.text)
        self.assertEqual(len(copied.json()["created"]), 2)
        self.assertTrue(all(s["capacity"] == 5 for s in copied.json()["created"]))
        empty = self.client.post(
            "/api/admin/slots/copy-week",
            json={
                "week_start": (start + timedelta(days=60)).date().isoformat(),
                "weeks": 1,
            },
        )
        self.assertEqual(empty.status_code, 404)
