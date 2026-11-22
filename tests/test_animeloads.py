"""Tests for the pure-logic helpers in bot/animeloads.py."""

import unittest

from lxml import etree

import support

animeloads = support.load_animeloads()
match_batch_episodes = animeloads._match_batch_episodes
format_ep_range = animeloads._format_ep_range
count_episode_links = animeloads._count_episode_links
searchResult = animeloads.searchResult
apihelper = animeloads.apihelper


def _release_dom(relID, episode_count, with_cnl_anchor=True):
    """Build a minimal HTML fragment mirroring a release's download anchors:
    `episode_count` per-episode anchors (`downloads_episodes_<relID>_0..N-1`)
    plus, when requested, the "Click'n'Load alle Folgen!" batch-download-all
    anchor (`downloads_episodes_<relID>_cnl`) that real release pages also
    carry (card c3074a3c: this shares the per-episode anchors' prefix and
    was previously miscounted as an extra episode)."""
    anchors = []
    if with_cnl_anchor:
        anchors.append('<a aria-controls="downloads_episodes_%d_cnl">Alle Folgen</a>' % relID)
    for i in range(episode_count):
        anchors.append('<a aria-controls="downloads_episodes_%d_%d">%d</a>' % (relID, i, i + 1))
    return etree.HTML("<html><body>%s</body></html>" % "".join(anchors))


def _grouped(episode_numbers):
    """Build a `grouped` dict (release/site-numbered episode -> [url]) for
    the given episode numbers, one throwaway URL each."""
    return {ep: ["https://example.invalid/ep%d.mkv" % ep] for ep in episode_numbers}


class BatchDiagTest(unittest.TestCase):
    """`[BATCH-DIAG]` logging (card ec1eb846): diagnostic only, must not leak
    URLs/hosts/tokens and must not change grouping."""

    GANTZ = "Gantz.S%02dE%02d.German.DL.EAC3.1080p.WebDL.x264-PuddingSama.mkv"
    SECRET = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"

    def _urls(self):
        out = []
        for s in (1, 2):
            for e in (1, 2):
                f = self.GANTZ % (s, e)
                out.append(("https://host.example/dl/%s/%s?key=%s" % (self.SECRET, f, self.SECRET)).encode())
        out.append(("https://host.example/f/%s" % self.SECRET).encode())
        return out

    def test_parse_season_episode(self):
        p = animeloads._parse_season_episode_from_url
        self.assertEqual(p("x.S02E13.mkv"), (2, 13))
        self.assertEqual(p("x.s01e05.mkv"), (1, 5))
        self.assertEqual(p("Show.Episode.7.mkv"), (None, 7))
        self.assertEqual(p("Show - 12 .mkv"), (None, 12))
        self.assertEqual(p("nothing.mkv"), (None, None))

    def test_parse_episode_unchanged(self):
        self.assertEqual(animeloads._parse_episode_from_url("x.S02E13.mkv"), 13)

    def test_tail_scrubbed(self):
        tail = animeloads._diag_filename_tail(
            "https://host.example/dl/%s/%s?key=%s" % (self.SECRET, self.GANTZ % (1, 1), self.SECRET))
        self.assertLessEqual(len(tail), 60)
        self.assertNotIn("http", tail)
        self.assertNotIn("/", tail)
        self.assertNotIn(self.SECRET, tail)
        self.assertIn("E01", tail)

    def test_opaque_and_long_token_redacted(self):
        self.assertEqual(animeloads._diag_filename_tail("https://h.example/f/" + self.SECRET), "<opaque>")
        tail = animeloads._diag_filename_tail("https://h/x/%s.mkv" % self.SECRET)
        self.assertNotIn(self.SECRET, tail)

    def test_log_lines_do_not_leak(self):
        with self.assertLogs("animeloads", level="INFO") as cm:
            animeloads._log_batch_diag(self._urls())
        text = "\n".join(cm.output)
        for bad in ("http", "/", "host.example", self.SECRET, "key="):
            self.assertNotIn(bad, text)
        self.assertEqual(len([o for o in cm.output if "#" in o]), 5)
        self.assertIn("season=1 count=2 max_ep=2", text)
        self.assertIn("season=2 count=2 max_ep=2", text)
        self.assertIn("season=None count=1", text)

    def test_grouping_unchanged(self):
        urls = self._urls()
        before = animeloads._group_urls_by_episode(urls, 4)
        with self.assertLogs("animeloads", level="INFO"):
            animeloads._log_batch_diag(urls)
        self.assertEqual(animeloads._group_urls_by_episode(urls, 4), before)
        self.assertEqual(sorted(before), [1, 2])
        self.assertEqual(len(before[1]), 2)


