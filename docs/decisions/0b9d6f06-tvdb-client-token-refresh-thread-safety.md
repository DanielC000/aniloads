# TVDBClient's token lock is shared across dashboard request threads

`bot/tvdb.py` — `TVDBClient.__init__`'s token lock.

This client is a single module-level instance (see `web/app.py`'s `tvdb = ...`), shared
across the dashboard's request-handling threads. Without the lock, two threads racing
`_ensure_token` when the cached token is expired would both see it as invalid and both fire a
redundant login.

## web/app.py: cooldown check + state write as one atomic step

`web/app.py` — `_run_now_lock` (and the analogous notify-test cooldown guard).

Guards the cooldown check and its state write as one atomic step — under the threaded server,
two concurrent `/run-now`/`/check-now` POSTs (two browser tabs, or a double-click) could
otherwise both read the cooldown as elapsed before either one's write lands, both passing a
check meant to allow only one.

## web/app.py: validate posted selection fields instead of re-scraping

`web/app.py` — `_resolve_release_selection` and its call sites in the add-anime flow.

The `release_id`/`media_type`/episode-count carried as hidden fields are validated against a
fresh re-derivation rather than re-scraped from scratch to look them up — re-scraping on every
postback would multiply Selenium calls across what's otherwise a single add-anime flow.

## web/app.py: duplicate-add dedupe happens inside one lock hold

`web/app.py` — the add-anime save paths (the auto-select path and the fetch-failure
pending-queue fallback).

Dedupe + append happen in ONE lock hold so a write that lands during the multi-second scrape
(the bot, or another `/add-url` request) can't be silently overwritten by this request's stale
pre-scrape snapshot. The authoritative duplicate check always happens inside `update_ani`'s
single lock hold at save time — a pre-scrape dedupe check is only a cheap skip-ahead, never the
real guard.

## web/app.py: no anistore lock is held across a scrape

`web/app.py` — the add-anime release-fetch call site.

No anistore lock is held across the network/Selenium release-fetch call — see `update_ani`'s
own docstring for why that matters now the server is threaded: holding a lock across a
multi-second scrape would block every other request touching `ani.json` for that long.

## web/app.py: ThreadingHTTPServer so Selenium can't freeze the dashboard

`web/app.py` — the server construction at the bottom of the module.

`ThreadingHTTPServer` (`daemon_threads=True` by default) so a slow Selenium-backed handler
(add-anime's `get_releases`, up to ~a minute) can't freeze the whole dashboard, including the
10s `/api/status` poll, for every other concurrent request.

## Do not

Don't drop the lock around `_ensure_token`'s refresh check — without it, two request threads
racing an expired token both see it as invalid and both fire a redundant TVDB login. Don't
split a cooldown check from its state write across two lock holds — two concurrent POSTs could
both pass the check before either write lands. Don't trust a posted `release_id`/`media_type`
outright, and don't re-scrape to re-derive them when a cheaper validation against already-known
data suffices. Don't let a dedupe check and its append land in two separate lock holds, and
don't hold an anistore lock across a network/Selenium call. Don't switch the server off
`ThreadingHTTPServer` — a single-threaded server lets one slow Selenium-backed request freeze
every other concurrent request, including the status poll.
