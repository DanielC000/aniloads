"""Tests for the pure-logic functions in web/app.py."""

import html
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs, quote

import support

app = support.load_app()


class LangInListTest(unittest.TestCase):
    def test_any_always_matches(self):
        self.assertTrue(app.lang_in_list("any", []))
        self.assertTrue(app.lang_in_list("any", None))
        self.assertTrue(app.lang_in_list("any", ["Deutsch"]))

    def test_unknown_pref_always_matches(self):
        # An unrecognised pref is treated as "no constraint" (like "any").
        self.assertTrue(app.lang_in_list("klingon", ["Deutsch"]))
        self.assertTrue(app.lang_in_list("klingon", []))

    def test_german_alias_tolerance(self):
        self.assertTrue(app.lang_in_list("german", ["Deutsch"]))
        self.assertTrue(app.lang_in_list("german", ["GER"]))

    def test_japanese_matches_german_label(self):
        self.assertTrue(app.lang_in_list("japanese", ["Japanisch"]))

    def test_english_alias_tolerance(self):
        self.assertTrue(app.lang_in_list("english", ["Englisch"]))
        self.assertTrue(app.lang_in_list("english", ["Eng"]))

    def test_no_match_returns_false(self):
        self.assertFalse(app.lang_in_list("german", ["Japanisch"]))
        self.assertFalse(app.lang_in_list("english", ["Deutsch", "Japanisch"]))

    def test_empty_list_with_real_pref_is_false(self):
        self.assertFalse(app.lang_in_list("german", []))
        self.assertFalse(app.lang_in_list("german", None))

    def test_multi_language_list(self):
        langs = ["Deutsch", "Japanisch"]
        self.assertTrue(app.lang_in_list("german", langs))
        self.assertTrue(app.lang_in_list("japanese", langs))
        self.assertFalse(app.lang_in_list("english", langs))

    def test_none_entry_is_tolerated(self):
        self.assertTrue(app.lang_in_list("german", [None, "Deutsch"]))
        self.assertFalse(app.lang_in_list("english", [None]))


class PickBestReleaseTest(unittest.TestCase):
    def _rel(self, res=1080, dubs=None, subs=None, episodes=12, rid="x"):
        return {
            "id": rid,
            "resolution": res,
            "dubs": dubs if dubs is not None else ["Deutsch"],
            "subs": subs if subs is not None else ["Deutsch"],
            "episodes": episodes,
        }

    def test_strict_no_match_returns_none(self):
        # audio german required, but no release has a german dub.
        prefs = {"audio_language": "german", "sub_language": "any", "min_resolution": 720}
        releases = [self._rel(dubs=["Japanisch"]), self._rel(dubs=["Englisch"])]
        self.assertIsNone(app.pick_best_release(releases, prefs))

    def test_empty_releases_returns_none(self):
        prefs = {"audio_language": "any", "sub_language": "any", "min_resolution": 0}
        self.assertIsNone(app.pick_best_release([], prefs))

    def test_min_resolution_hard_filter(self):
        prefs = {"audio_language": "any", "sub_language": "any", "min_resolution": 1080}
        # Only sub-1080 releases → nothing qualifies.
        releases = [self._rel(res=720), self._rel(res=480)]
        self.assertIsNone(app.pick_best_release(releases, prefs))

    def test_min_resolution_keeps_qualifying(self):
        prefs = {"audio_language": "any", "sub_language": "any", "min_resolution": 1080}
        good = self._rel(res=1080, rid="good")
        releases = [self._rel(res=720, rid="bad"), good]
        self.assertIs(app.pick_best_release(releases, prefs), good)

    def test_dub_list_membership(self):
        prefs = {"audio_language": "german", "sub_language": "any", "min_resolution": 720}
        match = self._rel(dubs=["Japanisch", "Deutsch"], rid="match")
        nomatch = self._rel(dubs=["Japanisch"], rid="nomatch")
        self.assertIs(app.pick_best_release([nomatch, match], prefs), match)

    def test_sub_list_membership(self):
        prefs = {"audio_language": "any", "sub_language": "english", "min_resolution": 720}
        match = self._rel(subs=["Englisch"], rid="match")
        nomatch = self._rel(subs=["Deutsch"], rid="nomatch")
        self.assertIs(app.pick_best_release([nomatch, match], prefs), match)

    def test_ranking_prefers_higher_resolution_plus_episodes(self):
        prefs = {"audio_language": "any", "sub_language": "any", "min_resolution": 720}
        low = self._rel(res=720, episodes=12, rid="low")
        high = self._rel(res=1080, episodes=12, rid="high")
        self.assertIs(app.pick_best_release([low, high], prefs), high)

    def test_ranking_episodes_break_resolution_tie(self):
        prefs = {"audio_language": "any", "sub_language": "any", "min_resolution": 720}
        fewer = self._rel(res=1080, episodes=12, rid="fewer")
        more = self._rel(res=1080, episodes=24, rid="more")
        self.assertIs(app.pick_best_release([fewer, more], prefs), more)

    def test_non_numeric_resolution_is_zero(self):
        # Garbage resolution → treated as 0, filtered out by a positive min_res.
        prefs = {"audio_language": "any", "sub_language": "any", "min_resolution": 720}
        bad = {"id": "bad", "resolution": "n/a", "dubs": [], "subs": [], "episodes": 1}
        self.assertIsNone(app.pick_best_release([bad], prefs))

    def test_defaults_used_when_prefs_missing_keys(self):
        # No keys → defaults: audio german, sub any, min_resolution 1080.
        rel = self._rel(res=1080, dubs=["Deutsch"])
        self.assertIs(app.pick_best_release([rel], {}), rel)
        # A german default means a japanese-only dub does not match.
        self.assertIsNone(app.pick_best_release([self._rel(dubs=["Japanisch"])], {}))


class LoadPrefsBackCompatTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._orig = app.PREFS_FILE
        app.PREFS_FILE = self.path

    def tearDown(self):
        app.PREFS_FILE = self._orig
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _write(self, obj):
        with open(self.path, "w") as f:
            json.dump(obj, f)

    def test_missing_file_returns_defaults(self):
        os.remove(self.path)
        prefs = app.load_prefs()
        self.assertEqual(prefs["audio_language"], "german")
        self.assertEqual(prefs["sub_language"], "any")
        self.assertEqual(prefs["min_resolution"], 1080)
        self.assertTrue(prefs["auto_select"])
        self.assertNotIn("language", prefs)

    def test_invalid_json_returns_defaults(self):
        with open(self.path, "w") as f:
            f.write("{not valid json")
        prefs = app.load_prefs()
        self.assertEqual(prefs["audio_language"], "german")

    def test_old_language_key_maps_to_audio_language(self):
        self._write({"language": "english"})
        prefs = app.load_prefs()
        self.assertEqual(prefs["audio_language"], "english")
        # Stale key must be dropped.
        self.assertNotIn("language", prefs)

    def test_audio_language_wins_when_both_present(self):
        self._write({"language": "english", "audio_language": "japanese"})
        prefs = app.load_prefs()
        self.assertEqual(prefs["audio_language"], "japanese")
        self.assertNotIn("language", prefs)

    def test_stored_values_override_defaults(self):
        self._write({"audio_language": "japanese", "min_resolution": 720, "auto_select": False})
        prefs = app.load_prefs()
        self.assertEqual(prefs["audio_language"], "japanese")
        self.assertEqual(prefs["min_resolution"], 720)
        self.assertFalse(prefs["auto_select"])
        # Untouched default still present.
        self.assertEqual(prefs["sub_language"], "any")


class ParseSeasonEpisodeTest(unittest.TestCase):
    def test_dot_separated(self):
        self.assertEqual(
            app.parse_season_episode("Anime.Name.S01E05.mkv"),
            ("Anime.Name", 1, 5),
        )

    def test_underscore_separator(self):
        self.assertEqual(
            app.parse_season_episode("Anime_Name_S02E10.mkv"),
            ("Anime_Name", 2, 10),
        )

    def test_lowercase_markers(self):
        self.assertEqual(
            app.parse_season_episode("Show.s03e07.mkv"),
            ("Show", 3, 7),
        )

    def test_multi_digit_season_and_episode(self):
        self.assertEqual(
            app.parse_season_episode("Long.S12E123.mkv"),
            ("Long", 12, 123),
        )

    def test_no_match_returns_none(self):
        self.assertIsNone(app.parse_season_episode("Anime Movie 1080p.mkv"))
        self.assertIsNone(app.parse_season_episode("NoSeasonHere.mkv"))


