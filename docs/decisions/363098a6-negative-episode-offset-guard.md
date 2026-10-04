# Guarding a negative episode_offset result before filing

`web/app.py` — `run_move_cycle()`'s episode-offset application, the `episode < 1` check
right after `episode += ep_offset`.

A season/offset set from the dashboard's Edit panel (board card `363098a6`) needs no
`tvdb_id`, so it's directly reachable with a steep negative `episode_offset` relative to a
low parsed episode number (e.g. offset `-12` on a parsed `E05`). Applying the offset can
drive the result to zero or negative, which is not a real episode number — the item is sent
to the stuck/review path (reason `bad_offset`) instead of being filed under a bogus `E00` or
negative episode token.

## Do not

Don't file a move whose offset-adjusted episode number is `< 1` — route it to the stuck path
(`_stuck_touch(..., "bad_offset", ...)`) instead of writing a bogus or negative episode token
into the filename.