def _gantz_urls():
    """The live 37-link Gantz release (card 5dbc5b68): S01 16 links, S02 21
    links; E01/E03/E07 of S01 and E02-E08/E13 of S02 are two-part rars."""
    single = "https://h.example/Gantz.S%02dE%02d.German.DL.EAC3.1080p.WebDL.x264-PuddingSama.rar"
    part = "https://h.example/S%02dE%02d.German.DL.EAC3.1080p.WebDL.x264-PuddingSama.part%d.rar"
    out = []
    for s, two_part in ((1, {1, 3, 7}), (2, set(range(2, 9)) | {13})):
        for e in range(1, 14):
            if e in two_part:
                out += [(part % (s, e, 1)).encode(), (part % (s, e, 2)).encode()]
            else:
                out.append((single % (s, e)).encode())
    return out


class SeasonAwareGroupingTest(unittest.TestCase):
    def test_fixture_is_37_links(self):
        self.assertEqual(len(_gantz_urls()), 37)

    def test_full_fixture_groups_into_1_to_26(self):
        info = {}
        with self.assertLogs("animeloads", level="INFO") as cm:
            grouped = animeloads._group_urls_by_episode(_gantz_urls(), 26, info)
        self.assertEqual(sorted(grouped), list(range(1, 27)))
        self.assertEqual(info["season_max_eps"], {1: 13, 2: 13})
        self.assertIn("season-aware numbering: seasons {1: 13, 2: 13}", "\n".join(cm.output))
        self.assertEqual(sum(len(v) for v in grouped.values()), 37)

    def test_wanted_14_to_26_selects_s02_including_both_rar_parts(self):
        grouped = animeloads._group_urls_by_episode(_gantz_urls(), 26)
        m = match_batch_episodes(set(range(14, 27)), grouped)
        self.assertEqual(m["episodes_sent"], list(range(14, 27)))
        self.assertEqual(m["available_max"], 26)
        self.assertEqual(len(m["filtered_links"]), 21)
        self.assertTrue(all("S02E" in u or "/S02" in u for u in m["filtered_links"]))
        # S02E02 -> absolute 15: both parts present
        self.assertEqual(len(grouped[15]), 2)
        self.assertTrue(any("part1" in u for u in grouped[15]))
        self.assertTrue(any("part2" in u for u in grouped[15]))

    def test_last_season_may_be_partial(self):
        urls = [u for u in _gantz_urls() if b"S02E13" not in u and b"S02E12" not in u]
        info = {}
        grouped = animeloads._group_urls_by_episode(urls, 26, info)
        self.assertEqual(info["season_max_eps"], {1: 13, 2: 11})
        self.assertEqual(sorted(grouped), list(range(1, 25)))

    def test_incomplete_earlier_season_falls_back(self):
        urls = [u for u in _gantz_urls() if b"S01E05" not in u]
        info = {}
        with self.assertLogs("animeloads", level="INFO") as cm:
            grouped = animeloads._group_urls_by_episode(urls, 26, info)
        self.assertEqual(sorted(grouped), list(range(1, 14)))
        self.assertNotIn("season_max_eps", info)
        self.assertIn("incomplete", "\n".join(cm.output))

    def test_season_gap_or_not_starting_at_1_falls_back(self):
        gap = [b"x.S01E01.mkv", b"x.S01E02.mkv", b"x.S03E01.mkv"]
        self.assertEqual(sorted(animeloads._group_urls_by_episode(gap, 3)), [1, 2])
        late = [b"x.S02E01.mkv", b"x.S03E01.mkv"]
        self.assertEqual(sorted(animeloads._group_urls_by_episode(late, 2)), [1])

    def test_single_season_unchanged(self):
        urls = [("x.S01E%02d.mkv" % e).encode() for e in range(1, 6)]
        info = {}
        grouped = animeloads._group_urls_by_episode(urls, 5, info)
        self.assertEqual(sorted(grouped), [1, 2, 3, 4, 5])
        self.assertNotIn("season_max_eps", info)

    def test_mixed_fixture_with_seasonless_link_falls_back(self):
        urls = _gantz_urls() + [b"https://h.example/Gantz.Episode.5.rar"]
        info = {}
        grouped = animeloads._group_urls_by_episode(urls, 26, info)
        self.assertEqual(sorted(grouped), list(range(1, 14)))
        self.assertNotIn("season_max_eps", info)

    def test_link_order_does_not_matter(self):
        grouped = animeloads._group_urls_by_episode(list(reversed(_gantz_urls())), 26)
        self.assertEqual(sorted(grouped), list(range(1, 27)))


