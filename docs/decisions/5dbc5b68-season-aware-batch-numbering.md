# Season-aware batch numbering and the mover's absolute renumber

`bot/animeloads.py` — `_season_absolute_numbering` / `_group_urls_by_episode`;
`bot/anibot.py` — `season_max_eps` in `BOT_OWNED_SCALAR_FIELDS`; `web/app.py` — `_absolute_season_shift`
in `run_move_cycle()`.

## The live reproduction (card 5dbc5b68)

Gantz release 4 decrypts 37 links: S01E01-13 and S02E01-13 (some two-part rars). Grouping keyed on
the bare episode number, so S02 folded onto 1-13, `available_max` stayed 13 and wanted 14-26 hit
"No links after filtering". Owner decision: episodes 14-26 go to Plex `Gantz/S01` as E14-E26,
single-season absolute numbering matching TVDB season 1 (26 episodes).

## Decision

- When every link carries a parsed season and episode, there are 2+ seasons, the lowest is 1, there is
  no season gap and every season but the last is gapless 1..max, the batch is renumbered absolutely:
  earlier seasons' max episode (taken from the links, never from link order) + ep. The last season may
  be partial. Anything else keeps the bare-episode numbering and logs why. This is deterministic from
  the links, not a guessed offset, so it does not conflict with `c0b7ab59`.
- The bot persists the per-season maxes as `season_max_eps` (`{"1": 13, "2": 13}`) on the entry; it is
  bot-owned (never dashboard-edited), written only by a successful batch that renumbered.
- The mover shifts a file by the sum of earlier seasons' maxes only when the entry has a `tvdb_season`,
  the file's season is greater than it, and all earlier seasons are recorded; `tvdb_season`/`episode_offset`
  semantics are otherwise unchanged. The existing `episode_offset` is applied afterwards.
- The single-episode path (`downloadEpisode`) requests the site's own per-episode index and never groups
  by filename, so it needs no change.

## Do not

Don't derive the season offsets in the mover from filenames or link order — read the persisted
`season_max_eps`. Don't shift an entry whose `tvdb_season` equals the file's season. Don't renumber when
any link lacks a season or an earlier season is incomplete — fall back, never guess.
