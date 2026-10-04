# Recheck throttle for a past-due TVDB airdate

`bot/anibot.py` — `TVDB_PASTDUE_RECHECK_HOURS` (module constant).

Hours between re-checks when TVDB's predicted airdate has already passed but
anime-loads.org hasn't published the episode yet. Without this throttle, a "Continuing"
series with a past-due airdate has no future `skip_until` to defer to and gets scraped
every poll cycle until the episode appears.

## Do not

Don't drop this throttle while a past-due airdate has no future `skip_until` to fall back
on — without it the entry gets scraped on every single poll cycle instead of waiting out
the recheck window.
