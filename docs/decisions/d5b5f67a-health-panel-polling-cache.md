# Health panel probes are memoized against the 10s poll

`web/app.py` — `_HEALTH_CACHE` / `_cached_health()`, used throughout the Health panel checks
(JDownloader / TVDB / site login / disk / bot staleness).

The dashboard polls `/api/status` every 10s (see the `refresh()` script in `HTML_TEMPLATE`),
so a naive per-poll network probe would hammer JDownloader/TVDB constantly. Each
network-touching check is memoized in `_HEALTH_CACHE` for its own TTL; the login/staleness
checks are free (pure reads of already-loaded state) and skip the cache entirely.

## Do not (health panel caching)

Don't add a network-touching health probe without memoizing it in `_HEALTH_CACHE` — the
dashboard polls `/api/status` every 10s, so an unmemoized probe hammers the target
(JDownloader/TVDB) on every poll instead of once per TTL window.

## Asserting write_run_state's open() encoding, not a content round-trip

`tests/test_anibot.py` — `test_write_run_state_write_open_uses_utf8_encoding`. This same
commit also touched `bot/anibot.py`'s `write_run_state`'s write-side `open()` call
(`encoding="utf-8"`).

A content round-trip can't catch a missing `encoding="utf-8"` on the *write* side of
`write_run_state`: its `json.dump` call keeps the default `ensure_ascii=True`, so non-ASCII
output is always escaped to plain-ASCII `\uXXXX` sequences regardless of which codec the file
was opened with — a round-trip test would pass identically whether or not the encoding kwarg
is present. Confirmed by reverting just the write-side `open(tmp, "w", ...)` to the platform
default: a pure-content round-trip test stayed green anyway. So this test asserts the
`open()` call's `encoding` kwarg directly (via a patched `open` spy) instead of the content
that call produces.

## Do not (write_run_state encoding assertion)

Don't replace this with a content round-trip under the assumption it also proves the
write-side encoding — `json.dump`'s `ensure_ascii=True` default masks exactly that, so a
round-trip test regresses silently if the `encoding="utf-8"` kwarg is ever dropped from the
write-side `open()` call.
