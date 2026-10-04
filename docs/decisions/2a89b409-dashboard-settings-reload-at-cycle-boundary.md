# Applying dashboard settings changes at the cycle boundary

`bot/anibot.py` — `startbot()`'s main loop, the `reload_settings_for_cycle()` call site
(board card `2a89b409`).

Dashboard settings changes are applied at this cycle boundary rather than taking effect
immediately or requiring a restart — see `reload_settings_for_cycle()`'s own docstring for
exactly what does/doesn't take effect without a restart (poll interval, hoster, pushbullet
key and JD/MyJDownloader connection fields reload live; `browserengine`/`browserlocation`
still need a container restart).

## Do not

Don't apply a settings reload mid-cycle — it's deliberately read once at the top of each
cycle (`reload_settings_for_cycle`), so an edit made mid-cycle takes effect starting next
cycle, not immediately.
