"""Tests for the pure-logic functions in web/app.py."""

import base64
import collections
import contextlib
import html
import http.client
import json
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
import zoneinfo
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs, quote

import support

app = support.load_app()


def setUpModule():
    # Render-time conversion uses the process's zone by default. The
    # expectations below are written for TZ=UTC, so pin that rather than lean
    # on the host's zone; the local-zone tests set their own.
    app._display_tz = timezone.utc


def tearDownModule():
    app._display_tz = None


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


class RenameSeasonEpisodeTest(unittest.TestCase):
    def test_rewrites_season_and_episode_preserving_width(self):
        self.assertEqual(
            app._rename_season_episode("Anime.Name.S01E05.mkv", 2, 5),
            "Anime.Name.S02E05.mkv",
        )

    def test_preserves_three_digit_episode_width(self):
        self.assertEqual(
            app._rename_season_episode("Long.S01E001.mkv", 1, 6),
            "Long.S01E006.mkv",
        )

    def test_minimum_width_of_two_for_single_digit_season(self):
        self.assertEqual(
            app._rename_season_episode("Show.S1E1.mkv", 3, 4),
            "Show.S03E04.mkv",
        )

    def test_only_touches_matched_span_leaving_other_tokens_alone(self):
        self.assertEqual(
            app._rename_season_episode("Show.E05.Bonus.S01E05.mkv", 1, 17),
            "Show.E05.Bonus.S01E17.mkv",
        )

    def test_returns_filename_unchanged_when_no_match(self):
        self.assertEqual(
            app._rename_season_episode("Anime Movie 1080p.mkv", 2, 5),
            "Anime Movie 1080p.mkv",
        )


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

    def test_legacy_hostile_custompackage_is_sanitized_flat(self):
        # A customPackage saved before save-time sanitization existed
        # (hand-edited ani.json, or an entry from before this fix) must still
        # come back as a single, contained path segment.
        anime = [{"name": "Bleach", "customPackage": "a/b", "tvdb_season": 1}]
        res = app.match_anime_entry("xxx", "Bleach.S01E01.1080p", anime)
        self.assertEqual(res["folder_name"], "ab")

    def test_legacy_traversal_custompackage_is_sanitized(self):
        anime = [{"name": "Bleach", "customPackage": "../../etc"}]
        res = app.match_anime_entry("xxx", "Bleach.S01E01.1080p", anime)
        self.assertEqual(res["folder_name"], "....etc")
        self.assertNotIn("/", res["folder_name"])

    def test_custompackage_with_colon_and_question_mark_unchanged(self):
        # These are legal on the Linux media filesystem and common in
        # anime-loads release names — must round-trip unchanged so an
        # existing library folder of the same name keeps matching (a real
        # regression: stripping them would split an existing show into a
        # second, differently-named folder).
        anime = [{"name": "ReZERO", "customPackage":
                  "Re:ZERO -Starting Life in Another World-"}]
        res = app.match_anime_entry("xxx", "ReZERO.S01E01.1080p", anime)
        self.assertEqual(res["folder_name"], "Re:ZERO -Starting Life in Another World-")

        anime2 = [{"name": "DanMachi", "customPackage":
                   "Is It Wrong to Try to Pick Up Girls in a Dungeon?"}]
        res2 = app.match_anime_entry("xxx", "DanMachi.S01E01.1080p", anime2)
        self.assertEqual(res2["folder_name"],
                          "Is It Wrong to Try to Pick Up Girls in a Dungeon?")


class SafeFolderSegmentTest(unittest.TestCase):
    """_safe_folder_segment is the choke point for every watchlist folder
    name — save paths (/update-folder, /add-release) and the mover's legacy
    hand-edited-entry path (_entry_to_match) all route through it. Unlike
    _sanitize_folder (Plex movie naming), it only strips what could let a
    name escape or nest a path — it must leave ``: ? * " < > |`` alone since
    those are legal on the actual (Linux) media filesystem and common in
    anime-loads release titles."""

    def test_strips_path_separators(self):
        self.assertEqual(app._safe_folder_segment("../../etc"), "....etc")
        self.assertEqual(app._safe_folder_segment("C:\\x"), "C:x")

    def test_slash_separated_title_flattens_to_one_segment(self):
        # Documented mapping: "Fate/stay night" -> "Fatestay night" — the
        # words merge (no separator inserted) because separators are
        # deleted, not replaced.
        self.assertEqual(app._safe_folder_segment("Fate/stay night"), "Fatestay night")

    def test_bare_dot_segments_rejected_as_empty(self):
        self.assertEqual(app._safe_folder_segment(".."), "")
        self.assertEqual(app._safe_folder_segment("."), "")
        self.assertEqual(app._safe_folder_segment("...."), "")

    def test_whitespace_only_rejected_as_empty(self):
        self.assertEqual(app._safe_folder_segment("   "), "")

    def test_control_chars_stripped(self):
        self.assertEqual(app._safe_folder_segment("A\x00B\x1f"), "AB")

    def test_normal_names_unchanged(self):
        self.assertEqual(app._safe_folder_segment("Fate & Zero's Rebellion"),
                          "Fate & Zero's Rebellion")
        self.assertEqual(app._safe_folder_segment("\u30c9\u30e9\u30b4\u30f3\u30dc\u30fc\u30eb"),
                          "\u30c9\u30e9\u30b4\u30f3\u30dc\u30fc\u30eb")

    def test_leading_dot_title_unaffected(self):
        # Real anime titles starting with a dot (".hack//SIGN") must not be
        # treated as a dot-only traversal segment — only a segment that is
        # ENTIRELY dots is rejected.
        self.assertEqual(app._safe_folder_segment(".hack"), ".hack")

    def test_colon_question_mark_and_other_windows_illegal_chars_preserved(self):
        # These are legal on the actual (Linux) media filesystem and common
        # in anime-loads release names — must NOT be stripped here (that's
        # _sanitize_folder's job, for Plex movie naming only).
        self.assertEqual(
            app._safe_folder_segment("Re:ZERO -Starting Life in Another World-"),
            "Re:ZERO -Starting Life in Another World-")
        self.assertEqual(
            app._safe_folder_segment("Is It Wrong to Try to Pick Up Girls in a Dungeon?"),
            "Is It Wrong to Try to Pick Up Girls in a Dungeon?")

    def test_movie_target_name_still_uses_sanitize_folder_unchanged(self):
        # _sanitize_folder (movie path) is untouched by this fix — it still
        # deletes Windows-illegal characters, including ':' and '?'.
        self.assertEqual(app._movie_target_name("Re:ZERO?", 2016), "ReZERO (2016)")


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


class SearchAnimeRealResultTest(unittest.TestCase):
    """Regression test for the dashboard's "search by name" add path: it was
    silently returning zero results because search_anime() called a method
    name (getURL) that only exists on animeloads.anime, not on the
    searchResult objects al.search() actually returns (getUrl). Drive
    search_anime() with REAL bot/animeloads.py searchResult instances (never
    a mock, which would hide a wrong method name) via a fake AL.search()."""

    def setUp(self):
        animeloads_mod = support.load_animeloads()
        self._searchResult = animeloads_mod.searchResult

        class FakeAL:
            def __init__(fake_self, *a, **k):
                pass

        self._orig_al_available = app.AL_AVAILABLE
        self._orig_al = getattr(app, "AL", None)
        app.AL_AVAILABLE = True
        app.AL = FakeAL
        app.AL.FIREFOX = "firefox"

    def tearDown(self):
        app.AL_AVAILABLE = self._orig_al_available
        app.AL = self._orig_al

    def _real_result(self, name, dubLang, subLang):
        return self._searchResult(
            "https://www.anime-loads.org/media/1-" + name, name, "series",
            "2020", "1", "12", dubLang, subLang, "Action", None, None)

    def test_real_results_are_not_dropped(self):
        results = [self._real_result("Show One", ["German"], ["English"])]
        app.AL.search = lambda self, query: results

        out, err = app.search_anime("show one")

        self.assertIsNone(err)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "Show One")
        self.assertEqual(out[0]["url"], "https://www.anime-loads.org/media/1-Show One")
        self.assertEqual(out[0]["dubs"], "German")
        self.assertEqual(out[0]["subs"], "English")

    def test_a_result_that_still_errors_is_logged_not_swallowed(self):
        class Broken:
            def getName(broken_self):
                raise RuntimeError("boom")

        app.AL.search = lambda self, query: [Broken()]

        with self.assertLogs("anime-web", level="WARNING") as log_ctx:
            out, err = app.search_anime("broken")

        self.assertIsNone(err)
        self.assertEqual(out, [])
        self.assertTrue(any("search_anime" in msg for msg in log_ctx.output))


class RenderSearchResultsLangTest(unittest.TestCase):
    def test_dub_and_sub_shown_when_present(self):
        out = app.render_search_results([{
            "name": "Show", "url": "http://x", "type": "series",
            "episodes": "1/12", "genre": "Action",
            "dubs": "German", "subs": "English",
        }])
        self.assertIn("Dub: German", out)
        self.assertIn("Sub: English", out)

    def test_lang_line_omitted_when_no_data(self):
        out = app.render_search_results([{
            "name": "Show", "url": "http://x", "type": "series",
            "episodes": "1/12", "genre": "Action",
            "dubs": "", "subs": "",
        }])
        self.assertNotIn("Dub:", out)
        self.assertNotIn("Sub:", out)

    def test_lang_values_are_html_escaped(self):
        out = app.render_search_results([{
            "name": "Show", "url": "http://x", "type": "series",
            "episodes": "1/12", "genre": "Action",
            "dubs": "Ger&man", "subs": "",
        }])
        self.assertIn("Dub: Ger&amp;man", out)


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
    """OK episodes render as compact ranges, never one row per episode; only
    retrying episodes get their own row (UI-2)."""

    def test_long_series_does_not_emit_all_ok_rows(self):
        out = app.render_watchlist([{"name": "Long", "episodes": 500, "missing": [7]}])
        self.assertIn("1–6, 8–500", out)
        self.assertNotIn("Ep 250", out)
        # Retrying episodes are always rendered up front.
        self.assertIn("Ep 7", out)
        self.assertIn("badge-retry", out)

    def test_all_ok_series_has_no_retry_rows(self):
        out = app.render_watchlist([{"name": "Clean", "episodes": 3, "missing": []}])
        self.assertIn("1–3 OK", out)
        self.assertNotIn("badge-retry", out)


