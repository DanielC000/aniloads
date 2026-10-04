# Feed dates/times render on the process's own local clock

`web/app.py` — `_display_tz`, `_to_local()`, `format_day()`, `format_day_time()`.

Timestamps are stored as naive UTC and only turned into local wall time at render time, so
every time, day header and "Today"/"Yesterday" is drawn on one clock: the process's own zone.
`_display_tz = None` means exactly that: `astimezone()` goes through libc, which honours `TZ`,
and compose hands the bot container the same `TZ`, so the feed reads like the bot's own log
lines. A `TZ` libc cannot resolve falls back to UTC, which is the old output. Tests pin the
zone so they don't depend on the host's; `now` is always the current UTC instant (naive) and
is injectable so tests can pin that too.

## Do not

Don't hardcode a timezone or convert at write time instead of render time — the process's own
`TZ` (shared with the bot container via compose) is what keeps the feed's clock consistent
with the bot's own log lines. Keep `now` injectable so tests aren't tied to wall-clock drift.
