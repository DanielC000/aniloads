"""Tests for the pure-logic helpers in bot/animeloads.py."""

import unittest

import support

animeloads = support.load_animeloads()
match_batch_episodes = animeloads._match_batch_episodes
format_ep_range = animeloads._format_ep_range


def _grouped(episode_numbers):
    """Build a `grouped` dict (release/site-numbered episode -> [url]) for
    the given episode numbers, one throwaway URL each."""
    return {ep: ["https://example.invalid/ep%d.mkv" % ep] for ep in episode_numbers}


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
        # The exact reproduction from the live homelab logs (card 62b4595a):
        # wanted 1-7 (per-cour watchlist numbering), release numbers its
        # files 41-46 (absolute, continuing across cours). Entirely
        # disjoint, but NOT because 1-7 are beyond the real max (46) — a
        # numbering mismatch, not the 6b9f0e3 all-phantom case.
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
        # Same fixture, with the user-set episode_offset (-40) that resolves
        # it: wanted 1-7 translates to release episodes 41-47, of which
        # 41-46 exist. The return leg must translate the matched release
        # episodes (41-46) back into watchlist numbering (1-6) — this is
        # the dangerous direction (card priority #2): a sign error here
        # would corrupt the watchlist on real data.
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
        # 6b9f0e3's case: DOM over-reports, every wanted episode is
        # genuinely beyond the real max. Must NOT be classified as a
        # numbering mismatch — reason_code stays None so handle_failed_batch
        # keeps routing it through the benign all-phantom path.
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


if __name__ == "__main__":
    unittest.main()
