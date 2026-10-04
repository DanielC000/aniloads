# Field-level merge ownership for ani.json anime entries

`bot/anibot.py` — `BOT_OWNED_SCALAR_FIELDS` / `BOT_OWNED_LIST_FIELDS`, and `save_ani()`'s
baseline (inside `startbot()`'s per-entry loop). See `anistore.merge_entry_fields`.

## Ownership split

Every field listed in `BOT_OWNED_SCALAR_FIELDS` is written ONLY by the bot — verified by
grepping every `animeentry[...] =` / `.update(` / `.pop(` in this module. A scalar field is
safe to overwrite wholesale on each save because nothing else ever writes it. Fields the
dashboard can also edit through its POST handlers — `customPackage`, `tvdb_id`,
`tvdb_season`, `episode_offset`, `name`, `url`, `releaseID`, `pref_*` overrides, `settings`
— must NEVER be added here: the bot's stale per-cycle snapshot would otherwise silently
revert a concurrent dashboard edit on its next save.

`missing` is the one field BOTH sides mutate (the dashboard adds/removes a single retry
episode; the bot removes a downloaded one and adds a failed one on the same list), so it is
handled as a DELTA against the fresh on-disk list, never a wholesale replacement — see
`BOT_OWNED_LIST_FIELDS` and `compute_entry_delta()`.

`episodes` is shared too: the dashboard sets it when the user says "I already have episodes
up to N". It stays in the scalar list, but the bot writes it only through
`compute_entry_delta()`, i.e. only when the bot itself changed it this cycle (advanced it
after a download, or rolled it back after an episode turned out unavailable) — and then its
value wins, even going DOWN. The bot never writes back a value it merely read. `startbot()`
re-reads the entry fresh at the top of each entry (`refresh_entry`) and re-reads `episodes`
again right before deciding what to download (`sync_user_episodes`), so a dashboard edit
made up to that point is honored this same cycle.

`paused` is user-owned like `releaseID` and the `pref_*` overrides: the bot only reads it
(fresh, at the top of each entry) and never clears it.

## Per-entry save baseline

`save_ani()`'s baseline is what's been persisted so far this cycle (initially, what
cycle-start `load_ani_cycle_start()` read). `save_ani()` diffs the live `animeentry` against
this baseline on each call and writes only what changed, onto a FRESH re-read of `ani.json`
under the lock — never the whole stale `data` snapshot.

## Do not

Don't add a dashboard-editable field (`customPackage`, `tvdb_id`, `tvdb_season`,
`episode_offset`, `name`, `url`, `releaseID`, `pref_*`, `settings`) to
`BOT_OWNED_SCALAR_FIELDS` — doing so makes the bot's stale per-cycle snapshot silently
overwrite a concurrent dashboard edit on the next save. And don't make `save_ani()` write the
whole `data` snapshot instead of a fresh re-read under the lock — that reintroduces the same
clobbering bug for every field, not just the owned ones.
