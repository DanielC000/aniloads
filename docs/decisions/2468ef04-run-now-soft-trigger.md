# Soft run-now trigger file

`bot/anibot.py` — `RUN_NOW_FILE` / `sleep_until_next_cycle()` / `consume_run_now_trigger()`.

The dashboard drops this trigger file next to `ani.json` (see `web/app.py`'s
`trigger_run_now()`) instead of restarting the bot container. The bot wakes its inter-cycle
sleep early when it appears (`sleep_until_next_cycle`) and consumes (deletes) it at the start
of the cycle it triggers (`consume_run_now_trigger`) — a request arriving mid-cycle just sits
there until that point, so it is always honored *after* the running cycle, never by
interrupting it.

## Do not

Don't make a run-now request interrupt an in-flight cycle — it must only wake the inter-cycle
sleep early and get consumed at the next cycle's start, never mid-cycle (e.g. mid a
JDownloader hand-off).
