# Health panel probes are memoized against the 10s poll

`web/app.py` — `_HEALTH_CACHE` / `_cached_health()`, used throughout the Health panel checks
(JDownloader / TVDB / site login / disk / bot staleness).

The dashboard polls `/api/status` every 10s (see the `refresh()` script in `HTML_TEMPLATE`),
so a naive per-poll network probe would hammer JDownloader/TVDB constantly. Each
network-touching check is memoized in `_HEALTH_CACHE` for its own TTL; the login/staleness
checks are free (pure reads of already-loaded state) and skip the cache entirely.

## Do not

Don't add a network-touching health probe without memoizing it in `_HEALTH_CACHE` — the
dashboard polls `/api/status` every 10s, so an unmemoized probe hammers the target
(JDownloader/TVDB) on every poll instead of once per TTL window.