class FormatEpRangeTest(unittest.TestCase):
    def test_single_episode(self):
        self.assertEqual(format_ep_range([5]), "5")

    def test_range(self):
        self.assertEqual(format_ep_range([3, 1, 2]), "1-3")

    def test_empty(self):
        self.assertEqual(format_ep_range([]), "")


class MatchBatchEpisodesTest(unittest.TestCase):
    """`_match_batch_episodes` is the pure core of `downloadBatchCNL`'s
    episode-numbering translation (card 62b4595a): it must honor
    `episode_offset` in both directions (wanted -> release numbering for
    matching, matched episodes back to watchlist numbering for the caller),
    and must distinguish an absolute-numbering mismatch from the benign
    all-phantom case (6b9f0e3) without disturbing the latter."""

    def test_offset_zero_matches_directly(self):
        # The overwhelmingly common case: release numbers episodes the same
        # way the watchlist does (episode_offset unset/0).
        result = match_batch_episodes({1, 2, 3}, _grouped([1, 2, 4]))
        self.assertEqual(sorted(result["episodes_sent"]), [1, 2])
        self.assertEqual(result["episodes_not_found"], [3])
        self.assertEqual(result["available_max"], 4)
        self.assertIsNone(result["reason_code"])
        self.assertIsNone(result["reason"])
        self.assertEqual(len(result["filtered_links"]), 2)

    def test_live_bleach_mismatch_with_offset_unset(self):
        # @decision sha:c0b7ab59 — the live homelab reproduction (card
        # 62b4595a); see docs/decisions.
        wanted = set(range(1, 8))
        grouped = _grouped(range(41, 47))
        result = match_batch_episodes(wanted, grouped)
        self.assertEqual(result["filtered_links"], [])
        self.assertEqual(result["episodes_sent"], [])
        self.assertEqual(sorted(result["episodes_not_found"]), list(range(1, 8)))
        self.assertEqual(result["reason_code"], "episode_numbering_mismatch")
        self.assertIn("wanted 1-7", result["reason"])
        self.assertIn("episodes 41-46", result["reason"])
        self.assertIn("episode_offset to -40", result["reason"])
        self.assertEqual(result["available_max"], 46)

    def test_live_bleach_resolved_with_episode_offset(self):
        # Same fixture, resolved via episode_offset=-40: see
        # _match_batch_episodes' docstring for the bidirectional translation.
        wanted = set(range(1, 8))
        grouped = _grouped(range(41, 47))
        result = match_batch_episodes(wanted, grouped, episode_offset=-40)
        self.assertEqual(sorted(result["episodes_sent"]), [1, 2, 3, 4, 5, 6])
        self.assertEqual(result["episodes_not_found"], [7])
        self.assertEqual(len(result["filtered_links"]), 6)
        self.assertEqual(result["available_max"], 6)
        self.assertIsNone(result["reason_code"])
        self.assertIsNone(result["reason"])

    def test_all_phantom_unaffected_by_mismatch_detection(self):
        # @decision sha:6b9f0e32 — must stay None here, not misclassified as
        # a mismatch; see docs/decisions.
        wanted = set(range(14, 28))
        grouped = _grouped(range(1, 14))
        result = match_batch_episodes(wanted, grouped)
        self.assertEqual(result["filtered_links"], [])
        self.assertIsNone(result["reason_code"])
        self.assertEqual(result["available_max"], 13)

    def test_no_links_decoded_is_plain_failure_not_mismatch(self):
        # Nothing decrypted at all (grouped empty) — a genuine failure, not
        # a numbering mismatch (there is no release numbering to compare
        # against).
        result = match_batch_episodes({1, 2, 3}, {})
        self.assertEqual(result["filtered_links"], [])
        self.assertIsNone(result["reason_code"])
        self.assertIsNone(result["available_max"])

    def test_no_filter_sends_everything_translated(self):
        # wanted_episodes falsy — send everything, still honoring offset.
        result = match_batch_episodes(None, _grouped([41, 42]), episode_offset=-40)
        self.assertEqual(sorted(result["episodes_sent"]), [1, 2])
        self.assertEqual(result["episodes_not_found"], [])