class MatchAnimeEntryTest(unittest.TestCase):
    def test_download_folder_pattern_match(self):
        anime = [{
            "name": "Mob Psycho 100",
            "customPackage": "Mob Psycho 100",
            "download_folder_pattern": "Mob.Psycho.100.S01",
            "tvdb_season": 1,
            "episode_offset": 0,
            "media_type": "series",
        }]
        res = app.match_anime_entry("Mob.Psycho.100", "Mob.Psycho.100.S01E01", anime)
        self.assertEqual(res["folder_name"], "Mob Psycho 100")
        self.assertEqual(res["tvdb_season"], 1)

    def test_season_tiebreaker(self):
        # Two seasons sharing a generic prefix; parsed_season picks the right one.
        anime = [
            {"name": "Mob S1", "customPackage": "Mob Psycho 100",
             "download_folder_pattern": "Mob.Psycho.100", "tvdb_season": 1},
            {"name": "Mob S2", "customPackage": "Mob Psycho 100 II",
             "download_folder_pattern": "Mob.Psycho.100", "tvdb_season": 2},
        ]
        res = app.match_anime_entry("Mob.Psycho.100", "Mob.Psycho.100", anime, parsed_season=2)
        self.assertEqual(res["tvdb_season"], 2)
        self.assertEqual(res["folder_name"], "Mob Psycho 100 II")

    def test_custompackage_fallback(self):
        anime = [{"name": "Frieren", "customPackage": "Frieren Dai 2 Ki", "tvdb_season": 2}]
        res = app.match_anime_entry("whatever", "somefolder.frieren dai 2 ki.x", anime)
        self.assertEqual(res["folder_name"], "Frieren Dai 2 Ki")
        self.assertEqual(res["tvdb_season"], 2)

    def test_name_in_dir_fallback(self):
        anime = [{"name": "Bleach", "tvdb_season": 1}]
        res = app.match_anime_entry("xxx", "Bleach.S01E01.1080p", anime)
        self.assertEqual(res["folder_name"], "Bleach")  # customPackage defaults to name

    def test_parsed_name_equals_entry_name(self):
        anime = [{"name": "Naruto", "customPackage": "Naruto Shippuden", "tvdb_season": 1}]
        res = app.match_anime_entry("naruto", "unrelated_dir", anime)
        self.assertEqual(res["folder_name"], "Naruto Shippuden")

    def test_no_match_returns_default(self):
        res = app.match_anime_entry("Some.Anime", "some_dir", [])
        self.assertEqual(res["folder_name"], "Some.Anime")
        self.assertIsNone(res["tvdb_season"])
        self.assertEqual(res["episode_offset"], 0)
        self.assertEqual(res["media_type"], "series")
        self.assertEqual(res["display_title"], "Some.Anime")


class MovieTargetNameTest(unittest.TestCase):
    def test_with_year(self):
        self.assertEqual(app._movie_target_name("Spirited Away", 2001), "Spirited Away (2001)")

    def test_without_year(self):
        self.assertEqual(app._movie_target_name("Spirited Away", None), "Spirited Away")
        self.assertEqual(app._movie_target_name("Spirited Away", 0), "Spirited Away")

    def test_sanitizes_illegal_chars(self):
        self.assertEqual(app._movie_target_name('A: B/C?', 2020), "A BC (2020)")

    def test_empty_title_falls_back_to_unknown(self):
        self.assertEqual(app._movie_target_name("", 2020), "Unknown (2020)")
        self.assertEqual(app._movie_target_name("", None), "Unknown")


class RenderReleasesEscapingTest(unittest.TestCase):
    def _info(self, dubs, subs):
        return {
            "name": "Test & <Anime>",
            "url": "http://x",
            "media_type": "series",
            "releases": [{
                "id": "r1", "resolution": 1080, "episodes": 12,
                "size_mb": 700, "group": "G&P", "dubs": dubs, "subs": subs,
            }],
        }

    def test_empty_subs_render_single_em_dash_entity(self):
        out = app.render_releases(self._info(["English"], []), "r1")
        # Single entity → renders as an em-dash, not the literal "&mdash;".
        self.assertIn("Sub: &mdash;", out)
        self.assertNotIn("&amp;mdash;", out)

    def test_empty_dubs_render_single_em_dash_entity(self):
        out = app.render_releases(self._info([], ["English"]), "r1")
        self.assertIn("Dub: &mdash;", out)
        self.assertNotIn("&amp;mdash;", out)

    def test_real_languages_are_html_escaped(self):
        out = app.render_releases(self._info(["Jap<x>"], ["Eng&Co"]), "r1")
        self.assertIn("Dub: Jap&lt;x&gt;", out)
        self.assertIn("Sub: Eng&amp;Co", out)


class RenderWatchlistPendingTest(unittest.TestCase):
    def test_no_match_pending_renders_explanatory_line(self):
        out = app.render_watchlist(
            [], [{"name": "Foo", "url": "http://x", "no_match": True}])
        self.assertIn("No release matches your language preference", out)
        self.assertIn("badge-warn", out)
        self.assertNotIn("Resolving", out)

    def test_normal_pending_renders_resolving(self):
        out = app.render_watchlist([], [{"name": "Bar", "url": "http://y"}])
        self.assertIn("Resolving", out)
        self.assertNotIn("No release matches", out)


class RenderWatchlistMovieBadgeTest(unittest.TestCase):
    """Movie entries get a neutral 'Movie' badge so routing is visible (UI-6)."""

    def test_movie_entry_shows_badge(self):
        out = app.render_watchlist([{"name": "Akira", "media_type": "movie", "episodes": 1}])
        self.assertIn(">Movie</span>", out)

    def test_series_entry_has_no_movie_badge(self):
        out = app.render_watchlist([{"name": "Bleach", "media_type": "series", "episodes": 12}])
        self.assertNotIn(">Movie</span>", out)


class RenderWatchlistEpisodeCollapseTest(unittest.TestCase):
    """OK episodes collapse behind a toggle; retrying rows render up front (UI-2)."""

    def test_long_series_does_not_emit_all_ok_rows(self):
        out = app.render_watchlist([{"name": "Long", "episodes": 500, "missing": [7]}])
        # The lazy toggle reports the OK count instead of rendering 499 rows.
        self.assertIn("Show 499 OK episodes", out)
        self.assertIn("expandEps", out)
        # The OK rows are NOT in the server-rendered HTML (built in JS on demand).
        self.assertNotIn("Ep 250", out)
        # Retrying episodes are always rendered up front.
        self.assertIn("Ep 7", out)
        self.assertIn("badge-retry", out)

    def test_all_ok_series_has_no_retry_rows_but_has_toggle(self):
        out = app.render_watchlist([{"name": "Clean", "episodes": 3, "missing": []}])
        self.assertIn("Show 3 OK episodes", out)
        self.assertNotIn("badge-retry", out)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class HumanizeEtaTest(unittest.TestCase):
    """The run-state ETA helper must mirror the log-tail next-run wording."""

    def _eta(self, future_seconds, delay=600):
        now = datetime(2026, 6, 13, 19, 0, 0)
        target = now + timedelta(seconds=future_seconds)
        return app._humanize_eta(target, now, delay)

    def test_future_minutes(self):
        self.assertEqual(self._eta(300), "~5 min")

    def test_under_one_minute(self):
        self.assertEqual(self._eta(30), "<1 min")

    def test_imminent_when_within_one_interval(self):
        self.assertEqual(self._eta(-60, delay=600), "any moment")

    def test_overdue_minutes(self):
        self.assertEqual(self._eta(-3600, delay=600), "overdue ~60 min")

    def test_overdue_hours(self):
        self.assertEqual(self._eta(-3 * 3600, delay=600), "overdue ~3h")

    def test_overdue_days(self):
        self.assertEqual(self._eta(-2 * 86400, delay=600), "overdue ~2d")


class FormatRunStateDisplayTest(unittest.TestCase):
    def test_next_run_from_state(self):
        # Anchor 5.5 min ahead so integer-minute flooring lands on "~5 min"
        # regardless of the few ms of wall-clock drift before the helper reads now.
        next_ts = _iso(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=330))
        out = app.format_next_run_display({"next_run_ts": next_ts, "timedelay": 600})
        self.assertEqual(out, "~5 min")

    def test_next_run_missing_ts_is_blank(self):
        self.assertEqual(app.format_next_run_display({"timedelay": 600}), "")
        self.assertEqual(app.format_next_run_display({"next_run_ts": ""}), "")

    def test_last_run_time_and_summary(self):
        out = app.format_last_run_display({
            "finished_ts": "2026-06-13T19:20:05Z",
            "counts": {"entries": 8, "checked": 5, "downloaded": 2},
        })
        self.assertIn("19:20:05", out)
        self.assertIn("checked 5/8", out)
        self.assertIn("2 downloaded", out)
        self.assertIn("&mdash;", out)

    def test_last_run_no_downloads_omits_downloaded(self):
        out = app.format_last_run_display({
            "finished_ts": "2026-06-13T19:20:05Z",
            "counts": {"entries": 3, "checked": 0, "downloaded": 0},
        })
        self.assertIn("checked 0/3", out)
        self.assertNotIn("downloaded", out)

    def test_last_run_blank_when_unparseable(self):
        self.assertEqual(app.format_last_run_display({"finished_ts": None, "counts": {}}), "")


