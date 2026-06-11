"""Tests for the pure-logic helpers in bot/anibot.py."""

import unittest
from datetime import date

import support

anibot = support.load_anibot()
scrape = anibot.should_scrape_despite_skip

SKIP = date(2026, 4, 22)  # the skip_until airdate used across cases
SKIP_STR = "2026-04-22"


class ShouldScrapeDespiteSkipTest(unittest.TestCase):
    def test_eve_scrapes(self):
        # window=1, the day before the airdate → scrape early to catch a release.
        self.assertTrue(scrape(SKIP_STR, date(2026, 4, 21), 1))

    def test_well_before_skips(self):
        self.assertFalse(scrape(SKIP_STR, date(2026, 4, 10), 1))

    def test_on_date_scrapes(self):
        self.assertTrue(scrape(SKIP_STR, SKIP, 1))

    def test_after_date_scrapes(self):
        self.assertTrue(scrape(SKIP_STR, date(2026, 4, 25), 1))

    def test_bad_date_safe_scrapes(self):
        # An unparseable date must not silently suppress scraping.
        self.assertTrue(scrape("not-a-date", SKIP, 1))
        self.assertTrue(scrape("2026-13-99", SKIP, 1))

    def test_empty_and_none_safe_scrape(self):
        self.assertTrue(scrape("", SKIP, 1))
        self.assertTrue(scrape(None, SKIP, 1))

    def test_window_zero_strict(self):
        # window=0 → honor skip_until strictly: only scrape on/after the date.
        self.assertFalse(scrape(SKIP_STR, date(2026, 4, 21), 0))
        self.assertTrue(scrape(SKIP_STR, SKIP, 0))

    def test_larger_window(self):
        # window=3 → start scraping 3 days before.
        self.assertFalse(scrape(SKIP_STR, date(2026, 4, 18), 3))
        self.assertTrue(scrape(SKIP_STR, date(2026, 4, 19), 3))
        self.assertTrue(scrape(SKIP_STR, date(2026, 4, 20), 3))


if __name__ == "__main__":
    unittest.main()