class RenderWatchlistOffsetBadgeTest(unittest.TestCase):
    """The TVDB episode-offset badge must show a signed number — a negative
    offset used to render as e.g. 'Offset +-2' (the template hard-coded a '+'
    in front of a value that could itself already be negative)."""

    def test_positive_offset_shows_plus(self):
        out = app.render_watchlist([{
            "name": "A", "url": "http://x/a", "tvdb_id": 1, "episode_offset": 12}])
        self.assertIn("Offset +12", out)

    def test_negative_offset_shows_single_minus(self):
        out = app.render_watchlist([{
            "name": "A", "url": "http://x/a", "tvdb_id": 1, "episode_offset": -2}])
        self.assertIn("Offset -2", out)
        self.assertNotIn("+-2", out)

    def test_zero_offset_has_no_badge(self):
        out = app.render_watchlist([{
            "name": "A", "url": "http://x/a", "tvdb_id": 1, "episode_offset": 0}])
        self.assertNotIn("Offset", out)


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
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        next_ts = _iso(now + timedelta(seconds=330))
        out = app.format_next_run_display({"next_run_ts": next_ts, "timedelay": 600}, now=now)
        self.assertIn("(in ~5 min)", out)

    def test_next_run_shows_absolute_local_time(self):
        now = datetime(2026, 6, 13, 19, 0, 0)
        next_ts = _iso(now + timedelta(seconds=330))
        out = app.format_next_run_display({"next_run_ts": next_ts, "timedelay": 600}, now=now)
        self.assertEqual(out, "19:05 (in ~5 min)")

    def test_next_run_overdue_is_not_wrapped_in_in(self):
        now = datetime(2026, 6, 13, 19, 0, 0)
        next_ts = _iso(now - timedelta(minutes=60))
        out = app.format_next_run_display({"next_run_ts": next_ts, "timedelay": 600}, now=now)
        self.assertEqual(out, "18:00 (overdue ~60 min)")

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
        self.assertIn("(in ~5 min)", act["next_run"])
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
        # The cycle's own markup is unchanged; it now sits under its day header.
        self.assertEqual(
            app.render_run_state_history([rec], now=datetime(2026, 6, 13, 22, 0)),
            '<div class="run-day">Today</div>'
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


# Thu 10 Sep 2026, 12:00 UTC — the pinned "now" for the day-grouping tests.
_NOW = datetime(2026, 9, 10, 12, 0)

# Europe/Berlin in summer, as a fixed offset so the cross-midnight tests need
# no tz database; the DST test uses the real zone when the host has one.
_CEST = timezone(timedelta(hours=2), "CEST")


@contextlib.contextmanager
def _display_zone(tz):
    """Render as if the process ran under ``tz`` (what TZ=... gives libc)."""
    saved = app._display_tz
    app._display_tz = tz
    try:
        yield
    finally:
        app._display_tz = saved


def _berlin():
    try:
        return zoneinfo.ZoneInfo("Europe/Berlin")
    except zoneinfo.ZoneInfoNotFoundError:
        return None


class RunHistoryDayGroupingTest(unittest.TestCase):
    """The bot cycles about once a day, so a bare HH:MM made last Tuesday's
    line indistinguishable from this morning's. Lines now sit under a header
    per calendar day (UTC, the zone the times are printed in)."""

    def _quiet(self, ts, checked=2):
        return _cycle(ts, entries=18, checked=checked, downloaded=0, errors=0)

    def test_day_labels(self):
        self.assertEqual(app.format_day(date(2026, 9, 10), _NOW), "Today")
        self.assertEqual(app.format_day(date(2026, 9, 9), _NOW), "Yesterday")
        self.assertEqual(app.format_day(date(2026, 9, 8), _NOW), "Tue 8 Sep")
        # Only another year's date spells the year out.
        self.assertEqual(app.format_day(date(2025, 9, 8), _NOW), "Mon 8 Sep 2025")

    def test_feed_groups_lines_under_day_headers_newest_first(self):
        state_runs = [  # oldest first, as run_state.json stores them
            _cycle("2026-09-08T18:44:00Z", entries=18, checked=3, downloaded=1),
            _cycle("2026-09-09T18:45:00Z", entries=18, checked=2, errors=1),
            _cycle("2026-09-10T06:10:00Z", entries=18, checked=4, downloaded=2),
        ]
        html_out = app.render_run_state_history(state_runs, now=_NOW)
        self.assertEqual(html_out.count('class="run-day"'), 3)
        today = html_out.index(">Today<")
        yesterday = html_out.index(">Yesterday<")
        older = html_out.index(">Tue 8 Sep<")
        self.assertLess(today, yesterday)
        self.assertLess(yesterday, older)
        # Each line keeps its compact HH:MM and sits under its own day.
        self.assertLess(today, html_out.index("06:10 — checked 4/18"))
        self.assertLess(html_out.index("06:10 — checked 4/18"), yesterday)
        self.assertLess(yesterday, html_out.index("18:45 — checked 2/18"))
        self.assertLess(html_out.index("18:45 — checked 2/18"), older)
        self.assertLess(older, html_out.index("18:44 — checked 3/18"))

    def test_one_header_per_day_not_per_line(self):
        state_runs = [_cycle("2026-09-10T0%d:00:00Z" % h, entries=5, checked=5, downloaded=1)
                      for h in (1, 2, 3)]
        html_out = app.render_run_state_history(state_runs, now=_NOW)
        self.assertEqual(html_out.count('class="run-day"'), 1)
        self.assertTrue(html_out.startswith('<div class="run-day">Today</div>'))

    def test_quiet_fold_splits_at_midnight(self):
        state_runs = [self._quiet("2026-09-08T23:50:00Z"), self._quiet("2026-09-08T23:55:00Z"),
                      self._quiet("2026-09-09T00:05:00Z"), self._quiet("2026-09-09T00:10:00Z")]
        html_out = app.render_run_state_history(state_runs, now=_NOW)
        # Two folds, one per day, each under its own header — never one span
        # reading "23:50 – 00:10".
        self.assertEqual(html_out.count("quiet cycles"), 2)
        self.assertIn("00:05 – 00:10 · 2 quiet cycles", html_out)
        self.assertIn("23:50 – 23:55 · 2 quiet cycles", html_out)
        self.assertNotIn("23:50 – 00:10", html_out)
        self.assertLess(html_out.index(">Yesterday<"), html_out.index("00:05 – 00:10"))
        self.assertLess(html_out.index("00:05 – 00:10"), html_out.index(">Tue 8 Sep<"))
        self.assertLess(html_out.index(">Tue 8 Sep<"), html_out.index("23:50 – 23:55"))
        # Both folds still expand to their own cycles.
        self.assertEqual(html_out.count("Show all 2"), 2)

    def test_a_lone_quiet_cycle_either_side_of_midnight_is_not_folded(self):
        state_runs = [self._quiet("2026-09-08T23:50:00Z"), self._quiet("2026-09-09T00:10:00Z")]
        html_out = app.render_run_state_history(state_runs, now=_NOW)
        self.assertNotIn("quiet cycles", html_out)
        self.assertIn("00:10 — checked 2/18", html_out)
        self.assertIn("23:50 — checked 2/18", html_out)

    def test_group_summary_names_days_when_handed_a_multi_day_span(self):
        group = [self._quiet("2026-09-09T00:10:00Z"), self._quiet("2026-09-08T23:50:00Z")]
        self.assertEqual(app.build_quiet_group_summary(group, _NOW),
                         "Tue 8 Sep 23:50 – Yesterday 00:10 · 2 quiet cycles"
                         " — checked 2/18, nothing new")

    def test_garbage_timestamps_still_render_without_a_header(self):
        state_runs = [
            {"finished_ts": "not-a-date", "counts": {"entries": 4, "checked": 4, "downloaded": 1}},
            {"finished_ts": None, "counts": {"entries": 4, "checked": 1}},
            {"counts": {"entries": 4, "checked": 2}},
        ]
        html_out = app.render_run_state_history(state_runs, now=_NOW)
        self.assertNotIn("run-day", html_out)
        self.assertIn("checked 4/4 · 1 downloaded", html_out)

    def test_undated_record_stays_under_the_current_header(self):
        state_runs = [_cycle("2026-09-09T18:00:00Z", entries=4, checked=4, downloaded=1),
                      {"finished_ts": "garbage", "counts": {"entries": 4, "checked": 3, "downloaded": 1}},
                      _cycle("2026-09-10T08:00:00Z", entries=4, checked=4, downloaded=2)]
        html_out = app.render_run_state_history(state_runs, now=_NOW)
        self.assertEqual(html_out.count('class="run-day"'), 2)
        self.assertLess(html_out.index("checked 3/4"), html_out.index(">Yesterday<"))

    def test_series_names_stay_escaped_under_a_day_header(self):
        rec = _cycle("2026-09-09T18:00:00Z", entries=4, checked=4, downloaded=1)
        rec["events"] = [{"kind": "download", "anime": "Fate/stay night & Heaven's Feel",
                          "episodes": [3]}]
        html_out = app.render_run_state_history([rec], now=_NOW)
        self.assertIn(">Yesterday<", html_out)
        self.assertIn("Fate/stay night &amp; Heaven&#x27;s Feel", html_out)
        self.assertNotIn("Heaven's", html_out)

    def test_last_run_stat_carries_its_day_when_not_today(self):
        state_last = {"finished_ts": "2026-09-10T06:10:05Z", "counts": {"entries": 8, "checked": 5}}
        self.assertEqual(app.format_last_run_display(state_last, now=_NOW),
                         "06:10:05 &mdash; checked 5/8")
        state_last["finished_ts"] = "2026-09-09T18:44:05Z"
        self.assertEqual(app.format_last_run_display(state_last, now=_NOW),
                         "Yesterday 18:44:05 &mdash; checked 5/8")
        state_last["finished_ts"] = "2026-09-08T18:44:05Z"
        self.assertEqual(app.format_last_run_display(state_last, now=_NOW),
                         "Tue 8 Sep 18:44:05 &mdash; checked 5/8")


class LogFeedDayGroupingTest(unittest.TestCase):
    """The log-fallback feed has only the bot's [HH:MM:SS]; the date comes from
    the Docker RFC3339 prefix parse_bot_logs() keeps as docker_ts."""

    def test_docker_prefix_dates_carry_into_day_headers(self):
        raw = [
            "2026-09-08T18:44:00.123456789Z [18:44:00] Prüfe Naruto & Co auf updates",
            "2026-09-08T18:44:05.000000000Z [DOWNLOAD] Naruto & Co ep5",
            "2026-09-10T06:10:00.000000000Z [06:10:00] Prüfe Frieren auf updates",
            "2026-09-10T06:10:04.000000000Z [DOWNLOAD] Frieren ep2",
        ]
        html_out = app.render_run_history(app.parse_bot_logs(raw), None, now=_NOW)
        self.assertEqual(html_out.count('class="run-day"'), 2)
        self.assertLess(html_out.index(">Today<"), html_out.index("Frieren"))
        self.assertLess(html_out.index("Frieren"), html_out.index(">Tue 8 Sep<"))
        self.assertLess(html_out.index(">Tue 8 Sep<"), html_out.index("Naruto &amp; Co"))
        self.assertIn('<span class="run-time">18:44:00</span>', html_out)

    def test_no_docker_timestamp_means_no_header(self):
        runs = [{"time": "19:40", "anime": "Naruto", "events": [{"type": "download", "msg": "ep5"}]}]
        html_out = app.render_run_history(runs, None, now=_NOW)
        self.assertIn("Naruto", html_out)
        self.assertNotIn("run-day", html_out)

    def test_headers_follow_the_local_clock_not_docker_utc(self):
        # TZ=Europe/Berlin (both containers): the bot prints 00:30 on the 10th
        # while Docker says 22:30Z on the 9th. The line shows 00:30, so it
        # belongs to the 10th.
        raw = ["2026-09-09T22:30:00.000000000Z [00:30:00] Prüfe Frieren auf updates",
               "2026-09-09T22:30:04.000000000Z [DOWNLOAD] Frieren ep2"]
        with _display_zone(_CEST):
            html_out = app.render_run_history(app.parse_bot_logs(raw), None,
                                              now=datetime(2026, 9, 9, 23, 0))
        self.assertIn(">Today<", html_out)
        self.assertNotIn("Yesterday", html_out)

    def test_log_last_run_stat_carries_its_day(self):
        raw = ["2026-09-09T18:44:00.000000000Z [18:44:00] Prüfe Frieren auf updates"]
        runs = app.parse_bot_logs(raw)
        act = {"status": {"running": True}, "runs": runs, "last_run": runs[-1], "next_run": ""}
        _s, last_html, _n = app.render_activity(act, now=_NOW)
        self.assertEqual(last_html, "Yesterday 18:44:00 &mdash; Frieren")


class LocalZoneDisplayTest(unittest.TestCase):
    """Run-state stores UTC, but the bot's log lines are in the container's TZ.
    Times, day headers and "Today" are converted to the process's zone at
    render time, so a TZ=Europe/Berlin dashboard reads like its own logs."""

    def _quiet(self, ts):
        return _cycle(ts, entries=18, checked=2, downloaded=0, errors=0)

    def test_times_are_shown_on_the_local_clock(self):
        rec = _cycle("2026-09-10T06:10:00Z", entries=4, checked=4, downloaded=1)
        with _display_zone(_CEST):
            self.assertEqual(app.build_run_summary(rec), "08:10 — checked 4/4 · 1 downloaded")
        self.assertEqual(app.build_run_summary(rec), "06:10 — checked 4/4 · 1 downloaded")

    def test_quiet_fold_splits_at_local_midnight_not_utc(self):
        # 21:50Z-22:10Z on the 9th is all one UTC day, but Berlin crosses
        # midnight at 22:00Z: 23:50/23:55 on the 9th, 00:05/00:10 on the 10th.
        state_runs = [self._quiet("2026-09-09T21:50:00Z"), self._quiet("2026-09-09T21:55:00Z"),
                      self._quiet("2026-09-09T22:05:00Z"), self._quiet("2026-09-09T22:10:00Z")]
        with _display_zone(_CEST):
            html_out = app.render_run_state_history(state_runs, now=_NOW)
        self.assertEqual(html_out.count('class="run-day"'), 2)
        self.assertLess(html_out.index(">Today<"), html_out.index("00:05 – 00:10 · 2 quiet cycles"))
        self.assertLess(html_out.index("00:05 – 00:10"), html_out.index(">Yesterday<"))
        self.assertLess(html_out.index(">Yesterday<"), html_out.index("23:50 – 23:55 · 2 quiet cycles"))
        # The same records on the UTC clock: one day, one fold.
        html_utc = app.render_run_state_history(state_runs, now=_NOW)
        self.assertEqual(html_utc.count('class="run-day"'), 1)
        self.assertIn(">Yesterday<", html_utc)
        self.assertIn("21:50 – 22:10 · 4 quiet cycles", html_utc)

    def test_today_is_the_local_day(self):
        # 22:30Z on the 9th is already 00:30 on the 10th in Berlin, so a run
        # at 20:00Z (22:00 local) was yesterday there, and today in UTC.
        now = datetime(2026, 9, 9, 22, 30)
        state_last = {"finished_ts": "2026-09-09T20:00:00Z", "counts": {"entries": 8, "checked": 5}}
        with _display_zone(_CEST):
            self.assertEqual(app.format_last_run_display(state_last, now=now),
                             "Yesterday 22:00:00 &mdash; checked 5/8")
        self.assertEqual(app.format_last_run_display(state_last, now=now),
                         "20:00:00 &mdash; checked 5/8")

    def test_last_run_after_local_midnight_is_today(self):
        state_last = {"finished_ts": "2026-09-09T22:30:05Z", "counts": {"entries": 8, "checked": 5}}
        now = datetime(2026, 9, 10, 6, 0)
        with _display_zone(_CEST):
            self.assertEqual(app.format_last_run_display(state_last, now=now),
                             "00:30:05 &mdash; checked 5/8")
        self.assertEqual(app.format_last_run_display(state_last, now=now),
                         "Yesterday 22:30:05 &mdash; checked 5/8")

    def test_move_status_uses_the_local_clock(self):
        saved = app._move_last_run
        app._move_last_run = datetime(2026, 6, 13, 23, 50, 0, tzinfo=timezone.utc)
        try:
            with _display_zone(_CEST):
                _s, last_html = app.render_move_status(now=datetime(2026, 6, 14, 0, 10))
        finally:
            app._move_last_run = saved
        self.assertEqual(last_html, "01:50:00")

    def test_state_and_log_feeds_agree(self):
        # One moment, 22:30Z on the 9th, seen through both feeds under Berlin:
        # the bot printed 00:30, the state line says 00:30, both under Today.
        now = datetime(2026, 9, 10, 6, 0)
        rec = _cycle("2026-09-09T22:30:00Z", entries=4, checked=4, downloaded=1)
        raw = ["2026-09-09T22:30:00.000000000Z [00:30:00] Prüfe Frieren auf updates",
               "2026-09-09T22:30:04.000000000Z [DOWNLOAD] Frieren ep2"]
        with _display_zone(_CEST):
            state_html = app.render_run_state_history([rec], now=now)
            log_html = app.render_run_history(app.parse_bot_logs(raw), None, now=now)
        self.assertTrue(state_html.startswith('<div class="run-day">Today</div>'))
        self.assertTrue(log_html.startswith('<div class="run-day">Today</div>'))
        self.assertIn("00:30 — checked 4/4", state_html)
        self.assertIn('<span class="run-time">00:30:00</span>', log_html)

    def test_next_run_relative_wording_does_not_depend_on_the_zone(self):
        # The absolute time is deliberately zone-dependent (that's the point
        # of showing local time); the relative countdown alongside it is not.
        now = datetime(2026, 6, 13, 19, 0, 0)
        next_ts = _iso(now + timedelta(seconds=330))
        with _display_zone(_CEST):
            out_cest = app.format_next_run_display(
                {"next_run_ts": next_ts, "timedelay": 600}, now=now)
        with _display_zone(timezone.utc):
            out_utc = app.format_next_run_display(
                {"next_run_ts": next_ts, "timedelay": 600}, now=now)
        self.assertTrue(out_cest.endswith("(in ~5 min)"))
        self.assertTrue(out_utc.endswith("(in ~5 min)"))
        self.assertNotEqual(out_cest, out_utc)

    def test_unconvertible_timestamp_stays_in_utc(self):
        # +2h past the last representable instant overflows; the page must not.
        with _display_zone(_CEST):
            self.assertEqual(app._to_local(datetime(9999, 12, 31, 23, 0)),
                             datetime(9999, 12, 31, 23, 0))

    @unittest.skipIf(_berlin() is None, "no tz database for Europe/Berlin")
    def test_dst_boundaries_use_each_timestamps_own_offset(self):
        berlin = _berlin()
        with _display_zone(berlin):
            # Autumn: 02:30 happens twice on 25 Oct, once in CEST, once in CET.
            self.assertEqual(app._local_ts("2026-10-25T00:30:00Z"), datetime(2026, 10, 25, 2, 30))
            self.assertEqual(app._local_ts("2026-10-25T01:30:00Z"), datetime(2026, 10, 25, 2, 30))
            # Spring: 02:xx never happens on 29 Mar; 01:30Z is already 03:30.
            self.assertEqual(app._local_ts("2026-03-29T00:30:00Z"), datetime(2026, 3, 29, 1, 30))
            self.assertEqual(app._local_ts("2026-03-29T01:30:00Z"), datetime(2026, 3, 29, 3, 30))
            # Winter is +1: 23:30Z is half past midnight on the next day.
            html_out = app.render_run_state_history(
                [_cycle("2026-12-01T23:30:00Z", entries=4, checked=4, downloaded=1)],
                now=datetime(2026, 12, 2, 8, 0))
        self.assertTrue(html_out.startswith('<div class="run-day">Today</div>'))
        self.assertIn("00:30 — checked 4/4", html_out)


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
            [{"name": "Frieren: Beyond Journey's End", "url": "http://x", "tvdb_id": 1}])
        # Unlink TVDB uses the entity-encoded confirm string …
        self.assertIn('onclick="return confirm(', out)
        # … and never the old single-quoted form a `'` would break. Remove
        # itself confirms in the page (see RenderWatchlistCardTest).
        self.assertNotIn("confirm('", out)

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

    def test_no_level_arg_omits_level_from_redirect(self):
        # A caller that hasn't been migrated to the explicit level keeps the
        # old redirect shape, so do_GET's prefix-sniff fallback still applies.
        handler = app.Handler.__new__(app.Handler)
        captured = {}
        handler._redirect = lambda url: captured.__setitem__("url", url)
        handler._redirect_msg("Removed: X")
        qs = parse_qs(urlparse(captured["url"]).query)
        self.assertNotIn("level", qs)

    def test_explicit_level_is_encoded_in_redirect(self):
        handler = app.Handler.__new__(app.Handler)
        captured = {}
        handler._redirect = lambda url: captured.__setitem__("url", url)
        handler._redirect_msg("Could not fetch releases: timeout", level="err")
        qs = parse_qs(urlparse(captured["url"]).query)
        self.assertEqual(qs["level"][0], "err")


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
        handler._redirect_msg = lambda msg, level=None: captured.update(msg=msg, level=level)
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

    def test_ep_add_non_numeric_ep_shows_error_no_crash(self):
        # BUG: int(params["ep"]) used to raise ValueError straight out of the
        # handler — a non-numeric episode dropped the connection.
        a = {"name": "A", "url": "http://x/a", "episodes": 12, "missing": []}
        app.save_ani({"anime": [a]})
        result = self._post("/ep-add", {"key": "http://x/a", "ep": "abc"})
        self.assertTrue(result["msg"].startswith("Error"))
        self.assertEqual(result.get("level"), "err")
        self.assertEqual(app.load_ani()["anime"][0]["missing"], [])

    def test_ep_remove_non_numeric_ep_shows_error_no_crash(self):
        a = {"name": "A", "url": "http://x/a", "episodes": 12, "missing": [3]}
        app.save_ani({"anime": [a]})
        result = self._post("/ep-remove", {"key": "http://x/a", "ep": "abc"})
        self.assertTrue(result["msg"].startswith("Error"))
        self.assertEqual(result.get("level"), "err")
        self.assertEqual(app.load_ani()["anime"][0]["missing"], [3])  # untouched

    def test_ep_add_beyond_downloaded_count_but_within_site_max_is_allowed(self):
        # "Add to retry" with the NEXT, not-yet-downloaded episode (episodes+1)
        # is the documented manual override for an episode published ahead of
        # its TVDB airdate — entry["episodes"] (highest already downloaded)
        # must NOT be the bound, only the site's known max.
        a = {"name": "A", "url": "http://x/a", "episodes": 6,
             "al_max_episodes": 12, "missing": []}
        app.save_ani({"anime": [a]})
        self._post("/ep-add", {"key": "http://x/a", "ep": "7"})
        self.assertEqual(app.load_ani()["anime"][0]["missing"], [7])
        self._post("/ep-add", {"key": "http://x/a", "ep": "12"})
        self.assertEqual(app.load_ani()["anime"][0]["missing"], [7, 12])

    def test_ep_add_rejects_episode_beyond_al_max_episodes(self):
        a = {"name": "A", "url": "http://x/a", "episodes": 6,
             "al_max_episodes": 12, "missing": []}
        app.save_ani({"anime": [a]})
        result = self._post("/ep-add", {"key": "http://x/a", "ep": "13"})
        self.assertTrue(result["msg"].startswith("Error"))
        self.assertEqual(app.load_ani()["anime"][0]["missing"], [])

    def test_ep_add_ignores_al_available_max(self):
        # al_available_max is the CURRENTLY-PUBLISHED cap (the phantom-episode
        # guard) — the early-release override is by definition adding an
        # episode beyond it, so it must never bound /ep-add. Only
        # al_max_episodes (the site's announced total) does.
        a = {"name": "A", "url": "http://x/a", "episodes": 6,
             "al_available_max": 6, "al_max_episodes": 12, "missing": []}
        app.save_ani({"anime": [a]})
        self._post("/ep-add", {"key": "http://x/a", "ep": "7"})
        self.assertEqual(app.load_ani()["anime"][0]["missing"], [7])
        self._post("/ep-add", {"key": "http://x/a", "ep": "12"})
        self.assertEqual(app.load_ani()["anime"][0]["missing"], [7, 12])
        result = self._post("/ep-add", {"key": "http://x/a", "ep": "13"})
        self.assertTrue(result["msg"].startswith("Error"))
        self.assertEqual(app.load_ani()["anime"][0]["missing"], [7, 12])

    def test_ep_add_al_available_max_alone_does_not_bound(self):
        a = {"name": "A", "url": "http://x/a", "episodes": 6,
             "al_available_max": 6, "missing": []}
        app.save_ani({"anime": [a]})
        self._post("/ep-add", {"key": "http://x/a", "ep": "7"})
        self.assertEqual(app.load_ani()["anime"][0]["missing"], [7])

    def test_ep_add_rejects_zero_and_negative(self):
        a = {"name": "A", "url": "http://x/a", "episodes": 12, "missing": []}
        app.save_ani({"anime": [a]})
        self._post("/ep-add", {"key": "http://x/a", "ep": "0"})
        self._post("/ep-add", {"key": "http://x/a", "ep": "-3"})
        self.assertEqual(app.load_ani()["anime"][0]["missing"], [])

    def test_ep_add_sanity_cap_when_max_unknown(self):
        # No known site max (e.g. a dashboard-added entry) still gets a
        # generous sanity cap rather than accepting any number.
        a = {"name": "A", "url": "http://x/a", "episodes": 6, "missing": []}
        app.save_ani({"anime": [a]})
        self._post("/ep-add", {"key": "http://x/a", "ep": "7"})
        self.assertEqual(app.load_ani()["anime"][0]["missing"], [7])
        result = self._post("/ep-add", {"key": "http://x/a", "ep": "99999"})
        self.assertTrue(result["msg"].startswith("Error"))
        self.assertEqual(app.load_ani()["anime"][0]["missing"], [7])

    def test_ep_add_al_max_episodes_999999_treated_as_unknown(self):
        # animeloads.py uses 999999 as its "unknown max" sentinel (e.g. for
        # movies, or a series not yet scraped for its total) — it must fall
        # through to the sanity cap, not be treated as a real bound.
        a = {"name": "A", "url": "http://x/a", "episodes": 6,
             "al_max_episodes": 999999, "missing": []}
        app.save_ani({"anime": [a]})
        self._post("/ep-add", {"key": "http://x/a", "ep": "100"})
        self.assertEqual(app.load_ani()["anime"][0]["missing"], [100])
        result = self._post("/ep-add", {"key": "http://x/a", "ep": "99999"})
        self.assertTrue(result["msg"].startswith("Error"))

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

    def test_update_folder_sanitizes_traversal(self):
        a = {"name": "A", "url": "http://x/a"}
        app.save_ani({"anime": [a]})
        result = self._post("/update-folder", {"key": "http://x/a", "folder": "../../etc"})
        entry = app.load_ani()["anime"][0]
        self.assertEqual(entry["customPackage"], "....etc")
        self.assertNotIn("Error", result["msg"])
        self.assertIn("saved as", result["msg"])

    def test_update_folder_flattens_slash_no_nesting(self):
        a = {"name": "A", "url": "http://x/a"}
        app.save_ani({"anime": [a]})
        self._post("/update-folder", {"key": "http://x/a", "folder": "a/b"})
        entry = app.load_ani()["anime"][0]
        self.assertEqual(entry["customPackage"], "ab")

    def test_update_folder_strips_backslash_keeps_colon(self):
        # ':' is legal on the actual (Linux) media filesystem — only the
        # backslash (a path separator on Windows) is removed.
        a = {"name": "A", "url": "http://x/a"}
        app.save_ani({"anime": [a]})
        self._post("/update-folder", {"key": "http://x/a", "folder": "C:\\x"})
        entry = app.load_ani()["anime"][0]
        self.assertEqual(entry["customPackage"], "C:x")

    def test_update_folder_colon_and_question_mark_preserved(self):
        # Real anime-loads release names routinely carry these — must not be
        # stripped, or a fresh save would diverge from the existing library
        # folder name for the same show.
        a = {"name": "A", "url": "http://x/a"}
        app.save_ani({"anime": [a]})
        result = self._post("/update-folder", {
            "key": "http://x/a",
            "folder": "Re:ZERO -Starting Life in Another World-",
        })
        entry = app.load_ani()["anime"][0]
        self.assertEqual(entry["customPackage"], "Re:ZERO -Starting Life in Another World-")
        self.assertNotIn("saved as", result["msg"])

    def test_update_folder_rejects_dot_dot_no_save(self):
        a = {"name": "A", "url": "http://x/a", "customPackage": "Original"}
        app.save_ani({"anime": [a]})
        result = self._post("/update-folder", {"key": "http://x/a", "folder": ".."})
        entry = app.load_ani()["anime"][0]
        self.assertEqual(entry["customPackage"], "Original")  # unchanged
        self.assertTrue(result["msg"].startswith("Error"))

    def test_update_folder_rejects_whitespace_only_no_save(self):
        a = {"name": "A", "url": "http://x/a", "customPackage": "Original"}
        app.save_ani({"anime": [a]})
        result = self._post("/update-folder", {"key": "http://x/a", "folder": "   "})
        entry = app.load_ani()["anime"][0]
        self.assertEqual(entry["customPackage"], "Original")
        self.assertTrue(result["msg"].startswith("Error"))

    def test_update_folder_normal_names_unchanged(self):
        a = {"name": "A", "url": "http://x/a"}
        app.save_ani({"anime": [a]})
        result = self._post("/update-folder",
                             {"key": "http://x/a", "folder": "Fate & Zero's Return"})
        entry = app.load_ani()["anime"][0]
        self.assertEqual(entry["customPackage"], "Fate & Zero's Return")
        self.assertNotIn("saved as", result["msg"])

    def test_add_release_sanitizes_custom_folder(self):
        app.save_ani({"anime": []})
        result = self._post("/add-release", {
            "url": "http://x/new", "name": "New Show",
            "custom_folder": "../../etc", "tvdb_skip": "1",
        })
        entries = app.load_ani()["anime"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["customPackage"], "....etc")
        self.assertIn("saved as", result["msg"])

    def test_add_release_rejects_dot_dot_custom_folder(self):
        app.save_ani({"anime": []})
        result = self._post("/add-release", {
            "url": "http://x/new2", "name": "New Show 2",
            "custom_folder": "..", "tvdb_skip": "1",
        })
        self.assertEqual(app.load_ani().get("anime", []), [])
        self.assertTrue(result["msg"].startswith("Error"))

    def test_add_release_fallback_name_with_slash_flattens(self):
        app.save_ani({"anime": []})
        result = self._post("/add-release", {
            "url": "http://x/fate", "name": "Fate/stay night", "tvdb_skip": "1",
        })
        entries = app.load_ani()["anime"]
        self.assertEqual(entries[0]["customPackage"], "Fatestay night")
        self.assertIn("saved as", result["msg"])


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


