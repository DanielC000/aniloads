"""Tests for the pure-logic helpers in bot/anibot.py."""

import builtins
import json
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

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

    def test_counts_carry_skipped_and_unavailable_free_form(self):
        # Same free-form growth as errors (see test_counts_carry_errors_free_form)
        # — skipped/unavailable ride along in counts with no schema bump.
        anibot.write_run_state(
            "2026-06-13T19:18:00Z", "2026-06-13T19:20:00Z", 600,
            {"entries": 8, "checked": 5, "downloaded": 2, "errors": 1,
             "skipped": 3, "unavailable": 1},
        )
        counts = self._read()["last_run"]["counts"]
        self.assertEqual(counts["skipped"], 3)
        self.assertEqual(counts["unavailable"], 1)

    def test_events_persisted_for_all_four_kinds(self):
        events = [
            {"kind": "download", "anime": "Gantz", "episodes": [1, 2]},
            {"kind": "error", "anime": "Bleach", "episodes": [5], "detail": "JDownloader unreachable"},
            {"kind": "unavailable", "anime": "Naruto", "episodes": [700], "detail": "No download links available"},
            {"kind": "complete", "anime": "One Piece"},
        ]
        anibot.write_run_state(
            "2026-06-13T19:18:00Z", "2026-06-13T19:20:00Z", 600,
            {"entries": 4, "checked": 4, "downloaded": 2, "errors": 1,
             "skipped": 0, "unavailable": 1},
            events,
        )
        last = self._read()["last_run"]
        self.assertEqual([e["kind"] for e in last["events"]],
                         ["download", "error", "unavailable", "complete"])
        self.assertNotIn("events_truncated", last)

    def test_events_capped_at_40_and_truncated_flag_set(self):
        events = [{"kind": "download", "anime": "Show %d" % i, "episodes": [1]}
                  for i in range(45)]
        anibot.write_run_state(
            "2026-06-13T19:18:00Z", "2026-06-13T19:20:00Z", 600, {}, events)
        last = self._read()["last_run"]
        self.assertEqual(len(last["events"]), anibot.EVENTS_CAP)
        self.assertTrue(last["events_truncated"])
        # First 40 kept in order, not an arbitrary/last-40 slice.
        self.assertEqual(last["events"][0]["anime"], "Show 0")
        self.assertEqual(last["events"][-1]["anime"], "Show 39")

    def test_no_events_still_writes_cleanly(self):
        # Routine cycle: nothing happened, events stays empty, no crash.
        anibot.write_run_state(
            "2026-06-13T19:18:00Z", "2026-06-13T19:20:00Z", 600,
            {"entries": 0, "checked": 0, "downloaded": 0, "errors": 0,
             "skipped": 0, "unavailable": 0},
        )
        last = self._read()["last_run"]
        self.assertEqual(last["events"], [])
        self.assertNotIn("events_truncated", last)

    def test_record_matches_fixed_schema_keys(self):
        # Locks the exact key names the sibling web card renders against.
        events = [{"kind": "download", "anime": "Gantz", "episodes": [1]}]
        anibot.write_run_state(
            "2026-06-13T19:18:00Z", "2026-06-13T19:20:00Z", 600,
            {"entries": 1, "checked": 1, "downloaded": 1, "errors": 0,
             "skipped": 0, "unavailable": 0},
            events,
        )
        last = self._read()["last_run"]
        self.assertEqual(set(last.keys()),
                         {"started_ts", "finished_ts", "timedelay",
                          "next_run_ts", "counts", "events"})
        self.assertEqual(set(last["counts"].keys()),
                         {"entries", "checked", "downloaded", "errors",
                          "skipped", "unavailable"})
        self.assertEqual(set(last["events"][0].keys()),
                         {"kind", "anime", "episodes"})