def _search_result(url="https://www.anime-loads.org/media/123-test", name="Test Anime",
                    typ="series", relDate="2020", epCountCurrent="5", epCountMax="12",
                    dubLang=None, subLang=None, genre="Action"):
    """Build a real searchResult, the shape animeloads.search() returns, with
    throwaway session/animeloads refs since the accessors under test never
    touch them."""
    return searchResult(url, name, typ, relDate, epCountCurrent, epCountMax,
                         dubLang if dubLang is not None else ["German"],
                         subLang if subLang is not None else ["English"],
                         genre, session=None, animeloads=None)


class SearchResultAccessorTest(unittest.TestCase):
    """web/app.py's search_anime() reads a live search result exclusively
    through these accessors (getUrl/getDubLang/getSubLang/...) — a wrong
    method name here silently drops every search result behind the caller's
    broad except/pass, so exercise the real class, never a mock."""

    def test_getUrl_returns_url(self):
        r = _search_result(url="https://www.anime-loads.org/media/42-foo")
        self.assertEqual(r.getUrl(), "https://www.anime-loads.org/media/42-foo")

    def test_has_no_getURL_capitalized_variant(self):
        # getURL (capital URL) belongs to the `anime` class, not searchResult.
        # web/app.py must never call it on a search result.
        self.assertFalse(hasattr(searchResult, "getURL"))

    def test_getDubLang_returns_the_list(self):
        r = _search_result(dubLang=["German", "Japanese"])
        self.assertEqual(r.getDubLang(), ["German", "Japanese"])

    def test_getSubLang_returns_the_list_not_the_method(self):
        r = _search_result(subLang=["English"])
        result = r.getSubLang()
        self.assertEqual(result, ["English"])
        self.assertNotEqual(result, r.getSubLang)


class SearchAnimeBuilderTest(unittest.TestCase):
    """Regression test for the dashboard's search-result builder (mirrors
    web/app.py's search_anime loop) against REAL searchResult instances, so
    a reintroduced wrong-accessor bug fails loudly instead of being swallowed."""

    def _build(self, results):
        out = []
        for r in results:
            out.append({
                "name": r.getName(),
                "url": r.getUrl(),
                "type": r.getTyp(),
                "episodes": "{}/{}".format(r.getCurrentEpisodeCount(), r.getMaxEpisodeCount()),
                "genre": r.getGenre(),
                "dubs": ", ".join(r.getDubLang() or []),
                "subs": ", ".join(r.getSubLang() or []),
            })
        return out

    def test_real_search_results_produce_non_empty_output(self):
        results = [
            _search_result(name="Show One", dubLang=["German"], subLang=["English"]),
            _search_result(name="Show Two", dubLang=[], subLang=["English"]),
        ]
        out = self._build(results)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["name"], "Show One")
        self.assertEqual(out[0]["dubs"], "German")
        self.assertEqual(out[1]["dubs"], "")
        self.assertEqual(out[1]["subs"], "English")


class CountEpisodeLinksTest(unittest.TestCase):
    """card c3074a3c: the "Click'n'Load alle Folgen!" batch-download-all
    anchor (`downloads_episodes_<relID>_cnl`) shares its prefix with every
    real per-episode anchor, so a bare `starts-with` xpath over-counts a
    26-episode release as 27 (one Gantz release 4 actually hit in
    production)."""

    def test_cnl_anchor_is_not_counted_as_an_episode(self):
        dom = _release_dom(relID=4, episode_count=26, with_cnl_anchor=True)
        self.assertEqual(count_episode_links(dom, 4), 26)

    def test_release_with_no_cnl_anchor_counts_unchanged(self):
        dom = _release_dom(relID=4, episode_count=26, with_cnl_anchor=False)
        self.assertEqual(count_episode_links(dom, 4), 26)

    def test_only_matches_the_given_release_id(self):
        # relID 1's anchors (including its own _cnl) must not bleed into
        # relID's count — the prefix match is release-scoped.
        dom = _release_dom(relID=1, episode_count=5, with_cnl_anchor=True)
        self.assertEqual(count_episode_links(dom, 2), 0)


class GetSearchURLEncodingTest(unittest.TestCase):
    def test_ampersand_is_encoded(self):
        url = apihelper.getSearchURL("fullmetal & alchemist")
        self.assertEqual(url, "https://www.anime-loads.org/search?q=fullmetal+%26+alchemist")
        self.assertNotIn("&alchemist", url.split("q=", 1)[1])

    def test_space_is_encoded(self):
        url = apihelper.getSearchURL("one piece")
        self.assertEqual(url, "https://www.anime-loads.org/search?q=one+piece")

    def test_hash_is_encoded(self):
        url = apihelper.getSearchURL("re:zero #2")
        self.assertNotIn("#", url)


if __name__ == "__main__":
    unittest.main()
