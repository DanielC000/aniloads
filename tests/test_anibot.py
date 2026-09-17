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


class RecordEntryOutcomeTest(unittest.TestCase):
    """_record_entry_outcome builds the per-entry outcome record that
    write_run_state persists under run_state.json's "entries" key."""

    def test_records_result_and_reason(self):
        outcomes = {}
        anibot._record_entry_outcome(outcomes, "http://x/a", "skipped", "complete")
        self.assertEqual(outcomes["http://x/a"]["result"], "skipped")
        self.assertEqual(outcomes["http://x/a"]["reason"], "complete")
        self.assertIn("checked_ts", outcomes["http://x/a"])
        self.assertNotIn("episode", outcomes["http://x/a"])

    def test_episode_included_when_given(self):
        outcomes = {}
        anibot._record_entry_outcome(outcomes, "http://x/a", "downloaded", "episode downloaded", episode=7)
        self.assertEqual(outcomes["http://x/a"]["episode"], 7)

    def test_reason_truncated_to_200_chars(self):
        outcomes = {}
        anibot._record_entry_outcome(outcomes, "http://x/a", "error", "x" * 300)
        self.assertEqual(len(outcomes["http://x/a"]["reason"]), 200)

    def test_later_call_overwrites_earlier_one_same_cycle(self):
        # Last-write-wins within a cycle — e.g. the bottom-of-loop completion
        # detection overwriting an earlier "downloaded" outcome for the same URL.
        outcomes = {}
        anibot._record_entry_outcome(outcomes, "http://x/a", "downloaded", "episode downloaded", episode=3)
        anibot._record_entry_outcome(outcomes, "http://x/a", "skipped", "complete")
        self.assertEqual(outcomes["http://x/a"]["result"], "skipped")
        self.assertNotIn("episode", outcomes["http://x/a"])


class RecordCompleteOutcomeTest(unittest.TestCase):
    """_record_complete_outcome (the bottom-of-loop auto-complete check)
    must not erase a "downloaded" outcome already recorded this cycle — a
    series/movie completing on the exact cycle its last episode downloads
    should still show "downloaded", not silently become "skipped"."""

    def test_falls_back_to_skipped_complete_when_nothing_recorded_yet(self):
        outcomes = {}
        anibot._record_complete_outcome(outcomes, "http://x/a")
        self.assertEqual(outcomes["http://x/a"]["result"], "skipped")
        self.assertEqual(outcomes["http://x/a"]["reason"], "complete")

    def test_falls_back_when_prior_outcome_this_cycle_was_not_downloaded(self):
        outcomes = {}
        anibot._record_entry_outcome(outcomes, "http://x/a", "skipped", "no new episode")
        anibot._record_complete_outcome(outcomes, "http://x/a")
        self.assertEqual(outcomes["http://x/a"]["result"], "skipped")
        self.assertEqual(outcomes["http://x/a"]["reason"], "complete")

    def test_preserves_downloaded_result_appending_series_complete(self):
        outcomes = {}
        anibot._record_entry_outcome(outcomes, "http://x/a", "downloaded", "episode downloaded", episode=12)
        anibot._record_complete_outcome(outcomes, "http://x/a")
        self.assertEqual(outcomes["http://x/a"]["result"], "downloaded")
        self.assertEqual(outcomes["http://x/a"]["reason"], "episode downloaded; series complete")
        self.assertEqual(outcomes["http://x/a"]["episode"], 12)

    def test_preserves_downloaded_result_appending_movie_complete(self):
        outcomes = {}
        anibot._record_entry_outcome(outcomes, "http://x/a", "downloaded", "episode downloaded", episode=1)
        anibot._record_complete_outcome(outcomes, "http://x/a", movie=True)
        self.assertEqual(outcomes["http://x/a"]["result"], "downloaded")
        self.assertEqual(outcomes["http://x/a"]["reason"], "episode downloaded; movie complete")

    def test_preserves_batch_downloaded_reason(self):
        outcomes = {}
        anibot._record_entry_outcome(outcomes, "http://x/a", "downloaded", "3 episode(s) batch-downloaded")
        anibot._record_complete_outcome(outcomes, "http://x/a")
        self.assertEqual(outcomes["http://x/a"]["result"], "downloaded")
        self.assertEqual(outcomes["http://x/a"]["reason"], "3 episode(s) batch-downloaded; series complete")
        self.assertNotIn("episode", outcomes["http://x/a"])


