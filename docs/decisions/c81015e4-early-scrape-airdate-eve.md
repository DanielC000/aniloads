# Early-scrape window for a "Continuing" series' predicted airdate

`bot/anibot.py` — `EARLY_SCRAPE_DAYS` (module constant).

Days before a `skip_until` date to start scraping anyway. The bot defers scraping a
"Continuing" series until `skip_until` (the TVDB-predicted airdate), but anime-loads.org
sometimes publishes an episode early. Once today is within this many days of `skip_until`,
the bot scrapes anyway to catch the early release. `0` disables this (strict `skip_until`
honoring). See `should_scrape_despite_skip()`.

## Do not

Don't remove the early-scrape window without another way to catch episodes anime-loads.org
publishes ahead of TVDB's predicted airdate — a strict `skip_until` honor-only policy will
silently miss those releases until the next scrape after the airdate.
