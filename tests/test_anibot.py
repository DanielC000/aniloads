"""Tests for the pure-logic helpers in bot/anibot.py."""

import json
import os
import tempfile
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


class WriteRunStateTest(unittest.TestCase):
    """The bot persists a per-cycle run-state record next to ani.json so the
    dashboard can show last_run/next_run without scraping the rolling log tail."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aniloads-runstate-")
        self._orig_botfile = anibot.botfile
        anibot.botfile = os.path.join(self.tmp, "ani.json")
        self.path = os.path.join(self.tmp, "run_state.json")

    def tearDown(self):
        anibot.botfile = self._orig_botfile

    def _read(self):
        with open(self.path, "r") as f:
            return json.load(f)

    def test_writes_record_with_next_run_ts(self):
        anibot.write_run_state(
            "2026-06-13T19:18:00Z", "2026-06-13T19:20:00Z", 600,
            {"entries": 8, "checked": 5, "downloaded": 2},
        )
        state = self._read()
        self.assertEqual(state["schema"], 1)
        last = state["last_run"]
        self.assertEqual(last["started_ts"], "2026-06-13T19:18:00Z")
        self.assertEqual(last["finished_ts"], "2026-06-13T19:20:00Z")
        self.assertEqual(last["timedelay"], 600)
        # next_run_ts = finished_ts + timedelay (600s = 10 min).
        self.assertEqual(last["next_run_ts"], "2026-06-13T19:30:00Z")
        self.assertEqual(last["counts"]["checked"], 5)

    def test_history_appends_and_last_run_tracks_newest(self):
        anibot.write_run_state("2026-06-13T19:00:00Z", "2026-06-13T19:01:00Z", 600, {"checked": 1})
        anibot.write_run_state("2026-06-13T19:10:00Z", "2026-06-13T19:11:00Z", 600, {"checked": 2})
        state = self._read()
        self.assertEqual(len(state["runs"]), 2)
        self.assertEqual(state["runs"][0]["counts"]["checked"], 1)
        self.assertEqual(state["runs"][-1]["counts"]["checked"], 2)
        # last_run is always the newest record.
        self.assertEqual(state["last_run"]["counts"]["checked"], 2)

    def test_history_is_bounded(self):
        for i in range(anibot.RUN_STATE_HISTORY_MAX + 10):
            anibot.write_run_state(
                "2026-06-13T19:00:00Z", "2026-06-13T19:01:00Z", 600, {"checked": i})
        state = self._read()
        self.assertEqual(len(state["runs"]), anibot.RUN_STATE_HISTORY_MAX)
        # Oldest entries trimmed; newest preserved.
        self.assertEqual(state["runs"][-1]["counts"]["checked"],
                         anibot.RUN_STATE_HISTORY_MAX + 9)

    def test_counts_carry_errors_free_form(self):
        # The per-cycle errors tally rides along in the free-form counts dict
        # (no schema bump) so the dashboard can surface "N errors" per run.
        anibot.write_run_state(
            "2026-06-13T19:18:00Z", "2026-06-13T19:20:00Z", 600,
            {"entries": 8, "checked": 5, "downloaded": 2, "errors": 1},
        )
        self.assertEqual(self._read()["last_run"]["counts"]["errors"], 1)

    def test_no_next_run_ts_when_timedelay_invalid(self):
        anibot.write_run_state("2026-06-13T19:00:00Z", "2026-06-13T19:01:00Z", 0, {})
        self.assertEqual(self._read()["last_run"]["next_run_ts"], "")

    def test_corrupt_existing_file_is_replaced_not_fatal(self):
        with open(self.path, "w") as f:
            f.write("{not valid json")
        anibot.write_run_state("2026-06-13T19:00:00Z", "2026-06-13T19:01:00Z", 600, {"checked": 3})
        state = self._read()
        self.assertEqual(len(state["runs"]), 1)
        self.assertEqual(state["last_run"]["counts"]["checked"], 3)


class HandleFailedBatchTest(unittest.TestCase):
    """A failed downloadBatchCNL must distinguish the benign all-phantom case
    (the site DOM over-reported the episode count, so every wanted episode is
    beyond the real available max) from a genuine failure. The phantom case is
    logged at INFO and is NOT counted as an error; genuine failures still log
    [ERROR] and bump run_counts["errors"]. Either way the available_max cap is
    refreshed when the CNL response carried one."""

    TODAY = "2026-06-24"

    def setUp(self):
        self.logs = []

    def _log(self, message, _push):
        self.logs.append(message)

    def _call(self, batch_result, all_wanted, animeentry=None, run_counts=None):
        animeentry = {} if animeentry is None else animeentry
        run_counts = {"errors": 0} if run_counts is None else run_counts
        saved = anibot.handle_failed_batch(
            batch_result, all_wanted, animeentry, run_counts,
            self.TODAY, "Gantz", push=None, log_fn=self._log)
        return saved, animeentry, run_counts

    def test_all_phantom_is_benign_not_an_error(self):
        # Gantz: DOM reports 27, real max is 13; bot wanted eps 14-27 — all
        # phantom. Must NOT count as an error, must re-cap, must log UNAVAILABLE.
        batch_result = {
            "success": False,
            "reason": "Keine gewünschten Episoden im Batch gefunden",
            "episodes_sent": [],
            "episodes_not_found": list(range(14, 28)),
            "available_max": 13,
        }
        saved, entry, counts = self._call(batch_result, list(range(14, 28)))
        # (a) errors NOT incremented
        self.assertEqual(counts["errors"], 0)
        # (b) cap set to 13 with today's stamp; caller told to persist
        self.assertTrue(saved)
        self.assertEqual(entry["al_available_max"], 13)
        self.assertEqual(entry["al_available_max_set_at"], self.TODAY)
        # (c) the log is the unavailable/info message, not [ERROR]
        self.assertEqual(len(self.logs), 1)
        self.assertIn("[UNAVAILABLE]", self.logs[0])
        self.assertNotIn("[ERROR]", self.logs[0])

    def test_genuine_failure_without_available_max_still_errors(self):
        # No available_max (e.g. MyJD/JD error) → genuine failure: [ERROR] +
        # error tally, and nothing to persist.
        batch_result = {
            "success": False,
            "reason": "JDownloader nicht erreichbar",
            "episodes_sent": [],
            "episodes_not_found": [],
            "available_max": None,
        }
        saved, entry, counts = self._call(batch_result, [3, 4, 5])
        self.assertEqual(counts["errors"], 1)
        self.assertFalse(saved)
        self.assertNotIn("al_available_max", entry)
        self.assertEqual(len(self.logs), 1)
        self.assertIn("[ERROR]", self.logs[0])

    def test_partial_phantom_still_errors_but_recaps(self):
        # Some wanted eps are still in range (<= available_max): a genuine
        # failure (an in-range ep should have downloaded). Logs [ERROR] and
        # increments errors, but still refreshes the cap from the response.
        batch_result = {
            "success": False,
            "reason": "Keine gewünschten Episoden im Batch gefunden",
            "episodes_sent": [],
            "episodes_not_found": [8, 9],
            "available_max": 8,
        }
        saved, entry, counts = self._call(batch_result, [8, 9])
        self.assertEqual(counts["errors"], 1)
        self.assertTrue(saved)
        self.assertEqual(entry["al_available_max"], 8)
        self.assertEqual(entry["al_available_max_set_at"], self.TODAY)
        self.assertIn("[ERROR]", self.logs[0])


if __name__ == "__main__":
    unittest.main()