class MergeEntryOutcomesTest(unittest.TestCase):
    """_merge_entry_outcomes folds one cycle's outcomes onto the previously
    persisted per-entry map: pruned to the current watchlist, carrying
    forward unvisited entries and a survives-a-later-skip last_error."""

    def test_prunes_entries_no_longer_on_watchlist(self):
        prev = {"http://x/gone": {"checked_ts": "t", "result": "skipped", "reason": "complete"}}
        merged = anibot._merge_entry_outcomes(prev, {}, ["http://x/a"])
        self.assertNotIn("http://x/gone", merged)

    def test_unvisited_entry_keeps_previous_record(self):
        prev = {"http://x/a": {"checked_ts": "t1", "result": "skipped", "reason": "complete"}}
        merged = anibot._merge_entry_outcomes(prev, {}, ["http://x/a"])
        self.assertEqual(merged["http://x/a"], prev["http://x/a"])

    def test_new_outcome_replaces_previous_record(self):
        prev = {"http://x/a": {"checked_ts": "t1", "result": "skipped", "reason": "complete"}}
        new = {"http://x/a": {"checked_ts": "t2", "result": "downloaded", "reason": "episode downloaded", "episode": 5}}
        merged = anibot._merge_entry_outcomes(prev, new, ["http://x/a"])
        self.assertEqual(merged["http://x/a"]["result"], "downloaded")
        self.assertEqual(merged["http://x/a"]["episode"], 5)

    def test_last_error_survives_a_later_non_error_outcome(self):
        prev = {"http://x/a": {"checked_ts": "t1", "result": "error", "reason": "JDownloader unreachable",
                                "last_error": {"reason": "JDownloader unreachable", "checked_ts": "t1"}}}
        new = {"http://x/a": {"checked_ts": "t2", "result": "skipped", "reason": "no new episode"}}
        merged = anibot._merge_entry_outcomes(prev, new, ["http://x/a"])
        self.assertEqual(merged["http://x/a"]["result"], "skipped")
        self.assertEqual(merged["http://x/a"]["last_error"]["reason"], "JDownloader unreachable")
        self.assertEqual(merged["http://x/a"]["last_error"]["checked_ts"], "t1")

    def test_new_error_becomes_the_last_error(self):
        prev = {}
        new = {"http://x/a": {"checked_ts": "t2", "result": "error", "reason": "JDownloader unreachable"}}
        merged = anibot._merge_entry_outcomes(prev, new, ["http://x/a"])
        self.assertEqual(merged["http://x/a"]["last_error"],
                         {"reason": "JDownloader unreachable", "checked_ts": "t2"})

    def test_no_prior_last_error_and_no_new_error_omits_the_key(self):
        merged = anibot._merge_entry_outcomes(
            {}, {"http://x/a": {"checked_ts": "t2", "result": "skipped", "reason": "complete"}},
            ["http://x/a"])
        self.assertNotIn("last_error", merged["http://x/a"])


