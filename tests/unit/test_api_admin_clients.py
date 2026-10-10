"""Админка сайта: карточка участника, абонементы, баланс, платежи, журналы."""

import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from flamenco_bot.payments.client import ProviderRefund
from tests.pg import sql

from .test_api_admin import ApiTestBase
from .test_api_payments import sign_telegram_payload

CLIENT_ID = 555


class AdminClientsApiTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.register()
        self.make_admin()
        member = self.make_client(self.client.app)
        member.post(
            "/api/auth/telegram",
            json=sign_telegram_payload(
                {"id": CLIENT_ID, "first_name": "Анна", "auth_date": int(time.time())}
            ),
        )
        self.member = member

    def paid(self, lessons=4):
        repo = self.repository

        async def create():
            attempt = await repo.begin_lesson_payment_attempt(
                CLIENT_ID, "pack_4", "Абонемент на 4 занятия", lessons, 360000
            )
            payment = await repo.create_lesson_payment(
                telegram_id=CLIENT_ID,
                package_key="pack_4",
                package_title="Абонемент на 4 занятия",
                lessons=lessons,
                amount_minor=360000,
                provider_payment_id="yk-{}".format(uuid.uuid4().hex),
                confirmation_url="https://yoomoney.ru/x",
                idempotence_key=attempt.idempotence_key,
            )
            await repo.complete_lesson_payment(payment.id, CLIENT_ID)
            return payment

        return self.run_async(create())

    # ---------- карточка ----------

    def test_card_shows_profile_packages_and_history(self):
        self.paid()
        card = self.client.get(f"/api/admin/clients/{CLIENT_ID}")
        self.assertEqual(card.status_code, 200, card.text)
        body = card.json()
        self.assertEqual(body["profile"]["user_name"], "Анна")
        self.assertTrue(body["has_web_account"])
        self.assertEqual(body["packages"]["balance"], 4)
        self.assertIsNotNone(body["packages"]["packages"][0]["expires_at"])
        self.assertEqual(self.client.get("/api/admin/clients/999999").status_code, 404)
        self.assertEqual(
            self.client.get(f"/api/admin/clients/{CLIENT_ID}/bookings").json()["items"],
            [],
        )
        ledger = self.client.get(
            "/api/admin/ledger", params={"telegram_id": CLIENT_ID}
        ).json()
        self.assertEqual([item["operation"] for item in ledger["items"]], ["purchase"])

    def test_profile_edit_validates_and_is_audited(self):
        response = self.client.patch(
            f"/api/admin/clients/{CLIENT_ID}/profile",
            json={"user_name": "  Анна Мария ", "phone": "+7 (999) 000-11-22"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user_name"], "Анна Мария")
        self.assertEqual(response.json()["phone"], "+7 (999) 000-11-22")
        self.assertEqual(
            self.client.patch(
                f"/api/admin/clients/{CLIENT_ID}/profile", json={"phone": "abc"}
            ).status_code,
            422,
        )
        audit = self.client.get("/api/admin/audit").json()["items"]
        self.assertEqual(audit[0]["action"], "profile_updated")
        self.assertEqual(audit[0]["label"], "изменён профиль участника")
        self.assertEqual(audit[0]["target_name"], "Анна Мария")

    # ---------- абонементы и баланс ----------

    def test_grant_revoke_and_adjust_are_idempotent(self):
        key = str(uuid.uuid4())
        payload = {
            "package_key": "pack_8",
            "reason": "наличные",
            "idempotence_key": key,
        }
        first = self.client.post(f"/api/admin/clients/{CLIENT_ID}/grants", json=payload)
        again = self.client.post(f"/api/admin/clients/{CLIENT_ID}/grants", json=payload)
        self.assertEqual((first.json()["applied"], first.json()["balance"]), (True, 8))
        self.assertEqual((again.json()["applied"], again.json()["balance"]), (False, 8))

        grant_id = next(
            item["id"]
            for item in self.client.get(f"/api/admin/clients/{CLIENT_ID}").json()[
                "packages"
            ]["packages"]
            if item["kind"] == "grant"
        )
        revoked = self.client.post(
            f"/api/admin/grants/{grant_id}/revoke", json={"reason": "ошибка"}
        ).json()
        self.assertEqual((revoked["lessons"], revoked["balance"]), (8, 0))

        adjust_key = str(uuid.uuid4())
        adjusted = self.client.post(
            f"/api/admin/clients/{CLIENT_ID}/credits",
            json={"delta": 2, "reason": "бонус", "idempotence_key": adjust_key},
        )
        self.assertEqual(adjusted.json()["balance"], 2)
        too_much = self.client.post(
            f"/api/admin/clients/{CLIENT_ID}/credits",
            json={
                "delta": -5,
                "reason": "ошибка",
                "idempotence_key": str(uuid.uuid4()),
            },
        )
        self.assertEqual(too_much.status_code, 409)
        self.assertEqual(
            self.client.post(
                f"/api/admin/clients/{CLIENT_ID}/grants",
                json=dict(
                    payload, package_key="nope", idempotence_key=str(uuid.uuid4())
                ),
            ).status_code,
            422,
        )

    def test_client_endpoints_are_admin_only(self):
        teacher = self.make_client(self.client.app)
        self.register("t@example.com", client=teacher)
        self.make_admin(telegram_id=901, role="teacher", client=teacher)
        for path in (
            f"/api/admin/clients/{CLIENT_ID}",
            "/api/admin/payments",
            "/api/admin/ledger",
            "/api/admin/audit",
            "/api/admin/reconciliation",
        ):
            self.assertEqual(teacher.get(path).status_code, 403, path)

    # ---------- платежи ----------

    def test_payments_filter_and_page(self):
        first = self.paid()
        second = self.paid()
        page = self.client.get("/api/admin/payments", params={"limit": 1}).json()
        self.assertEqual([item["id"] for item in page["items"]], [second.id])
        older = self.client.get(
            "/api/admin/payments",
            params={"limit": 1, "before_id": page["next_before_id"]},
        ).json()
        self.assertEqual([item["id"] for item in older["items"]], [first.id])
        self.assertIsNone(older["next_before_id"])
        none_pending = self.client.get(
            "/api/admin/payments", params={"status": "pending"}
        ).json()
        self.assertEqual(none_pending["items"], [])

    def test_refund_of_unused_payment_through_provider(self):
        payment = self.paid()
        provider_id = self.run_async(self._provider_id(payment.id))
        gateway = SimpleNamespace(
            is_configured=True,
            create_refund=AsyncMock(
                return_value=ProviderRefund(
                    "rf-1", provider_id, "succeeded", 360000, "RUB"
                )
            ),
            get_refund=AsyncMock(),
        )
        self.client.app.state.admin_payment_gateway = gateway

        refunded = self.client.post(
            f"/api/admin/payments/{payment.id}/refund", json={"reason": "передумала"}
        )
        self.assertEqual(refunded.status_code, 200, refunded.text)
        self.assertEqual(refunded.json()["status"], "completed")
        listed = self.client.get(
            "/api/admin/payments", params={"telegram_id": CLIENT_ID}
        ).json()["items"]
        self.assertEqual(listed[0]["status"], "refunded")
        self.assertEqual(
            self.client.post(
                f"/api/admin/payments/{payment.id}/refund", json={"reason": "ещё"}
            ).status_code,
            409,
        )

    async def _provider_id(self, payment_id):
        async with self.repository._pool.acquire() as connection:
            return await connection.fetchval(
                "SELECT provider_payment_id FROM lesson_payments WHERE id = $1",
                payment_id,
            )

    def test_reconciliation_reports_mismatch(self):
        self.paid()
        self.run_async(
            sql(
                self.repository,
                "UPDATE bot_users SET lesson_credits = 7 WHERE telegram_id = $1",
                CLIENT_ID,
            )
        )
        body = self.client.get("/api/admin/reconciliation").json()
        self.assertEqual(body["mismatched_users"], 1)
        self.assertEqual(body["mismatches"][0]["difference"], 3)


class AdminSlotsArchiveAndSupportTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.register()
        self.make_admin()

    def test_archive_lists_past_slots_newest_first(self):
        repo = self.repository
        now = datetime.now(timezone.utc)

        async def seed():
            ids = []
            for days in (3, 1):
                slot = await repo.create_class_slot(
                    "beginner", now + timedelta(days=1), 5, 900
                )
                await sql(
                    repo,
                    "UPDATE lesson_slots SET starts_at = $2 WHERE id = $1",
                    slot.id,
                    now - timedelta(days=days),
                )
                ids.append(slot.id)
            return ids

        older, newer = self.run_async(seed())
        archive = self.client.get(
            "/api/admin/slots",
            params={
                "from": (now - timedelta(days=10)).isoformat(),
                "to": now.isoformat(),
            },
        )
        self.assertEqual(archive.status_code, 200, archive.text)
        self.assertEqual([slot["id"] for slot in archive.json()], [newer, older])
        self.assertEqual(
            self.client.get(
                "/api/admin/slots", params={"from": now.isoformat()}
            ).status_code,
            422,
        )

    def test_closed_ticket_can_be_reopened(self):
        member = self.make_client(self.client.app)
        member.post(
            "/api/auth/telegram",
            json=sign_telegram_payload(
                {"id": 777, "first_name": "Ира", "auth_date": int(time.time())}
            ),
        )
        member.post("/api/support", json={"body": "Вопрос"})
        ticket_id = self.client.get("/api/admin/support").json()[0]["id"]
        self.client.post(f"/api/admin/support/{ticket_id}/close")
        reopened = self.client.post(f"/api/admin/support/{ticket_id}/reopen")
        self.assertEqual(reopened.status_code, 200, reopened.text)
        self.assertEqual(
            self.client.get(f"/api/admin/support/{ticket_id}").json()["status"], "open"
        )
