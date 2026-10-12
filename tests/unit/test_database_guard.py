"""Защита тестов от подключения к рабочей базе (tests/pg.py)."""

import os
import unittest
from unittest.mock import patch

from tests.pg import _guard_test_database


class TestDatabaseGuardTests(unittest.TestCase):
    def test_local_hosts_are_allowed(self):
        for url in (
            "postgresql://test@127.0.0.1:55432/test",
            "postgresql://u:p@localhost/db",
            "postgresql://u@[::1]/db",
        ):
            self.assertEqual(_guard_test_database(url), url)

    def test_empty_means_tests_are_skipped_not_blocked(self):
        self.assertIsNone(_guard_test_database(None))
        self.assertEqual(_guard_test_database(""), "")

    def test_remote_host_is_refused_unless_explicitly_allowed(self):
        url = "postgresql://u:p@db.supabase.example/postgres"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALLOW_REMOTE_TEST_DATABASE", None)
            with self.assertRaisesRegex(RuntimeError, "не на локальный"):
                _guard_test_database(url)
        with patch.dict(os.environ, {"ALLOW_REMOTE_TEST_DATABASE": "1"}):
            self.assertEqual(_guard_test_database(url), url)

    def test_same_url_as_the_working_database_is_always_refused(self):
        url = "postgresql://u:p@localhost:5432/flamenco_bot"
        with patch.dict(
            os.environ, {"DATABASE_URL": url, "ALLOW_REMOTE_TEST_DATABASE": "1"}
        ):
            with self.assertRaisesRegex(RuntimeError, "совпадает"):
                _guard_test_database(url)

    def test_error_message_does_not_leak_credentials(self):
        url = "postgresql://user:SECRETPASS@db.example/x"
        os.environ.pop("ALLOW_REMOTE_TEST_DATABASE", None)
        with self.assertRaises(RuntimeError) as raised:
            _guard_test_database(url)
        self.assertNotIn("SECRETPASS", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