class WriteRunStateEntryOutcomesTest(unittest.TestCase):
    """write_run_state's additive "entries" key (see ENTRY_RESULTS /
    _merge_entry_outcomes) — the per-entry check outcome the (later) web
    card renders. Old run_state.json files (written before this key
    existed) must still load fine with the key simply absent."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aniloads-runstate-entries-")
        self._orig_botfile = anibot.botfile
        anibot.botfile = os.path.join(self.tmp, "ani.json")
        self.path = os.path.join(self.tmp, "run_state.json")

    def tearDown(self):
        anibot.botfile = self._orig_botfile

    def _read(self):
        with open(self.path, "r") as f:
            return json.load(f)

    def test_entries_persisted_when_watchlist_urls_given(self):
        outcomes = {"http://x/a": {"checked_ts": "t", "result": "downloaded", "reason": "episode downloaded"}}
        anibot.write_run_state(
            "2026-06-13T19:00:00Z", "2026-06-13T19:01:00Z", 600, {"checked": 1},
            entry_outcomes=outcomes, watchlist_urls=["http://x/a"])
        self.assertEqual(self._read()["entries"]["http://x/a"]["result"], "downloaded")

    def test_entries_key_absent_when_watchlist_urls_omitted(self):
        anibot.write_run_state("2026-06-13T19:00:00Z", "2026-06-13T19:01:00Z", 600, {"checked": 1})
        self.assertNotIn("entries", self._read())

    def test_prior_entries_carried_forward_when_watchlist_urls_omitted(self):
        # A corrupt-ani.json cycle can't know the real watchlist — it must not
        # blank out (or otherwise touch) a previously-persisted entries map.
        anibot.write_run_state(
            "2026-06-13T19:00:00Z", "2026-06-13T19:01:00Z", 600, {"checked": 1},
            entry_outcomes={"http://x/a": {"checked_ts": "t", "result": "downloaded", "reason": "r"}},
            watchlist_urls=["http://x/a"])
        anibot.write_run_state("2026-06-13T19:10:00Z", "2026-06-13T19:11:00Z", 600, {"checked": 0})
        self.assertEqual(self._read()["entries"]["http://x/a"]["result"], "downloaded")

    def test_entries_pruned_to_current_watchlist_on_next_write(self):
        anibot.write_run_state(
            "2026-06-13T19:00:00Z", "2026-06-13T19:01:00Z", 600, {"checked": 1},
            entry_outcomes={"http://x/a": {"checked_ts": "t", "result": "downloaded", "reason": "r"},
                             "http://x/gone": {"checked_ts": "t", "result": "skipped", "reason": "complete"}},
            watchlist_urls=["http://x/a", "http://x/gone"])
        anibot.write_run_state(
            "2026-06-13T19:10:00Z", "2026-06-13T19:11:00Z", 600, {"checked": 1},
            entry_outcomes={}, watchlist_urls=["http://x/a"])
        entries = self._read()["entries"]
        self.assertIn("http://x/a", entries)
        self.assertNotIn("http://x/gone", entries)

    def test_old_run_state_without_entries_key_still_loads(self):
        # Simulates a run_state.json written before this feature existed.
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "last_run": {}, "runs": []}, f)
        anibot.write_run_state("2026-06-13T19:00:00Z", "2026-06-13T19:01:00Z", 600, {"checked": 1})
        state = self._read()
        self.assertNotIn("entries", state)
        self.assertEqual(state["last_run"]["counts"]["checked"], 1)


class WriteLoginStateTest(unittest.TestCase):
    """The bot records the anime-loads.org login outcome as an additive
    top-level `login` key in run_state.json, independent of per-cycle
    last_run/runs records — login happens once at startup, not per cycle."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aniloads-loginstate-")
        self._orig_botfile = anibot.botfile
        anibot.botfile = os.path.join(self.tmp, "ani.json")
        self.path = os.path.join(self.tmp, "run_state.json")

    def tearDown(self):
        anibot.botfile = self._orig_botfile

    def _read(self):
        with open(self.path, "r") as f:
            return json.load(f)

    def test_successful_login_recorded(self):
        anibot.write_login_state(True, True, vip=True)
        login = self._read()["login"]
        self.assertEqual(login["user_configured"], True)
        self.assertEqual(login["ok"], True)
        self.assertEqual(login["vip"], True)
        self.assertIn("checked_ts", login)
        self.assertNotIn("error", login)

    def test_failed_login_records_error_no_credentials(self):
        anibot.write_login_state(True, False, error="Login data is invalid")
        login = self._read()["login"]
        self.assertEqual(login["user_configured"], True)
        self.assertEqual(login["ok"], False)
        self.assertEqual(login["error"], "Login data is invalid")
        # Never a username/password field, only the generic error string.
        self.assertNotIn("user", login)
        self.assertNotIn("password", login)

    def test_anonymous_run_recorded_as_not_configured(self):
        anibot.write_login_state(False, False)
        login = self._read()["login"]
        self.assertEqual(login["user_configured"], False)
        self.assertEqual(login["ok"], False)

    def test_additive_alongside_existing_run_history(self):
        anibot.write_run_state("2026-06-13T19:00:00Z", "2026-06-13T19:01:00Z", 600, {"checked": 1})
        anibot.write_login_state(True, True)
        state = self._read()
        self.assertIn("last_run", state)
        self.assertIn("login", state)
        self.assertEqual(state["last_run"]["counts"]["checked"], 1)

    def test_login_state_preserved_across_later_run_state_writes(self):
        anibot.write_login_state(True, True)
        anibot.write_run_state("2026-06-13T19:00:00Z", "2026-06-13T19:01:00Z", 600, {"checked": 1})
        state = self._read()
        self.assertEqual(state["login"]["ok"], True)

    def test_later_login_attempt_overwrites_earlier_one(self):
        anibot.write_login_state(True, False, error="Login data is invalid")
        anibot.write_login_state(True, True)
        login = self._read()["login"]
        self.assertEqual(login["ok"], True)
        self.assertNotIn("error", login)

    def test_error_message_truncated_at_200_chars(self):
        anibot.write_login_state(True, False, error="x" * 500)
        self.assertEqual(len(self._read()["login"]["error"]), 200)

    def test_corrupt_existing_file_is_replaced_not_fatal(self):
        with open(self.path, "w") as f:
            f.write("{not valid json")
        anibot.write_login_state(True, True)
        self.assertEqual(self._read()["login"]["ok"], True)

    def test_never_raises_on_unwritable_path(self):
        anibot.botfile = os.path.join(self.tmp, "nonexistent", "deeply", "ani.json")
        # os.makedirs on a nested missing dir succeeds, so force a genuine
        # write failure instead: point at a directory that already exists as
        # a file, which os.replace/open cannot write through.
        blocker = os.path.join(self.tmp, "blocked")
        with open(blocker, "w") as f:
            f.write("x")
        anibot.botfile = os.path.join(blocker, "ani.json")
        anibot.write_login_state(True, True)  # must not raise


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


