import unittest

from flamenco_bot.config import (
    parse_credit_adjustment_limit,
    parse_database_pool_sizes,
    parse_website_url,
)


class ConfigTests(unittest.TestCase):
    def test_database_pool_sizes_accept_valid_bounds(self):
        self.assertEqual(parse_database_pool_sizes("2", "8"), (2, 8))

    def test_database_pool_sizes_reject_invalid_values(self):
        for minimum, maximum in (("x", "8"), ("0", "2"), ("3", "2")):
            with self.subTest(minimum=minimum, maximum=maximum):
                with self.assertRaises(ValueError):
                    parse_database_pool_sizes(minimum, maximum)

    def test_website_url_is_optional_and_normalized(self):
        self.assertIsNone(parse_website_url("", "production"))
        self.assertEqual(
            parse_website_url(" https://studio.example/ ", "production"),
            "https://studio.example",
        )

    def test_website_url_requires_https_outside_local_development(self):
        with self.assertRaises(ValueError):
            parse_website_url("http://studio.example", "production")
        with self.assertRaises(ValueError):
            parse_website_url("http://localhost:3000", "production")
        self.assertEqual(
            parse_website_url("http://localhost:3000", "development"),
            "http://localhost:3000",
        )

    def test_credit_adjustment_limit_accepts_positive_integers_only(self):
        self.assertEqual(parse_credit_adjustment_limit("50"), 50)
        for value in ("0", "-3", "many", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_credit_adjustment_limit(value)