class AniJsonCorruptTest(unittest.TestCase):
    """A corrupt ani.json (e.g. the bot caught mid-write) must never be
    silently treated as an empty watchlist — that's what let the next POST
    wipe a real watchlist+settings. load_ani() now raises anistore's
    CorruptStoreError instead, and do_GET/do_POST catch it centrally to
    refuse to save and show an error banner instead."""

    def setUp(self):
        fd, self._path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(self._path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        self._orig_ani = app.ANI_JSON
        app.ANI_JSON = self._path

    def tearDown(self):
        app.ANI_JSON = self._orig_ani
        try:
            os.remove(self._path)
        except OSError:
            pass

    def _raw_bytes(self):
        with open(self._path, "rb") as f:
            return f.read()

    def test_load_ani_raises_instead_of_returning_empty_default(self):
        with self.assertRaises(app.anistore.CorruptStoreError):
            app.load_ani()

    def _post(self, path, params):
        captured = {}
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h._read_post = lambda: params
        h._redirect_msg = lambda msg, level=None: captured.update(msg=msg, level=level)
        h._redirect = lambda url: captured.__setitem__("url", url)
        h._respond = lambda code, html_body: captured.__setitem__("html", html_body)
        h.do_POST()
        return captured

    def test_post_refuses_to_save_and_shows_error_banner(self):
        before = self._raw_bytes()
        result = self._post("/remove", {"key": "http://x/a"})
        # File is byte-for-byte untouched — no save happened.
        self.assertEqual(self._raw_bytes(), before)
        self.assertTrue(result["msg"].startswith("Error"))
        self.assertIn("corrupt", result["msg"].lower())

    def test_add_url_post_also_refuses_to_save(self):
        # A different route that also load_ani()s before mutating/saving.
        before = self._raw_bytes()
        result = self._post("/add-url", {"url": "https://www.anime-loads.org/anime/x"})
        self.assertEqual(self._raw_bytes(), before)
        self.assertTrue(result["msg"].startswith("Error"))

    def _get(self, path):
        captured = {}
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h._respond = lambda code, html_body: captured.__setitem__("resp", (code, html_body))
        h.do_GET()
        return captured["resp"]

    def test_get_shows_error_banner_instead_of_crashing(self):
        code, html_out = self._get("/")
        self.assertEqual(code, 200)
        self.assertIn("status-err", html_out)
        self.assertIn("corrupt", html_out.lower())

    def test_api_status_degrades_jdownloader_row_instead_of_500(self):
        # /api/status's JDownloader check reads ani.json for the configured
        # jdhost — a corrupt file must degrade that one row to "unknown"
        # rather than raising CorruptStoreError out of the whole endpoint.
        h = app.Handler.__new__(app.Handler)
        h.path = "/api/status"
        captured = {}
        h.send_response = lambda code: captured.__setitem__("code", code)
        h.send_header = lambda *a: None
        h.end_headers = lambda: None
        h.wfile = types.SimpleNamespace(write=lambda b: captured.__setitem__("body", b))
        h.do_GET()
        self.assertEqual(captured["code"], 200)
        payload = json.loads(captured["body"].decode("utf-8"))
        self.assertIn("ani.json unreadable", payload["health"])
        self.assertIn("Unknown", payload["health"])


class LoadAniSeedsMissingConfigTest(unittest.TestCase):
    """load_ani()/update_ani() must seed a brand-new ani.json with full
    default settings the first time they find none — never a bare
    {"settings": {}} the bot could never boot with — and must never
    overwrite a real, existing file."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="aniloads-seed-")
        self._path = os.path.join(self.tmp_dir, "ani.json")
        self._orig_ani = app.ANI_JSON
        app.ANI_JSON = self._path

    def tearDown(self):
        app.ANI_JSON = self._orig_ani
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_load_ani_seeds_full_defaults_when_file_missing(self):
        self.assertFalse(os.path.exists(self._path))

        data = app.load_ani()

        self.assertTrue(os.path.exists(self._path))
        self.assertEqual(data["settings"], app.config_defaults.DEFAULT_SETTINGS)
        with open(self._path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["settings"], app.config_defaults.DEFAULT_SETTINGS)

    def test_load_ani_never_overwrites_existing_file(self):
        fixture = {"settings": {"jdhost": "custom-host"}, "anime": [{"name": "A", "url": "http://x/a"}]}
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(fixture, f)

        data = app.load_ani()

        self.assertEqual(data["settings"], {"jdhost": "custom-host"})
        self.assertEqual(len(data["anime"]), 1)

    def test_update_ani_seeds_before_a_post_arrives_first(self):
        self.assertFalse(os.path.exists(self._path))

        app.update_ani(lambda data: data)

        self.assertTrue(os.path.exists(self._path))
        with open(self._path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["settings"], app.config_defaults.DEFAULT_SETTINGS)


class ValidateSettingsFormTest(unittest.TestCase):
    """Pure validation for the /save-settings POST body."""

    def test_valid_non_secret_fields_are_accepted(self):
        params = {
            "hoster": "1", "timedelay_minutes": "10",
            "jdhost": "jdownloader", "myjd_user": "me", "myjd_device": "laptop",
        }
        updates, errors = app.validate_settings_form(params, auth_enabled=False)
        self.assertEqual(errors, [])
        self.assertEqual(updates["hoster"], 1)
        self.assertEqual(updates["timedelay"], 600)
        self.assertEqual(updates["jdhost"], "jdownloader")
        self.assertEqual(updates["myjd_user"], "me")
        self.assertEqual(updates["myjd_device"], "laptop")

    def test_invalid_hoster_rejected(self):
        params = {"hoster": "99", "timedelay_minutes": "10"}
        updates, errors = app.validate_settings_form(params, auth_enabled=False)
        self.assertIn("hoster", " ".join(errors).lower())
        self.assertNotIn("hoster", updates)

    def test_non_numeric_hoster_rejected(self):
        updates, errors = app.validate_settings_form(
            {"hoster": "not-a-number", "timedelay_minutes": "10"}, auth_enabled=False)
        self.assertTrue(errors)
        self.assertNotIn("hoster", updates)

    def test_timedelay_out_of_range_rejected(self):
        for minutes in ("0", "-5", "1441", "not-a-number", ""):
            with self.subTest(minutes=minutes):
                params = {"hoster": "1", "timedelay_minutes": minutes}
                updates, errors = app.validate_settings_form(params, auth_enabled=False)
                self.assertTrue(errors)
                self.assertNotIn("timedelay", updates)

    def test_timedelay_boundaries_accepted(self):
        for minutes in ("1", "1440"):
            with self.subTest(minutes=minutes):
                params = {"hoster": "1", "timedelay_minutes": minutes}
                updates, errors = app.validate_settings_form(params, auth_enabled=False)
                self.assertEqual(errors, [])
                self.assertEqual(updates["timedelay"], int(minutes) * 60)

    def test_secret_replace_rejected_when_auth_disabled(self):
        params = {"hoster": "1", "timedelay_minutes": "10", "myjd_pw_new": "hunter2"}
        updates, errors = app.validate_settings_form(params, auth_enabled=False)
        self.assertTrue(errors)
        self.assertNotIn("myjd_pw", updates)
        self.assertNotIn("hunter2", " ".join(errors))

    def test_secret_clear_rejected_when_auth_disabled(self):
        params = {"hoster": "1", "timedelay_minutes": "10", "pushbullet_apikey_clear": "on"}
        updates, errors = app.validate_settings_form(params, auth_enabled=False)
        self.assertTrue(errors)
        self.assertNotIn("pushbullet_apikey", updates)

    def test_secret_replace_accepted_when_auth_enabled(self):
        params = {"hoster": "1", "timedelay_minutes": "10", "myjd_pw_new": "hunter2"}
        updates, errors = app.validate_settings_form(params, auth_enabled=True)
        self.assertEqual(errors, [])
        self.assertEqual(updates["myjd_pw"], "hunter2")

    def test_secret_clear_accepted_when_auth_enabled(self):
        params = {"hoster": "1", "timedelay_minutes": "10", "myjd_pw_clear": "on"}
        updates, errors = app.validate_settings_form(params, auth_enabled=True)
        self.assertEqual(errors, [])
        self.assertEqual(updates["myjd_pw"], "")

    def test_clear_wins_over_replace_when_both_present(self):
        params = {
            "hoster": "1", "timedelay_minutes": "10",
            "myjd_pw_new": "hunter2", "myjd_pw_clear": "on",
        }
        updates, errors = app.validate_settings_form(params, auth_enabled=True)
        self.assertEqual(errors, [])
        self.assertEqual(updates["myjd_pw"], "")

    def test_blank_replace_leaves_secret_untouched(self):
        params = {"hoster": "1", "timedelay_minutes": "10", "myjd_pw_new": ""}
        updates, errors = app.validate_settings_form(params, auth_enabled=True)
        self.assertEqual(errors, [])
        self.assertNotIn("myjd_pw", updates)

    def test_no_secret_fields_is_valid_regardless_of_auth(self):
        params = {"hoster": "1", "timedelay_minutes": "10"}
        for auth in (True, False):
            with self.subTest(auth_enabled=auth):
                updates, errors = app.validate_settings_form(params, auth_enabled=auth)
                self.assertEqual(errors, [])
                self.assertNotIn("myjd_pw", updates)
                self.assertNotIn("pushbullet_apikey", updates)


class RenderSettingsCardSecretsTest(unittest.TestCase):
    """The Settings card must never echo a secret VALUE — only a set/not-set
    badge — and must gate the replace/clear inputs on auth being enabled."""

    def test_secret_value_never_rendered(self):
        settings = {"myjd_pw": "super-secret-pw", "pushbullet_apikey": "pb-secret-key"}
        html_out = app.render_settings_card(settings, auth_enabled=True)
        self.assertNotIn("super-secret-pw", html_out)
        self.assertNotIn("pb-secret-key", html_out)

    def test_set_and_not_set_badges(self):
        html_out = app.render_settings_card(
            {"myjd_pw": "x", "pushbullet_apikey": ""}, auth_enabled=True)
        self.assertIn("set", html_out)
        self.assertIn("not set", html_out)

    def test_secret_inputs_disabled_when_auth_off(self):
        html_out = app.render_settings_card({}, auth_enabled=False)
        self.assertIn("myjd_pw_new", html_out)
        self.assertIn("disabled", html_out)
        self.assertIn("Enable dashboard login", html_out)

    def test_secret_inputs_enabled_when_auth_on(self):
        html_out = app.render_settings_card({}, auth_enabled=True)
        # The password/checkbox inputs for BOTH secrets must be free of
        # `disabled` when auth is on (no hint shown either).
        self.assertNotIn("disabled", html_out)
        self.assertNotIn("Enable dashboard login", html_out)

    def test_non_dict_settings_does_not_crash(self):
        # The do_GET corrupt-file fallback renders with ani_data={"settings":
        # {}, "anime": []} -- and a hand-edited ani.json could set "settings"
        # to something that isn't even an object.
        for bogus in (None, [], "oops"):
            with self.subTest(bogus=bogus):
                html_out = app.render_settings_card(bogus, auth_enabled=False)
                self.assertIn("<form", html_out)

    def test_editable_fields_prefilled(self):
        settings = {"jdhost": "127.0.0.1", "myjd_user": "me", "myjd_device": "laptop", "timedelay": 300}
        html_out = app.render_settings_card(settings, auth_enabled=True)
        self.assertIn("127.0.0.1", html_out)
        self.assertIn("me", html_out)
        self.assertIn("laptop", html_out)
        self.assertIn('value="5"', html_out)  # 300s -> 5 minutes

    def test_every_visible_control_has_a_linked_label(self):
        # Same a11y convention as the watchlist card redesign's
        # _ControlCollector check: every input/select has a unique id and a
        # <label for> that targets it (disabled controls still need one).
        for auth_enabled in (True, False):
            with self.subTest(auth_enabled=auth_enabled):
                html_out = app.render_settings_card(
                    {"myjd_pw": "x", "pushbullet_apikey": "y"}, auth_enabled=auth_enabled)
                c = _ControlCollector()
                c.feed(html_out)
                self.assertEqual(len(c.ids), len(set(c.ids)))
                self.assertTrue(c.controls)
                for ctl in c.controls:
                    self.assertTrue(ctl.get("id") in c.label_for, ctl)


class SaveSettingsPostTest(unittest.TestCase):
    """End-to-end /save-settings POST -> update_ani (through the anistore
    lock), mirroring the existing _post-style handler tests above."""

    def setUp(self):
        fd, self._path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(app.config_defaults.default_ani_data(), f)
        self._orig_ani = app.ANI_JSON
        app.ANI_JSON = self._path
        self._orig_auth = app.AUTH_ENABLED
        self.log_records = []
        self._log_handler = logging.Handler()
        self._log_handler.emit = lambda record: self.log_records.append(record.getMessage())
        app._log.addHandler(self._log_handler)

    def tearDown(self):
        app._log.removeHandler(self._log_handler)
        app.ANI_JSON = self._orig_ani
        app.AUTH_ENABLED = self._orig_auth
        try:
            os.remove(self._path)
        except OSError:
            pass

    def _post(self, params):
        captured = {}
        h = app.Handler.__new__(app.Handler)
        h.path = "/save-settings"
        h._read_post = lambda: params
        h._redirect_msg = lambda msg, level=None: captured.update(msg=msg, level=level)
        h._redirect = lambda url: captured.__setitem__("url", url)
        h._respond = lambda code, html_body: captured.__setitem__("html", html_body)
        h.do_POST()
        return captured

    def test_valid_save_persists_under_the_lock_and_reports_restart_needed(self):
        app.AUTH_ENABLED = False
        result = self._post({
            "hoster": "0", "timedelay_minutes": "5",
            "jdhost": "myhost", "myjd_user": "", "myjd_device": "",
        })
        self.assertEqual(result.get("level"), None)
        self.assertIn("restart the bot container", result["msg"])

        saved = app.load_ani()["settings"]
        self.assertEqual(saved["hoster"], 0)
        self.assertEqual(saved["timedelay"], 300)
        self.assertEqual(saved["jdhost"], "myhost")
        # Untouched keys survive the merge (defaults filled, not wiped).
        self.assertIn("jd_deprecatedport", saved)

    def test_invalid_save_shows_error_and_does_not_write(self):
        before = app.load_ani()["settings"]
        result = self._post({"hoster": "not-a-number", "timedelay_minutes": "10"})
        self.assertEqual(result.get("level"), "err")
        after = app.load_ani()["settings"]
        self.assertEqual(before, after)

    def test_secret_replace_rejected_and_not_saved_when_auth_off(self):
        app.AUTH_ENABLED = False
        result = self._post({
            "hoster": "1", "timedelay_minutes": "10", "myjd_pw_new": "hunter2",
        })
        self.assertEqual(result.get("level"), "err")
        saved = app.load_ani()["settings"]
        self.assertEqual(saved.get("myjd_pw", ""), "")

    def test_secret_replace_saved_when_auth_on(self):
        app.AUTH_ENABLED = True
        result = self._post({
            "hoster": "1", "timedelay_minutes": "10", "myjd_pw_new": "hunter2",
        })
        self.assertIsNone(result.get("level"))
        saved = app.load_ani()["settings"]
        self.assertEqual(saved["myjd_pw"], "hunter2")

    def test_secret_value_never_appears_in_logs(self):
        app.AUTH_ENABLED = True
        self._post({
            "hoster": "1", "timedelay_minutes": "10",
            "myjd_pw_new": "hunter2-secret", "pushbullet_apikey_new": "pb-secret-xyz",
        })
        combined = "\n".join(self.log_records)
        self.assertNotIn("hunter2-secret", combined)
        self.assertNotIn("pb-secret-xyz", combined)

    def test_secret_never_in_rendered_page_after_save(self):
        app.AUTH_ENABLED = True
        self._post({"hoster": "1", "timedelay_minutes": "10", "myjd_pw_new": "hunter2-secret"})
        page = app.render_page()
        self.assertNotIn("hunter2-secret", page)
        self.assertIn("badge-ok", page)  # now shows as "set"


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

    def test_docker_unavailable_status_is_unknown_not_stopped(self):
        # BUG: an unreachable Docker socket used to render a red "Stopped" —
        # indistinguishable from the bot container genuinely being down.
        act = {"status": {"status": "unavailable", "running": False,
                           "docker_available": False},
               "last_run": None, "next_run": ""}
        status_html, _last, _next = app.render_activity(act)
        self.assertIn("Unknown", status_html)
        self.assertIn("status-dot unknown", status_html)
        self.assertIn("Docker socket unavailable", status_html)
        self.assertNotIn("Stopped", status_html)
        self.assertNotIn("status-dot stopped", status_html)

    def test_docker_available_and_container_stopped_is_still_stopped(self):
        # Docker itself distinguishes "unreachable" from "reachable, and it
        # told us the container isn't running" — the latter stays red.
        act = {"status": {"status": "exited", "running": False,
                           "docker_available": True},
               "last_run": None, "next_run": ""}
        status_html, _last, _next = app.render_activity(act)
        self.assertIn("Stopped", status_html)
        self.assertIn("status-dot stopped", status_html)
        self.assertNotIn("Unknown", status_html)

    def test_running_with_stale_health_explains_instead_of_contradicting(self):
        act = {"status": {"status": "running", "running": True, "docker_available": True},
               "last_run": None, "next_run": "",
               "staleness": {"state": "warn", "detail": "No cycle finished in 45 min (expected every ~10 min)"}}
        status_html, _last, _next = app.render_activity(act)
        self.assertIn("Running", status_html)
        self.assertIn("status-dot running", status_html)
        self.assertIn("No cycle finished in 45 min", status_html)

    def test_running_with_ok_health_has_no_hint(self):
        act = {"status": {"status": "running", "running": True, "docker_available": True},
               "last_run": None, "next_run": "",
               "staleness": {"state": "ok", "detail": "Last cycle finished 2 min ago"}}
        status_html, _last, _next = app.render_activity(act)
        self.assertIn("Running", status_html)
        self.assertNotIn("hint", status_html)


class RenderMoveStatusTest(unittest.TestCase):
    """render_move_status: running vs idle dot, last-run time, and the
    dir-not-mounted / not-yet empty states."""

    def setUp(self):
        self._orig_running = app._move_running
        self._orig_last = app._move_last_run
        self._orig_dl = app.DOWNLOAD_DIR
        # Most cases here are about last-run formatting on top of a normally
        # mounted dir; the not-mounted tests below point DOWNLOAD_DIR elsewhere.
        self._dl_dir = tempfile.mkdtemp(prefix="aniloads-dl-")
        app.DOWNLOAD_DIR = self._dl_dir

    def tearDown(self):
        app._move_running = self._orig_running
        app._move_last_run = self._orig_last
        app.DOWNLOAD_DIR = self._orig_dl
        shutil.rmtree(self._dl_dir, ignore_errors=True)

    def test_running_state(self):
        app._move_running = True
        status_html, _last = app.render_move_status()
        self.assertIn("Running", status_html)
        self.assertIn("status-dot running", status_html)

    def test_idle_with_last_run_time(self):
        app._move_running = False
        app._move_last_run = datetime(2026, 6, 13, 19, 20, 5)
        status_html, last_html = app.render_move_status(now=datetime(2026, 6, 13, 23, 0))
        self.assertIn("Idle", status_html)
        self.assertEqual(last_html, "19:20:05")

    def test_last_run_names_its_day_when_not_today(self):
        app._move_running = False
        app._move_last_run = datetime(2026, 6, 13, 19, 20, 5)
        _s, last_html = app.render_move_status(now=datetime(2026, 6, 14, 8, 0))
        self.assertEqual(last_html, "Yesterday 19:20:05")
        _s, last_html = app.render_move_status(now=datetime(2026, 6, 16, 8, 0))
        self.assertEqual(last_html, "Sat 13 Jun 19:20:05")

    def test_aware_utc_stamp_is_compared_as_utc(self):
        # The mover worker stamps datetime.now(timezone.utc) — aware, not naive.
        app._move_running = False
        app._move_last_run = datetime(2026, 6, 13, 23, 50, 0, tzinfo=timezone.utc)
        _s, last_html = app.render_move_status(now=datetime(2026, 6, 14, 0, 10))
        self.assertEqual(last_html, "Yesterday 23:50:00")

    def test_download_dir_not_mounted(self):
        app._move_running = False
        app._move_last_run = None
        app.DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "aniloads-no-such-dl")
        status_html, last_html = app.render_move_status()
        self.assertIn("Not mounted", status_html)
        self.assertIn("status-dot unknown", status_html)
        self.assertIn("Download dir not mounted", last_html)

    def test_not_yet_when_dir_present(self):
        app._move_running = False
        app._move_last_run = None
        status_html, last_html = app.render_move_status()
        self.assertIn("Idle", status_html)
        self.assertIn("Not yet", last_html)


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

        # Stuck-item state is module-global; isolate it per test so a "new"
        # detection in one test doesn't look like a repeat in the next.
        self._orig_stuck = dict(app._stuck_items)
        app._stuck_items.clear()
        self._orig_move_history_file = app.MOVE_HISTORY_FILE
        app.MOVE_HISTORY_FILE = os.path.join(self.tmp, "move_history.json")

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(app, k, v)
        app._stuck_items.clear()
        app._stuck_items.update(self._orig_stuck)
        app.MOVE_HISTORY_FILE = self._orig_move_history_file
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

    def test_tvdb_season_override_changes_season_dir_and_filename(self):
        self._write_ani([{"name": "Bleach", "media_type": "series", "tvdb_season": 2}])
        self._make_dl("Bleach.S01", ["Bleach.S01E05.mkv"])
        app.run_move_cycle()
        # Both the season folder AND the filename's SxxExx token are
        # overridden to S02 — Plex's scanner reads the token from the
        # filename, so a stale S01 token would still file it under season 1.
        self.assertTrue(os.path.isfile(
            os.path.join(self.media, "Bleach", "S02", "Bleach.S02E05.mkv")))
        self.assertFalse(os.path.isfile(
            os.path.join(self.media, "Bleach", "S02", "Bleach.S01E05.mkv")))

    def test_episode_offset_renames_episode_in_filename(self):
        self._write_ani([{"name": "Bleach", "media_type": "series", "episode_offset": 12}])
        self._make_dl("Bleach.S01", ["Bleach.S01E05.mkv"])
        app.run_move_cycle()
        self.assertTrue(os.path.isfile(
            os.path.join(self.media, "Bleach", "S01", "Bleach.S01E17.mkv")))

    def test_episode_offset_preserves_three_digit_episode_width(self):
        self._write_ani([{"name": "LongShow", "media_type": "series", "episode_offset": 5}])
        self._make_dl("LongShow.S01", ["LongShow.S01E001.mkv"])
        app.run_move_cycle()
        dest_dir = os.path.join(self.media, "LongShow", "S01")
        self.assertTrue(os.path.isfile(os.path.join(dest_dir, "LongShow.S01E006.mkv")))
        self.assertFalse(os.path.isfile(os.path.join(dest_dir, "LongShow.S01E001.mkv")))

    def test_episode_offset_rename_does_not_touch_unrelated_token_in_title(self):
        # The title itself contains "E05" (unrelated to the real SxxExx token
        # at the end) with the SAME digits as the pre-offset episode number —
        # a naive filename.replace('E05', 'E17') would corrupt it too.
        self._write_ani([{"name": "Show", "media_type": "series", "episode_offset": 12}])
        self._make_dl("Show.S01", ["Show.E05.Bonus.S01E05.mkv"])
        app.run_move_cycle()
        dest_dir = os.path.join(self.media, "Show", "S01")
        self.assertTrue(os.path.isfile(os.path.join(dest_dir, "Show.E05.Bonus.S01E17.mkv")))

    def test_no_override_leaves_filename_byte_identical(self):
        self._write_ani([{"name": "Bleach", "media_type": "series"}])
        self._make_dl("Bleach.S01", ["Bleach.S01E05.mkv"])
        app.run_move_cycle()
        self.assertTrue(os.path.isfile(
            os.path.join(self.media, "Bleach", "S01", "Bleach.S01E05.mkv")))

    def test_season_override_subtitle_follows_renamed_video_stem(self):
        self._write_ani([{"name": "Bleach", "media_type": "series", "tvdb_season": 2}])
        self._make_dl("Bleach.S01", ["Bleach.S01E05.mkv", "Bleach.S01E05.en.srt"])
        app.run_move_cycle()
        dest_dir = os.path.join(self.media, "Bleach", "S02")
        self.assertTrue(os.path.isfile(os.path.join(dest_dir, "Bleach.S02E05.mkv")))
        self.assertTrue(os.path.isfile(os.path.join(dest_dir, "Bleach.S02E05.en.srt")))

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

    def test_subtitle_sidecar_moved_alongside_video_keeping_lang_suffix(self):
        self._write_ani([{"name": "Naruto", "media_type": "series"}])
        self._make_dl("Naruto.S01", ["Naruto.S01E05.mkv", "Naruto.S01E05.en.srt"])
        app.run_move_cycle()
        dest_dir = os.path.join(self.media, "Naruto", "S01")
        self.assertTrue(os.path.isfile(os.path.join(dest_dir, "Naruto.S01E05.mkv")))
        self.assertTrue(os.path.isfile(os.path.join(dest_dir, "Naruto.S01E05.en.srt")))

    def test_subtitle_prefix_collision_not_wrongly_attached(self):
        # "Show.S01E1" is a literal string-prefix of "Show.S01E10.en.srt"'s
        # stem — the subtitle belongs to a (non-existent, in this fixture)
        # E10 video, not E1, and must not be swept up by the prefix match.
        self._write_ani([{"name": "Show", "media_type": "series"}])
        self._make_dl("Show.S01", ["Show.S01E1.mkv", "Show.S01E10.en.srt"])
        app.run_move_cycle()
        dest_dir = os.path.join(self.media, "Show", "S01")
        self.assertTrue(os.path.isfile(os.path.join(dest_dir, "Show.S01E1.mkv")))
        self.assertFalse(os.path.isfile(os.path.join(dest_dir, "Show.S01E10.en.srt")))
        # Left behind in the download dir — not claimed, and not junk either.
        self.assertTrue(os.path.isfile(
            os.path.join(self.download, "Show.S01", "Show.S01E10.en.srt")))

    def test_subtitle_sidecar_gets_same_episode_offset_rename_as_video(self):
        self._write_ani([{"name": "Bleach", "media_type": "series", "episode_offset": 12}])
        self._make_dl("Bleach.S01", ["Bleach.S01E05.mkv", "Bleach.S01E05.srt"])
        app.run_move_cycle()
        dest_dir = os.path.join(self.media, "Bleach", "S01")
        self.assertTrue(os.path.isfile(os.path.join(dest_dir, "Bleach.S01E17.mkv")))
        self.assertTrue(os.path.isfile(os.path.join(dest_dir, "Bleach.S01E17.srt")))

    def test_junk_deleted_but_unknown_extension_kept(self):
        self._write_ani([{"name": "Naruto", "media_type": "series"}])
        self._make_dl("Naruto.S01", ["Naruto.S01E05.mkv", "Naruto.nfo", "Naruto.weird"])
        app.run_move_cycle()
        dl_dir = os.path.join(self.download, "Naruto.S01")
        self.assertFalse(os.path.exists(os.path.join(dl_dir, "Naruto.nfo")))
        self.assertTrue(os.path.isfile(os.path.join(dl_dir, "Naruto.weird")))
        # An unrecognized leftover keeps the directory from being pruned too.
        self.assertTrue(os.path.isdir(dl_dir))

    def test_parse_failure_recorded_as_stuck_and_not_repeated(self):
        self._write_ani([{"name": "Akira", "media_type": "series"}])
        self._make_dl("Akira", ["Akira.Movie.1080p.mkv"])
        events = app.run_move_cycle()
        self.assertIn("error", self._types(events))
        stuck = [v for v in app._stuck_items.values() if v["reason"] == "parse"]
        self.assertEqual(len(stuck), 1)

        # Second cycle: same unresolved file — no repeat event this time.
        events2 = app.run_move_cycle()
        self.assertEqual(events2, [])
        self.assertTrue(os.path.isfile(os.path.join(self.download, "Akira", "Akira.Movie.1080p.mkv")))

    def test_already_exists_stuck_delete_removes_only_the_download_copy(self):
        self._write_ani([{"name": "Naruto", "media_type": "series"}])
        dest_dir = os.path.join(self.media, "Naruto", "S01")
        os.makedirs(dest_dir)
        with open(os.path.join(dest_dir, "Naruto.S01E05.mkv"), "w") as f:
            f.write("existing")
        self._make_dl("Naruto.S01", ["Naruto.S01E05.mkv"])
        events = app.run_move_cycle()
        self.assertIn("skip", self._types(events))

        stuck = [v for v in app._stuck_items.values() if v["reason"] == "exists"]
        self.assertEqual(len(stuck), 1)
        key = stuck[0]["key"]
        dl_path = os.path.join(self.download, "Naruto.S01", "Naruto.S01E05.mkv")
        self.assertTrue(os.path.isfile(dl_path))

        result = app.stuck_delete_download(key)
        self.assertIsNotNone(result)
        self.assertFalse(result.startswith("error:"))
        self.assertFalse(os.path.isfile(dl_path))
        self.assertNotIn(key, app._stuck_items)
        # The library copy is untouched.
        with open(os.path.join(dest_dir, "Naruto.S01E05.mkv")) as f:
            self.assertEqual(f.read(), "existing")

    def test_unmatched_download_goes_stuck_instead_of_autocreating_folder(self):
        self._write_ani([])
        self._make_dl("Some.Anime.S01", ["Some.Anime.S01E01.mkv"])
        events = app.run_move_cycle()
        self.assertIn("error", self._types(events))
        self.assertFalse(os.path.isdir(os.path.join(self.media, "Some Anime")))

        stuck = [v for v in app._stuck_items.values() if v["reason"] == "unmatched"]
        self.assertEqual(len(stuck), 1)
        key = stuck[0]["key"]

        msg = app.stuck_move_anyway(key)
        self.assertIsNotNone(msg)
        events2 = app.run_move_cycle()
        self.assertIn("moved", self._types(events2))
        self.assertTrue(os.path.isfile(
            os.path.join(self.media, "Some Anime", "S01", "Some.Anime.S01E01.mkv")))
        self.assertNotIn(key, app._stuck_items)

    def test_unmatched_download_with_existing_library_folder_files_normally(self):
        # No watchlist entry at all, but a folder for the parsed name already
        # exists in the library (e.g. a show removed from the watchlist, or a
        # manual JDownloader add of something already in Plex) — this must
        # file normally, not go stuck, matching main's existing-folder lookup.
        self._write_ani([])
        os.makedirs(os.path.join(self.media, "Old Show"))
        self._make_dl("Old.Show.S01", ["Old.Show.S01E01.mkv"])
        events = app.run_move_cycle()
        self.assertIn("moved", self._types(events))
        self.assertTrue(os.path.isfile(
            os.path.join(self.media, "Old Show", "S01", "Old.Show.S01E01.mkv")))
        self.assertEqual(app._stuck_items, {})

    def test_ignored_stuck_item_stops_repeating(self):
        self._write_ani([{"name": "Akira", "media_type": "series"}])
        self._make_dl("Akira", ["Akira.Movie.1080p.mkv"])
        app.run_move_cycle()
        key = next(iter(app._stuck_items))

        msg = app.stuck_ignore(key)
        self.assertIsNotNone(msg)
        self.assertTrue(app._stuck_items[key]["ignored"])

        events = app.run_move_cycle()
        self.assertEqual(events, [])
        # Still present (so it isn't rediscovered as "new"), just ignored.
        self.assertIn(key, app._stuck_items)

    def test_hostile_existing_custom_package_traversal_never_escapes(self):
        # A customPackage saved before save-time sanitization existed (hand-
        # edited ani.json, or a pre-fix entry) must not let the mover write
        # outside MEDIA_DIR — sanitization at match time flattens it first.
        self._write_ani([{"name": "Naruto", "media_type": "series",
                           "customPackage": "../../etc"}])
        self._make_dl("Naruto.S01", ["Naruto.S01E05.mkv"])
        events = app.run_move_cycle()
        self.assertIn("moved", self._types(events))
        self.assertTrue(os.path.isfile(
            os.path.join(self.media, "....etc", "S01", "Naruto.S01E05.mkv")))
        # Nothing landed outside the sandboxed media dir.
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "etc")))

    def test_hostile_existing_custom_package_slash_flattens_no_nesting(self):
        self._write_ani([{"name": "Bleach", "media_type": "series",
                           "customPackage": "a/b"}])
        self._make_dl("Bleach.S01", ["Bleach.S01E05.mkv"])
        app.run_move_cycle()
        self.assertTrue(os.path.isfile(
            os.path.join(self.media, "ab", "S01", "Bleach.S01E05.mkv")))
        self.assertFalse(os.path.isdir(os.path.join(self.media, "a")))

    def test_colon_title_matches_existing_folder_without_touching_filesystem(self):
        # Regression guard: ':' and '?' are legal on the real (Linux) media
        # filesystem and common in anime-loads release names, so match-time
        # sanitization must leave them alone — otherwise an existing library
        # folder using them would stop matching and get a second, mangled
        # folder created alongside it. Exercised at the match_anime_entry
        # level (not a real os.makedirs) because ':'/'?' are themselves
        # illegal in a real directory name on this Windows dev/CI host, even
        # though they're legal on the Linux host this code actually runs on.
        existing_folder = "Re:ZERO -Starting Life in Another World-"
        self._write_ani([{"name": "ReZERO", "media_type": "series",
                           "customPackage": existing_folder}])
        match = app.match_anime_entry("rezero", "ReZERO.S01", app._lookup_anime_entries())
        self.assertEqual(match["folder_name"], existing_folder)

    def test_mover_containment_check_blocks_series_escape(self):
        # Defense-in-depth: even if something upstream of the mover ever
        # hands back an unsanitized folder_name, the mover's own realpath
        # containment check must refuse to write outside MEDIA_DIR.
        orig_match = app.match_anime_entry

        def fake_match(parsed_name, dir_basename, anime_list, parsed_season=None):
            return {
                "folder_name": "../../escaped", "tvdb_season": None,
                "episode_offset": 0, "media_type": "series", "year": None,
                "display_title": "Evil", "matched": True,
            }

        app.match_anime_entry = fake_match
        try:
            self._make_dl("Whatever.S01", ["Whatever.S01E01.mkv"])
            events = app.run_move_cycle()
        finally:
            app.match_anime_entry = orig_match

        self.assertIn("error", self._types(events))
        escaped_dir = os.path.realpath(os.path.join(self.media, "..", "escaped"))
        self.assertFalse(os.path.isdir(escaped_dir))
        stuck = [v for v in app._stuck_items.values() if v["reason"] == "unsafe_folder"]
        self.assertEqual(len(stuck), 1)

    def test_mover_containment_check_blocks_movie_escape(self):
        # _movie_target_name already sanitizes internally, so to exercise the
        # mover's OWN containment check for the movie branch (defense in
        # depth against a future regression there), patch it out directly
        # rather than going through match_anime_entry.
        self._write_ani([{"name": "Evil", "media_type": "movie"}])
        orig_target_name = app._movie_target_name
        app._movie_target_name = lambda display_title, year: "../../escaped"
        try:
            self._make_dl("Evil.Movie", ["Evil.Movie.mkv"])
            events = app.run_move_cycle()
        finally:
            app._movie_target_name = orig_target_name

        self.assertIn("error", self._types(events))
        escaped_dir = os.path.realpath(os.path.join(self.movies, "..", "escaped"))
        self.assertFalse(os.path.isdir(escaped_dir))
        stuck = [v for v in app._stuck_items.values() if v["reason"] == "unsafe_folder"]
        self.assertEqual(len(stuck), 1)


class MoveNowButtonTest(unittest.TestCase):
    """render_move_now_button: enabled when DOWNLOAD_DIR is mounted, disabled
    with a reason tooltip when it isn't."""

    def setUp(self):
        self._orig_dl = app.DOWNLOAD_DIR

    def tearDown(self):
        app.DOWNLOAD_DIR = self._orig_dl

    def test_enabled_when_mounted(self):
        d = tempfile.mkdtemp(prefix="aniloads-dl-")
        app.DOWNLOAD_DIR = d
        try:
            html_out = app.render_move_now_button()
            self.assertNotIn("disabled", html_out)
            self.assertIn("Move Now", html_out)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_disabled_with_reason_when_not_mounted(self):
        app.DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "aniloads-no-such-dl-3")
        html_out = app.render_move_now_button()
        self.assertIn("disabled", html_out)
        self.assertIn("Download directory not mounted", html_out)


