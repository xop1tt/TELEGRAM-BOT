"""Документация API (/docs, /redoc, /openapi.json) закрыта в production."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from flamenco_bot.api.app import create_app
from flamenco_bot.api.config import WebConfig


def _status(path: str, env: str) -> int:
    with patch.object(WebConfig, "ENV", env):
        app = create_app()
    # Без lifespan: приложению для этих адресов не нужна база.
    return TestClient(app).get(path).status_code


class ApiDocsTests(unittest.TestCase):
    def test_docs_are_hidden_in_production(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(_status(path, "production"), 404)

    def test_docs_are_available_in_development(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(_status(path, "development"), 200)


if __name__ == "__main__":
    unittest.main()
