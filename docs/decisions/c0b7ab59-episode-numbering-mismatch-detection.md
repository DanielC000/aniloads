# Detecting an episode-numbering mismatch in a batch result

`bot/animeloads.py` — the batch-CNL filtering helper, the `not filtered_links` branch.

When the wanted episodes are entirely disjoint from `grouped` (the release's actual episode
numbers), but not because every wanted episode is genuinely beyond what the release has
published yet (that's the benign all-phantom case, left to the caller), the release likely
numbers its files in a different scheme (e.g. absolute numbering continuing across cours).
This is detected and suggested only; it is never auto-applied, since guessing wrong would
push the wrong episodes to JDownloader.

## The live reproduction (card 62b4595a)

The homelab logs that prompted this: Bleach TYBW, watchlist wanted episodes 1-7 (per-cour
numbering), but the release numbered its files 41-46 (absolute, continuing across cours) —
entirely disjoint from 1-7, but not because 1-7 are beyond the real max (46); a numbering
mismatch, not an all-phantom case (see `6b9f0e32`).

## Do not

Don't auto-apply the suggested `episode_offset` — a wrong guess would push the wrong episodes
to JDownloader. Surface it as a suggestion (the `[MISMATCH]` log / dashboard-visible reason)
and let the owner set `episode_offset` by hand.