class RunAndRecordMoveCycleTest(unittest.TestCase):
    """_run_and_record_move_cycle (the worker loop's per-cycle body, extracted
    for testability): must not stamp _move_last_run for a cycle that couldn't
    run because DOWNLOAD_DIR isn't mounted."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aniloads-cycle-")
        self._orig_dl = app.DOWNLOAD_DIR
        self._orig_last = app._move_last_run
        self._orig_history_file = app.MOVE_HISTORY_FILE
        app.MOVE_HISTORY_FILE = os.path.join(self.tmp, "move_history.json")
        app._move_last_run = None

    def tearDown(self):
        app.DOWNLOAD_DIR = self._orig_dl
        app._move_last_run = self._orig_last
        app.MOVE_HISTORY_FILE = self._orig_history_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_does_not_stamp_last_run_when_not_mounted(self):
        app.DOWNLOAD_DIR = os.path.join(self.tmp, "no-such-downloads")
        app._run_and_record_move_cycle()
        self.assertIsNone(app._move_last_run)

    def test_stamps_last_run_when_mounted(self):
        app.DOWNLOAD_DIR = os.path.join(self.tmp, "downloads")
        os.makedirs(app.DOWNLOAD_DIR)
        app._run_and_record_move_cycle()
        self.assertIsNotNone(app._move_last_run)


class MoveStatePersistenceTest(unittest.TestCase):
    """save_move_state / load_move_state: atomic tmp+os.replace write, bounded
    history, and stuck/ignored items surviving a simulated web restart."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aniloads-move-state-")
        self._orig_file = app.MOVE_HISTORY_FILE
        app.MOVE_HISTORY_FILE = os.path.join(self.tmp, "move_history.json")
        self._orig_history = list(app._move_history)
        self._orig_stuck = dict(app._stuck_items)
        app._move_history.clear()
        app._stuck_items.clear()

    def tearDown(self):
        app.MOVE_HISTORY_FILE = self._orig_file
        app._move_history.clear()
        app._move_history.extend(self._orig_history)
        app._stuck_items.clear()
        app._stuck_items.update(self._orig_stuck)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_then_load_round_trips_history_and_stuck(self):
        for i in range(5):
            app._move_history.append({"type": "moved", "msg": "m{}".format(i)})
        app._stuck_items["k1"] = {
            "key": "k1", "reason": "exists", "ignored": True, "msg": "x",
            "path": "p", "dir": "d", "first_seen": "t", "last_seen": "t",
        }
        app.save_move_state()

        self.assertTrue(os.path.isfile(app.MOVE_HISTORY_FILE))
        # No leftover mkstemp tmp file (a fixed ".tmp" suffix used to be the
        # collision risk; mkstemp's own name is collision-proof, but a failed
        # cleanup after a write error would still leave one behind).
        leftover = [f for f in os.listdir(self.tmp) if f != os.path.basename(app.MOVE_HISTORY_FILE)]
        self.assertEqual(leftover, [])

        loaded = app.load_move_state()
        self.assertEqual(len(loaded["history"]), 5)
        self.assertTrue(loaded["stuck"]["k1"]["ignored"])

    @unittest.skipIf(os.name == "nt", "POSIX file mode bits aren't meaningful on Windows")
    def test_saved_file_mode_is_not_mkstemps_restrictive_default(self):
        import stat
        app.save_move_state()
        mode = stat.S_IMODE(os.stat(app.MOVE_HISTORY_FILE).st_mode)
        # mkstemp defaults to 0o600 (owner-only) — this file must be readable
        # by the group/other bits too, since save_move_state fixes the mode
        # up explicitly rather than leaving mkstemp's default in place.
        self.assertEqual(mode, 0o644)

    def test_concurrent_saves_do_not_corrupt_the_file(self):
        app._stuck_items["k9"] = {
            "key": "k9", "reason": "exists", "ignored": False, "msg": "x",
            "path": "p", "dir": "d", "first_seen": "t", "last_seen": "t",
        }
        errors = []

        def _save():
            try:
                for _ in range(10):
                    app.save_move_state()
            except Exception as e:  # pragma: no cover - surfaced via errors list
                errors.append(e)

        threads = [threading.Thread(target=_save) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        # The file must always be valid, complete JSON — never a partial or
        # interleaved write from two concurrent savers.
        loaded = app.load_move_state()
        self.assertEqual(loaded["stuck"]["k9"]["reason"], "exists")

    def test_history_persisted_is_bounded(self):
        for i in range(app.MOVE_HISTORY_MAX + 20):
            app._move_history.append({"type": "moved", "msg": "m{}".format(i)})
        app.save_move_state()
        loaded = app.load_move_state()
        self.assertLessEqual(len(loaded["history"]), app.MOVE_HISTORY_MAX)

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(app.load_move_state(), {})

    def test_ignore_persists_across_simulated_restart(self):
        app._stuck_items["k2"] = {
            "key": "k2", "reason": "parse", "ignored": False, "msg": "y",
            "path": "p2", "dir": "d2", "first_seen": "t", "last_seen": "t",
        }
        app.stuck_ignore("k2")
        self.assertTrue(app._stuck_items["k2"]["ignored"])

        # Simulate a web restart: drop in-memory state, reload from disk.
        app._move_history.clear()
        app._stuck_items.clear()
        app._restore_move_state()

        self.assertIn("k2", app._stuck_items)
        self.assertTrue(app._stuck_items["k2"]["ignored"])


class RenderMoveStuckTest(unittest.TestCase):
    """render_move_stuck: reason-specific actions, and ignored items hidden."""

    def setUp(self):
        self._orig_stuck = dict(app._stuck_items)
        app._stuck_items.clear()

    def tearDown(self):
        app._stuck_items.clear()
        app._stuck_items.update(self._orig_stuck)

    def test_no_stuck_items_shows_empty_state(self):
        self.assertIn("No stuck downloads", app.render_move_stuck())

    def test_exists_reason_offers_delete_action(self):
        app._stuck_items["k1"] = {
            "key": "k1", "reason": "exists", "ignored": False,
            "msg": "Show.S01E01.mkv — already exists", "path": "d/Show.S01E01.mkv",
            "dir": "d", "first_seen": "t", "last_seen": "t",
        }
        html_out = app.render_move_stuck()
        self.assertIn("/move-stuck-delete", html_out)
        self.assertIn("/move-stuck-ignore", html_out)
        self.assertNotIn("/move-stuck-anyway", html_out)

    def test_unmatched_reason_offers_move_anyway_action(self):
        app._stuck_items["k2"] = {
            "key": "k2", "reason": "unmatched", "ignored": False,
            "msg": "no watchlist match", "path": "d/Show.S01E01.mkv",
            "dir": "d", "first_seen": "t", "last_seen": "t",
        }
        html_out = app.render_move_stuck()
        self.assertIn("/move-stuck-anyway", html_out)
        self.assertNotIn("/move-stuck-delete", html_out)

    def test_ignored_items_are_hidden(self):
        app._stuck_items["k3"] = {
            "key": "k3", "reason": "parse", "ignored": True,
            "msg": "hidden", "path": "d/x.mkv", "dir": "d",
            "first_seen": "t", "last_seen": "t",
        }
        self.assertIn("No stuck downloads", app.render_move_stuck())


class HandlerPostRoutingTest(unittest.TestCase):
    """Light do_POST integration coverage for the non-network routes, using the
    same __new__/stub harness as WatchlistMutationKeyByUrlTest."""

    def setUp(self):
        fd, self._prefs = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._orig_prefs = app.PREFS_FILE
        app.PREFS_FILE = self._prefs

        self._tmp = tempfile.mkdtemp(prefix="aniloads-post-")
        self._orig_move_history_file = app.MOVE_HISTORY_FILE
        app.MOVE_HISTORY_FILE = os.path.join(self._tmp, "move_history.json")
        self._orig_stuck = dict(app._stuck_items)
        app._stuck_items.clear()
        self._orig_dl = app.DOWNLOAD_DIR
        app.DOWNLOAD_DIR = os.path.join(self._tmp, "downloads")
        os.makedirs(app.DOWNLOAD_DIR)

    def tearDown(self):
        app.PREFS_FILE = self._orig_prefs
        try:
            os.remove(self._prefs)
        except OSError:
            pass
        app._move_trigger.clear()
        app.MOVE_HISTORY_FILE = self._orig_move_history_file
        app._stuck_items.clear()
        app._stuck_items.update(self._orig_stuck)
        app.DOWNLOAD_DIR = self._orig_dl
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _post(self, path, params):
        captured = {}
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h._read_post = lambda: params
        h._redirect_msg = lambda msg, level=None: captured.update(msg=msg, level=level)
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

    def test_save_prefs_non_numeric_resolution_shows_error_no_write(self):
        # BUG: int(params["min_resolution"]) used to raise ValueError straight
        # out of the handler, dropping the connection instead of banner-erroring.
        before = app.load_prefs()
        result = self._post("/save-prefs", {"min_resolution": "not-a-number"})
        self.assertTrue(result["msg"].startswith("Error"))
        self.assertEqual(result.get("level"), "err")
        self.assertEqual(app.load_prefs(), before)  # nothing was saved

    def test_move_now_sets_trigger(self):
        app._move_trigger.clear()
        result = self._post("/move-now", {})
        self.assertTrue(app._move_trigger.is_set())
        self.assertEqual(result["msg"], "Move cycle triggered")

    def test_move_now_rejected_when_download_dir_not_mounted(self):
        app.DOWNLOAD_DIR = os.path.join(self._tmp, "no-such-downloads")
        app._move_trigger.clear()
        result = self._post("/move-now", {})
        self.assertFalse(app._move_trigger.is_set())
        self.assertTrue(result["msg"].startswith("Error"))
        self.assertEqual(result.get("level"), "err")

    def test_add_url_rejects_non_site_url(self):
        result = self._post("/add-url", {"url": "http://evil.example/x"})
        self.assertTrue(result["msg"].startswith("Error: Invalid URL"))

    def test_search_empty_query_errors(self):
        result = self._post("/search", {"q": "  "})
        self.assertTrue(result["msg"].startswith("Error: Empty search"))
        self.assertEqual(result.get("level"), "err")

    def test_move_stuck_ignore_marks_item_and_redirects(self):
        app._stuck_items["k1"] = {
            "key": "k1", "reason": "parse", "ignored": False, "msg": "boom",
            "path": "d/x.mkv", "dir": "d", "first_seen": "t", "last_seen": "t",
        }
        result = self._post("/move-stuck-ignore", {"key": "k1"})
        self.assertEqual(result["msg"], "Ignoring: boom")
        self.assertTrue(app._stuck_items["k1"]["ignored"])

    def test_move_stuck_ignore_missing_key_errors(self):
        result = self._post("/move-stuck-ignore", {"key": "nope"})
        self.assertEqual(result["msg"], "Error: stuck item not found")
        self.assertEqual(result.get("level"), "err")

    def test_move_stuck_delete_removes_download_copy(self):
        # app.DOWNLOAD_DIR is already the setUp-managed tmp dir.
        dl_dir = os.path.join(app.DOWNLOAD_DIR, "d")
        os.makedirs(dl_dir)
        dl_file = os.path.join(dl_dir, "x.mkv")
        with open(dl_file, "w") as f:
            f.write("x")
        app._stuck_items["k2"] = {
            "key": "k2", "reason": "exists", "ignored": False, "msg": "x.mkv exists",
            "path": "d/x.mkv", "dir": "d", "first_seen": "t", "last_seen": "t",
        }
        result = self._post("/move-stuck-delete", {"key": "k2"})
        self.assertEqual(result["msg"], "Deleted download copy: x.mkv exists")
        self.assertFalse(os.path.isfile(dl_file))
        self.assertNotIn("k2", app._stuck_items)

    def test_move_stuck_delete_wrong_reason_errors(self):
        app._stuck_items["k3"] = {
            "key": "k3", "reason": "parse", "ignored": False, "msg": "boom",
            "path": "d/x.mkv", "dir": "d", "first_seen": "t", "last_seen": "t",
        }
        result = self._post("/move-stuck-delete", {"key": "k3"})
        self.assertEqual(result["msg"], "Error: stuck item not found")
        self.assertIn("k3", app._stuck_items)

    def test_move_stuck_anyway_flags_item_and_triggers_cycle(self):
        app._move_trigger.clear()
        app._stuck_items["k4"] = {
            "key": "k4", "reason": "unmatched", "ignored": False, "msg": "no match",
            "path": "d/x.mkv", "dir": "d", "first_seen": "t", "last_seen": "t",
        }
        result = self._post("/move-stuck-anyway", {"key": "k4"})
        self.assertEqual(result["msg"], "Will move on next cycle: no match")
        self.assertTrue(app._stuck_items["k4"]["move_anyway"])
        self.assertTrue(app._move_trigger.is_set())


class HandlerGetMsgBannerTest(unittest.TestCase):
    """do_GET's status-banner branch keys the banner CSS class off an explicit
    ``level`` query param when present, falling back to sniffing a leading
    'Error' only for a redirect that didn't set one (back-compat). render_page
    is stubbed so no full page is built."""

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

    def test_explicit_err_level_wins_without_error_prefix(self):
        # BUG: a failure message that doesn't start with "Error" (e.g. from
        # a fetch failure) used to render green via the old prefix sniff.
        url = "/?msg={}&level=err".format(quote("Could not fetch releases: timeout"))
        _code, html_out = self._get(url)
        self.assertIn("status-err", html_out)
        self.assertNotIn("status-ok", html_out)

    def test_explicit_ok_level_overrides_error_looking_text(self):
        url = "/?msg={}&level=ok".format(quote("Error-prone but actually fine"))
        _code, html_out = self._get(url)
        self.assertIn("status-ok", html_out)


class ApplyResolvedPendingTest(unittest.TestCase):
    """Pure tests for app.apply_resolved_pending — resolve_pending()'s
    field-level merge, replacing a whole-document save of the stale
    "pending" snapshot loaded before the (multi-second, per-entry) scrape."""

    def test_resolved_entry_moves_from_pending_to_anime(self):
        data = {"pending": [{"url": "https://x/a", "name": "A"}], "anime": []}
        resolved = [{"url": "https://x/a", "name": "A", "episodes": 0, "missing": []}]
        app.apply_resolved_pending(data, resolved)
        self.assertEqual(data["pending"], [])
        self.assertEqual([e["url"] for e in data["anime"]], ["https://x/a"])

    def test_user_removed_pending_entry_mid_scrape_stays_removed(self):
        # The dashboard removed this pending entry (via its own single-lock
        # update) while the resolver was mid-scrape on it — the fresh
        # snapshot no longer has it, so the resolved result must NOT
        # resurrect it into "anime".
        data = {"pending": [], "anime": []}
        resolved = [{"url": "https://x/a", "name": "A", "episodes": 0, "missing": []}]
        app.apply_resolved_pending(data, resolved)
        self.assertEqual(data["anime"], [])
        self.assertEqual(data["pending"], [])

    def test_user_added_pending_entry_mid_scrape_survives(self):
        # A pending entry added by the dashboard after the scrape pass
        # started — the resolver never saw it and named it in neither list —
        # must be left completely untouched.
        data = {"pending": [
            {"url": "https://x/a", "name": "A"},
            {"url": "https://x/new", "name": "New"},
        ], "anime": []}
        resolved = [{"url": "https://x/a", "name": "A", "episodes": 0, "missing": []}]
        app.apply_resolved_pending(data, resolved)
        self.assertEqual([e["url"] for e in data["pending"]], ["https://x/new"])

    def test_no_duplicate_when_entry_already_migrated(self):
        # A retried resolve for a URL already present in "anime" (e.g. a
        # previous pass's merge succeeded but the loop retried) must not
        # append a second copy.
        data = {"pending": [{"url": "https://x/a", "name": "A"}],
                "anime": [{"url": "https://x/a", "name": "A", "episodes": 3}]}
        resolved = [{"url": "https://x/a", "name": "A", "episodes": 0, "missing": []}]
        app.apply_resolved_pending(data, resolved)
        self.assertEqual(len(data["anime"]), 1)
        self.assertEqual(data["anime"][0]["episodes"], 3)  # untouched, not overwritten

    def test_no_match_flag_applied_to_fresh_entry(self):
        data = {"pending": [{"url": "https://x/a", "name": "A"}], "anime": []}
        app.apply_resolved_pending(data, [], no_match_urls={"https://x/a"})
        self.assertTrue(data["pending"][0]["no_match"])

    def test_no_match_flag_skipped_if_entry_no_longer_pending(self):
        data = {"pending": [], "anime": []}
        # Must not raise or fabricate an entry for a URL that's already gone.
        app.apply_resolved_pending(data, [], no_match_urls={"https://x/gone"})
        self.assertEqual(data["pending"], [])

    def test_other_pending_entries_untouched(self):
        data = {"pending": [
            {"url": "https://x/a", "name": "A"},
            {"url": "https://x/b", "name": "B", "no_match": True},
        ], "anime": []}
        resolved = [{"url": "https://x/a", "name": "A", "episodes": 0, "missing": []}]
        app.apply_resolved_pending(data, resolved)
        self.assertEqual([e["url"] for e in data["pending"]], ["https://x/b"])
        self.assertTrue(data["pending"][0]["no_match"])


class CachedHealthTest(unittest.TestCase):
    """_cached_health must not re-run `compute` (a network probe) inside the
    TTL window, and must recompute once it expires."""

    def setUp(self):
        self._orig_cache = dict(app._HEALTH_CACHE)
        app._HEALTH_CACHE.clear()

    def tearDown(self):
        app._HEALTH_CACHE.clear()
        app._HEALTH_CACHE.update(self._orig_cache)

    def test_result_reused_within_ttl(self):
        calls = []

        def compute():
            calls.append(1)
            return {"state": "ok"}

        first = app._cached_health("k1", 1000, compute)
        second = app._cached_health("k1", 1000, compute)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first, second)

    def test_recomputed_once_ttl_elapsed(self):
        calls = []

        def compute():
            calls.append(1)
            return {"state": "ok"}

        # ttl=0: "elapsed < ttl" is never true, so every call recomputes —
        # exercises expiry without a real sleep.
        app._cached_health("k2", 0, compute)
        app._cached_health("k2", 0, compute)
        self.assertEqual(len(calls), 2)

    def test_distinct_keys_cached_independently(self):
        calls = {"a": 0, "b": 0}
        app._cached_health("a", 1000, lambda: calls.__setitem__("a", calls["a"] + 1) or {"state": "ok"})
        app._cached_health("b", 1000, lambda: calls.__setitem__("b", calls["b"] + 1) or {"state": "ok"})
        app._cached_health("a", 1000, lambda: calls.__setitem__("a", calls["a"] + 1) or {"state": "ok"})
        self.assertEqual(calls["a"], 1)
        self.assertEqual(calls["b"], 1)


