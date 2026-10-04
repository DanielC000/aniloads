# Recent bot warnings & errors: redaction, source priority, and the level heuristic

`web/app.py` — the "Recent bot warnings & errors" panel: `_REDACT_RULES`, `BOT_LOG_FILE`,
`filter_bot_log_lines()`, and `render_bot_log()`'s lazy-fetch call site.

## Redaction rules

No general-purpose redactor existed for free-text log lines — `bot/notify.py`'s `_redact()`
only reshapes a single already-parsed target URL down to scheme+host, which doesn't help with
a raw log line that embeds a secret inline. Each `_REDACT_RULES` entry targets one secret shape
the bot can log: URL credentials, MyJD/al_pass/API-key values, and Discord/ntfy/Gotify webhook
paths/tokens (whether the bot logged the canonical `scheme://host/path` form `notify.py`
builds, or the `?token=` query form `_send_gotify()` sends).

## File log is the primary source

`bot/anibot.py`'s own rotating file log (`LOG_FILE` there) writes real levels —
`"%(asctime)s %(levelname)s %(name)s %(message)s"` — unlike its stdout handler (message-only).
The dashboard container mounts the same `LOG_DIR` volume the bot writes into, so `BOT_LOG_FILE`
is the primary source: real WARNING/ERROR/CRITICAL levels, no guessing. `BOT_LOG_TAIL_BYTES` is
bounded so a 14-day rotated file is never read whole.

## Docker-log fallback is a content heuristic, not a real level

Used only when the file log is absent/unreadable. The docker-captured stdout stream carries no
real logging level: the bot's stdout handler formats records as `"%(message)s"` only, so a
`_log.warning()`/`_log.error()` call is byte-for-byte indistinguishable on the wire from an
`_log.info()` one except by its own wording. `filter_bot_log_lines()` matches on content
markers the bot's own messages actually carry — the `[ERROR]` tag `log()` prefixes user-facing
failures with, plus the English/German words its direct `_log.warning()`/`_log.error()` calls
tend to use — rather than a true level field, which never survives the docker socket. Labelled
"approximate" in the rendered panel so this guesswork is never mistaken for the real thing.

## Lazy fetch, never on the status poll

The file (or, as a fallback, a fresh 500-line docker tail) is fetched only when the client
opens the "Recent bot warnings & errors" `<details>` panel — never on the `/api/status` poll —
so it's only read when someone actually looks.

## Do not

Don't add a new secret shape to the bot's logged output without a matching `_REDACT_RULES`
entry — a raw log line reaching the dashboard unredacted is a leak. Don't drop the file-log
priority over the docker-stdout heuristic — the file log carries real levels; the docker
heuristic is content-matched guesswork and must stay labelled "approximate". Don't fetch the
bot log on the `/api/status` poll — keep it lazy, fetched only when the panel is opened.
