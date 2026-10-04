# Persisted run-state record

`bot/anibot.py` — `RUN_STATE_FILE` / `write_run_state()`, and the per-cycle bookkeeping call
site inside `startbot()`'s main loop.

The dashboard derives `last_run` / `next_run` from this file instead of scraping the rolling
container-log tail (the German run markers "Prüfe …"/"Schlafe N Sekunden" roll off the
500-line window under verbose logging, which made the UI show "No runs yet" / "—" while the
bot was running fine). Written next to `ani.json` so the bot and the dashboard share one
config dir. The `runs` history is bounded and holds one summary per cycle, so a future
run-history UI can render one summary per run from it.

Per-cycle run-state bookkeeping is also persisted for the dashboard's `last_run`/`next_run`,
independent of the rolling log tail. A run-now request is consumed at the START of the cycle
it triggers — a request that arrived mid-cycle just sits in the file until that point, so it
is always honored *after* the previous cycle finished, never by interrupting it.

`web/app.py`'s `get_activity()` treats the persisted run-state record (when present) as
authoritative for `last_run`/`next_run` — it survives the log-tail rollover that log-parsing
alone is vulnerable to. The log-parsed `runs` still drive the event feed as a fallback.
`run_state` is also surfaced on the result so a sibling run-history UI can render one summary
per run from it directly.

## Do not

Don't go back to deriving `last_run`/`next_run` from the container log tail — under verbose
logging the relevant lines roll off the window and the dashboard falsely shows "No runs yet"
even when the bot is healthy. Keep writing `run_state.json` next to `ani.json`.