class CheckJdownloaderHealthTest(unittest.TestCase):
    """JDownloader reachability: a plain TCP probe of the configured local
    jdhost's CNL port, stubbed here so no real network call is ever made."""

    def setUp(self):
        app._HEALTH_CACHE.clear()
        fd, self._path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._orig_ani = app.ANI_JSON
        app.ANI_JSON = self._path
        self._orig_connect = app.socket.create_connection

    def tearDown(self):
        app.ANI_JSON = self._orig_ani
        app.socket.create_connection = self._orig_connect
        app._HEALTH_CACHE.clear()
        try:
            os.remove(self._path)
        except OSError:
            pass

    def _write_ani(self, settings):
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump({"settings": settings, "anime": []}, f)

    def test_no_jdhost_configured_is_unknown(self):
        self._write_ani({})
        result = app.check_jdownloader_health()
        self.assertEqual(result["state"], "unknown")
        self.assertIn("hint", result)

    def test_reachable_host_is_ok(self):
        self._write_ani({"jdhost": "127.0.0.1"})

        class FakeSock:
            def close(self):
                pass

        app.socket.create_connection = lambda addr, timeout=None: FakeSock()
        result = app.check_jdownloader_health()
        self.assertEqual(result["state"], "ok")
        self.assertNotIn("hint", result)

    def test_unreachable_host_is_fail_with_hint(self):
        self._write_ani({"jdhost": "127.0.0.1"})

        def raise_refused(addr, timeout=None):
            raise OSError("Connection refused")

        app.socket.create_connection = raise_refused
        result = app.check_jdownloader_health()
        self.assertEqual(result["state"], "fail")
        self.assertIn("hint", result)
        # The failure detail may echo the OSError text but never a credential.
        self.assertNotIn("password", result["detail"].lower())

    def test_result_cached_across_calls(self):
        self._write_ani({"jdhost": "127.0.0.1"})
        calls = []

        def fake_connect(addr, timeout=None):
            calls.append(addr)
            raise OSError("refused")

        app.socket.create_connection = fake_connect
        app.check_jdownloader_health()
        app.check_jdownloader_health()
        self.assertEqual(len(calls), 1)


class CheckTvdbHealthTest(unittest.TestCase):
    """TVDB health must distinguish "no key" (unknown) from "key present but
    invalid/unreachable" (fail) — `tvdb.available` alone only means non-empty."""

    def setUp(self):
        app._HEALTH_CACHE.clear()
        self._orig_available = app.tvdb.available
        self._orig_check = app.tvdb.check_health

    def tearDown(self):
        app.tvdb.available = self._orig_available
        app.tvdb.check_health = self._orig_check
        app._HEALTH_CACHE.clear()

    def test_no_key_is_unknown(self):
        app.tvdb.available = False
        result = app.check_tvdb_health()
        self.assertEqual(result["state"], "unknown")

    def test_valid_key_is_ok(self):
        app.tvdb.available = True
        app.tvdb.check_health = lambda: (True, "Token valid")
        result = app.check_tvdb_health()
        self.assertEqual(result["state"], "ok")
        self.assertNotIn("hint", result)

    def test_invalid_key_is_fail_with_hint(self):
        app.tvdb.available = True
        app.tvdb.check_health = lambda: (False, "TVDB login failed — check TVDB_API_KEY")
        result = app.check_tvdb_health()
        self.assertEqual(result["state"], "fail")
        self.assertIn("hint", result)

    def test_result_cached_across_calls(self):
        app.tvdb.available = True
        calls = []
        app.tvdb.check_health = lambda: (calls.append(1), (True, "Token valid"))[1]
        app.check_tvdb_health()
        app.check_tvdb_health()
        self.assertEqual(len(calls), 1)


class CheckDiskHealthTest(unittest.TestCase):
    """Disk health thresholds off (free GB, free %), for both watched dirs."""

    def setUp(self):
        app._HEALTH_CACHE.clear()
        self._orig_media = app.MEDIA_DIR
        self._orig_dl = app.DOWNLOAD_DIR
        self._orig_disk_usage = app.shutil.disk_usage

    def tearDown(self):
        app.MEDIA_DIR = self._orig_media
        app.DOWNLOAD_DIR = self._orig_dl
        app.shutil.disk_usage = self._orig_disk_usage
        app._HEALTH_CACHE.clear()

    def _fake_usage(self, total_gb, free_gb):
        usage = collections.namedtuple("usage", "total used free")
        total = int(total_gb * 1024 ** 3)
        free = int(free_gb * 1024 ** 3)
        return usage(total=total, used=total - free, free=free)

    def test_plenty_of_space_is_ok(self):
        app.shutil.disk_usage = lambda path: self._fake_usage(500, 400)
        result = app.check_disk_health()
        self.assertEqual(result["state"], "ok")
        self.assertNotIn("hint", result)

    def test_low_space_is_warn(self):
        # 8 GB free of 200 GB (4%) trips the GB threshold but not the %
        # threshold — must land on warn, not fail.
        app.shutil.disk_usage = lambda path: self._fake_usage(200, 8)
        result = app.check_disk_health()
        self.assertEqual(result["state"], "warn")
        self.assertIn("hint", result)

    def test_critical_space_is_fail(self):
        app.shutil.disk_usage = lambda path: self._fake_usage(500, 1)
        result = app.check_disk_health()
        self.assertEqual(result["state"], "fail")
        self.assertIn("hint", result)

    def test_worst_of_the_two_dirs_wins(self):
        def fake_usage(path):
            if path == app.MEDIA_DIR:
                return self._fake_usage(500, 400)  # plenty
            return self._fake_usage(500, 1)  # critical

        app.shutil.disk_usage = fake_usage
        result = app.check_disk_health()
        self.assertEqual(result["state"], "fail")

    def test_missing_mount_is_unknown(self):
        def raise_missing(path):
            raise OSError("No such file or directory")

        app.shutil.disk_usage = raise_missing
        result = app.check_disk_health()
        self.assertEqual(result["state"], "unknown")

    def test_result_cached_across_calls(self):
        calls = []

        def fake_usage(path):
            calls.append(path)
            return self._fake_usage(500, 400)

        app.shutil.disk_usage = fake_usage
        app.check_disk_health()
        app.check_disk_health()
        # Two dirs checked per probe, but the probe itself must run only once.
        self.assertEqual(len(calls), 2)