class ConfigUtf8Test(unittest.TestCase):
    """loadconfig/write_run_state must read/write UTF-8 regardless of the
    platform's default locale encoding (cp1252 on Windows). Fixtures are
    written as explicit UTF-8 *bytes* (not via the platform-default `open`)
    so a regression to a bare `open(path, "r")`/`open(path, "w")` would
    genuinely mangle or fail these, rather than passing on any host."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aniloads-configutf8-")
        self._orig_botfile = anibot.botfile
        self._orig_botfolder = anibot.botfolder
        anibot.botfile = os.path.join(self.tmp, "ani.json")
        anibot.botfolder = self.tmp
        self.run_state_path = os.path.join(self.tmp, "run_state.json")

    def tearDown(self):
        anibot.botfile = self._orig_botfile
        anibot.botfolder = self._orig_botfolder

    def test_loadconfig_reads_umlaut_browserlocation(self):
        location = "C:\\Übertragung\\firefox.exe"
        fixture = {
            "settings": {
                "jdhost": "127.0.0.1", "hoster": 1, "browserengine": "firefox",
                "browserlocation": location, "pushbullet_apikey": "", "timedelay": 600,
                "myjd_user": "u", "myjd_pw": "p", "myjd_device": "d",
                "jd_deprecated": False, "jd_deprecatedport": 0,
            }
        }
        with open(anibot.botfile, "wb") as f:
            f.write(json.dumps(fixture, ensure_ascii=False).encode("utf-8"))
        result = anibot.loadconfig()
        self.assertEqual(result[3], location)  # browserlocation

    def test_write_run_state_write_open_uses_utf8_encoding(self):
        # A content round-trip can't catch a missing encoding="utf-8" on the
        # *write* side here: write_run_state's json.dump call keeps the
        # default ensure_ascii=True, so non-ASCII output is always escaped to
        # plain-ASCII \uXXXX sequences regardless of which codec the file was
        # opened with — a round-trip test would pass identically whether or
        # not the fix is applied (confirmed: reverting just the write-side
        # `open(tmp, "w", ...)` back to platform-default left a pure-content
        # round-trip test green). So this asserts the open() call itself
        # instead, which does regress if the encoding kwarg is dropped.
        real_open = builtins.open
        write_calls = []

        def spy_open(file, mode="r", *args, **kwargs):
            if isinstance(file, str) and file.endswith(".tmp") and mode == "w":
                write_calls.append(kwargs.get("encoding"))
            return real_open(file, mode, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=spy_open):
            anibot.write_run_state(
                "2026-06-13T19:18:00Z", "2026-06-13T19:20:00Z", 600, {"checked": 1},
                [{"kind": "unavailable", "anime": "Naruto", "detail": "skip — already have"}],
            )
        self.assertEqual(write_calls, ["utf-8"])

    def test_write_run_state_preserves_existing_umlaut_history(self):
        # A run_state.json written by a previous cycle may itself contain
        # non-ASCII bytes; the read side must decode it as UTF-8 rather than
        # the platform default before appending the new record.
        fixture = {
            "schema": 1,
            "last_run": {"finished_ts": "2026-06-13T19:00:00Z",
                         "counts": {}, "events": [{"kind": "complete", "anime": "Ü-Anime"}]},
            "runs": [{"finished_ts": "2026-06-13T19:00:00Z", "counts": {},
                      "events": [{"kind": "complete", "anime": "Ü-Anime"}]}],
        }
        with open(self.run_state_path, "wb") as f:
            f.write(json.dumps(fixture, ensure_ascii=False).encode("utf-8"))
        anibot.write_run_state("2026-06-13T19:10:00Z", "2026-06-13T19:11:00Z", 600, {"checked": 2})
        with open(self.run_state_path, "rb") as f:
            raw = f.read()
        state = json.loads(raw.decode("utf-8"))
        self.assertEqual(state["runs"][0]["events"][0]["anime"], "Ü-Anime")
        self.assertEqual(len(state["runs"]), 2)


class RecordEventTest(unittest.TestCase):
    """`_record_event` is the shared append helper every event call site uses."""

    def test_episodes_omitted_when_absent(self):
        events = []
        anibot._record_event(events, "complete", "One Piece")
        self.assertEqual(events, [{"kind": "complete", "anime": "One Piece"}])

    def test_episodes_included_when_present(self):
        events = []
        anibot._record_event(events, "download", "Gantz", episodes=[1, 2, 3])
        self.assertEqual(events[0]["episodes"], [1, 2, 3])

    def test_detail_truncated_to_200_chars(self):
        events = []
        anibot._record_event(events, "error", "Show", detail="x" * 300)
        self.assertEqual(len(events[0]["detail"]), 200)


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

    def test_all_phantom_records_unavailable_event_and_count(self):
        batch_result = {
            "success": False,
            "reason": "Keine gewünschten Episoden im Batch gefunden",
            "episodes_sent": [],
            "episodes_not_found": list(range(14, 28)),
            "available_max": 13,
        }
        run_counts = {"errors": 0, "unavailable": 0}
        events = []
        anibot.handle_failed_batch(
            batch_result, list(range(14, 28)), {}, run_counts,
            self.TODAY, "Gantz", push=None, log_fn=self._log, events=events)
        self.assertEqual(run_counts["unavailable"], 1)
        self.assertEqual(run_counts["errors"], 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "unavailable")
        self.assertEqual(events[0]["anime"], "Gantz")
        self.assertEqual(events[0]["episodes"], list(range(14, 28)))
        # No URL/host/credential in detail — dashboard-screenshot safe.
        self.assertNotIn("://", events[0]["detail"])

    def test_genuine_failure_records_error_event(self):
        batch_result = {
            "success": False,
            "reason": "JDownloader nicht erreichbar",
            "episodes_sent": [],
            "episodes_not_found": [],
            "available_max": None,
        }
        run_counts = {"errors": 0, "unavailable": 0}
        events = []
        anibot.handle_failed_batch(
            batch_result, [3, 4, 5], {}, run_counts,
            self.TODAY, "Bleach", push=None, log_fn=self._log, events=events)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "error")
        self.assertEqual(events[0]["anime"], "Bleach")
        self.assertEqual(events[0]["episodes"], [3, 4, 5])
        self.assertEqual(events[0]["detail"], "JDownloader nicht erreichbar")

    def test_mismatch_reason_code_is_not_an_error(self):
        # The live Bleach shape from card 62b4595a: downloadBatchCNL flags
        # reason_code, and all_wanted (1-7) sit entirely below available_max
        # (46) — the phantom heuristic alone would (wrongly) call this a
        # genuine failure. The reason_code must take priority over it.
        batch_result = {
            "success": False,
            "reason": "wanted 1-7 but release numbers episodes 41-46 — set episode_offset to -40",
            "reason_code": "episode_numbering_mismatch",
            "episodes_sent": [],
            "episodes_not_found": list(range(1, 8)),
            "available_max": 46,
        }
        run_counts = {"errors": 0, "unavailable": 0, "mismatch": 0}
        saved, entry, counts = self._call(batch_result, list(range(1, 8)), run_counts=run_counts)
        self.assertEqual(counts["errors"], 0)
        self.assertEqual(counts["unavailable"], 0)
        self.assertEqual(counts["mismatch"], 1)
        self.assertTrue(saved)
        self.assertEqual(entry["al_available_max"], 46)
        self.assertEqual(entry["al_available_max_set_at"], self.TODAY)
        self.assertEqual(len(self.logs), 1)
        self.assertIn("[MISMATCH]", self.logs[0])
        self.assertNotIn("[ERROR]", self.logs[0])
        self.assertNotIn("[UNAVAILABLE]", self.logs[0])

    def test_mismatch_records_mismatch_event_not_error(self):
        batch_result = {
            "success": False,
            "reason": "wanted 1-7 but release numbers episodes 41-46 — set episode_offset to -40",
            "reason_code": "episode_numbering_mismatch",
            "episodes_sent": [],
            "episodes_not_found": list(range(1, 8)),
            "available_max": 46,
        }
        run_counts = {"errors": 0, "mismatch": 0}
        events = []
        anibot.handle_failed_batch(
            batch_result, list(range(1, 8)), {}, run_counts,
            self.TODAY, "Bleach", push=None, log_fn=self._log, events=events)
        self.assertEqual(run_counts["errors"], 0)
        self.assertEqual(run_counts["mismatch"], 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "mismatch")
        self.assertEqual(events[0]["anime"], "Bleach")
        self.assertEqual(events[0]["episodes"], list(range(1, 8)))
        self.assertIn("episode_offset", events[0]["detail"])

    def test_mismatch_without_run_counts_key_still_tallies(self):
        # run_counts dicts built before this fix have no "mismatch" key.
        batch_result = {
            "success": False, "reason": "x", "reason_code": "episode_numbering_mismatch",
            "episodes_sent": [], "episodes_not_found": [1], "available_max": 5,
        }
        saved, entry, counts = self._call(batch_result, [1], run_counts={"errors": 0})
        self.assertEqual(counts["mismatch"], 1)
        self.assertEqual(counts["errors"], 0)

    def test_events_param_is_optional(self):
        # Existing call sites (no events kwarg) must keep working unchanged.
        batch_result = {
            "success": False, "reason": "x", "episodes_sent": [],
            "episodes_not_found": [], "available_max": None,
        }
        saved, entry, counts = self._call(batch_result, [1])
        self.assertFalse(saved)
        self.assertEqual(counts["errors"], 1)


if __name__ == "__main__":
    unittest.main()
