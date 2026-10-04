# An unconfigured download backend must read as "waiting", never as a healthy start

`web/app.py` — `get_activity()`'s `waiting_for_config` surfacing, and
`render_activity()`'s boot-backoff rendering.

On a fresh install the seed writes an empty `jdhost`/`myjd_user` deliberately (see
`bot/config_defaults.py`), so the bot sits in its boot backoff loop (see
`bot/anibot.py`'s `write_waiting_for_config`). Left alone, Health shows JDownloader "Unknown"
and Bot Cycles "Unknown — no completed cycle", which looks identical to a healthy fresh start
— the one thing actually blocking everything would be invisible. `get_activity()` surfaces the
`waiting_for_config` marker so `render_activity()` can say "Waiting for configuration" instead
of a plain "Running" dot; `check_bot_staleness` is likewise surfaced so `render_activity` stays
consistent with the Health panel's own "Bot Cycles" row instead of independently claiming
everything is fine while Health already flagged a stale cycle. Health's "Bot Cycles" row is
the authority on staleness — nothing here may contradict it with a green dot.

## Do not

Don't let a container/process being "up" render as a plain healthy "Running" dot while
`waiting_for_config` is set — the container/process can be up with no cycle able to start at
all, for want of a download backend. Don't let this rendering silently contradict Health's own
"Bot Cycles" staleness verdict.