class CheckLoginHealthTest(unittest.TestCase):
    """Site-login health reads the bot-written `login` run_state key —
    absent, anonymous, ok, and failed."""

    def test_absent_key_is_unknown(self):
        result = app.check_login_health({})
        self.assertEqual(result["state"], "unknown")

    def test_not_configured_is_warn(self):
        result = app.check_login_health({"login": {"user_configured": False, "ok": False}})
        self.assertEqual(result["state"], "warn")
        self.assertIn("hint", result)

    def test_configured_and_ok_is_ok(self):
        result = app.check_login_health({"login": {"user_configured": True, "ok": True}})
        self.assertEqual(result["state"], "ok")
        self.assertNotIn("hint", result)

    def test_vip_shown_in_detail(self):
        result = app.check_login_health(
            {"login": {"user_configured": True, "ok": True, "vip": True}})
        self.assertIn("VIP", result["detail"])

    def test_configured_and_failed_is_fail(self):
        result = app.check_login_health(
            {"login": {"user_configured": True, "ok": False, "error": "Login data is invalid"}})
        self.assertEqual(result["state"], "fail")
        self.assertIn("Login data is invalid", result["detail"])
        self.assertIn("hint", result)


class CheckBotStalenessTest(unittest.TestCase):
    """Bot-cycle staleness: warn once a cycle is well overdue relative to its
    own configured interval."""

    def setUp(self):
        self._orig_now = app._utc_now

    def tearDown(self):
        app._utc_now = self._orig_now

    def test_no_last_run_is_unknown(self):
        result = app.check_bot_staleness({})
        self.assertEqual(result["state"], "unknown")

    def test_recent_cycle_is_ok(self):
        app._utc_now = lambda: datetime(2026, 6, 13, 19, 25, 0)
        result = app.check_bot_staleness({
            "last_run": {"finished_ts": "2026-06-13T19:20:00Z", "timedelay": 600},
        })
        self.assertEqual(result["state"], "ok")

    def test_overdue_cycle_is_warn(self):
        # timedelay=600 (10 min) → threshold is 20 min; 45 min elapsed is stale.
        app._utc_now = lambda: datetime(2026, 6, 13, 20, 5, 0)
        result = app.check_bot_staleness({
            "last_run": {"finished_ts": "2026-06-13T19:20:00Z", "timedelay": 600},
        })
        self.assertEqual(result["state"], "warn")
        self.assertIn("hint", result)


class RenderHealthCardTest(unittest.TestCase):
    """render_health_card: badge per state, hint only on non-ok rows, and
    output is HTML-escaped (never renders a credential either way)."""

    def setUp(self):
        self._orig_get_health = app.get_health

    def tearDown(self):
        app.get_health = self._orig_get_health

    def test_ok_row_has_no_hint(self):
        app.get_health = lambda: [("Site Login", {"state": "ok", "detail": "Logged in"})]
        out = app.render_health_card()
        self.assertIn("badge-ok", out)
        self.assertIn("Logged in", out)
        self.assertNotIn("hint", out)

    def test_fail_row_renders_hint(self):
        app.get_health = lambda: [
            ("JDownloader", {"state": "fail", "detail": "Unreachable", "hint": "Check the host."})
        ]
        out = app.render_health_card()
        self.assertIn("badge-danger", out)
        self.assertIn("Check the host.", out)

    def test_warn_and_unknown_badges(self):
        app.get_health = lambda: [
            ("Disk Space", {"state": "warn", "detail": "low"}),
            ("TVDB", {"state": "unknown", "detail": "no key"}),
        ]
        out = app.render_health_card()
        self.assertIn("badge-warn", out)
        self.assertIn("badge-neutral", out)

    def test_detail_and_hint_are_escaped(self):
        app.get_health = lambda: [
            ("X", {"state": "fail", "detail": "<script>bad</script>", "hint": "<b>hint</b>"}),
        ]
        out = app.render_health_card()
        self.assertNotIn("<script>", out)
        self.assertNotIn("<b>hint</b>", out)


class GetHealthDefaultStateTest(unittest.TestCase):
    """With no ani.json/run_state/TVDB key present, get_health must return one
    row per check without touching the network (jdhost/TVDB key both absent
    → unknown, no probe attempted)."""

    def setUp(self):
        fd, self._path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self._path)  # load_ani() must tolerate a missing file too
        self._orig_ani = app.ANI_JSON
        self._orig_rs = app.RUN_STATE_FILE
        self._orig_tvdb_available = app.tvdb.available
        app.ANI_JSON = self._path
        app.RUN_STATE_FILE = os.path.join(tempfile.gettempdir(), "aniloads-no-health-run-state.json")
        app.tvdb.available = False
        app._HEALTH_CACHE.clear()

    def tearDown(self):
        app.ANI_JSON = self._orig_ani
        app.RUN_STATE_FILE = self._orig_rs
        app.tvdb.available = self._orig_tvdb_available
        app._HEALTH_CACHE.clear()

    def test_five_rows_returned(self):
        rows = app.get_health()
        labels = [label for label, _ in rows]
        self.assertEqual(labels, ["Site Login", "JDownloader", "TVDB", "Disk Space", "Bot Cycles"])
        for _label, result in rows:
            self.assertIn(result["state"], {"ok", "warn", "fail", "unknown"})


class GetHealthResilientToRaisingCheckTest(unittest.TestCase):
    """A single check raising must degrade only that row to "unknown" — the
    other rows still render instead of the whole card/poll breaking."""

    def setUp(self):
        self._orig_tvdb_check = app.check_tvdb_health

    def tearDown(self):
        app.check_tvdb_health = self._orig_tvdb_check

    def test_other_rows_still_render_when_one_check_raises(self):
        def boom():
            raise RuntimeError("boom")
        app.check_tvdb_health = boom
        rows = app.get_health()
        labels = [label for label, _ in rows]
        self.assertEqual(labels, ["Site Login", "JDownloader", "TVDB", "Disk Space", "Bot Cycles"])
        by_label = dict(rows)
        self.assertEqual(by_label["TVDB"]["state"], "unknown")
        self.assertIn("boom", by_label["TVDB"]["detail"])
        # The rest are unaffected by TVDB's failure.
        self.assertIn(by_label["Disk Space"]["state"], {"ok", "warn", "fail", "unknown"})


class RunNowTriggerTest(unittest.TestCase):
    """Soft run-now/check-now: writes a trigger file for the bot to wake on
    instead of restarting the container (see bot/anibot.py's
    sleep_until_next_cycle / consume_run_now_trigger), subject to a shared
    cooldown."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="aniloads-runnow-")
        self._orig_ani = app.ANI_JSON
        self._orig_run_now = app.RUN_NOW_FILE
        self._orig_run_now_state = app.RUN_NOW_STATE_FILE
        self._orig_cooldown = app.RUN_NOW_COOLDOWN_SECONDS
        app.ANI_JSON = os.path.join(self._tmp, "ani.json")
        app.RUN_NOW_FILE = os.path.join(self._tmp, "run_now")
        app.RUN_NOW_STATE_FILE = os.path.join(self._tmp, "run_now_last.json")
        app.RUN_NOW_COOLDOWN_SECONDS = 120

    def tearDown(self):
        app.ANI_JSON = self._orig_ani
        app.RUN_NOW_FILE = self._orig_run_now
        app.RUN_NOW_STATE_FILE = self._orig_run_now_state
        app.RUN_NOW_COOLDOWN_SECONDS = self._orig_cooldown
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_global_trigger_writes_file_and_cooldown_state(self):
        ok, msg = app.trigger_run_now()
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(app.RUN_NOW_FILE))
        self.assertIn("queued", msg.lower())
        self.assertTrue(os.path.isfile(app.RUN_NOW_STATE_FILE))

    def test_no_prior_request_has_no_cooldown(self):
        self.assertEqual(app.run_now_cooldown_remaining(), 0)

    def test_second_request_within_cooldown_is_rejected(self):
        ok1, _ = app.trigger_run_now()
        self.assertTrue(ok1)
        ok2, msg2 = app.trigger_run_now()
        self.assertFalse(ok2)
        self.assertIn("Cooling down", msg2)

    def test_cooldown_elapses(self):
        app.trigger_run_now()
        self.assertGreater(app.run_now_cooldown_remaining(), 0)
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=app.RUN_NOW_COOLDOWN_SECONDS + 1)
        self.assertEqual(app.run_now_cooldown_remaining(now=future), 0)

    def test_per_entry_check_now_sets_force_check(self):
        app.save_ani({"anime": [{"name": "A", "url": "http://x/a"}]})
        ok, msg = app.trigger_run_now(entry_url="http://x/a")
        self.assertTrue(ok)
        self.assertIn("A", msg)
        data = app.load_ani()
        self.assertTrue(data["anime"][0]["force_check"])
        self.assertTrue(os.path.isfile(app.RUN_NOW_FILE))

    def test_per_entry_check_now_missing_entry_errors(self):
        app.save_ani({"anime": []})
        ok, msg = app.trigger_run_now(entry_url="http://x/missing")
        self.assertFalse(ok)
        self.assertIn("not found", msg.lower())
        self.assertFalse(os.path.isfile(app.RUN_NOW_FILE))

    def test_check_now_also_subject_to_cooldown_and_leaves_entry_untouched(self):
        app.save_ani({"anime": [{"name": "A", "url": "http://x/a"}]})
        app.trigger_run_now()
        ok, msg = app.trigger_run_now(entry_url="http://x/a")
        self.assertFalse(ok)
        self.assertIn("Cooling down", msg)
        # The rejected request must not have set force_check.
        data = app.load_ani()
        self.assertNotIn("force_check", data["anime"][0])

    @unittest.skipIf(sys.platform == "win32",
                      "POSIX permission bits; not meaningful on the Windows test host")
    def test_trigger_file_is_world_readable(self):
        app.trigger_run_now()
        mode = stat.S_IMODE(os.stat(app.RUN_NOW_FILE).st_mode)
        self.assertTrue(mode & 0o004,
                         "trigger file must be readable by others — the bot "
                         "container consumes it as a different (root) user")


class RunNowCheckNowPostTest(unittest.TestCase):
    """do_POST dispatch for /run-now and /check-now, using the same
    __new__/stub harness as HandlerPostRoutingTest."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="aniloads-runnow-post-")
        self._orig_ani = app.ANI_JSON
        self._orig_run_now = app.RUN_NOW_FILE
        self._orig_run_now_state = app.RUN_NOW_STATE_FILE
        self._orig_cooldown = app.RUN_NOW_COOLDOWN_SECONDS
        app.ANI_JSON = os.path.join(self._tmp, "ani.json")
        app.RUN_NOW_FILE = os.path.join(self._tmp, "run_now")
        app.RUN_NOW_STATE_FILE = os.path.join(self._tmp, "run_now_last.json")
        app.RUN_NOW_COOLDOWN_SECONDS = 120

    def tearDown(self):
        app.ANI_JSON = self._orig_ani
        app.RUN_NOW_FILE = self._orig_run_now
        app.RUN_NOW_STATE_FILE = self._orig_run_now_state
        app.RUN_NOW_COOLDOWN_SECONDS = self._orig_cooldown
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _post(self, path, params):
        captured = {}
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h._read_post = lambda: params
        h._redirect_msg = lambda msg, level=None: captured.update(msg=msg, level=level)
        h._redirect = lambda url: captured.__setitem__("url", url)
        h._respond = lambda code, html_body: captured.__setitem__("html", html_body)
        h.do_POST()
        return captured

    def test_run_now_queues(self):
        result = self._post("/run-now", {})
        self.assertIn("queued", result["msg"].lower())
        self.assertTrue(os.path.isfile(app.RUN_NOW_FILE))

    def test_run_now_within_cooldown_errors(self):
        self._post("/run-now", {})
        result = self._post("/run-now", {})
        self.assertTrue(result["msg"].startswith("Error:"))
        self.assertEqual(result.get("level"), "err")

    def test_check_now_sets_force_check_and_queues(self):
        app.save_ani({"anime": [{"name": "A", "url": "http://x/a"}]})
        result = self._post("/check-now", {"key": "http://x/a"})
        self.assertIn("queued", result["msg"].lower())
        self.assertTrue(app.load_ani()["anime"][0]["force_check"])

    def test_check_now_missing_key_errors(self):
        result = self._post("/check-now", {"key": "http://x/missing"})
        self.assertTrue(result["msg"].startswith("Error:"))
        self.assertEqual(result.get("level"), "err")


class AddUrlClobberTest(unittest.TestCase):
    """/add-url used to load ani.json, run the multi-second Selenium scrape,
    then mutate/save that now-stale in-memory snapshot — a bot-cycle write
    landing during the scrape got silently clobbered. It must scrape FIRST,
    then read-modify-write inside ONE update_ani lock hold."""

    def setUp(self):
        fd, self._ani_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(self._ani_path, "w", encoding="utf-8") as f:
            json.dump({"settings": {}, "anime": []}, f)
        self._orig_ani = app.ANI_JSON
        app.ANI_JSON = self._ani_path

        self._orig_get_releases = app.get_releases
        self._scrape_started = threading.Event()
        self._release_scrape = threading.Event()

        def blocking_get_releases(url):
            self._scrape_started.set()
            self._release_scrape.wait(timeout=5)
            return None, "stubbed: unavailable"

        app.get_releases = blocking_get_releases

    def tearDown(self):
        app.get_releases = self._orig_get_releases
        app.ANI_JSON = self._orig_ani
        try:
            os.remove(self._ani_path)
        except OSError:
            pass

    def _post(self, path, params):
        captured = {}
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h._read_post = lambda: params
        h._redirect_msg = lambda msg, level=None: captured.update(msg=msg, level=level)
        h._redirect = lambda url: captured.__setitem__("url", url)
        h._respond = lambda code, html_body: captured.__setitem__("html", html_body)
        h.do_POST()
        return captured

    def test_bot_write_during_scrape_survives_add_url_fallback_save(self):
        url = "https://www.anime-loads.org/anime/new-show"
        result = {}

        def do_add_url():
            result["captured"] = self._post("/add-url", {"url": url})

        poster = threading.Thread(target=do_add_url)
        poster.start()
        self.assertTrue(self._scrape_started.wait(timeout=5), "scrape never started")

        # A bot-cycle write landing while the scrape above is still in
        # flight — this must survive /add-url's own save once the scrape
        # (and its fallback-to-pending save) completes.
        app.update_ani(lambda data: data.setdefault("anime", []).append(
            {"url": "https://www.anime-loads.org/anime/existing", "name": "Existing"}))

        self._release_scrape.set()
        poster.join(timeout=5)

        with open(self._ani_path, encoding="utf-8") as f:
            saved = json.load(f)

        anime_urls = {a.get("url") for a in saved.get("anime", [])}
        pending_urls = {p.get("url") for p in saved.get("pending", [])}
        self.assertIn("https://www.anime-loads.org/anime/existing", anime_urls,
                       "bot's concurrent write was clobbered by /add-url's stale snapshot")
        self.assertIn(url, pending_urls)


class ThreadedServerConcurrencyTest(unittest.TestCase):
    """The core fix: a slow scrape-backed handler must not block a
    concurrent /api/status request. Runs a real ThreadingHTTPServer bound to
    a real (ephemeral) socket — this only proves anything if the server
    under test is actually multi-threaded."""

    def setUp(self):
        fd, self._ani_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(self._ani_path, "w", encoding="utf-8") as f:
            json.dump({"settings": {}, "anime": []}, f)
        self._orig_ani = app.ANI_JSON
        app.ANI_JSON = self._ani_path

        self._orig_get_releases = app.get_releases
        self._scrape_started = threading.Event()

        def slow_get_releases(url):
            self._scrape_started.set()
            time.sleep(1.0)
            return None, "stubbed: unavailable"

        app.get_releases = slow_get_releases

        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.server.server_address[1]
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        app.get_releases = self._orig_get_releases
        app.ANI_JSON = self._orig_ani
        try:
            os.remove(self._ani_path)
        except OSError:
            pass

    def _request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_slow_add_url_does_not_block_concurrent_status_poll(self):
        results = {}

        def do_slow_post():
            results["post_status"] = self._request(
                "POST", "/add-url", body="url=" + quote("https://www.anime-loads.org/anime/x"))

        poster = threading.Thread(target=do_slow_post)
        poster.start()
        self.assertTrue(self._scrape_started.wait(timeout=5),
                         "slow /add-url handler never started")

        start = time.monotonic()
        status_code = self._request("GET", "/api/status")
        elapsed = time.monotonic() - start
        poster.join(timeout=5)

        self.assertEqual(status_code, 200)
        # The stubbed scrape sleeps 1s while holding no lock — a concurrent
        # /api/status answered in well under that proves it ran on its own
        # thread instead of queuing behind a single-threaded accept loop.
        self.assertLess(elapsed, 0.5)
        self.assertEqual(results.get("post_status"), 303)


