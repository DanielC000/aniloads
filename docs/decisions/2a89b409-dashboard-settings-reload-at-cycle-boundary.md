# Applying dashboard settings changes at the cycle boundary

`bot/anibot.py` — `startbot()`'s main loop, the `reload_settings_for_cycle()` call site
(board card `2a89b409`).

Dashboard settings changes are applied at this cycle boundary rather than taking effect
immediately or requiring a restart — see `reload_settings_for_cycle()`'s own docstring for
exactly what does/doesn't take effect without a restart (poll interval, hoster, pushbullet
key and JD/MyJDownloader connection fields reload live; `browserengine`/`browserlocation`
still need a container restart).

## Why a jdhost/myjd_* edit needs no explicit reconnect

There is no persisted JD/MyJDownloader connection object anywhere in this codebase —
`animeloads.downloadEpisode`/`downloadBatchCNL` take `jdhost`/`myjd_*` as plain parameters and
connect fresh on every call (see `animeloads.utils.addToMYJD`/`addToJD`). So once
`reload_settings_for_cycle` picks up a changed value, "reconnect" is just that updated tuple
value flowing into the next download call — there is no separate connection state to tear
down or re-establish.

## Do not

Don't apply a settings reload mid-cycle — it's deliberately read once at the top of each
cycle (`reload_settings_for_cycle`), so an edit made mid-cycle takes effect starting next
cycle, not immediately.
