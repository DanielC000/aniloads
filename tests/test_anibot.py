"""Tests for the pure-logic helpers in bot/anibot.py."""

import builtins
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

import support

anibot = support.load_anibot()
scrape = anibot.should_scrape_despite_skip
tvdb_skip_decision = anibot.tvdb_skip_decision

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


class FakePushbullet:
    """Stands in for pushbullet.Pushbullet: the real constructor validates
    the key over the network, which is exactly the failure mode under test."""

    def __init__(self, key):
        if key != "valid-key":
            raise Exception("Invalid access token")
        self.key = key

    def push_note(self, title, message):
        raise Exception("network error")


class InitPushbulletTest(unittest.TestCase):
    """bot/anibot.py ~L885: an invalid/revoked Pushbullet key, or a null key
    in ani.json, must not crash the bot into a container restart loop."""

    def setUp(self):
        self._orig_pushbullet = anibot.Pushbullet
        anibot.Pushbullet = FakePushbullet

    def tearDown(self):
        anibot.Pushbullet = self._orig_pushbullet

    def test_valid_key_constructs_client(self):
        pb = anibot.init_pushbullet("valid-key")
        self.assertIsInstance(pb, FakePushbullet)

    def test_invalid_key_disables_without_raising(self):
        pb = anibot.init_pushbullet("revoked-key")
        self.assertEqual(pb, "")

    def test_none_key_is_treated_as_unset(self):
        self.assertEqual(anibot.init_pushbullet(None), "")

    def test_false_key_is_treated_as_unset(self):
        self.assertEqual(anibot.init_pushbullet(False), "")

    def test_whitespace_key_is_treated_as_unset(self):
        self.assertEqual(anibot.init_pushbullet("   "), "")

    def test_empty_string_key_is_treated_as_unset(self):
        self.assertEqual(anibot.init_pushbullet(""), "")

    def test_pushbullet_import_missing_does_not_raise(self):
        # Simulates the `from pushbullet import Pushbullet` ImportError path,
        # where the module sets Pushbullet = None.
        anibot.Pushbullet = None
        self.assertEqual(anibot.init_pushbullet("valid-key"), "")


class LogPushbulletNeverRaisesTest(unittest.TestCase):
    """log() must never propagate a failed push (invalid key, network error,
    or pb == "" for a disabled client) — only best-effort deliver + log."""

    def test_unset_pushbullet_does_not_raise(self):
        anibot.log("hello", "")

    def test_pushbullet_push_failure_does_not_raise(self):
        anibot.log("hello", FakePushbullet("valid-key"))


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


class ComputeEntryDeltaTest(unittest.TestCase):
    """Pure tests for anibot.compute_entry_delta — the diff that drives
    startbot()'s per-entry field-level save_ani(), replacing a whole-document
    save with only what actually changed since the last save this cycle."""

    def _delta(self, before, after):
        return anibot.compute_entry_delta(before, after)

    def test_no_change_yields_empty_delta(self):
        entry = {"episodes": 3, "missing": [1, 2], "complete": False}
        fields, unset, list_deltas = self._delta(dict(entry), dict(entry))
        self.assertEqual(fields, {})
        self.assertEqual(unset, [])
        self.assertEqual(list_deltas, {})

    def test_scalar_change_detected(self):
        before = {"episodes": 3}
        after = {"episodes": 4}
        fields, unset, list_deltas = self._delta(before, after)
        self.assertEqual(fields, {"episodes": 4})
        self.assertEqual(unset, [])
        self.assertEqual(list_deltas, {})

    def test_scalar_decrease_is_still_a_plain_overwrite(self):
        # episodes is fully bot-owned (the dashboard never writes it), so a
        # rollback (e.g. an episode turned out unavailable) is just as valid
        # a change as an increase — no "only rises" special-casing needed.
        before = {"episodes": 5}
        after = {"episodes": 3}
        fields, _unset, _list_deltas = self._delta(before, after)
        self.assertEqual(fields, {"episodes": 3})

    def test_field_removed_goes_to_unset(self):
        before = {"al_available_max": 12, "al_available_max_set_at": "2026-09-17"}
        after = {}
        fields, unset, _list_deltas = self._delta(before, after)
        self.assertEqual(fields, {})
        self.assertEqual(sorted(unset), ["al_available_max", "al_available_max_set_at"])

    def test_field_newly_appearing_is_a_field_not_unset(self):
        before = {}
        after = {"tvdb_series_status": "Continuing"}
        fields, unset, _list_deltas = self._delta(before, after)
        self.assertEqual(fields, {"tvdb_series_status": "Continuing"})
        self.assertEqual(unset, [])

    def test_missing_add_and_remove_reported_as_delta(self):
        before = {"missing": [1, 2, 3]}
        after = {"missing": [1, 4]}  # 2, 3 removed (downloaded); 4 added (failed)
        _fields, _unset, list_deltas = self._delta(before, after)
        added, removed = list_deltas["missing"]
        self.assertEqual(added, {4})
        self.assertEqual(removed, {2, 3})

    def test_missing_unchanged_produces_no_delta(self):
        before = {"missing": [1, 2]}
        after = {"missing": [2, 1]}  # same set, different order
        _fields, _unset, list_deltas = self._delta(before, after)
        self.assertEqual(list_deltas, {})

    def test_user_owned_fields_never_appear_in_any_output(self):
        before = {"customPackage": "Old", "tvdb_id": 1, "episodes": 1}
        after = {"customPackage": "New", "tvdb_id": 2, "episodes": 2}
        fields, unset, list_deltas = self._delta(before, after)
        self.assertEqual(fields, {"episodes": 2})
        self.assertEqual(unset, [])
        self.assertEqual(list_deltas, {})
        self.assertNotIn("customPackage", fields)
        self.assertNotIn("tvdb_id", fields)