class AddFlowHiddenFieldsTest(unittest.TestCase):
    """The release -> TVDB -> season forms carry media_type/episode-count/
    release-id metadata as hidden fields (render_releases onward) so
    /add-release and /tvdb-seasons never need to re-scrape to re-derive or
    double-check them — except to recover from a value that doesn't check
    out (missing, or tampered)."""

    def setUp(self):
        fd, self._ani_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(self._ani_path, "w", encoding="utf-8") as f:
            json.dump({"settings": {}, "anime": []}, f)
        self._orig_ani = app.ANI_JSON
        app.ANI_JSON = self._ani_path

        self._orig_get_releases = app.get_releases
        self.scrape_calls = []

        def counting_get_releases(url):
            self.scrape_calls.append(url)
            return {
                "name": "Test Anime",
                "url": url,
                "releases": [
                    {"id": 111, "resolution": 1080, "dubs": ["german"], "subs": [],
                     "episodes": 12, "size_mb": 4000, "group": "grp"},
                ],
                "media_type": "series",
                "year": 2020,
                "display_title": "Test Anime",
            }, None

        app.get_releases = counting_get_releases

        self._orig_tvdb_available = app.tvdb.available
        app.tvdb.available = False

        # These walk the release picker by hand, so auto-select stays off.
        fd, self._prefs_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._orig_prefs = app.PREFS_FILE
        app.PREFS_FILE = self._prefs_path
        app.save_prefs({"audio_language": "german", "sub_language": "any",
                        "min_resolution": 1080, "auto_select": False})

    def tearDown(self):
        app.get_releases = self._orig_get_releases
        app.tvdb.available = self._orig_tvdb_available
        app.PREFS_FILE = self._orig_prefs
        try:
            os.remove(self._prefs_path)
        except OSError:
            pass
        app.ANI_JSON = self._orig_ani
        try:
            os.remove(self._ani_path)
        except OSError:
            pass

    def _post(self, path, params):
        captured = {}
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h._read_post = lambda: params
        h._redirect_msg = lambda msg, level=None: captured.update(msg=msg, level=level)
        h._redirect = lambda url: captured.__setitem__("url", url)
        h._respond = lambda code, html_body: captured.__setitem__("html", html_body)
        h.do_POST()
        return captured

    def _hidden_field(self, html_out, name):
        m = re.search(r'name="{}" value="([^"]*)"'.format(re.escape(name)), html_out)
        self.assertIsNotNone(m, "missing hidden field {!r} in:\n{}".format(name, html_out))
        return html.unescape(m.group(1))

    def test_add_flow_scrapes_at_most_once(self):
        url = "https://www.anime-loads.org/anime/x"
        add_url_result = self._post("/add-url", {"url": url})
        self.assertEqual(len(self.scrape_calls), 1)
        self.assertIn("Add this release", add_url_result["html"])

        html_out = add_url_result["html"]
        release_params = {
            "url": self._hidden_field(html_out, "url"),
            "name": self._hidden_field(html_out, "name"),
            "release_id": self._hidden_field(html_out, "release_id"),
            "release_ids": self._hidden_field(html_out, "release_ids"),
            "episodes": self._hidden_field(html_out, "episodes"),
            "custom_folder": "",
            "media_type": self._hidden_field(html_out, "media_type"),
        }

        add_release_result = self._post("/add-release", release_params)
        self.assertEqual(len(self.scrape_calls), 1, "add-release re-scraped")
        self.assertTrue(add_release_result["msg"].startswith("Added:"))

        with open(self._ani_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(len(saved["anime"]), 1)
        self.assertEqual(saved["anime"][0]["releaseID"], 111)

    def test_add_release_rejects_tampered_release_id(self):
        url = "https://www.anime-loads.org/anime/x"
        self._post("/add-url", {"url": url})
        self.assertEqual(len(self.scrape_calls), 1)

        tampered_params = {
            "url": url,
            "name": "Test Anime",
            "release_id": "999",  # never one of the ids actually offered
            "release_ids": "111",
            "episodes": "12",
            "custom_folder": "",
            "media_type": "series",
        }
        result = self._post("/add-release", tampered_params)
        self.assertTrue(result["msg"].startswith("Error"))

        with open(self._ani_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["anime"], [])

    def test_add_release_rejects_tampered_episode_count(self):
        url = "https://www.anime-loads.org/anime/x"
        self._post("/add-url", {"url": url})
        self.assertEqual(len(self.scrape_calls), 1)

        # release_id/media_type check out, but episodes is out of range —
        # the whole selection must still be re-derived from a fresh scrape
        # rather than trusting the tampered episode count.
        tampered_params = {
            "url": url,
            "name": "Test Anime",
            "release_id": "111",
            "release_ids": "111",
            "episodes": "999999",
            "custom_folder": "",
            "media_type": "series",
        }
        result = self._post("/add-release", tampered_params)
        self.assertEqual(len(self.scrape_calls), 2, "did not re-derive from a fresh scrape")
        self.assertTrue(result["msg"].startswith("Added:"))

        with open(self._ani_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["anime"][0]["releaseID"], 111)


class _SyncThread:
    """Stand-in for threading.Thread that runs its target synchronously, so
    a test can assert on a notify send dispatched onto a daemon thread
    without racing it."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class NotifyMoverEventsTest(unittest.TestCase):
    """_notify_mover_events: batches this cycle's NEW mover errors/stuck
    items into one notification. No network — send_all is patched and the
    dispatch thread replaced with a synchronous stand-in."""

    def setUp(self):
        self._orig_targets = app.NOTIFY_TARGETS
        self._orig_thread = app.threading.Thread
        app.threading.Thread = _SyncThread
        self.sent = []
        self._orig_send_all = app.notify.send_all
        app.notify.send_all = lambda targets, title, message: self.sent.append(
            (targets, title, message))

    def tearDown(self):
        app.NOTIFY_TARGETS = self._orig_targets
        app.threading.Thread = self._orig_thread
        app.notify.send_all = self._orig_send_all

    def test_no_targets_configured_sends_nothing(self):
        app.NOTIFY_TARGETS = []
        app._notify_mover_events([{"type": "error", "msg": "boom"}])
        self.assertEqual(self.sent, [])

    def test_no_noteworthy_events_sends_nothing(self):
        app.NOTIFY_TARGETS = ["fake-target"]
        app._notify_mover_events([
            {"type": "moved", "msg": "x -> y"},
            {"type": "wait", "msg": "still active"},
            {"type": "cleanup", "msg": "cleaned archive"},
        ])
        self.assertEqual(self.sent, [])

    def test_empty_events_sends_nothing(self):
        app.NOTIFY_TARGETS = ["fake-target"]
        app._notify_mover_events([])
        self.assertEqual(self.sent, [])

    def test_error_events_are_batched_into_one_message(self):
        app.NOTIFY_TARGETS = ["fake-target"]
        events = [
            {"type": "error", "msg": "Cannot parse season/episode: foo.mkv"},
            {"type": "moved", "msg": "bar.mkv -> Show/S01"},
        ]
        app._notify_mover_events(events)
        self.assertEqual(len(self.sent), 1)
        targets, title, message = self.sent[0]
        self.assertEqual(targets, ["fake-target"])
        self.assertEqual(title, "Aniloads")
        self.assertIn("1 mover issue", message)
        self.assertIn("Cannot parse season/episode: foo.mkv", message)
        self.assertNotIn("bar.mkv -> Show/S01", message)

    def test_skip_events_count_as_stuck_items(self):
        app.NOTIFY_TARGETS = ["fake-target"]
        events = [{"type": "skip", "msg": "foo.mkv — already exists"}]
        app._notify_mover_events(events)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("already exists", self.sent[0][2])

    def test_more_than_five_events_are_truncated_with_a_count(self):
        app.NOTIFY_TARGETS = ["fake-target"]
        events = [{"type": "error", "msg": "err{}".format(i)} for i in range(7)]
        app._notify_mover_events(events)
        message = self.sent[0][2]
        self.assertIn("7 mover issues", message)
        self.assertIn("+2 more", message)


class NotifyMoverOncePerStuckItemTest(unittest.TestCase):
    """End-to-end with run_move_cycle(): a stuck item notifies on the cycle
    it first appears, then goes quiet on later cycles while unresolved, and
    stays quiet once dashboard-ignored — mirroring the existing
    'not repeated'/'ignored' coverage in RunMoveCycleTest, but through the
    notify hook instead of the raw events list."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aniloads-move-notify-")
        self.download = os.path.join(self.tmp, "downloads")
        self.media = os.path.join(self.tmp, "media")
        os.makedirs(self.download)
        os.makedirs(self.media)
        self._orig = {k: getattr(app, k) for k in
                      ("DOWNLOAD_DIR", "MEDIA_DIR", "MIN_AGE_MINUTES", "ANI_JSON")}
        app.DOWNLOAD_DIR = self.download
        app.MEDIA_DIR = self.media
        app.MIN_AGE_MINUTES = 5
        app.ANI_JSON = os.path.join(self.tmp, "ani.json")
        with open(app.ANI_JSON, "w", encoding="utf-8") as f:
            json.dump({"anime": []}, f)

        self._orig_stuck = dict(app._stuck_items)
        app._stuck_items.clear()

        self._orig_targets = app.NOTIFY_TARGETS
        app.NOTIFY_TARGETS = ["fake-target"]
        self._orig_thread = app.threading.Thread
        app.threading.Thread = _SyncThread
        self.sent = []
        self._orig_send_all = app.notify.send_all
        app.notify.send_all = lambda targets, title, message: self.sent.append(
            (targets, title, message))

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(app, k, v)
        app._stuck_items.clear()
        app._stuck_items.update(self._orig_stuck)
        app.NOTIFY_TARGETS = self._orig_targets
        app.threading.Thread = self._orig_thread
        app.notify.send_all = self._orig_send_all
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_unparseable_download(self):
        d = os.path.join(self.download, "Akira")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "Akira.Movie.1080p.mkv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("x")
        past = time.time() - 3600
        os.utime(p, (past, past))

    def test_notifies_once_then_stays_quiet_while_unresolved(self):
        self._make_unparseable_download()

        app._notify_mover_events(app.run_move_cycle())
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Cannot parse season/episode", self.sent[0][2])

        app._notify_mover_events(app.run_move_cycle())
        self.assertEqual(len(self.sent), 1)  # no repeat notification

    def test_ignored_item_never_notifies_again(self):
        self._make_unparseable_download()
        app._notify_mover_events(app.run_move_cycle())
        self.assertEqual(len(self.sent), 1)

        key = next(iter(app._stuck_items))
        app.stuck_ignore(key)

        app._notify_mover_events(app.run_move_cycle())
        self.assertEqual(len(self.sent), 1)  # still just the first notification


class _DashboardServerTestBase(unittest.TestCase):
    """Shared real-HTTP-server fixture for the auth/CSRF gate tests below.
    Runs a genuine ThreadingHTTPServer bound to an ephemeral port so the gate
    (Handler.parse_request) is exercised exactly as a real client hits it,
    not by calling its methods directly."""

    def setUp(self):
        fd, self._ani_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(self._ani_path, "w", encoding="utf-8") as f:
            json.dump({"settings": {}, "anime": []}, f)
        self._orig_ani = app.ANI_JSON
        app.ANI_JSON = self._ani_path

        self._orig_auth_enabled = app.AUTH_ENABLED
        self._orig_user = app.DASHBOARD_USER
        self._orig_pass = app.DASHBOARD_PASS
        app._move_trigger.clear()

        # These tests use POST /move-now purely as a probe for the auth/CSRF
        # gate, not to exercise the mover itself — mount a real dir so that
        # probe isn't rejected for an unrelated reason (DOWNLOAD_DIR missing).
        self._orig_download_dir = app.DOWNLOAD_DIR
        self._download_dir = tempfile.mkdtemp(prefix="aniloads-dl-")
        app.DOWNLOAD_DIR = self._download_dir

        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.server.server_address[1]
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        app.AUTH_ENABLED = self._orig_auth_enabled
        app.DASHBOARD_USER = self._orig_user
        app.DASHBOARD_PASS = self._orig_pass
        app._move_trigger.clear()
        app.ANI_JSON = self._orig_ani
        app.DOWNLOAD_DIR = self._orig_download_dir
        shutil.rmtree(self._download_dir, ignore_errors=True)
        try:
            os.remove(self._ani_path)
        except OSError:
            pass

    def _request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if body and "Content-Type" not in hdrs:
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        # resp.msg is an email.message.Message: case-insensitive .get(),
        # unlike a plain dict built from getheaders().
        resp_headers = resp.msg
        conn.close()
        return resp.status, resp_headers, data


class DashboardAuthDisabledTest(_DashboardServerTestBase):
    """Off (today's behavior) whenever either env var is missing."""

    def test_200_when_both_unset(self):
        app.AUTH_ENABLED = False
        app.DASHBOARD_USER = ""
        app.DASHBOARD_PASS = ""
        status, _, _ = self._request("GET", "/")
        self.assertEqual(status, 200)

    def test_200_when_only_user_set(self):
        app.DASHBOARD_USER = "u"
        app.DASHBOARD_PASS = ""
        app.AUTH_ENABLED = False
        status, _, _ = self._request("GET", "/")
        self.assertEqual(status, 200)

    def test_200_when_only_pass_set(self):
        app.DASHBOARD_USER = ""
        app.DASHBOARD_PASS = "p"
        app.AUTH_ENABLED = False
        status, _, _ = self._request("GET", "/")
        self.assertEqual(status, 200)


class DashboardAuthEnabledTest(_DashboardServerTestBase):
    def setUp(self):
        super().setUp()
        app.DASHBOARD_USER = "tester"
        app.DASHBOARD_PASS = "s3cret-pw"
        app.AUTH_ENABLED = True

    def _basic(self, user, password):
        token = base64.b64encode("{}:{}".format(user, password).encode("utf-8")).decode("ascii")
        return {"Authorization": "Basic " + token}

    def test_no_header_401(self):
        status, headers, _ = self._request("GET", "/")
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers.get("WWW-Authenticate", ""))

    def test_wrong_user_401(self):
        status, _, _ = self._request("GET", "/", headers=self._basic("nope", "s3cret-pw"))
        self.assertEqual(status, 401)

    def test_wrong_pass_401(self):
        status, _, _ = self._request("GET", "/", headers=self._basic("tester", "wrong"))
        self.assertEqual(status, 401)

    def test_malformed_base64_401(self):
        status, _, _ = self._request(
            "GET", "/", headers={"Authorization": "Basic !!!not-base64!!!"})
        self.assertEqual(status, 401)

    def test_non_basic_scheme_401(self):
        status, _, _ = self._request("GET", "/", headers={"Authorization": "Bearer abc123"})
        self.assertEqual(status, 401)

    def test_correct_creds_root_200(self):
        status, _, _ = self._request("GET", "/", headers=self._basic("tester", "s3cret-pw"))
        self.assertEqual(status, 200)

    def test_correct_creds_api_status_200(self):
        status, _, _ = self._request(
            "GET", "/api/status", headers=self._basic("tester", "s3cret-pw"))
        self.assertEqual(status, 200)

    def test_correct_creds_post_ok(self):
        status, _, _ = self._request(
            "POST", "/move-now", headers=self._basic("tester", "s3cret-pw"))
        self.assertEqual(status, 303)
        self.assertTrue(app._move_trigger.is_set())

    def test_password_never_logged(self):
        log_records = []
        handler = logging.Handler()
        handler.emit = lambda record: log_records.append(record.getMessage())
        app._log.addHandler(handler)
        try:
            creds_headers = self._basic("tester", "wrong-password-xyz")
            self._request("GET", "/", headers=creds_headers)
        finally:
            app._log.removeHandler(handler)
        combined = "\n".join(log_records)
        self.assertNotIn("wrong-password-xyz", combined)
        self.assertNotIn(creds_headers["Authorization"], combined)


