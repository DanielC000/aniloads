# Standalone log events hoisted above the no-run guard

`web/app.py` — `parse_bot_logs()`.

Standalone events (`[SKIP]`/`[COMPLETE]`/`[THROTTLE]`) can appear between cycles with no
active run. They're handled before the "no current run" guard further down so they always
create their own entry — these never attach to `current_run` anyway, so hoisting them keeps
active-run behavior identical while no longer silently dropping them when `current_run` is
`None`.

## Do not

Don't move the `[SKIP]`/`[COMPLETE]`/`[THROTTLE]` handling below the no-run guard — they
never attach to `current_run`, so doing so would silently drop every standalone event that
arrives between cycles instead of recording it as its own entry.
