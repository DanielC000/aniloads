# Classifying a failed batch-CNL download

`bot/anibot.py` — `startbot()`'s per-entry loop, the `else` branch after a failed
`anime.downloadBatchCNL()` call. Mirrors `handle_failed_batch()`'s own classification (kept
in sync with it — see that function's docstring).

Decide error-vs-benign and refresh the cap in one place (`handle_failed_batch`); `save_ani()`
stays at the call site so that helper remains pure/testable. This block re-derives the same
all-phantom / numbering-mismatch / genuine-failure classification `handle_failed_batch` uses
internally, so the persisted entry outcome (`_record_entry_outcome`) matches the
`[MISMATCH]`/`[UNAVAILABLE]`/`[ERROR]` tag `handle_failed_batch` actually logs.

## Do not

Don't let this classification drift out of sync with `handle_failed_batch()`'s own — both
must agree on all-phantom vs. numbering-mismatch vs. genuine-failure, or the persisted entry
outcome will show a different result than what was actually logged. Don't move `save_ani()`
into `handle_failed_batch()` itself — keeping it pure/testable (no I/O) is deliberate.