class GetActivityRunStateTest(unittest.TestCase):
    """get_activity must prefer the persisted run-state over the log tail for
    last_run/next_run, and survive a missing record."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._orig = app.RUN_STATE_FILE
        app.RUN_STATE_FILE = self.path

    def tearDown(self):
        app.RUN_STATE_FILE = self._orig
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _write_state(self, obj):
        with open(self.path, "w") as f:
            json.dump(obj, f)

    def test_missing_record_returns_defaults(self):
        os.remove(self.path)
        self.assertEqual(app.load_run_state(), {})
        act = app.get_activity()
        # No run-state → no run-state display keys; log-tail fallback stands.
        self.assertNotIn("last_run_display", act)
        self.assertNotIn("run_state", act)

    def test_corrupt_record_returns_defaults(self):
        with open(self.path, "w") as f:
            f.write("{nope")
        self.assertEqual(app.load_run_state(), {})

    def test_run_state_drives_last_and_next_run(self):
        next_ts = _iso(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=330))
        self._write_state({
            "schema": 1,
            "last_run": {
                "finished_ts": "2026-06-13T19:20:05Z",
                "next_run_ts": next_ts,
                "timedelay": 600,
                "counts": {"entries": 8, "checked": 5, "downloaded": 2},
            },
            "runs": [],
        })
        act = app.get_activity()
        self.assertIn("checked 5/8", act["last_run_display"])
        self.assertEqual(act["next_run"], "~5 min")
        self.assertIn("run_state", act)

    def test_render_activity_uses_run_state_display(self):
        act = {
            "status": {"running": True},
            "runs": [],
            "last_run": None,
            "last_run_display": "19:20:05 &mdash; checked 5/8",
            "next_run": "~5 min",
        }
        _status, last_html, next_html = app.render_activity(act)
        self.assertEqual(last_html, "19:20:05 &mdash; checked 5/8")
        self.assertEqual(next_html, "~5 min")
        # Must NOT fall through to the "No runs yet" log-tail empty state.
        self.assertNotIn("No runs yet", last_html)


class BuildRunSummaryTest(unittest.TestCase):
    """One concise summary line per cycle, sourced from run-state counts."""

    def _rec(self, **counts):
        return {"finished_ts": "2026-06-13T19:40:00Z", "counts": counts}

    def test_full_line_with_downloads_and_errors(self):
        out = app.build_run_summary(self._rec(entries=12, checked=12, downloaded=2, errors=1))
        self.assertEqual(out, "19:40 — checked 12/12 · 2 downloaded · 1 error")

    def test_errors_pluralize(self):
        out = app.build_run_summary(self._rec(entries=5, checked=5, errors=3))
        self.assertIn("3 errors", out)

    def test_zero_noise_segments_omitted(self):
        # No downloads, no errors → just time + checked, nothing else.
        out = app.build_run_summary(self._rec(entries=12, checked=12, downloaded=0, errors=0))
        self.assertEqual(out, "19:40 — checked 12/12")
        self.assertNotIn("downloaded", out)
        self.assertNotIn("error", out)

    def test_downloads_always_surface(self):
        out = app.build_run_summary(self._rec(entries=4, checked=4, downloaded=1))
        self.assertIn("1 downloaded", out)

    def test_idle_cycle_is_just_time(self):
        # A cycle with nothing checked (all skipped / no entries) → just the time.
        self.assertEqual(app.build_run_summary(self._rec()), "19:40")
        self.assertEqual(app.build_run_summary(self._rec(entries=0, checked=0)), "19:40")

    def test_checked_without_entries(self):
        out = app.build_run_summary(self._rec(checked=3))
        self.assertEqual(out, "19:40 — checked 3")

    def test_garbage_record_is_blank(self):
        self.assertEqual(app.build_run_summary(None), "")
        self.assertEqual(app.build_run_summary({"finished_ts": None, "counts": {}}), "")


class RunSummaryToneTest(unittest.TestCase):
    def test_errors_dominate(self):
        self.assertEqual(app._run_summary_tone({"counts": {"downloaded": 2, "errors": 1}}), "danger")

    def test_downloads_are_ok(self):
        self.assertEqual(app._run_summary_tone({"counts": {"downloaded": 2}}), "ok")

    def test_routine_is_muted(self):
        self.assertEqual(app._run_summary_tone({"counts": {"checked": 5}}), "muted")
        self.assertEqual(app._run_summary_tone({}), "muted")


class RenderRunHistoryTest(unittest.TestCase):
    """The feed prefers run-state summaries (one line per run, newest first) and
    falls back to the log-parsed event feed when no records exist."""

    def test_prefers_state_runs_one_line_each_newest_first(self):
        state_runs = [
            {"finished_ts": "2026-06-13T19:30:00Z", "counts": {"entries": 12, "checked": 12, "downloaded": 0, "errors": 0}},
            {"finished_ts": "2026-06-13T19:40:00Z", "counts": {"entries": 12, "checked": 12, "downloaded": 2, "errors": 1}},
        ]
        # A log-parsed run that must NOT appear when state runs are present.
        log_runs = [{"time": "19:40", "anime": "FromLog", "events": [{"type": "download", "msg": "x"}]}]
        html = app.render_run_history(log_runs, state_runs)
        self.assertIn("checked 12/12 · 2 downloaded · 1 error", html)
        self.assertNotIn("FromLog", html)
        # Newest (19:40) rendered before older (19:30).
        self.assertLess(html.index("19:40"), html.index("19:30"))
        # The noisy run carries the danger tone.
        self.assertIn("event--danger", html)

    def test_falls_back_to_log_feed_when_no_state_runs(self):
        log_runs = [{"time": "19:40", "anime": "FromLog", "events": [{"type": "download", "msg": "x"}]}]
        html = app.render_run_history(log_runs, None)
        self.assertIn("FromLog", html)

    def test_falls_back_when_state_runs_all_blank(self):
        # Records that render to nothing must not swallow the log fallback.
        log_runs = [{"time": "19:40", "anime": "FromLog", "events": [{"type": "download", "msg": "x"}]}]
        html = app.render_run_history(log_runs, [{"finished_ts": None, "counts": {}}])
        self.assertIn("FromLog", html)


def _cycle(ts, **counts):
    """A run-state record shaped like the bot writes it (events added by tests)."""
    return {"finished_ts": ts, "counts": counts}


class RunSummarySkipBreakdownTest(unittest.TestCase):
    """"checked 2/18" alone never said why the other 16 were passed over. The
    skip breakdown is what makes the line self-explanatory."""

    def test_skipped_and_unavailable_surface(self):
        rec = _cycle("2026-06-13T19:40:00Z", entries=18, checked=2, skipped=16,
                     downloaded=2, errors=1)
        self.assertEqual(
            app.build_run_summary(rec),
            "19:40 — checked 2/18 · 16 skipped · 2 downloaded · 1 error")

    def test_unavailable_segment(self):
        rec = _cycle("2026-06-13T19:40:00Z", entries=18, checked=4, skipped=11,
                     unavailable=3)
        self.assertEqual(app.build_run_summary(rec),
                         "19:40 — checked 4/18 · 11 skipped · 3 unavailable")

    def test_zero_and_absent_keys_are_omitted(self):
        # Both a zero and a missing key stay out of the line (no "0 skipped").
        self.assertEqual(
            app.build_run_summary(_cycle("2026-06-13T19:40:00Z", entries=18,
                                         checked=2, skipped=0, unavailable=0)),
            "19:40 — checked 2/18")
        self.assertEqual(
            app.build_run_summary(_cycle("2026-06-13T19:40:00Z", entries=18, checked=2)),
            "19:40 — checked 2/18")


class RunStateEventsTest(unittest.TestCase):
    """Events are optional at read time and may be malformed; the feed must not
    trust them."""

    def test_missing_or_wrong_type_yields_nothing(self):
        self.assertEqual(app.run_state_events({"counts": {}}), [])
        self.assertEqual(app.run_state_events({"events": None}), [])
        self.assertEqual(app.run_state_events({"events": "nope"}), [])
        self.assertEqual(app.run_state_events(None), [])

    def test_unknown_kinds_are_dropped(self):
        rec = {"events": [{"kind": "download", "anime": "A", "episodes": [1]},
                          {"kind": "wat", "anime": "B"},
                          "not-a-dict"]}
        kinds = [ev["kind"] for ev in app.run_state_events(rec)]
        self.assertEqual(kinds, ["download"])


class FormatRunEventTest(unittest.TestCase):
    def test_download_names_series_and_episodes(self):
        self.assertEqual(
            app.format_run_event({"kind": "download", "anime": "One Piece",
                                  "episodes": [1112, 1113]}),
            "One Piece — episodes 1112, 1113")

    def test_single_episode_is_singular(self):
        self.assertEqual(
            app.format_run_event({"kind": "download", "anime": "Gantz", "episodes": [7]}),
            "Gantz — episode 7")

    def test_error_carries_its_detail(self):
        self.assertEqual(
            app.format_run_event({"kind": "error", "anime": "Gantz", "episodes": [],
                                  "detail": "JDownloader nicht erreichbar"}),
            "Gantz — JDownloader nicht erreichbar")

    def test_complete_is_just_the_series(self):
        self.assertEqual(app.format_run_event({"kind": "complete", "anime": "Bleach"}),
                         "Bleach")

    def test_empty_event_says_nothing(self):
        self.assertEqual(app.format_run_event({"kind": "download"}), "")

    def test_non_int_episodes_are_ignored(self):
        out = app.format_run_event({"kind": "download", "anime": "A",
                                    "episodes": ["x", None, 3]})
        self.assertEqual(out, "A — episode 3")


class RenderRunCycleTest(unittest.TestCase):
    """A cycle that did something spells out WHAT: which series, which episodes,
    what went wrong. Low-signal events hide behind the expand toggle."""

    def test_downloads_and_errors_render_as_visible_lines(self):
        rec = _cycle("2026-06-13T21:54:00Z", entries=20, checked=4, downloaded=14, errors=1)
        rec["events"] = [
            {"kind": "download", "anime": "One Piece", "episodes": [1112, 1113]},
            {"kind": "error", "anime": "Gantz", "episodes": [],
             "detail": "JDownloader nicht erreichbar"},
        ]
        html_out = app.render_run_cycle(rec)
        self.assertIn("One Piece — episodes 1112, 1113", html_out)
        self.assertIn("Gantz — JDownloader nicht erreichbar", html_out)
        # ... and outside any collapsed panel.
        self.assertNotIn("run-detail", html_out.split('class="run-events"')[1][:400])
        self.assertIn("event--ok", html_out)
        self.assertIn("event--danger", html_out)

    def test_low_signal_events_hide_behind_a_closed_toggle(self):
        rec = _cycle("2026-06-13T19:00:00Z", entries=18, checked=2, downloaded=1)
        rec["events"] = [
            {"kind": "download", "anime": "Frieren", "episodes": [4]},
            {"kind": "unavailable", "anime": "Dandadan", "episodes": [9],
             "detail": "no German release yet"},
            {"kind": "complete", "anime": "Bleach"},
        ]
        html_out = app.render_run_cycle(rec)
        self.assertIn('onclick="expandRun(this)"', html_out)
        self.assertIn("Show 2 more details", html_out)
        self.assertIn('aria-expanded="false"', html_out)
        # The panel exists but starts closed; its rows are server-rendered.
        self.assertIn('<div class="run-detail" data-run-key=', html_out)
        self.assertNotIn("run-detail-open", html_out)
        panel = html_out.split('class="run-detail" data-run-key=')[1]
        self.assertIn("Dandadan — episode 9 — no German release yet", panel)
        self.assertIn("Bleach", panel)
        # The download stays out of the collapsed panel.
        self.assertNotIn("Frieren", panel)

    def test_no_toggle_when_there_is_no_low_signal_detail(self):
        rec = _cycle("2026-06-13T19:00:00Z", entries=18, checked=2, downloaded=1)
        rec["events"] = [{"kind": "download", "anime": "Frieren", "episodes": [4]}]
        self.assertNotIn("run-detail", app.render_run_cycle(rec))

    def test_truncation_is_stated_not_silently_swallowed(self):
        rec = _cycle("2026-06-13T21:54:00Z", entries=20, checked=20, downloaded=40)
        rec["events"] = [{"kind": "download", "anime": "S%d" % n, "episodes": [n]}
                         for n in range(1, 41)]
        rec["events_truncated"] = True
        html_out = app.render_run_cycle(rec)
        self.assertIn("event list truncated", html_out)
        self.assertIn("first 40", html_out)
        # The notice is a caveat about completeness: never behind the toggle.
        self.assertNotIn("run-detail", html_out)

    def test_truncation_notice_counts_the_raw_recorded_list(self):
        # The filtered list drops kinds this dashboard does not know (a newer
        # bot). The notice must still report what was RECORDED — the one line
        # whose job is "this list is incomplete" cannot state a wrong number.
        rec = _cycle("2026-06-13T21:54:00Z", entries=20, checked=20, downloaded=3)
        rec["events"] = [{"kind": "download", "anime": "A", "episodes": [1]},
                         {"kind": "download", "anime": "B", "episodes": [2]},
                         {"kind": "from-a-newer-bot", "anime": "C"},
                         {"kind": "also-unknown", "anime": "D"}]
        rec["events_truncated"] = True
        html_out = app.render_run_cycle(rec)
        self.assertIn("only the first 4 of this cycle were recorded", html_out)
        self.assertNotIn("first 2 of this cycle", html_out)

    def test_series_name_with_ampersand_and_apostrophe_is_escaped(self):
        # BUG class this repo has shipped two fixes for (a61c2bf, 5292059):
        # watchlist names are user data and reach HTML here.
        name = "Fate/stay night & Heaven's Feel"
        rec = _cycle("2026-06-13T19:00:00Z", entries=1, checked=1, downloaded=1)
        rec["events"] = [{"kind": "download", "anime": name, "episodes": [3]},
                         {"kind": "complete", "anime": name}]
        html_out = app.render_run_cycle(rec)
        # html.escape() quotes too, so the apostrophe lands as &#x27; — exactly
        # what the Remove-confirm fixes established as this repo's baseline.
        self.assertIn("Fate/stay night &amp; Heaven&#x27;s Feel", html_out)
        # No raw ampersand or apostrophe from the name survives into the markup.
        self.assertNotIn("night & Heaven", html_out)
        self.assertNotIn("Heaven's", html_out)
        self.assertNotIn("<script", html_out)

    def test_detail_string_cannot_inject_markup(self):
        rec = _cycle("2026-06-13T19:00:00Z", entries=1, checked=1, errors=1)
        rec["events"] = [{"kind": "error", "anime": "X", "episodes": [],
                          "detail": "<img src=x onerror=alert(1)>"}]
        html_out = app.render_run_cycle(rec)
        self.assertNotIn("<img", html_out)
        self.assertIn("&lt;img", html_out)


class MismatchEventTest(unittest.TestCase):
    """A numbering mismatch (a release numbering its files 41-46 while the
    watchlist wants 1-7) is the most actionable line the feed can show: it names
    the config change that fixes it. It must never hide behind the toggle and
    must never fold into a "nothing new" group."""

    DETAIL = ("wanted 1-7 but release numbers episodes 41-46 — "
              "set episode_offset to -40")

    def _mismatch(self):
        return {"kind": "mismatch", "anime": "Bleach", "episodes": [1, 2, 3, 4, 5, 6, 7],
                "detail": self.DETAIL}

    def test_it_is_a_loud_kind(self):
        self.assertIn("mismatch", app._LOUD_EVENT_KINDS)
        self.assertNotIn("mismatch", app._QUIET_EVENT_KINDS)

    def test_presentation_is_warn_not_danger(self):
        # A fixable configuration gap, not a failure.
        self.assertEqual(app._EVENT_PRESENTATION["mismatch"], ("warn", "Numbering"))

    def test_the_shared_event_shape_needs_no_special_formatting(self):
        self.assertEqual(
            app.format_run_event(self._mismatch()),
            "Bleach — episodes 1, 2, 3, 4, 5, 6, 7 — " + self.DETAIL)

    def test_it_renders_visibly_never_behind_the_toggle(self):
        rec = _cycle("2026-06-13T19:10:00Z", entries=18, checked=1)
        rec["events"] = [self._mismatch()]
        html_out = app.render_run_cycle(rec)
        self.assertIn("Numbering", html_out)
        self.assertIn("set episode_offset to -40", html_out)
        self.assertIn("event--warn", html_out)
        # Nothing collapsed: the cycle carries no toggle at all here.
        self.assertNotIn("run-detail", html_out)

    def test_a_mismatch_cycle_is_not_quiet(self):
        # The counts say "nothing happened" — only the event knows better.
        rec = _cycle("2026-06-13T19:10:00Z", entries=18, checked=1,
                     downloaded=0, errors=0)
        self.assertTrue(app.cycle_is_quiet(dict(rec)))
        rec["events"] = [self._mismatch()]
        self.assertFalse(app.cycle_is_quiet(rec))

    def test_a_mismatch_cycle_is_never_folded_into_a_quiet_group(self):
        quiet = [_cycle("2026-06-13T18:%d:00Z" % m, entries=18, checked=2)
                 for m in (50, 51, 52, 54, 55)]
        loud = _cycle("2026-06-13T18:53:00Z", entries=18, checked=1)
        loud["events"] = [self._mismatch()]
        state_runs = quiet[:3] + [loud] + quiet[3:]

        html_out = app.render_run_state_history(state_runs)
        self.assertIn("18:53 — checked 1/18", html_out)
        self.assertIn("set episode_offset to -40", html_out)
        # It split the wall in two rather than disappearing into it.
        self.assertEqual(html_out.count("quiet cycles"), 2)
        self.assertIn("18:54 – 18:55 · 2 quiet cycles", html_out)
        self.assertIn("18:50 – 18:52 · 3 quiet cycles", html_out)
        # Its line stands between the two groups, not inside either.
        detail_at = html_out.index("set episode_offset to -40")
        self.assertLess(html_out.index("18:54 – 18:55"), detail_at)
        self.assertLess(detail_at, html_out.index("18:50 – 18:52"))
        # And it is nowhere inside a collapsed panel.
        for panel in html_out.split('<div class="run-detail" ')[1:]:
            self.assertNotIn("episode_offset", panel.split("</div></div>")[0])

    def test_the_detail_is_escaped_like_every_other_event_string(self):
        rec = _cycle("2026-06-13T19:10:00Z", entries=18, checked=1)
        rec["events"] = [{"kind": "mismatch", "anime": "Fate/stay night & Heaven's Feel",
                          "episodes": [1], "detail": "wanted 1-7 & got <41-46>"}]
        html_out = app.render_run_cycle(rec)
        self.assertIn("Fate/stay night &amp; Heaven&#x27;s Feel", html_out)
        self.assertIn("wanted 1-7 &amp; got &lt;41-46&gt;", html_out)
        self.assertNotIn("<41-46>", html_out)


class QuietCycleGroupingTest(unittest.TestCase):
    """The owner's complaint: sixteen near-identical routine lines buried the
    cycles that actually did something."""

    def _quiet(self, hhmm, checked=2):
        return _cycle("2026-06-13T%s:00Z" % hhmm, entries=18, checked=checked,
                      downloaded=0, errors=0)

    def test_a_cycle_with_downloads_is_never_quiet(self):
        self.assertTrue(app.cycle_is_quiet(self._quiet("18:50")))
        self.assertFalse(app.cycle_is_quiet(
            _cycle("2026-06-13T19:00:00Z", entries=18, checked=2, downloaded=1)))
        self.assertFalse(app.cycle_is_quiet(
            _cycle("2026-06-13T19:00:00Z", entries=18, checked=2, errors=1)))

    def test_a_download_event_alone_disqualifies_a_group(self):
        # Counts may lag; an event that says a download happened is enough.
        rec = self._quiet("19:00")
        rec["events"] = [{"kind": "download", "anime": "A", "episodes": [1]}]
        self.assertFalse(app.cycle_is_quiet(rec))

    def test_low_signal_events_do_not_disqualify_a_group(self):
        rec = self._quiet("19:00")
        rec["events"] = [{"kind": "unavailable", "anime": "A", "episodes": [1],
                          "detail": "not out yet"}]
        self.assertTrue(app.cycle_is_quiet(rec))

    def test_group_summary_carries_span_count_and_checked_range(self):
        group = [self._quiet("19:06", checked=2), self._quiet("18:47", checked=0),
                 self._quiet("18:55", checked=3)]
        self.assertEqual(app.build_quiet_group_summary(group),
                         "18:47 – 19:06 · 3 quiet cycles — checked 0–3/18, nothing new")

    def test_group_summary_collapses_an_identical_range(self):
        group = [self._quiet("19:06"), self._quiet("18:47")]
        self.assertEqual(app.build_quiet_group_summary(group),
                         "18:47 – 19:06 · 2 quiet cycles — checked 2/18, nothing new")

    def test_group_summary_drops_the_denominator_when_it_varied(self):
        group = [self._quiet("19:06", checked=2),
                 _cycle("2026-06-13T18:47:00Z", entries=20, checked=1)]
        out = app.build_quiet_group_summary(group)
        self.assertIn("checked 1–2, nothing new", out)
        self.assertNotIn("/18", out)
        self.assertNotIn("/20", out)

    def test_a_lone_quiet_cycle_is_not_grouped(self):
        html_out = app.render_run_state_history([self._quiet("18:50")])
        self.assertNotIn("quiet cycles", html_out)
        self.assertIn("18:50 — checked 2/18", html_out)

    def test_the_owners_wall_of_repeats_folds_into_one_line(self):
        # The owner's paste, oldest-first as run_state.json stores it.
        state_runs = [self._quiet("18:46", checked=0)]
        state_runs += [self._quiet("18:%d" % m, checked=2)
                       for m in (47, 48, 50, 51, 52)]
        state_runs.append(_cycle("2026-06-13T18:53:00Z", entries=18, checked=2, downloaded=1))
        state_runs += [self._quiet("18:%d" % m, checked=2) for m in (54, 55, 56)]
        state_runs.append(_cycle("2026-06-13T21:54:00Z", entries=20, checked=4, downloaded=14))

        html_out = app.render_run_state_history(state_runs)
        # Two quiet runs folded (18:46–18:52 and 18:54–18:56), so exactly two
        # grouped lines — not eleven repeated ones.
        self.assertEqual(html_out.count("quiet cycles"), 2)
        self.assertIn("18:46 – 18:52 · 6 quiet cycles — checked 0–2/18, nothing new", html_out)
        self.assertIn("18:54 – 18:56 · 3 quiet cycles — checked 2/18, nothing new", html_out)
        # The cycles that DID something still stand alone, newest first.
        self.assertIn("21:54 — checked 4/20 · 14 downloaded", html_out)
        self.assertIn("18:53 — checked 2/18 · 1 downloaded", html_out)
        self.assertLess(html_out.index("21:54"), html_out.index("18:53"))
        # A grouped line is muted; nothing was thrown away — the folded cycles
        # stay reachable behind the toggle.
        self.assertIn("run-entry--quiet", html_out)
        self.assertIn("Show all 6", html_out)
        self.assertIn("18:50 — checked 2/18", html_out)

    def test_group_folds_at_the_end_of_the_feed(self):
        state_runs = [self._quiet("18:47"), self._quiet("18:48"),
                      _cycle("2026-06-13T19:00:00Z", entries=18, checked=2, downloaded=1)]
        html_out = app.render_run_state_history(state_runs)
        self.assertIn("2 quiet cycles", html_out)
        self.assertLess(html_out.index("19:00"), html_out.index("quiet cycles"))


class RunHistoryBackCompatTest(unittest.TestCase):
    """A record written before the events/skip schema (older bot, fresh deploy,
    not-yet-redeployed bot) must render exactly as it did — no crash, no blank
    feed, no phantom "0 skipped"."""

    def test_pre_schema_record_renders_byte_for_byte_as_before(self):
        rec = {"finished_ts": "2026-06-13T19:40:00Z",
               "counts": {"entries": 12, "checked": 12, "downloaded": 2, "errors": 1}}
        self.assertEqual(app.build_run_summary(rec),
                         "19:40 — checked 12/12 · 2 downloaded · 1 error")
        self.assertEqual(
            app.render_run_state_history([rec]),
            '<div class="run-entry"><div class="event event--danger">'
            '<span class="event-msg">19:40 &mdash; checked 12/12 &middot; '
            '2 downloaded &middot; 1 error</span></div></div>'.replace(
                "&mdash;", "—").replace("&middot;", "·"))

    def test_pre_schema_records_still_group_and_carry_no_toggle(self):
        runs = [{"finished_ts": "2026-06-13T18:5%d:00Z" % n,
                 "counts": {"entries": 18, "checked": 2, "downloaded": 0, "errors": 0}}
                for n in range(3)]
        html_out = app.render_run_state_history(runs)
        self.assertIn("18:50 – 18:52 · 3 quiet cycles", html_out)
        self.assertNotIn("more detail", html_out)
        self.assertNotIn("0 skipped", html_out)


class ConfirmAttrTest(unittest.TestCase):
    """BUG-1: the inline Remove ``confirm()`` must survive names with
    apostrophes (also quotes / &). Previously the name was only HTML-escaped, so
    a raw ``'`` inside ``confirm('Remove ...')`` aborted the inline JS and Remove
    submitted with NO confirmation."""

    def _decode_arg(self, attr):
        # The attribute lives inside onclick="..." — it must carry no bare double
        # quote that would close the attribute early.
        self.assertNotIn('"', attr)
        js = html.unescape(attr)
        self.assertTrue(js.startswith("return confirm(") and js.endswith(")"))
        # The argument is a valid JS/JSON string literal.
        return json.loads(js[len("return confirm("):-1])

    def test_apostrophe_name_is_well_formed(self):
        msg = "Remove Frieren: Beyond Journey's End?"
        self.assertEqual(self._decode_arg(app.confirm_attr(msg)), msg)

    def test_double_quote_name_is_well_formed(self):
        msg = 'Remove Re:"Zero" Starting Life?'
        self.assertEqual(self._decode_arg(app.confirm_attr(msg)), msg)

    def test_ampersand_name_is_well_formed(self):
        msg = "Remove Fate & Stay?"
        self.assertEqual(self._decode_arg(app.confirm_attr(msg)), msg)

    def test_render_watchlist_apostrophe_button_is_safe(self):
        out = app.render_watchlist(
            [{"name": "Frieren: Beyond Journey's End", "url": "http://x"}])
        # The onclick now uses the entity-encoded confirm string …
        self.assertIn('onclick="return confirm(', out)
        # … and never the old single-quoted form a `'` would break.
        self.assertNotIn("confirm('Remove", out)

    def test_render_watchlist_pending_apostrophe_button_is_safe(self):
        out = app.render_watchlist(
            [], [{"name": "Frieren: Beyond Journey's End", "url": "http://x"}])
        self.assertIn('onclick="return confirm(', out)
        self.assertNotIn("confirm('Remove", out)


class RedirectMsgEncodingTest(unittest.TestCase):
    """BUG-2: status messages must be URL-encoded so a name containing
    ``& = # %`` survives the redirect round-trip. A raw ``/?msg=...`` was
    truncated at the first ``&`` (parse_qs splits the query on it)."""

    def _captured_url(self, msg):
        captured = {}
        handler = app.Handler.__new__(app.Handler)
        # Stub the low-level redirect so no socket is touched.
        handler._redirect = lambda url: captured.__setitem__("url", url)
        handler._redirect_msg(msg)
        return captured["url"]

    def test_ampersand_name_survives_roundtrip(self):
        msg = "Removed: Fate/stay night & Heaven's Feel"
        url = self._captured_url(msg)
        # The raw "& Heaven..." must NOT sit unencoded in the query.
        self.assertNotIn("& Heaven", url)
        # parse_qs (the GET side) decodes it back to the exact original.
        qs = parse_qs(urlparse(url).query)
        self.assertEqual(qs["msg"][0], msg)

    def test_special_chars_survive_roundtrip(self):
        msg = "Folder updated: A=B -> C#1 100% done"
        qs = parse_qs(urlparse(self._captured_url(msg)).query)
        self.assertEqual(qs["msg"][0], msg)

    def test_error_prefix_preserved_for_banner_class(self):
        # The GET side keys the error styling off msg.startswith("Error"), so the
        # prefix must survive the encode round-trip.
        qs = parse_qs(urlparse(self._captured_url("Error: Invalid index")).query)
        self.assertTrue(qs["msg"][0].startswith("Error"))


class ParseBotLogsStandaloneTest(unittest.TestCase):
    """Standalone [SKIP]/[THROTTLE]/[COMPLETE] lines arriving with no active run
    (between cycles) must still produce their own entry. Previously the
    `if not current_run: continue` guard short-circuited before the standalone
    handlers, so these were silently dropped whenever no run was in progress."""

    def test_skip_without_active_run_creates_entry(self):
        runs = app.parse_bot_logs(["[SKIP] Naruto already up to date"])
        self.assertEqual(len(runs), 1)
        ev = runs[0]["events"][0]
        self.assertEqual(ev["type"], "skip")
        self.assertEqual(ev["msg"], "Naruto already up to date")

    def test_throttle_without_active_run_creates_entry(self):
        runs = app.parse_bot_logs(["[THROTTLE] Rate limited, backing off"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["events"][0]["type"], "throttle")

    def test_complete_without_active_run_creates_entry(self):
        runs = app.parse_bot_logs(["[COMPLETE] All caught up"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["events"][0]["type"], "complete")

    def test_standalone_event_during_active_run_stays_separate(self):
        # A [SKIP] mid-run gets its own entry and does NOT attach to the active
        # run — unchanged from the original behavior.
        runs = app.parse_bot_logs([
            "[12:00:00] Prüfe Naruto auf updates",
            "[SKIP] Bleach already up to date",
        ])
        self.assertEqual(len(runs), 2)
        # Standalone skip entry is appended first; the active run flushes at end.
        self.assertEqual(runs[0]["events"][0]["type"], "skip")
        self.assertEqual(runs[1]["anime"], "Naruto")
        self.assertEqual(runs[1]["events"], [])


class WatchlistMutationKeyByUrlTest(unittest.TestCase):
    """UI-1: watchlist mutations must resolve their target by stable URL, not by
    array index. The ``resolve_pending`` thread pops/appends entries concurrently,
    so an index captured at page-render time can point at a *different* entry by
    the time the form is submitted (TOCTOU) — a Remove could delete the wrong
    anime. Keying off the unique URL removes the hazard."""

    def setUp(self):
        fd, self._path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._orig_ani = app.ANI_JSON
        app.ANI_JSON = self._path

    def tearDown(self):
        app.ANI_JSON = self._orig_ani
        try:
            os.remove(self._path)
        except OSError:
            pass

    def _post(self, path, params):
        captured = {}
        handler = app.Handler.__new__(app.Handler)
        handler.path = path
        handler._read_post = lambda: params
        handler._redirect_msg = lambda msg: captured.__setitem__("msg", msg)
        handler._redirect = lambda url: captured.__setitem__("url", url)
        handler._respond = lambda code, html: captured.__setitem__("html", html)
        handler.do_POST()
        return captured

    def test_remove_hits_correct_entry_after_index_shift(self):
        a = {"name": "A", "url": "http://x/a"}
        b = {"name": "B", "url": "http://x/b"}
        c = {"name": "C", "url": "http://x/c"}
        # Page rendered while the list was [A, B, C]: B sat at array index 1.
        app.save_ani({"anime": [a, b, c]})
        b_key = b["url"]
        # Then A resolves/leaves concurrently and the list shifts to [B, C].
        # An old index-1 form would now wrongly target C.
        app.save_ani({"anime": [b, c]})
        result = self._post("/remove", {"key": b_key})
        names = [e["name"] for e in app.load_ani()["anime"]]
        # B is removed (correct) — NOT C (what index 1 would have hit).
        self.assertEqual(names, ["C"])
        self.assertIn("Removed: B", result["msg"])

    def test_remove_pending_keyed_by_url(self):
        p1 = {"name": "P1", "url": "http://x/p1", "status": "pending"}
        p2 = {"name": "P2", "url": "http://x/p2", "status": "pending"}
        app.save_ani({"pending": [p1, p2]})
        self._post("/remove-pending", {"key": "http://x/p2"})
        names = [e["name"] for e in app.load_ani()["pending"]]
        self.assertEqual(names, ["P1"])

    def test_ep_add_targets_entry_by_url(self):
        a = {"name": "A", "url": "http://x/a", "episodes": 12, "missing": []}
        b = {"name": "B", "url": "http://x/b", "episodes": 12, "missing": []}
        app.save_ani({"anime": [a, b]})
        self._post("/ep-add", {"key": "http://x/b", "ep": "5"})
        by_name = {e["name"]: e for e in app.load_ani()["anime"]}
        self.assertEqual(by_name["B"]["missing"], [5])
        self.assertEqual(by_name["A"]["missing"], [])  # A untouched

    def test_unknown_url_reports_not_found(self):
        app.save_ani({"anime": [{"name": "A", "url": "http://x/a"}]})
        result = self._post("/remove", {"key": "http://x/gone"})
        self.assertEqual(len(app.load_ani()["anime"]), 1)  # nothing removed
        self.assertTrue(result["msg"].startswith("Error"))

    def test_find_entry_by_url_empty_key_does_not_match_urlless_entry(self):
        entries = [{"name": "A", "url": ""}, {"name": "B", "url": "http://x/b"}]
        # An empty/missing key must not silently match a URL-less entry.
        self.assertEqual(app.find_entry_by_url(entries, ""), (-1, None))
        self.assertEqual(app.find_entry_by_url(entries, "http://x/b"),
                         (1, entries[1]))


class ConfigUtf8Test(unittest.TestCase):
    """load_ani/load_run_state must read/write UTF-8 regardless of the
    platform's default locale encoding (cp1252 on Windows). Fixtures are
    written as explicit UTF-8 *bytes* (not via the platform-default `open`)
    so a regression to a bare `open(path, "r")`/`open(path, "w")` would
    genuinely mangle or fail these, rather than passing on any host."""

    def setUp(self):
        fd, self._ani_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        fd, self._rs_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._orig_ani = app.ANI_JSON
        self._orig_rs = app.RUN_STATE_FILE
        app.ANI_JSON = self._ani_path
        app.RUN_STATE_FILE = self._rs_path

    def tearDown(self):
        app.ANI_JSON = self._orig_ani
        app.RUN_STATE_FILE = self._orig_rs
        for path in (self._ani_path, self._rs_path):
            try:
                os.remove(path)
            except OSError:
                pass

    def test_ani_json_umlaut_title_round_trips_through_load_and_save(self):
        title = "Ü-Anime"
        fixture = {"settings": {}, "anime": [{"name": title, "url": "http://x/u"}]}
        with open(self._ani_path, "wb") as f:
            f.write(json.dumps(fixture, ensure_ascii=False, indent=4,
                                sort_keys=True).encode("utf-8"))
        loaded = app.load_ani()
        self.assertEqual(loaded["anime"][0]["name"], title)

        # Save must also round-trip cleanly (write side of the same bug).
        app.save_ani(loaded)
        with open(self._ani_path, "rb") as f:
            raw = f.read()
        self.assertEqual(json.loads(raw.decode("utf-8"))["anime"][0]["name"], title)

    def test_run_state_em_dash_detail_survives_read_intact(self):
        detail = "skip — already have"
        fixture = {"last_run": {"finished_ts": "2026-06-13T19:20:05Z", "detail": detail}}
        with open(self._rs_path, "wb") as f:
            f.write(json.dumps(fixture, ensure_ascii=False).encode("utf-8"))
        loaded = app.load_run_state()
        self.assertEqual(loaded["last_run"]["detail"], detail)

    def test_undecodable_run_state_degrades_to_empty_state(self):
        # 0x80 alone is a well-defined character under cp1252 (so a
        # platform-default `open` would silently mangle rather than fail
        # here) but is not valid standalone UTF-8 — reading it with
        # encoding="utf-8" raises UnicodeDecodeError, which must degrade to
        # {} rather than propagate as a 500.
        with open(self._rs_path, "wb") as f:
            f.write(b'{"last_run": {"detail": "\x80"}}')
        self.assertEqual(app.load_run_state(), {})


class ParseBotLogsBranchesTest(unittest.TestCase):
    """Coverage for parse_bot_logs branches beyond the BUG-3 standalone cases:
    docker-ts stripping, in-run event classification, the glued anime-name-prefix
    strip, sleep-flush, login info between runs, and raw-line skipping."""

    def test_strips_docker_timestamp_prefix(self):
        runs = app.parse_bot_logs([
            "2026-06-13T19:00:00.123456789Z [12:00:00] Prüfe Naruto auf updates",
        ])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["anime"], "Naruto")
        self.assertEqual(runs[0]["time"], "12:00:00")
        self.assertTrue(runs[0]["docker_ts"].startswith("2026-06-13T19:00:00"))

    def test_in_run_events_are_classified(self):
        runs = app.parse_bot_logs([
            "[12:00:00] Prüfe Naruto auf updates",
            "[DOWNLOAD] Episode 5 grabbed",
            "[BATCH] queued 3",
            "[INFO] note",
            "[ERROR] boom",
        ])
        self.assertEqual(len(runs), 1)
        types = [e["type"] for e in runs[0]["events"]]
        self.assertEqual(types, ["download", "batch", "info", "error"])
        self.assertEqual(runs[0]["events"][0]["msg"], "Episode 5 grabbed")

    def test_glued_anime_name_prefix_is_stripped_and_capitalized(self):
        # The bot sometimes glues the anime name to the status text. The name
        # prefix is stripped and the remainder re-capitalized.
        runs = app.parse_bot_logs([
            "[12:00:00] Prüfe Dorohedoro: Staffel 2 auf updates",
            "[INFO] Dorohedoro: Staffel 2 hat fehlende Episoden",
        ])
        self.assertEqual(runs[0]["events"][0]["msg"], "Hat fehlende Episoden")

    def test_prefix_strip_leaving_empty_msg_is_safe(self):
        # msg identical to the anime name → stripped to empty, no capitalize crash.
        runs = app.parse_bot_logs([
            "[12:00:00] Prüfe Naruto auf updates",
            "[INFO] Naruto",
        ])
        self.assertEqual(runs[0]["events"][0]["msg"], "")

    def test_sleep_line_flushes_run_and_records_sleep_event(self):
        runs = app.parse_bot_logs([
            "2026-06-13T19:00:00Z [12:00:00] Prüfe Naruto auf updates",
            "2026-06-13T19:00:05Z Schlafe 600 Sekunden",
        ])
        self.assertEqual(len(runs), 1)
        ev = runs[0]["events"][-1]
        self.assertEqual(ev["type"], "sleep")
        self.assertEqual(ev["docker_ts"], "2026-06-13T19:00:05Z")

    def test_login_info_between_runs_creates_entry(self):
        runs = app.parse_bot_logs(["Erfolgreich eingeloggt als user"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["events"][0]["type"], "info")

    def test_raw_lines_without_marker_are_skipped(self):
        runs = app.parse_bot_logs([
            "[12:00:00] Prüfe Naruto auf updates",
            "some raw api dump {json: true}",
        ])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["events"], [])

    def test_new_run_flushes_previous(self):
        runs = app.parse_bot_logs([
            "[12:00:00] Prüfe A auf updates",
            "[12:01:00] Prüfe B auf updates",
        ])
        self.assertEqual([r["anime"] for r in runs], ["A", "B"])


class GetActivityNextRunOverdueTest(unittest.TestCase):
    """get_activity's log-tail next-run math (the fallback used when no run-state
    record exists): future estimate plus the imminent/overdue branches. The
    docker status/logs are stubbed and run-state is pointed at a missing file so
    the log-tail path is exercised end-to-end."""

    def setUp(self):
        self._orig_status = app.docker.get_status
        self._orig_logs = app.docker.get_logs
        app.docker.get_status = lambda *a, **k: {"running": True}
        self._orig_rs = app.RUN_STATE_FILE
        app.RUN_STATE_FILE = os.path.join(tempfile.gettempdir(), "aniloads-no-run-state.json")
        try:
            os.remove(app.RUN_STATE_FILE)
        except OSError:
            pass

    def tearDown(self):
        app.docker.get_status = self._orig_status
        app.docker.get_logs = self._orig_logs
        app.RUN_STATE_FILE = self._orig_rs

    def _next_run(self, ago_seconds, delay=600):
        """Drive get_activity with a synthetic sleep line whose next-run lands
        `ago_seconds` in the past (negative = future)."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        last_time = now - timedelta(seconds=ago_seconds) - timedelta(seconds=delay)
        ts = last_time.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        app.docker.get_logs = lambda *a, **k: [
            "{} [12:00:00] Prüfe Naruto auf updates".format(ts),
            "{} Schlafe {} Sekunden".format(ts, delay),
        ]
        return app.get_activity()["next_run"]

    def test_future_estimate(self):
        self.assertEqual(self._next_run(-330), "~5 min")

    def test_under_one_minute(self):
        self.assertEqual(self._next_run(-30), "<1 min")

    def test_any_moment_within_one_interval(self):
        self.assertEqual(self._next_run(300, delay=600), "any moment")

    def test_overdue_minutes(self):
        self.assertEqual(self._next_run(3600, delay=600), "overdue ~60 min")

    def test_overdue_hours(self):
        self.assertEqual(self._next_run(3 * 3600, delay=600), "overdue ~3h")

    def test_overdue_days(self):
        self.assertEqual(self._next_run(2 * 86400, delay=600), "overdue ~2d")

    def test_no_sleep_line_leaves_estimate_blank(self):
        app.docker.get_logs = lambda *a, **k: ["[12:00:00] Prüfe Naruto auf updates"]
        self.assertEqual(app.get_activity()["next_run"], "")