class LoadConfigTest(unittest.TestCase):
    """loadconfig() must seed a missing ani.json with full defaults, tolerate
    a hand-edited file missing OPTIONAL keys, and only hard-fail when neither
    download backend (jdhost / myjd_user) is configured."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aniloads-loadconfig-")
        self._orig_botfile = anibot.botfile
        self._orig_botfolder = anibot.botfolder
        anibot.botfile = os.path.join(self.tmp, "ani.json")
        anibot.botfolder = self.tmp

    def tearDown(self):
        anibot.botfile = self._orig_botfile
        anibot.botfolder = self._orig_botfolder

    def _write_settings(self, settings):
        with open(anibot.botfile, "w", encoding="utf-8") as f:
            json.dump({"settings": settings, "anime": []}, f)

    def test_seeds_full_defaults_when_file_missing(self):
        self.assertFalse(os.path.exists(anibot.botfile))

        anibot.loadconfig()

        self.assertTrue(os.path.exists(anibot.botfile))
        with open(anibot.botfile, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["settings"], anibot.config_defaults.DEFAULT_SETTINGS)
        self.assertEqual(on_disk["anime"], [])

    def test_seeding_never_overwrites_an_existing_file(self):
        self._write_settings({
            "jdhost": "127.0.0.1", "hoster": 0, "browserengine": 0,
            "browserlocation": "", "pushbullet_apikey": "", "timedelay": 600,
            "myjd_user": "", "myjd_pw": "", "myjd_device": "",
            "jd_deprecated": False, "jd_deprecatedport": "",
        })
        with open(anibot.botfile, "r", encoding="utf-8") as f:
            before = f.read()

        anibot.loadconfig()

        with open(anibot.botfile, "r", encoding="utf-8") as f:
            after = f.read()
        self.assertEqual(before, after)

    def test_missing_optional_keys_fall_back_to_defaults_and_log_info(self):
        # jdhost present (a chosen backend) but every optional key omitted.
        self._write_settings({"jdhost": "127.0.0.1"})

        with self.assertLogs(anibot._log, level="INFO") as cm:
            result = anibot.loadconfig()

        jdhost, hoster, browser, browserlocation, pushkey, timedelay = result[:6]
        myjd_user, myjd_pass, myjd_device, jd_deprecated, jd_deprecatedport = result[6:11]
        self.assertEqual(jdhost, "127.0.0.1")
        self.assertEqual(hoster, anibot.config_defaults.DEFAULT_SETTINGS["hoster"])
        self.assertEqual(timedelay, anibot.config_defaults.DEFAULT_SETTINGS["timedelay"])
        self.assertEqual(jd_deprecatedport, anibot.config_defaults.DEFAULT_SETTINGS["jd_deprecatedport"])
        self.assertTrue(any("hoster" in msg for msg in cm.output))

    def test_missing_download_backend_is_a_clear_fatal_error(self):
        # Neither jdhost nor myjd_user set -- the one thing loadconfig()
        # cannot default away.
        self._write_settings({"hoster": 1, "timedelay": 600})

        with self.assertLogs(anibot._log, level="ERROR") as cm:
            result = anibot.loadconfig()

        self.assertEqual(result, (False,) * 13)
        self.assertTrue(any("jdhost" in msg and "myjd_user" in msg for msg in cm.output))

    def test_myjd_user_alone_satisfies_the_backend_requirement(self):
        # MyJDownloader mode: myjd_user set, jdhost empty, password left
        # blank (entered interactively at startup) -- must NOT be treated as
        # "no backend configured".
        self._write_settings({"jdhost": "", "myjd_user": "myuser", "myjd_pw": ""})

        result = anibot.loadconfig()

        self.assertEqual(result[6], "myuser")  # myjd_user
        self.assertNotEqual(result[0], False)  # jdhost is "" (valid), not the False sentinel

    def test_no_settings_block_returns_false_tuple(self):
        with open(anibot.botfile, "w", encoding="utf-8") as f:
            json.dump({"anime": []}, f)

        result = anibot.loadconfig()

        self.assertEqual(result, (False,) * 13)


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


class SleepUntilNextCycleTest(unittest.TestCase):
    """The soft run-now trigger: the bot's inter-cycle sleep is sliced so a
    run-now request wakes it early instead of restarting the container."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aniloads-runnow-")
        self.trigger_path = os.path.join(self.tmp, "run_now")

    def tearDown(self):
        try:
            os.remove(self.trigger_path)
        except OSError:
            pass

    def _fake_clock(self):
        now = [0.0]
        calls = []

        def sleep_fn(s):
            calls.append(s)
            now[0] += s

        def time_fn():
            return now[0]

        return sleep_fn, time_fn, calls

    def test_sleeps_full_duration_when_no_trigger_appears(self):
        sleep_fn, time_fn, calls = self._fake_clock()
        woke_early = anibot.sleep_until_next_cycle(
            12, self.trigger_path, slice_seconds=5, sleep_fn=sleep_fn, time_fn=time_fn)
        self.assertFalse(woke_early)
        self.assertAlmostEqual(sum(calls), 12)
        # Sliced into <= slice_seconds chunks, not one long sleep.
        self.assertTrue(all(c <= 5 for c in calls))

    def test_wakes_early_when_trigger_appears_mid_sleep(self):
        sleep_fn, time_fn, calls = self._fake_clock()

        def sleep_and_maybe_trigger(s):
            sleep_fn(s)
            if len(calls) == 2:
                with open(self.trigger_path, "w", encoding="utf-8") as f:
                    f.write("2026-06-13T19:00:00Z")

        woke_early = anibot.sleep_until_next_cycle(
            600, self.trigger_path, slice_seconds=5,
            sleep_fn=sleep_and_maybe_trigger, time_fn=time_fn)
        self.assertTrue(woke_early)
        # Woke after the 2 slices that preceded the trigger appearing, not
        # the full 600s — a mid-sleep trigger is noticed within slice_seconds.
        self.assertEqual(len(calls), 2)

    def test_trigger_already_present_never_sleeps(self):
        # Simulates a request that arrived *during the previous cycle* (not
        # during this sleep) — by the time this sleep call begins, the file
        # is already sitting there, so it must return instantly without
        # ever calling sleep_fn. This is how a mid-cycle trigger is honored
        # right after the cycle ends, never by interrupting it.
        with open(self.trigger_path, "w", encoding="utf-8") as f:
            f.write("2026-06-13T19:00:00Z")
        calls = []
        woke_early = anibot.sleep_until_next_cycle(
            600, self.trigger_path, slice_seconds=5,
            sleep_fn=lambda s: calls.append(s), time_fn=lambda: 0.0)
        self.assertTrue(woke_early)
        self.assertEqual(calls, [])

    def test_zero_or_invalid_delay_checks_trigger_once(self):
        self.assertFalse(anibot.sleep_until_next_cycle(0, self.trigger_path))
        self.assertFalse(anibot.sleep_until_next_cycle(None, self.trigger_path))
        with open(self.trigger_path, "w", encoding="utf-8") as f:
            f.write("x")
        self.assertTrue(anibot.sleep_until_next_cycle(0, self.trigger_path))


class ConsumeRunNowTriggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aniloads-consume-")
        self.path = os.path.join(self.tmp, "run_now")

    def test_absent_file_returns_none(self):
        self.assertIsNone(anibot.consume_run_now_trigger(self.path))

    def test_present_file_is_deleted_and_content_returned(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("2026-06-13T19:00:00Z")
        content = anibot.consume_run_now_trigger(self.path)
        self.assertEqual(content, "2026-06-13T19:00:00Z")
        self.assertFalse(os.path.exists(self.path))

    def test_empty_file_still_signals_a_trigger(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("")
        content = anibot.consume_run_now_trigger(self.path)
        self.assertEqual(content, "manual")
        self.assertFalse(os.path.exists(self.path))

    def test_second_call_after_consumption_returns_none(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("2026-06-13T19:00:00Z")
        anibot.consume_run_now_trigger(self.path)
        self.assertIsNone(anibot.consume_run_now_trigger(self.path))


class ResolveForceCheckTest(unittest.TestCase):
    """Per-entry force_check (dashboard 'Check now'): a one-shot flag the bot
    peeks fresh and clears explicitly, independent of the bot-owned
    field-level merge (see BOT_OWNED_SCALAR_FIELDS)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aniloads-forcecheck-")
        self.path = os.path.join(self.tmp, "ani.json")

    def _write(self, anime):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"settings": {}, "anime": anime}, f)

    def _read(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_pending_request_is_reported_and_cleared(self):
        self._write([{"name": "A", "url": "http://x/a", "force_check": True}])
        self.assertTrue(anibot.resolve_force_check(self.path, "http://x/a"))
        self.assertNotIn("force_check", self._read()["anime"][0])

    def test_no_request_returns_false_and_leaves_entry_untouched(self):
        entry = {"name": "A", "url": "http://x/a", "skip_until": "2099-01-01"}
        self._write([entry])
        self.assertFalse(anibot.resolve_force_check(self.path, "http://x/a"))
        self.assertEqual(self._read()["anime"][0], entry)

    def test_missing_url_returns_false(self):
        self._write([{"name": "A", "url": "http://x/a"}])
        self.assertFalse(anibot.resolve_force_check(self.path, "http://x/nope"))

    def test_entry_removed_mid_cycle_does_not_resurrect_it(self):
        # The bot's per-cycle snapshot still has this entry (with
        # force_check=True from before the cycle started), but the
        # dashboard has since removed it from the on-disk file — the fresh
        # peek must see it as gone, not resurrect it via the unset merge.
        self._write([{"name": "Other", "url": "http://x/other"}])
        self.assertFalse(anibot.resolve_force_check(self.path, "http://x/a"))
        anime = self._read()["anime"]
        self.assertEqual(len(anime), 1)
        self.assertEqual(anime[0]["url"], "http://x/other")

    def test_corrupt_file_returns_false_instead_of_raising(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        self.assertFalse(anibot.resolve_force_check(self.path, "http://x/a"))


class PreScrapeSkipDecisionTest(unittest.TestCase):
    """Steps 1-3 of the smart-skip logic, and the force_check bypass that
    overrides all of them for one scrape."""

    TODAY = date(2026, 6, 13)

    def _decide(self, force_check=False, **entry_fields):
        entry = dict(entry_fields)
        missing_count = len(entry.pop("missing", []) or [])
        episodes = entry.pop("episodes", 5)
        return anibot.pre_scrape_skip_decision(
            entry, "Naruto", missing_count, episodes, force_check, self.TODAY)

    def test_complete_with_no_missing_skips(self):
        d = self._decide(complete=True)
        self.assertTrue(d["skip"])
        self.assertFalse(d["mark_complete"])
        self.assertEqual(d["log"], ("info", "SKIP", "Naruto is complete"))

    def test_complete_with_missing_does_not_skip(self):
        d = self._decide(complete=True, missing=[3])
        self.assertFalse(d["skip"])
        self.assertIsNone(d["log"])

    def test_al_status_complete_marks_and_skips(self):
        d = self._decide(al_status="Abgeschlossen", al_max_episodes=12, episodes=12)
        self.assertTrue(d["skip"])
        self.assertTrue(d["mark_complete"])
        self.assertEqual(d["log"][1], "COMPLETE")

    def test_skip_until_future_skips_when_no_missing(self):
        d = self._decide(skip_until="2026-06-20")
        self.assertTrue(d["skip"])
        self.assertEqual(d["log"], ("info", "SKIP", "Naruto — next episode airs 2026-06-20"))

    def test_skip_until_future_retries_when_missing(self):
        d = self._decide(skip_until="2026-06-20", missing=[4])
        self.assertFalse(d["skip"])
        self.assertEqual(d["log"][1], "RETRY")

    def test_skip_until_past_does_not_skip(self):
        d = self._decide(skip_until="2026-01-01")
        self.assertFalse(d["skip"])
        self.assertIsNone(d["log"])

    def test_real_airdate_honors_early_scrape_window(self):
        # EARLY_SCRAPE_DAYS defaults to 1 — the eve of a real airdate scrapes.
        d = self._decide(skip_until="2026-06-14", skip_real_airdate=True)
        self.assertFalse(d["skip"])

    def test_no_skip_conditions_falls_through(self):
        d = self._decide()
        self.assertFalse(d["skip"])
        self.assertFalse(d["mark_complete"])
        self.assertIsNone(d["log"])

    def test_force_check_bypasses_complete_flag(self):
        d = self._decide(force_check=True, complete=True)
        self.assertFalse(d["skip"])
        self.assertFalse(d["mark_complete"])
        self.assertIsNone(d["log"])

    def test_force_check_bypasses_al_status_complete(self):
        d = self._decide(force_check=True, al_status="Abgeschlossen",
                          al_max_episodes=12, episodes=12)
        self.assertFalse(d["skip"])
        self.assertFalse(d["mark_complete"])

    def test_force_check_bypasses_skip_until(self):
        d = self._decide(force_check=True, skip_until="2026-12-31")
        self.assertFalse(d["skip"])
        self.assertIsNone(d["log"])


class WriteRunStateTriggerTest(unittest.TestCase):
    """run_state records whether a cycle was woken by a manual run-now
    trigger — additive, so a routine timer-driven cycle's record is
    unchanged (see WriteRunStateTest.test_record_matches_fixed_schema_keys)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aniloads-runstate-trigger-")
        self._orig_botfile = anibot.botfile
        anibot.botfile = os.path.join(self.tmp, "ani.json")
        self.path = os.path.join(self.tmp, "run_state.json")

    def tearDown(self):
        anibot.botfile = self._orig_botfile

    def _read(self):
        with open(self.path, "r") as f:
            return json.load(f)

    def test_manual_trigger_recorded(self):
        anibot.write_run_state(
            "2026-06-13T19:00:00Z", "2026-06-13T19:01:00Z", 600, {"checked": 1},
            trigger="manual")
        self.assertEqual(self._read()["last_run"]["trigger"], "manual")

    def test_no_trigger_omits_the_key(self):
        anibot.write_run_state(
            "2026-06-13T19:00:00Z", "2026-06-13T19:01:00Z", 600, {"checked": 1})
        self.assertNotIn("trigger", self._read()["last_run"])


class LogNoLongerPushesPerLineTest(unittest.TestCase):
    """log() must keep logging locally but stop pushing every call to
    Pushbullet — that behavior moved to the one-per-cycle summary in
    _notify_cycle(). See LogPushbulletNeverRaisesTest above for the
    never-raise contract, which still holds now that log() ignores pb."""

    def test_log_never_calls_push_note(self):
        calls = []
        fake_pb = mock.Mock()
        fake_pb.push_note.side_effect = lambda *a, **k: calls.append((a, k))
        anibot.log("[DOWNLOAD] Lade Episode 5 von Frieren", fake_pb)
        self.assertEqual(calls, [])
        fake_pb.push_note.assert_not_called()


class FormatCycleSummaryTest(unittest.TestCase):
    """_format_cycle_summary: quiet cycles return None; noteworthy cycles
    build one English message from the seeded events list."""

    def test_no_events_and_no_login_error_is_quiet(self):
        self.assertIsNone(anibot._format_cycle_summary([]))

    def test_only_complete_events_is_quiet(self):
        events = [{"kind": "complete", "anime": "Frieren"}]
        self.assertIsNone(anibot._format_cycle_summary(events))

    def test_downloads_are_summarized_with_series_and_episodes(self):
        events = [
            {"kind": "download", "anime": "Frieren", "episodes": [27, 28]},
            {"kind": "download", "anime": "Dandadan", "episodes": [5]},
        ]
        message = anibot._format_cycle_summary(events)
        self.assertTrue(message.startswith("Aniloads: "))
        self.assertIn("3 episodes downloaded", message)
        self.assertIn("Frieren (27, 28)", message)
        self.assertIn("Dandadan (5)", message)

    def test_single_download_uses_singular_noun(self):
        events = [{"kind": "download", "anime": "Frieren", "episodes": [27]}]
        message = anibot._format_cycle_summary(events)
        self.assertIn("1 episode downloaded", message)

    def test_errors_include_first_few_details(self):
        events = [
            {"kind": "error", "anime": "Frieren", "detail": "JDownloader unreachable"},
        ]
        message = anibot._format_cycle_summary(events)
        self.assertIn("1 error", message)
        self.assertIn("Frieren: JDownloader unreachable", message)

    def test_error_count_caps_detail_to_first_three(self):
        events = [
            {"kind": "error", "anime": "A{}".format(i), "detail": "boom{}".format(i)}
            for i in range(5)
        ]
        message = anibot._format_cycle_summary(events)
        self.assertIn("5 errors", message)
        self.assertIn("A0: boom0", message)
        self.assertIn("A2: boom2", message)
        self.assertNotIn("A3: boom3", message)

    def test_mismatches_are_counted(self):
        events = [{"kind": "mismatch", "anime": "Frieren", "detail": "episode count mismatch"}]
        message = anibot._format_cycle_summary(events)
        self.assertIn("1 mismatch", message)

    def test_downloads_and_errors_combine_matching_the_readme_example(self):
        events = [
            {"kind": "download", "anime": "Frieren", "episodes": [27, 28]},
            {"kind": "download", "anime": "Dandadan", "episodes": [5]},
            {"kind": "error", "anime": "Bleach", "detail": "JDownloader unreachable"},
        ]
        message = anibot._format_cycle_summary(events)
        self.assertEqual(
            message,
            "Aniloads: 3 episodes downloaded — Frieren (27, 28), Dandadan (5) "
            "· 1 error: Bleach: JDownloader unreachable",
        )

    def test_login_error_alone_is_noteworthy(self):
        message = anibot._format_cycle_summary([], login_error="invalid credentials")
        self.assertIn("login failed: invalid credentials", message)


class NotifyCycleTest(unittest.TestCase):
    """_notify_cycle: sends at most one summary to notify targets + Pushbullet
    per cycle, and sends nothing for a quiet cycle."""

    def setUp(self):
        self._orig_send_all = anibot.notify.send_all
        self.sent = []
        anibot.notify.send_all = lambda targets, title, message: self.sent.append(
            (targets, title, message))

    def tearDown(self):
        anibot.notify.send_all = self._orig_send_all

    def test_quiet_cycle_sends_nothing(self):
        fake_pb = mock.Mock()
        anibot._notify_cycle(["fake-target"], fake_pb, [])
        self.assertEqual(self.sent, [])
        fake_pb.push_note.assert_not_called()

    def test_noteworthy_cycle_notifies_targets_and_pushbullet_once(self):
        fake_pb = mock.Mock()
        events = [{"kind": "download", "anime": "Frieren", "episodes": [1]}]
        anibot._notify_cycle(["fake-target"], fake_pb, events)
        self.assertEqual(len(self.sent), 1)
        targets, title, message = self.sent[0]
        self.assertEqual(targets, ["fake-target"])
        self.assertEqual(title, "Aniloads")
        self.assertIn("Frieren", message)
        fake_pb.push_note.assert_called_once_with("Aniloads", message)

    def test_no_targets_configured_skips_notify_send_all(self):
        fake_pb = mock.Mock()
        events = [{"kind": "error", "anime": "Frieren", "detail": "boom"}]
        anibot._notify_cycle([], fake_pb, events)
        self.assertEqual(self.sent, [])
        fake_pb.push_note.assert_called_once()

    def test_disabled_pushbullet_is_skipped_without_raising(self):
        events = [{"kind": "error", "anime": "Frieren", "detail": "boom"}]
        anibot._notify_cycle(["fake-target"], "", events)  # pb disabled == ""
        self.assertEqual(len(self.sent), 1)

    def test_pushbullet_failure_does_not_raise(self):
        fake_pb = mock.Mock()
        fake_pb.push_note.side_effect = Exception("network error")
        events = [{"kind": "error", "anime": "Frieren", "detail": "boom"}]
        anibot._notify_cycle(["fake-target"], fake_pb, events)  # must not raise

    def test_login_error_notifies_even_with_no_events(self):
        fake_pb = mock.Mock()
        anibot._notify_cycle(["fake-target"], fake_pb, [], login_error="bad creds")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("bad creds", self.sent[0][2])


if __name__ == "__main__":
    unittest.main()