class CSRFProtectionTest(_DashboardServerTestBase):
    """Always on, independent of dashboard auth — isolated here by leaving
    auth disabled so a rejection can only be attributed to CSRF."""

    def setUp(self):
        super().setUp()
        app.AUTH_ENABLED = False

    def test_cross_origin_post_403_and_no_write(self):
        status, _, _ = self._request(
            "POST", "/move-now", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertFalse(app._move_trigger.is_set())

    def test_origin_null_403(self):
        status, _, _ = self._request("POST", "/move-now", headers={"Origin": "null"})
        self.assertEqual(status, 403)
        self.assertFalse(app._move_trigger.is_set())

    def test_same_origin_post_ok(self):
        origin = "http://127.0.0.1:{}".format(self.port)
        status, _, _ = self._request("POST", "/move-now", headers={"Origin": origin})
        self.assertEqual(status, 303)
        self.assertTrue(app._move_trigger.is_set())

    def test_referer_only_mismatch_403(self):
        status, _, _ = self._request(
            "POST", "/move-now", headers={"Referer": "http://evil.example/page"})
        self.assertEqual(status, 403)
        self.assertFalse(app._move_trigger.is_set())

    def test_referer_only_match_ok(self):
        referer = "http://127.0.0.1:{}/".format(self.port)
        status, _, _ = self._request("POST", "/move-now", headers={"Referer": referer})
        self.assertEqual(status, 303)
        self.assertTrue(app._move_trigger.is_set())

    def test_neither_header_ok(self):
        status, _, _ = self._request("POST", "/move-now")
        self.assertEqual(status, 303)
        self.assertTrue(app._move_trigger.is_set())


class ReverseProxyCSRFTest(_DashboardServerTestBase):
    """A reverse proxy (e.g. nginx's default proxy_pass) forwards the
    UPSTREAM Host, not the public name the browser's Origin carries — so the
    CSRF check must also accept X-Forwarded-Host and an explicit
    DASHBOARD_ALLOWED_ORIGINS allowlist, not just an exact Host match."""

    def setUp(self):
        super().setUp()
        app.AUTH_ENABLED = False
        self._orig_allowed = app.DASHBOARD_ALLOWED_ORIGINS

    def tearDown(self):
        app.DASHBOARD_ALLOWED_ORIGINS = self._orig_allowed
        super().tearDown()

    def test_forwarded_host_matching_origin_ok(self):
        # Host (as seen by the dashboard) is "127.0.0.1:<port>" — the real
        # request's own connection — but the proxy rewrote it and tells the
        # dashboard the public name via X-Forwarded-Host, matching Origin.
        status, _, _ = self._request(
            "POST", "/move-now",
            headers={
                "Origin": "https://aniloads.home.lan",
                "X-Forwarded-Host": "aniloads.home.lan",
            })
        self.assertEqual(status, 303)
        self.assertTrue(app._move_trigger.is_set())

    def test_allowlisted_origin_ok(self):
        app.DASHBOARD_ALLOWED_ORIGINS = {"https://aniloads.home.lan"}
        status, _, _ = self._request(
            "POST", "/move-now", headers={"Origin": "https://aniloads.home.lan"})
        self.assertEqual(status, 303)
        self.assertTrue(app._move_trigger.is_set())

    def test_mismatched_everything_403_with_hint(self):
        app.DASHBOARD_ALLOWED_ORIGINS = set()
        status, _, body = self._request(
            "POST", "/move-now", headers={"Origin": "https://aniloads.home.lan"})
        self.assertEqual(status, 403)
        self.assertFalse(app._move_trigger.is_set())
        text = body.decode("utf-8")
        self.assertIn("X-Forwarded-Host", text)
        self.assertIn("DASHBOARD_ALLOWED_ORIGINS", text)


class NormalizeAnimeUrlTest(unittest.TestCase):
    def test_folds_scheme_host_case_www_and_trailing_slash(self):
        base = app.normalize_anime_url("https://www.anime-loads.org/media/frieren")
        for variant in (
            "https://anime-loads.org/media/frieren/",
            "HTTPS://WWW.Anime-Loads.org/media/frieren",
            "http://anime-loads.org/media/frieren",
            "  https://www.anime-loads.org/media/frieren/  ",
        ):
            self.assertEqual(app.normalize_anime_url(variant), base, variant)

    def test_path_case_and_different_slug_stay_distinct(self):
        base = app.normalize_anime_url("https://anime-loads.org/media/frieren")
        self.assertNotEqual(app.normalize_anime_url("https://anime-loads.org/media/Frieren"), base)
        self.assertNotEqual(app.normalize_anime_url("https://anime-loads.org/media/frieren-2"), base)

    def test_find_duplicate_entry_checks_anime_and_pending(self):
        data = {
            "anime": [{"url": "https://www.anime-loads.org/media/a", "name": "A"}],
            "pending": [{"url": "https://www.anime-loads.org/media/b", "name": "B"}],
        }
        self.assertEqual(app.find_duplicate_entry(data, "https://anime-loads.org/media/a/")["name"], "A")
        self.assertEqual(app.find_duplicate_entry(data, "https://anime-loads.org/media/b/")["name"], "B")
        self.assertIsNone(app.find_duplicate_entry(data, "https://anime-loads.org/media/c"))
        self.assertIsNone(app.find_duplicate_entry(data, ""))


class SuggestTvdbSeasonTest(unittest.TestCase):
    def _s(self, *pairs):
        return [{"season_number": n, "episode_count": c} for n, c in pairs]

    def test_closest_regular_season_wins(self):
        self.assertEqual(app.suggest_tvdb_season(self._s((1, 12), (2, 24)), 23), 2)

    def test_specials_excluded_even_when_exact(self):
        self.assertEqual(app.suggest_tvdb_season(self._s((0, 12), (1, 10), (2, 20)), 12), 1)

    def test_specials_used_when_only_season(self):
        self.assertEqual(app.suggest_tvdb_season(self._s((0, 3)), 12), 0)

    def test_tie_gives_no_suggestion(self):
        self.assertIsNone(app.suggest_tvdb_season(self._s((0, 12), (1, 10), (2, 14)), 12))

    def test_unknown_episode_count_gives_no_suggestion(self):
        self.assertIsNone(app.suggest_tvdb_season(self._s((1, 12)), 0))


class RenderTvdbStepFlowTest(unittest.TestCase):
    SEASONS = [{"season_number": 0, "episode_count": 12},
               {"season_number": 1, "episode_count": 10},
               {"season_number": 2, "episode_count": 14},
               {"season_number": 3, "episode_count": 30}]

    def _advanced_season(self, out):
        m = re.search(r'id="adv-season" name="tvdb_season" min="0" value="(\d+)"', out)
        self.assertIsNotNone(m)
        return int(m.group(1))

    def test_add_mode_has_steps_cancel_and_save_without_tvdb(self):
        out = app.render_tvdb_step("Show", "https://anime-loads.org/media/show", "1", "")
        self.assertIn('class="steps"', out)
        self.assertIn('<li class="step step-current" aria-current="step"><span class="step-num">2</span>TVDB</li>', out)
        self.assertIn(">Cancel</a>", out)
        self.assertIn(">Save without TVDB</button>", out)
        self.assertIn(">Change release</button>", out)
        self.assertNotIn("Skip TVDB", out)

    def test_edit_mode_has_no_steps_and_a_cancel_button(self):
        out = app.render_tvdb_step("Show", "u", "", "", edit_key="u")
        self.assertNotIn('class="steps"', out)
        self.assertIn(">Cancel</button>", out)
        self.assertNotIn("Save without TVDB", out)
        self.assertNotIn("Change release", out)

    def test_tie_has_no_likely_match_and_advanced_defaults_to_first_regular(self):
        out = app.render_tvdb_step("Show", "u", "1", "", seasons=self.SEASONS,
                                   selected_tvdb_id="9", ep_count=12)
        self.assertNotIn("Likely match", out)
        self.assertEqual(self._advanced_season(out), 1)

    def test_advanced_defaults_to_suggested_season(self):
        out = app.render_tvdb_step("Show", "u", "1", "", seasons=self.SEASONS,
                                   selected_tvdb_id="9", ep_count=29)
        self.assertEqual(out.count("Likely match"), 1)
        self.assertEqual(self._advanced_season(out), 3)

    def test_results_carry_the_search_query(self):
        out = app.render_tvdb_step("Show", "u", "1", "", query="Custom & Query",
                                   search_results=[{"tvdb_id": 5, "name": "X"}])
        self.assertIn('name="query" value="Custom &amp; Query"', out)
        self.assertIn('name="tvdb_query" value="Custom &amp; Query"', out)


class AddAnimeFlowTest(unittest.TestCase):
    """End-to-end handler behaviour of the add flow with scrapes and TVDB
    stubbed: auto-select, duplicates, per-entry prefs, carried metadata."""

    URL = "https://www.anime-loads.org/media/test-anime"

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="aniloads-addflow-")
        self._orig = {
            "ANI_JSON": app.ANI_JSON, "PREFS_FILE": app.PREFS_FILE,
            "get_releases": app.get_releases, "search_anime": app.search_anime,
        }
        self._orig_tvdb = (app.tvdb.available, app.tvdb.__dict__.get("search"),
                           app.tvdb.__dict__.get("get_seasons"))
        app.ANI_JSON = os.path.join(self._tmp, "ani.json")
        app.PREFS_FILE = os.path.join(self._tmp, "web-prefs.json")
        app.save_ani({"settings": {}, "anime": []})
        self.set_prefs(auto_select=True)

        self.scrapes = []
        self.releases = [
            {"id": 11, "resolution": 1080, "dubs": ["German"], "subs": [],
             "episodes": 12, "size_mb": 9000, "group": "g1"},
            {"id": 12, "resolution": 720, "dubs": ["Japanese"], "subs": ["German"],
             "episodes": 12, "size_mb": 4000, "group": "g2"},
        ]

        def fake_get_releases(url):
            self.scrapes.append(url)
            return {"name": "Test Anime", "url": url, "releases": self.releases,
                    "media_type": "movie" if "movie" in url else "series",
                    "year": 2021, "display_title": "Test Anime DE"}, None

        app.get_releases = fake_get_releases

        self.tvdb_searches = []

        def fake_search(query, content_type="series"):
            self.tvdb_searches.append((query, content_type))
            return [{"tvdb_id": 77, "name": "Result", "year": 2021, "overview": ""}]

        app.tvdb.available = False
        app.tvdb.search = fake_search
        app.tvdb.get_seasons = lambda tvdb_id: [{"season_number": 1, "episode_count": 12}]

    def tearDown(self):
        app.ANI_JSON = self._orig["ANI_JSON"]
        app.PREFS_FILE = self._orig["PREFS_FILE"]
        app.get_releases = self._orig["get_releases"]
        app.search_anime = self._orig["search_anime"]
        available, search, get_seasons = self._orig_tvdb
        app.tvdb.available = available
        for name, value in (("search", search), ("get_seasons", get_seasons)):
            if value is None:
                app.tvdb.__dict__.pop(name, None)
            else:
                setattr(app.tvdb, name, value)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def set_prefs(self, **overrides):
        prefs = {"audio_language": "german", "sub_language": "any",
                 "min_resolution": 1080, "auto_select": True}
        prefs.update(overrides)
        app.save_prefs(prefs)

    def _post(self, path, params):
        captured = {}
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h._read_post = lambda: params
        h._redirect_msg = lambda msg, level=None: captured.update(msg=msg, level=level)
        h._redirect = lambda url: captured.__setitem__("url", url)
        h._respond = lambda code, html_body: captured.__setitem__("html", html_body)
        h.do_POST()
        return captured

    def _form_fields(self, out, action, index=0):
        forms = re.findall(r'<form method="POST" action="{}"[^>]*>(.*?)</form>'.format(
            re.escape(action)), out, re.S)
        self.assertGreater(len(forms), index, "no form posting to {}".format(action))
        return {html.unescape(k): html.unescape(v) for k, v in
                re.findall(r'name="([^"]+)" value="([^"]*)"', forms[index])}

    # -- auto-select --------------------------------------------------------

    def test_auto_select_skips_picker_to_tvdb_step(self):
        app.tvdb.available = True
        result = self._post("/add-url", {"url": self.URL})
        out = result["html"]
        self.assertNotIn("Add this release", out)
        self.assertIn("TVDB Correlation: Test Anime", out)
        self.assertIn("Auto-selected the release", out)
        self.assertIn(">Change release</button>", out)
        self.assertEqual(self._form_fields(out, "/tvdb-seasons")["release_id"], "11")
        self.assertEqual(len(self.scrapes), 1)

    def test_auto_select_without_tvdb_saves_best_release(self):
        result = self._post("/add-url", {"url": self.URL})
        self.assertEqual(result["level"], "ok")
        self.assertIn("auto-selected the 1080p release", result["msg"])
        entry = app.load_ani()["anime"][0]
        self.assertEqual(entry["releaseID"], 11)

    def test_auto_select_no_match_falls_back_to_picker(self):
        self.set_prefs(audio_language="english")
        out = self._post("/add-url", {"url": self.URL})["html"]
        self.assertIn("Add this release", out)
        self.assertIn("auto-select was skipped", out)
        self.assertEqual(app.load_ani()["anime"], [])

    def test_auto_select_off_shows_picker(self):
        self.set_prefs(auto_select=False)
        out = self._post("/add-url", {"url": self.URL})["html"]
        self.assertIn("Add this release", out)
        self.assertIn('<li class="step step-current" aria-current="step"><span class="step-num">1</span>Release</li>', out)
        self.assertNotIn("auto-select was skipped", out)

    def test_change_release_forces_picker(self):
        app.tvdb.available = True
        out = self._post("/add-url", {"url": self.URL, "pick": "1"})["html"]
        self.assertIn("Add this release", out)
        self.assertNotIn("TVDB Correlation", out)

    # -- duplicates ---------------------------------------------------------

    def test_add_url_rejects_normalized_duplicate_without_scraping(self):
        app.save_ani({"anime": [{"url": self.URL, "name": "Test Anime"}]})
        for variant in ("https://anime-loads.org/media/test-anime/",
                        "HTTPS://Anime-Loads.org/media/test-anime"):
            result = self._post("/add-url", {"url": variant})
            self.assertEqual(result["msg"], "Already in watchlist: Test Anime")
            self.assertEqual(result["level"], "err")
        self.assertEqual(self.scrapes, [])

    def test_add_release_rejects_url_already_pending(self):
        app.save_ani({"anime": [], "pending": [{"url": self.URL + "/", "name": "Pend"}]})
        result = self._post("/add-release", {"url": self.URL, "name": "Test Anime", "tvdb_skip": "1"})
        self.assertEqual(result["msg"], "Already in watchlist: Pend")
        self.assertEqual(result["level"], "err")
        self.assertEqual(app.load_ani()["anime"], [])

    def test_add_release_dedupes_inside_the_lock(self):
        # A duplicate that lands between the pre-check and the save (e.g. a
        # concurrent add) is still caught by the in-lock re-check.
        real_update = app.update_ani

        def racing_update(fn):
            real_update(lambda d: d.setdefault("pending", []).append(
                {"url": "https://anime-loads.org/media/test-anime/", "name": "Racer"}))
            return real_update(fn)

        app.update_ani = racing_update
        try:
            result = self._post("/add-release", {"url": self.URL, "name": "Test Anime", "tvdb_skip": "1"})
        finally:
            app.update_ani = real_update
        self.assertEqual(result["msg"], "Already in watchlist: Racer")
        self.assertEqual(app.load_ani()["anime"], [])

    def test_fetch_failure_banner_is_an_error(self):
        app.get_releases = lambda url: (None, "timeout")
        result = self._post("/add-url", {"url": self.URL})
        self.assertEqual(result["msg"], "Could not fetch releases: timeout, added to pending queue")
        self.assertEqual(result["level"], "err")
        pending = app.load_ani()["pending"][0]
        self.assertNotIn("pref_audio_language", pending)

    def test_invalid_url_banner_is_an_error(self):
        result = self._post("/add-url", {"url": "https://example.com/x"})
        self.assertEqual(result["level"], "err")

    # -- saved entry --------------------------------------------------------

    def test_saved_entry_gets_prefs_and_release_metadata(self):
        self.set_prefs(auto_select=False, sub_language="english", min_resolution=720)
        out = self._post("/add-url", {"url": self.URL + "-movie"})["html"]
        params = self._form_fields(out, "/add-release")
        self.assertEqual(params["year"], "2021")
        self.assertEqual(params["display_title"], "Test Anime DE")
        params["tvdb_skip"] = "1"
        result = self._post("/add-release", params)
        self.assertEqual(result["level"], "ok")
        entry = app.load_ani()["anime"][0]
        self.assertEqual(entry["pref_audio_language"], "german")
        self.assertEqual(entry["pref_sub_language"], "english")
        self.assertEqual(entry["pref_resolution"], 720)
        self.assertEqual(entry["media_type"], "movie")
        self.assertEqual(entry["year"], 2021)
        self.assertEqual(entry["display_title"], "Test Anime DE")
        self.assertEqual(len(self.scrapes), 1)

    def test_implausible_year_is_not_saved(self):
        self._post("/add-release", {"url": self.URL, "name": "X", "tvdb_skip": "1",
                                    "year": "99999", "display_title": ""})
        entry = app.load_ani()["anime"][0]
        self.assertNotIn("year", entry)
        self.assertNotIn("display_title", entry)

    # -- TVDB query carry-through --------------------------------------------

    def test_tvdb_seasons_reuses_custom_query_and_content_type(self):
        app.tvdb.available = True
        self.set_prefs(auto_select=False)
        out = self._post("/add-url", {"url": self.URL})["html"]
        params = self._form_fields(out, "/add-release")
        out = self._post("/add-release", params)["html"]
        search = self._form_fields(out, "/tvdb-search")
        search["query"] = "Custom Query"
        out = self._post("/tvdb-search", search)["html"]
        select = self._form_fields(out, "/tvdb-seasons")
        self.assertEqual(select["tvdb_query"], "Custom Query")
        self.tvdb_searches.clear()
        out = self._post("/tvdb-seasons", select)["html"]
        self.assertEqual(self.tvdb_searches, [("Custom Query", "series")])
        self.assertIn('name="query" value="Custom Query"', out)
        self.assertEqual(len(self.scrapes), 1)

    # -- search ---------------------------------------------------------------

    def test_search_keeps_query_and_tags_tracked_results(self):
        app.save_ani({
            "anime": [{"url": "https://www.anime-loads.org/media/a", "name": "A"}],
            "pending": [{"url": "https://www.anime-loads.org/media/b", "name": "B"}],
        })
        app.search_anime = lambda q: ([
            {"name": "A", "url": "https://anime-loads.org/media/a/", "type": "Serie",
             "episodes": "1/1", "genre": "", "dubs": "", "subs": ""},
            {"name": "B", "url": "https://anime-loads.org/media/b", "type": "Serie",
             "episodes": "1/1", "genre": "", "dubs": "", "subs": ""},
            {"name": "C", "url": "https://anime-loads.org/media/c", "type": "Serie",
             "episodes": "1/1", "genre": "", "dubs": "", "subs": ""},
        ], None)
        out = self._post("/search", {"q": "Fate & \"Zero\""})["html"]
        self.assertIn('name="q" value="Fate &amp; &quot;Zero&quot;"', out)
        self.assertIn('A <span class="badge badge-ok">In watchlist</span>', out)
        self.assertIn('B <span class="badge badge-accent">Pending</span>', out)
        results = out.split("<h2>Search Results</h2>", 1)[1]
        self.assertEqual(results.count("Add to watchlist"), 1)


class WatchlistStatusLineTest(unittest.TestCase):
    """Each watchlist card leads with one human status line (tone, headline,
    details) instead of a row of equal-weight badges."""

    TODAY = date(2026, 9, 17)

    def status(self, **entry):
        entry.setdefault("name", "X")
        return app.watchlist_status(entry, today=self.TODAY)

    def test_retrying_takes_precedence(self):
        self.assertEqual(
            self.status(episodes=12, missing=[3, 4], complete=True, skip_until="2026-09-26"),
            ("danger", "2 episodes retrying", ["12 episodes"]))

    def test_single_retry_is_singular(self):
        self.assertEqual(self.status(episodes=1, missing=[1])[1], "1 episode retrying")

    def test_movie_with_year(self):
        self.assertEqual(self.status(media_type="movie", episodes=1, year=1988),
                         ("ok", "Movie (1988)", ["Downloaded"]))

    def test_movie_not_downloaded_yet(self):
        self.assertEqual(self.status(media_type="movie", episodes=0),
                         ("neutral", "Movie", ["Waiting for download"]))

    def test_movie_retry(self):
        self.assertEqual(self.status(media_type="movie", episodes=0, missing=[1]),
                         ("danger", "Download retrying", []))

    def test_complete(self):
        self.assertEqual(self.status(episodes=28, complete=True),
                         ("ok", "Complete", ["28 episodes"]))

    def test_new_entry(self):
        self.assertEqual(self.status(episodes=0),
                         ("neutral", "Waiting for first download", []))
        self.assertEqual(self.status(episodes=0, al_status="Laufend")[2], ["Airing on site"])

    def test_airing_with_real_airdate_is_neutral(self):
        self.assertEqual(
            self.status(episodes=15, skip_until="2026-09-26", skip_real_airdate=True),
            ("neutral", "Airing", ["next episode Sat 26 Sep", "15 episodes"]))

    def test_throttled_date_is_a_check_not_an_episode(self):
        self.assertEqual(self.status(episodes=15, skip_until="2026-09-26")[2][0],
                         "next check Sat 26 Sep")

    def test_other_year_and_past_dates(self):
        self.assertEqual(self.status(episodes=1, skip_until="2027-01-02",
                                     skip_real_airdate=True)[2][0],
                         "next episode Sat 2 Jan 2027")
        self.assertEqual(self.status(episodes=1, skip_until="2026-09-10",
                                     skip_real_airdate=True)[2][0],
                         "next episode was due Thu 10 Sep")

    def test_unparseable_date_shown_verbatim(self):
        self.assertEqual(self.status(episodes=1, skip_until="soon")[2][0], "next check soon")

    def test_falls_back_to_translated_site_status(self):
        self.assertEqual(self.status(episodes=5, al_status="Pausiert"),
                         ("neutral", "Paused", ["5 episodes"]))
        self.assertEqual(self.status(episodes=5)[1], "Watching")

    def test_translate_al_status(self):
        self.assertEqual(app.translate_al_status("Laufend"), "Airing")
        self.assertEqual(app.translate_al_status("Abgeschlossen"), "Finished")
        self.assertEqual(app.translate_al_status(""), "")
        self.assertEqual(app.translate_al_status("Irgendwas"), "Irgendwas")


from html.parser import HTMLParser  # noqa: E402 — only the label check below needs it


class _ControlCollector(HTMLParser):
    """Collects form controls, <label for> targets and element ids."""

    def __init__(self):
        super().__init__()
        self.controls, self.label_for, self.ids = [], set(), []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "label" and a.get("for"):
            self.label_for.add(a["for"])
        if tag in ("input", "select", "textarea") and a.get("type") != "hidden":
            self.controls.append(a)


class RenderWatchlistCardTest(unittest.TestCase):
    """Watchlist card redesign: status line, facts-only badges, actions in an
    Edit disclosure, in-page remove confirm, compact episode ranges, labels."""

    def render(self, **entry):
        entry.setdefault("name", "One Piece")
        entry.setdefault("url", "https://www.anime-loads.org/media/one-piece")
        return app.render_watchlist([entry])

    def test_name_and_url_are_escaped(self):
        out = self.render(name='<img src=x onerror="a()"> & Co',
                          url='https://x.org/a"><script>b()</script>')
        self.assertNotIn("<img src=x", out)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;img src=x onerror=&quot;a()&quot;&gt; &amp; Co", out)
        self.assertIn('href="https://x.org/a&quot;&gt;&lt;script&gt;', out)

    def test_url_opens_in_new_tab_safely(self):
        out = self.render()
        self.assertIn('<a class="wl-url" href="https://www.anime-loads.org/media/one-piece" '
                      'target="_blank" rel="noopener noreferrer"', out)
        self.assertIn(">anime-loads.org/media/one-piece<", out)

    def test_non_http_url_is_not_a_link(self):
        out = self.render(url="javascript:alert(1)")
        self.assertNotIn("href=", out)
        self.assertIn('<span class="wl-url"', out)

    def test_movie_copy_has_no_episode_count(self):
        out = self.render(name="Akira", media_type="movie", episodes=1, year=1988)
        self.assertIn(">Movie (1988)</span>", out)
        self.assertNotIn("1 eps", out)
        self.assertNotIn("1 episodes", out)

    def test_new_entry_copy(self):
        out = self.render(episodes=0)
        self.assertIn("Waiting for first download", out)
        self.assertIn("none downloaded yet", out)
        self.assertNotIn("0 eps", out)

    def test_next_date_is_not_a_warning(self):
        out = self.render(episodes=3, skip_until="2099-01-01", skip_real_airdate=True)
        self.assertIn("wl-status--neutral", out)
        self.assertNotIn("badge-warn", out)
        self.assertNotIn("Next:", out)

    def test_site_status_translated_and_not_duplicated(self):
        out = self.render(episodes=3, al_status="Laufend", missing=[2])
        self.assertIn("Site: Airing", out)
        self.assertNotIn("Laufend", out)
        airing = self.render(episodes=3, al_status="Laufend")
        self.assertNotIn("Site: Airing", airing)
        done = self.render(episodes=3, al_status="Abgeschlossen", complete=True)
        self.assertNotIn("Site: Finished", done)

    def test_large_series_compacts_ok_episodes_into_ranges(self):
        out = self.render(episodes=1118, missing=[1080, 1119])
        self.assertIn("1–1079, 1081–1118", out)
        self.assertEqual(out.count('<li class="ep-row">'), 2)
        self.assertNotIn("Ep 500", out)
        self.assertIn("Ep 1119", out)
        self.assertIn(">Queued<", out)

    def test_single_range_summary(self):
        out = self.render(episodes=1116)
        self.assertIn("1–1116 OK", out)
        self.assertNotIn("ep-row", out)

    def test_scattered_retries_cap_the_range_list(self):
        out = self.render(episodes=100, missing=list(range(2, 100, 2)))
        self.assertIn("and {} more ranges".format(50 - app._MAX_EP_RANGES), out)

    def test_retry_by_number_uses_ep_add_bound(self):
        out = self.render(episodes=10, al_max_episodes=24)
        self.assertIn('action="/ep-add"', out)
        self.assertIn('max="24"', out)
        self.assertIn('max="5000"', self.render(episodes=10, al_max_episodes=999999))
        self.assertIn('max="5000"', self.render(episodes=10))

    def test_every_control_has_a_label_and_ids_are_unique(self):
        out = app.render_watchlist([
            {"name": "A", "url": "https://x/a", "episodes": 3, "missing": [2],
             "tvdb_id": 1, "complete": True},
            {"name": "B", "url": "https://x/b", "episodes": 0},
        ])
        c = _ControlCollector()
        c.feed(out)
        self.assertEqual(len(c.ids), len(set(c.ids)))
        self.assertEqual(len(c.controls), 4)  # folder + retry number, per card
        for ctl in c.controls:
            self.assertTrue(ctl.get("id") in c.label_for or ctl.get("aria-label"), ctl)

    def test_actions_carry_the_series_name(self):
        out = self.render(episodes=3, missing=[2], tvdb_id=1, complete=True)
        for text in ("Check now", "Save folder", "Add to retry", "Unlink TVDB",
                     "Mark incomplete"):
            self.assertIn(text + '<span class="sr-only"> for One Piece</span>', out)
        self.assertIn('Stop retrying<span class="sr-only"> episode 2 of One Piece</span>', out)
        self.assertIn('Edit<span class="sr-only"> One Piece</span>', out)

    def test_actions_live_in_edit_panel_and_check_now_stays_primary(self):
        out = self.render(episodes=3, tvdb_id=1, complete=True)
        head, edit = out.split('<details class="wl-panel wl-edit">', 1)
        self.assertIn('action="/check-now"', head)
        for action in ("/update-folder", "/tvdb-unlink", "/mark-incomplete", "/remove"):
            self.assertNotIn('action="{}"'.format(action), head)
            self.assertIn('action="{}"'.format(action), edit)

    def test_remove_confirms_in_page_naming_the_series(self):
        out = self.render(name="Frieren: Beyond Journey's End", episodes=3)
        edit = out.split('<details class="wl-remove">', 1)[1]
        self.assertIn("Remove <strong>Frieren: Beyond Journey&#x27;s End</strong> from the watchlist?", edit)
        self.assertIn(">Remove Frieren: Beyond Journey&#x27;s End</button>", edit)
        self.assertIn(">Cancel</button>", edit)
        self.assertNotIn("confirm(", out)

    def test_offset_badge_signed(self):
        self.assertIn("Offset +2", self.render(tvdb_id=1, episode_offset=2))
        self.assertIn("Offset -1", self.render(tvdb_id=1, episode_offset=-1))

    def test_compact_ranges(self):
        self.assertEqual(app.compact_ranges([5, 1, 2, 3, 7, 8]), [(1, 3), (5, 5), (7, 8)])
        self.assertEqual(app.compact_ranges([]), [])


class DashboardLandmarksTest(unittest.TestCase):
    def test_header_and_main_landmarks(self):
        t = app.HTML_TEMPLATE
        self.assertLess(t.index("<header>"), t.index("<h1>"))
        self.assertLess(t.index("<main>"), t.index("%%WATCHLIST%%"))
        self.assertLess(t.index("%%WATCHLIST%%"), t.index("</main>"))

    def test_run_history_collapses_on_narrow_screens(self):
        t = app.HTML_TEMPLATE
        self.assertIn('<details open id="run-history-panel">', t)
        self.assertIn("matchMedia('(max-width: 600px)')", t)

    def test_per_episode_js_builder_is_gone(self):
        self.assertNotIn("expandEps", app.HTML_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