class RenderRunHistoryLogFeedTest(unittest.TestCase):
    """The log-parsed fallback feed (no run-state records): [COMPLETE]
    suppression, empty-run skipping, and headerless standalone lines. Also the
    state-feed one-line-per-run shape."""

    def test_complete_events_suppressed_in_log_feed(self):
        runs = [{"time": "19:40", "anime": "Naruto", "events": [
            {"type": "download", "msg": "ep5"},
            {"type": "complete", "msg": "all caught up"},
        ]}]
        html_out = app.render_run_history(runs, None)
        self.assertIn("ep5", html_out)
        self.assertNotIn("all caught up", html_out)
        self.assertNotIn("Complete", html_out)

    def test_run_reduced_to_only_complete_is_dropped(self):
        # A headerless run whose only event is [COMPLETE] renders nothing.
        runs = [{"time": "", "anime": "", "events": [{"type": "complete", "msg": "x"}]}]
        self.assertEqual(app.render_run_history(runs, None), "")

    def test_standalone_skip_renders_without_header(self):
        runs = [{"time": "", "anime": "", "events": [{"type": "skip", "msg": "up to date"}]}]
        html_out = app.render_run_history(runs, None)
        self.assertIn("up to date", html_out)
        self.assertNotIn("run-header", html_out)

    def test_anime_run_renders_header_and_time(self):
        runs = [{"time": "19:40", "anime": "Naruto & Co", "events": [{"type": "download", "msg": "ep5"}]}]
        html_out = app.render_run_history(runs, None)
        self.assertIn("run-header", html_out)
        self.assertIn("Naruto &amp; Co", html_out)  # header is escaped
        self.assertIn("19:40", html_out)

    def test_state_feed_is_one_line_per_run(self):
        state_runs = [
            {"finished_ts": "2026-06-13T19:30:00Z", "counts": {"entries": 3, "checked": 3}},
            {"finished_ts": "2026-06-13T19:40:00Z", "counts": {"entries": 3, "checked": 3, "downloaded": 1}},
        ]
        html_out = app.render_run_history([], state_runs)
        self.assertEqual(html_out.count("run-entry"), 2)


