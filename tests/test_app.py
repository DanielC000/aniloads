"""Tests for the pure-logic functions in web/app.py."""

import json
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