class TvdbSkipDecisionTest(unittest.TestCase):
    """Step 4 TVDB skip/complete checks (anibot.tvdb_skip_decision).

    `episodes` is always passed in watchlist/TVDB numbering — the same
    numbering the mover's `episode_offset` return-leg translation
    (c0b7ab5, `_match_batch_episodes`) already leaves in the watchlist's
    `episodes` field. tvdb_skip_decision never re-applies `episode_offset`
    itself; these "offset entry" cases assert that pre-translated episodes
    line up directly against a TVDB season's own numbering with no further
    translation needed."""

    TODAY = date(2026, 9, 17)
    NOW = datetime(2026, 9, 17, 12, 0, 0)

    def _decide(self, **kw):
        defaults = dict(
            series_status=None, tvdb_season=None, tvdb_ep_count=None, airdate=None,
            episodes=0, missing_count=0, today=self.TODAY, now=self.NOW,
            skip_recheck_at=None, early_scrape_days=1, pastdue_recheck_hours=2,
        )
        defaults.update(kw)
        return tvdb_skip_decision(**defaults)

    # -- offset entry: skips until airdate ---------------------------------
    # Bleach TYBW "The Calamity": site files 41-46, episode_offset -40,
    # watchlist wants 1-7 → animeentry['episodes'] holds 6 (watchlist/TVDB
    # numbering) once episodes 1-6 are downloaded; tvdb_season is the season
    # whose own numbering is 1-7 for that cour.
    def test_offset_entry_skips_until_future_airdate(self):
        d = self._decide(series_status="Continuing", tvdb_season=4, episodes=6,
                          airdate="2026-09-24", missing_count=0)
        self.assertEqual(d["action"], "skip")
        self.assertTrue(d["terminal"])
        self.assertEqual(d["updates"], {"skip_until": "2026-09-24", "skip_real_airdate": True})

    def test_offset_entry_retries_when_missing_despite_future_airdate(self):
        d = self._decide(series_status="Continuing", tvdb_season=4, episodes=6,
                          airdate="2026-09-24", missing_count=2)
        self.assertEqual(d["action"], "retry")
        self.assertFalse(d["terminal"])
        self.assertIn("2 missing episodes", d["log"][1])

    def test_offset_entry_scrapes_early_within_window(self):
        # today is the eve of the airdate, within early_scrape_days=1.
        d = self._decide(series_status="Continuing", tvdb_season=4, episodes=6,
                          airdate="2026-09-18", missing_count=0)
        self.assertEqual(d["action"], "early")
        self.assertFalse(d["terminal"])

    # -- offset entry: auto-completes at the right count --------------------
    def test_offset_entry_completes_at_full_season_count(self):
        # All 7 watchlist-numbered episodes (site 41-47) downloaded, matching
        # the TVDB season's own 7-episode count directly — no offset applied.
        d = self._decide(series_status="Ended", tvdb_season=4, tvdb_ep_count=7,
                          episodes=7, missing_count=0)
        self.assertEqual(d["action"], "complete")
        self.assertTrue(d["terminal"])
        self.assertEqual(d["updates"], {"complete": True})

    def test_offset_entry_not_yet_complete_below_season_count(self):
        d = self._decide(series_status="Ended", tvdb_season=4, tvdb_ep_count=7,
                          episodes=6, missing_count=0)
        self.assertEqual(d["action"], "none")
        self.assertFalse(d["terminal"])
        self.assertEqual(d["updates"], {})

    def test_ended_with_missing_episodes_not_complete(self):
        d = self._decide(series_status="Ended", tvdb_season=4, tvdb_ep_count=7,
                          episodes=7, missing_count=1)
        self.assertEqual(d["action"], "none")
        self.assertFalse(d["terminal"])

    # -- past-due throttle ---------------------------------------------------
    def test_pastdue_first_check_sets_recheck_and_falls_through_to_scrape(self):
        # Airdate already passed, no recheck timestamp yet → allow this
        # cycle's scrape and arm the throttle for the next one.
        d = self._decide(series_status="Continuing", tvdb_season=4, episodes=6,
                          airdate="2026-09-10", skip_recheck_at=None, missing_count=0,
                          pastdue_recheck_hours=2)
        self.assertEqual(d["action"], "none")
        self.assertFalse(d["terminal"])
        self.assertEqual(d["updates"], {"skip_recheck_at": "2026-09-17T14:00:00"})

    def test_pastdue_within_throttle_window_skips(self):
        d = self._decide(series_status="Continuing", tvdb_season=4, episodes=6,
                          airdate="2026-09-10", skip_recheck_at="2026-09-17T14:00:00",
                          missing_count=0)
        self.assertEqual(d["action"], "skip")
        self.assertTrue(d["terminal"])
        self.assertEqual(d["updates"], {})
        self.assertIn("re-checking after 2026-09-17T14:00:00", d["log"][1])

    def test_pastdue_within_throttle_window_but_missing_retries(self):
        d = self._decide(series_status="Continuing", tvdb_season=4, episodes=6,
                          airdate="2026-09-10", skip_recheck_at="2026-09-17T14:00:00",
                          missing_count=3)
        self.assertEqual(d["action"], "retry")
        self.assertFalse(d["terminal"])

    def test_pastdue_after_throttle_window_rechecks_again(self):
        # The previously armed recheck time has elapsed → allow another
        # scrape and re-arm the throttle for another window.
        d = self._decide(series_status="Continuing", tvdb_season=4, episodes=6,
                          airdate="2026-09-10", skip_recheck_at="2026-09-17T10:00:00",
                          missing_count=0, pastdue_recheck_hours=2)
        self.assertEqual(d["action"], "none")
        self.assertFalse(d["terminal"])
        self.assertEqual(d["updates"], {"skip_recheck_at": "2026-09-17T14:00:00"})

    def test_pastdue_ignores_unparseable_recheck_timestamp(self):
        d = self._decide(series_status="Continuing", tvdb_season=4, episodes=6,
                          airdate="2026-09-10", skip_recheck_at="not-a-timestamp",
                          missing_count=0)
        self.assertEqual(d["action"], "none")
        self.assertEqual(d["updates"], {"skip_recheck_at": "2026-09-17T14:00:00"})

    # -- no-offset behavior unchanged -----------------------------------------
    def test_no_offset_entry_skips_until_airdate(self):
        d = self._decide(series_status="Continuing", tvdb_season=1, episodes=2,
                          airdate="2026-09-30", missing_count=0)
        self.assertEqual(d["action"], "skip")
        self.assertTrue(d["terminal"])

    def test_no_offset_entry_completes(self):
        d = self._decide(series_status="Ended", tvdb_season=1, tvdb_ep_count=12,
                          episodes=12, missing_count=0)
        self.assertEqual(d["action"], "complete")

    def test_no_airdate_known_uses_synthetic_daily_throttle(self):
        d = self._decide(series_status="Continuing", tvdb_season=1, episodes=2,
                          airdate=None, missing_count=0)
        self.assertEqual(d["action"], "skip")
        self.assertTrue(d["terminal"])
        self.assertEqual(d["updates"], {"skip_until": "2026-09-18", "skip_real_airdate": False})

    def test_no_tvdb_season_no_op(self):
        d = self._decide(series_status="Continuing", tvdb_season=None, episodes=2)
        self.assertEqual(d["action"], "none")
        self.assertFalse(d["terminal"])

    def test_unknown_series_status_no_op(self):
        d = self._decide(series_status="Upcoming", tvdb_season=1, episodes=2)
        self.assertEqual(d["action"], "none")
        self.assertFalse(d["terminal"])

    def test_unparseable_airdate_is_ignored(self):
        d = self._decide(series_status="Continuing", tvdb_season=1, episodes=2,
                          airdate="not-a-date")
        self.assertEqual(d["action"], "none")
        self.assertEqual(d["updates"], {})