class RenderActivityFallbackTest(unittest.TestCase):
    """render_activity branches the run-state test does not reach: the log-parsed
    last-run line, the status dot, the next-run dash fallback, and the
    docker-unavailable / no-runs empty states."""

    def test_log_parsed_last_run_with_anime_and_dash_next(self):
        act = {"status": {"running": True},
               "last_run": {"time": "19:40", "anime": "Naruto & Co"},
               "next_run": ""}
        status_html, last_html, next_html = app.render_activity(act)
        self.assertIn("Running", status_html)
        self.assertIn("status-dot running", status_html)
        self.assertIn("19:40", last_html)
        self.assertIn("Naruto &amp; Co", last_html)  # escaped
        self.assertIn("&mdash;", next_html)  # empty next_run → dash fallback

    def test_stopped_status_and_explicit_next(self):
        act = {"status": {"running": False}, "last_run": None, "next_run": "~5 min"}
        status_html, _last, next_html = app.render_activity(act)
        self.assertIn("Stopped", status_html)
        self.assertIn("status-dot stopped", status_html)
        self.assertEqual(next_html, "~5 min")

    def test_no_runs_yet_when_docker_available(self):
        orig = app.docker.available
        app.docker.available = True
        try:
            _s, last_html, _n = app.render_activity(
                {"status": {}, "last_run": None, "next_run": ""})
            self.assertIn("No runs yet", last_html)
        finally:
            app.docker.available = orig

    def test_docker_unavailable_message(self):
        orig = app.docker.available
        app.docker.available = False
        try:
            _s, last_html, _n = app.render_activity(
                {"status": {}, "last_run": None, "next_run": ""})
            self.assertIn("Docker socket unavailable", last_html)
        finally:
            app.docker.available = orig


