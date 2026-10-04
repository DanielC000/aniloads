# Detecting an episode-numbering mismatch in a batch result

`bot/animeloads.py` — the batch-CNL filtering helper, the `not filtered_links` branch.

When the wanted episodes are entirely disjoint from `grouped` (the release's actual episode
numbers), but not because every wanted episode is genuinely beyond what the release has
published yet (that's the benign all-phantom case, left to the caller), the release likely
numbers its files in a different scheme (e.g. absolute numbering continuing across cours).
This is detected and suggested only; it is never auto-applied, since guessing wrong would
push the wrong episodes to JDownloader.

## Do not

Don't auto-apply the suggested `episode_offset` — a wrong guess would push the wrong episodes
to JDownloader. Surface it as a suggestion (the `[MISMATCH]` log / dashboard-visible reason)
and let the owner set `episode_offset` by hand.
