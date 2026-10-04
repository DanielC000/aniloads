# Deriving the human-readable skip reason from a decision dict

`bot/anibot.py` — `startbot()`'s per-entry loop, around the `pre_scrape_skip_decision()` and
`tvdb_skip_decision()` call sites.

For the `pre_scrape_skip_decision()` result: by the time its `"skip"` outcome is recorded,
Step 1 ("already complete") and Step 2 ("al_status complete", `mark_complete` above) are the
only ways `complete` can be set — Step 3 (`skip_until`) returns before ever touching it. So
`complete` alone tells the two apart when building the persisted outcome reason.

For the `tvdb_skip_decision()` result: `decision["log"][1]` is already a human reason for
every terminal case `tvdb_skip_decision` can return (complete / waiting-for-airdate / TVDB
past-due recheck throttle / no-airdate-known synthetic skip), so the caller reuses it
directly instead of re-deriving a reason from the other fields in `decision`.

## Do not

Don't re-derive the skip reason from `tvdb_skip_decision()`'s other output fields — its
`decision["log"][1]` already covers every terminal case. And don't assume `complete` can be
set by `pre_scrape_skip_decision()`'s Step 3 — only Steps 1 and 2 set it, which is what makes
checking `complete` alone sufficient to tell those two cases apart.