class RenderMoveStatusTest(unittest.TestCase):
    """render_move_status: running vs idle dot, last-run time, and the
    dir-not-mounted / not-yet empty states."""

    def setUp(self):
        self._orig_running = app._move_running
        self._orig_last = app._move_last_run
        self._orig_dl = app.DOWNLOAD_DIR

    def tearDown(self):
        app._move_running = self._orig_running
        app._move_last_run = self._orig_last
        app.DOWNLOAD_DIR = self._orig_dl

    def test_running_state(self):
        app._move_running = True
        status_html, _last = app.render_move_status()
        self.assertIn("Running", status_html)
        self.assertIn("status-dot running", status_html)

    def test_idle_with_last_run_time(self):
        app._move_running = False
        app._move_last_run = datetime(2026, 6, 13, 19, 20, 5)
        status_html, last_html = app.render_move_status()
        self.assertIn("Idle", status_html)
        self.assertEqual(last_html, "19:20:05")

    def test_download_dir_not_mounted(self):
        app._move_running = False
        app._move_last_run = None
        app.DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "aniloads-no-such-dl")
        _s, last_html = app.render_move_status()
        self.assertIn("Download dir not mounted", last_html)

    def test_not_yet_when_dir_present(self):
        app._move_running = False
        app._move_last_run = None
        d = tempfile.mkdtemp(prefix="aniloads-dl-")
        app.DOWNLOAD_DIR = d
        try:
            _s, last_html = app.render_move_status()
            self.assertIn("Not yet", last_html)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class RenderMoveHistoryTest(unittest.TestCase):
    """render_move_history: empty states (dir present / not mounted) and the
    newest-first escaped event feed."""

    def setUp(self):
        self._orig_hist = list(app._move_history)
        self._orig_dl = app.DOWNLOAD_DIR
        app._move_history.clear()

    def tearDown(self):
        app._move_history.clear()
        app._move_history.extend(self._orig_hist)
        app.DOWNLOAD_DIR = self._orig_dl

    def test_empty_with_dir_present(self):
        d = tempfile.mkdtemp(prefix="aniloads-dl-")
        app.DOWNLOAD_DIR = d
        try:
            self.assertIn("No move activity yet", app.render_move_history())
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_empty_without_dir(self):
        app.DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "aniloads-no-such-dl-2")
        self.assertIn("Download directory not mounted", app.render_move_history())

    def test_renders_events_newest_first_and_escapes(self):
        app._move_history.append({"type": "moved", "msg": "A & B → X"})
        app._move_history.append({"type": "error", "msg": "boom"})
        html_out = app.render_move_history()
        # newest first → the error (appended last) renders before the move.
        self.assertLess(html_out.index("boom"), html_out.index("A &amp; B"))
        self.assertIn("event--danger", html_out)
        self.assertIn("event--ok", html_out)


