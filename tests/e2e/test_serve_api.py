"""Сервер сквозных тестов сайта (tests/e2e/serve_api.py) запускается и отдаёт
заранее созданные данные.

Сам сервер используют тесты репозитория WEBSITE (сайт ↔ настоящий API); здесь
проверяется, что он работает, чтобы поломку нашёл CI бота, а не CI сайта.
"""

import json
import os
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path

from tests.pg import DATABASE_URL, SKIP_REASON

SERVER = Path(__file__).with_name("serve_api.py")


def request(port, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:{}{}".format(port, path),
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.status, json.loads(response.read().decode())


@unittest.skipUnless(DATABASE_URL, SKIP_REASON)
class ServeApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.process = subprocess.Popen(
            [sys.executable, str(SERVER), "0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ),
        )
        deadline = 60
        import threading

        timer = threading.Timer(deadline, cls.process.kill)
        timer.start()
        try:
            for line in cls.process.stdout:
                if line.startswith("READY "):
                    cls.info = json.loads(line[len("READY ") :])
                    break
            else:
                raise RuntimeError(
                    "сервер не запустился: " + cls.process.stderr.read()[-2000:]
                )
        finally:
            timer.cancel()

    @classmethod
    def tearDownClass(cls):
        cls.process.kill()
        cls.process.wait(timeout=15)
        cls.process.stdout.close()
        cls.process.stderr.close()

    def test_health_and_catalog_come_from_the_migrated_database(self):
        status, body = request(self.info["port"], "/api/health")
        self.assertEqual((status, body), (200, {"status": "ok"}))
        _, packages = request(self.info["port"], "/api/packages")
        self.assertEqual([p["key"] for p in packages], ["single", "pack_4", "pack_8"])

    def test_seeded_accounts_can_sign_in(self):
        for email in ("user@example.com", "admin@example.com", "other@example.com"):
            with self.subTest(email=email):
                status, _ = request(
                    self.info["port"],
                    "/api/auth/login",
                    {"email": email, "password": self.info["password"]},
                )
                self.assertEqual(status, 200)

    def test_seeded_schedule_has_the_documented_slots(self):
        _, slots = request(self.info["port"], "/api/schedule")
        by_id = {slot["id"]: slot for slot in slots}
        self.assertEqual(by_id[self.info["slots"]["roomy"]]["capacity"], 5)
        self.assertEqual(by_id[self.info["slots"]["single_seat"]]["capacity"], 1)
        self.assertIn(self.info["slots"]["soon"], by_id)


if __name__ == "__main__":
    unittest.main()