class LoadAniCycleStartTest(unittest.TestCase):
    """The per-cycle load at the top of startbot()'s while(True) loop, guarded
    against a torn/corrupt ani.json (the dashboard writing concurrently) so it
    can never crash the loop and restart-loop the container."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aniloads-cyclestart-")
        self._orig_botfile = anibot.botfile
        anibot.botfile = os.path.join(self.tmp, "ani.json")

    def tearDown(self):
        anibot.botfile = self._orig_botfile

    def test_missing_file_returns_default_and_no_error(self):
        data, err = anibot.load_ani_cycle_start(anibot.botfile)
        self.assertIsNone(err)
        self.assertEqual(data, {"settings": {}, "anime": []})

    def test_valid_file_returns_data_and_no_error(self):
        fixture = {"settings": {}, "anime": [{"name": "A", "url": "http://x/a"}]}
        with open(anibot.botfile, "w", encoding="utf-8") as f:
            json.dump(fixture, f)
        data, err = anibot.load_ani_cycle_start(anibot.botfile)
        self.assertIsNone(err)
        self.assertEqual(data, fixture)

    def test_corrupt_file_returns_error_instead_of_raising(self):
        with open(anibot.botfile, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        # No exception escapes — the caller (the cycle loop) gets a
        # (None, error) pair back to log and skip to the next cycle on.
        data, err = anibot.load_ani_cycle_start(anibot.botfile)
        self.assertIsNone(data)
        self.assertIsInstance(err, anibot.anistore.CorruptStoreError)


if __name__ == "__main__":
    unittest.main()