class RunMoveCycleTest(unittest.TestCase):
    """run_move_cycle: movie vs series routing, tvdb_season override,
    episode_offset rename, archive cleanup, and the safety/skip branches.
    Exercises real filesystem fixtures in a sandbox; touches no Docker/network."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aniloads-move-")
        self.download = os.path.join(self.tmp, "downloads")
        self.media = os.path.join(self.tmp, "media")
        self.movies = os.path.join(self.tmp, "movies")
        for d in (self.download, self.media, self.movies):
            os.makedirs(d)
        self.ani_path = os.path.join(self.tmp, "ani.json")

        self._orig = {k: getattr(app, k) for k in
                      ("DOWNLOAD_DIR", "MEDIA_DIR", "MOVIE_MEDIA_DIR",
                       "MIN_AGE_MINUTES", "ANI_JSON")}
        app.DOWNLOAD_DIR = self.download
        app.MEDIA_DIR = self.media
        app.MOVIE_MEDIA_DIR = self.movies
        app.MIN_AGE_MINUTES = 5
        app.ANI_JSON = self.ani_path
        self._write_ani([])

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(app, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_ani(self, anime):
        with open(self.ani_path, "w", encoding="utf-8") as f:
            json.dump({"anime": anime}, f)

    def _make_dl(self, dirname, files, old=True):
        """Create a download subdir with the given files, back-dated by default
        so the MIN_AGE 'still being modified' guard does not trip."""
        d = os.path.join(self.download, dirname)
        os.makedirs(d, exist_ok=True)
        for name in files:
            p = os.path.join(d, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write("x")
            if old:
                past = time.time() - 3600
                os.utime(p, (past, past))
        return d

    def _types(self, events):
        return [e["type"] for e in events]

    def test_missing_download_dir_returns_empty(self):
        app.DOWNLOAD_DIR = os.path.join(self.tmp, "does-not-exist")
        self.assertEqual(app.run_move_cycle(), [])

    def test_series_routes_into_anime_season_folder(self):
        self._write_ani([{"name": "Naruto", "media_type": "series"}])
        self._make_dl("Naruto.S01", ["Naruto.S01E05.mkv"])
        events = app.run_move_cycle()
        self.assertIn("moved", self._types(events))
        self.assertTrue(os.path.isfile(
            os.path.join(self.media, "Naruto", "S01", "Naruto.S01E05.mkv")))
        # emptied source download dir is pruned
        self.assertFalse(os.path.isdir(os.path.join(self.download, "Naruto.S01")))

    def test_tvdb_season_override_changes_season_dir(self):
        self._write_ani([{"name": "Bleach", "media_type": "series", "tvdb_season": 2}])
        self._make_dl("Bleach.S01", ["Bleach.S01E05.mkv"])
        app.run_move_cycle()
        # filename keeps its S01 token; only the season folder is overridden to S02.
        self.assertTrue(os.path.isfile(
            os.path.join(self.media, "Bleach", "S02", "Bleach.S01E05.mkv")))

    def test_episode_offset_renames_episode_in_filename(self):
        self._write_ani([{"name": "Bleach", "media_type": "series", "episode_offset": 12}])
        self._make_dl("Bleach.S01", ["Bleach.S01E05.mkv"])
        app.run_move_cycle()
        self.assertTrue(os.path.isfile(
            os.path.join(self.media, "Bleach", "S01", "Bleach.S01E17.mkv")))

    def test_movie_single_file_renamed_to_folder(self):
        self._write_ani([{"name": "Akira", "media_type": "movie", "year": 1988}])
        self._make_dl("Akira.1988.1080p", ["Akira.1988.1080p.mkv"])
        events = app.run_move_cycle()
        self.assertIn("moved", self._types(events))
        self.assertTrue(os.path.isfile(
            os.path.join(self.movies, "Akira (1988)", "Akira (1988).mkv")))

    def test_movie_multiple_videos_keep_original_names(self):
        self._write_ani([{"name": "Akira", "media_type": "movie", "year": 1988}])
        self._make_dl("Akira.1988", ["Akira.1988.mkv", "Akira.extras.mkv"])
        app.run_move_cycle()
        folder = os.path.join(self.movies, "Akira (1988)")
        self.assertTrue(os.path.isfile(os.path.join(folder, "Akira.1988.mkv")))
        self.assertTrue(os.path.isfile(os.path.join(folder, "Akira.extras.mkv")))

    def test_archives_removed_alongside_extracted_video(self):
        self._write_ani([{"name": "Naruto", "media_type": "series"}])
        self._make_dl("Naruto.S01", ["Naruto.S01E05.mkv", "part1.rar", "part2.r00"])
        events = app.run_move_cycle()
        self.assertIn("cleanup", self._types(events))
        self.assertIn("moved", self._types(events))
        self.assertTrue(os.path.isfile(
            os.path.join(self.media, "Naruto", "S01", "Naruto.S01E05.mkv")))

    def test_archives_present_no_video_waits(self):
        self._write_ani([{"name": "Naruto", "media_type": "series"}])
        self._make_dl("Naruto.S01", ["part1.rar"])
        events = app.run_move_cycle()
        self.assertEqual(self._types(events), ["wait"])
        self.assertIn("archives present", events[0]["msg"])
        # nothing moved; the archive is left in place for the next cycle.
        self.assertTrue(os.path.isfile(os.path.join(self.download, "Naruto.S01", "part1.rar")))

    def test_recently_modified_files_wait(self):
        self._write_ani([{"name": "Naruto", "media_type": "series"}])
        self._make_dl("Naruto.S01", ["Naruto.S01E05.mkv"], old=False)
        events = app.run_move_cycle()
        self.assertEqual(self._types(events), ["wait"])
        self.assertIn("still being modified", events[0]["msg"])

    def test_partial_downloads_wait(self):
        self._write_ani([{"name": "Naruto", "media_type": "series"}])
        self._make_dl("Naruto.S01", ["Naruto.S01E05.mkv", "Naruto.S01E06.mkv.part"])
        events = app.run_move_cycle()
        self.assertEqual(self._types(events), ["wait"])
        self.assertIn("incomplete downloads", events[0]["msg"])

    def test_existing_target_is_skipped_not_overwritten(self):
        self._write_ani([{"name": "Naruto", "media_type": "series"}])
        dest_dir = os.path.join(self.media, "Naruto", "S01")
        os.makedirs(dest_dir)
        with open(os.path.join(dest_dir, "Naruto.S01E05.mkv"), "w") as f:
            f.write("existing")
        self._make_dl("Naruto.S01", ["Naruto.S01E05.mkv"])
        events = app.run_move_cycle()
        self.assertIn("skip", self._types(events))
        with open(os.path.join(dest_dir, "Naruto.S01E05.mkv")) as f:
            self.assertEqual(f.read(), "existing")

    def test_unparseable_series_filename_errors(self):
        self._write_ani([{"name": "Akira", "media_type": "series"}])
        self._make_dl("Akira", ["Akira.Movie.1080p.mkv"])
        events = app.run_move_cycle()
        self.assertIn("error", self._types(events))
        err = [e for e in events if e["type"] == "error"][0]
        self.assertIn("Cannot parse", err["msg"])

    def test_leftover_nonvideo_files_removed_and_empty_dir_pruned(self):
        self._write_ani([{"name": "Naruto", "media_type": "series"}])
        self._make_dl("Naruto.S01", ["Naruto.S01E05.mkv", "Naruto.nfo", "poster.jpg"])
        app.run_move_cycle()
        self.assertFalse(os.path.isdir(os.path.join(self.download, "Naruto.S01")))
        self.assertTrue(os.path.isfile(
            os.path.join(self.media, "Naruto", "S01", "Naruto.S01E05.mkv")))


class HandlerPostRoutingTest(unittest.TestCase):
    """Light do_POST integration coverage for the non-network routes, using the
    same __new__/stub harness as WatchlistMutationKeyByUrlTest."""

    def setUp(self):
        fd, self._prefs = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._orig_prefs = app.PREFS_FILE
        app.PREFS_FILE = self._prefs

    def tearDown(self):
        app.PREFS_FILE = self._orig_prefs
        try:
            os.remove(self._prefs)
        except OSError:
            pass
        app._move_trigger.clear()

    def _post(self, path, params):
        captured = {}
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h._read_post = lambda: params
        h._redirect_msg = lambda msg: captured.__setitem__("msg", msg)
        h._redirect = lambda url: captured.__setitem__("url", url)
        h._respond = lambda code, html_body: captured.__setitem__("html", html_body)
        h.do_POST()
        return captured

    def test_save_prefs_persists_and_redirects(self):
        result = self._post("/save-prefs", {
            "audio_language": "japanese", "sub_language": "english",
            "min_resolution": "720", "auto_select": "on"})
        self.assertEqual(result["msg"], "Preferences saved")
        prefs = app.load_prefs()
        self.assertEqual(prefs["audio_language"], "japanese")
        self.assertEqual(prefs["min_resolution"], 720)
        self.assertTrue(prefs["auto_select"])

    def test_save_prefs_auto_select_unchecked_defaults_false(self):
        self._post("/save-prefs", {"min_resolution": "1080"})
        self.assertFalse(app.load_prefs()["auto_select"])

    def test_move_now_sets_trigger(self):
        app._move_trigger.clear()
        result = self._post("/move-now", {})
        self.assertTrue(app._move_trigger.is_set())
        self.assertEqual(result["msg"], "Move cycle triggered")

    def test_add_url_rejects_non_site_url(self):
        result = self._post("/add-url", {"url": "http://evil.example/x"})
        self.assertTrue(result["msg"].startswith("Error: Invalid URL"))

    def test_search_empty_query_errors(self):
        result = self._post("/search", {"q": "  "})
        self.assertTrue(result["msg"].startswith("Error: Empty search"))


class HandlerGetMsgBannerTest(unittest.TestCase):
    """do_GET's status-banner branch keys the banner CSS class off whether the
    msg starts with 'Error'. render_page is stubbed so no full page is built."""

    def _get(self, path):
        captured = {}
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h._respond = lambda code, html_body: captured.__setitem__("resp", (code, html_body))
        orig = app.render_page
        app.render_page = lambda **kw: kw.get("status", "")
        try:
            h.do_GET()
        finally:
            app.render_page = orig
        return captured["resp"]

    def test_ok_msg_uses_ok_class(self):
        code, html_out = self._get("/?msg=" + quote("Removed: X"))
        self.assertEqual(code, 200)
        self.assertIn("status-ok", html_out)
        self.assertIn("Removed: X", html_out)

    def test_error_msg_uses_err_class(self):
        _code, html_out = self._get("/?msg=" + quote("Error: nope"))
        self.assertIn("status-err", html_out)


if __name__ == "__main__":
    unittest.main()
