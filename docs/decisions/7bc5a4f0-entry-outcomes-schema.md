# Per-entry check outcome in run_state.json

`bot/anibot.py` — `ENTRY_RESULTS`, `_record_entry_outcome()`, `_record_complete_outcome()`,
`_merge_entry_outcomes()`.

Persisted in `run_state.json`'s additive top-level `"entries"` key (NOT `ani.json` — the
watchlist store the dashboard also writes; keeping this in `run_state.json` avoids write
contention with it). Answers "why didn't X download?" (UX audit finding 3, card `7bc5a4f0`)
without scraping logs.

Shape, keyed by entry URL:

```
"entries": {
  "<url>": {
    "checked_ts": "2026-09-17T02:00:00Z",   # this cycle's check, RFC3339 UTC
    "result": "skipped",                     # see ENTRY_RESULTS
    "reason": "waiting for airdate 2026-09-20",  # human-readable, <=200 chars
    "episode": 12,                            # optional: the episode a
                                               # downloaded/unavailable/error
                                               # result concerns
    "last_error": {                           # optional: survives a LATER
      "reason": "JDownloader unreachable",    # non-error result, so "last
      "checked_ts": "2026-09-16T14:00:00Z"    # error" stays visible after
    }                                          # a subsequent clean skip
  }, ...
}
```

One entry per URL (latest outcome only, not a list) — bounded to the current watchlist size
since entries no longer on the watchlist are pruned on every write (see
`_merge_entry_outcomes`). Additive: a reader (the web dashboard's later card) must tolerate
both the key's absence (older `run_state.json`) and any per-entry sub-key's absence
(`episode`/`last_error` are optional).

`_merge_entry_outcomes` folds this cycle's outcomes onto the previously-persisted map:
pruned to the current watchlist; a URL not visited this cycle keeps its previous record
unchanged (a partial cycle never blanks entries it didn't reach); `last_error` survives a
later non-error outcome (a fresh "error" sets it, otherwise it carries forward untouched).

`_record_complete_outcome` must NOT blindly overwrite an existing "downloaded" outcome for
the same entry this cycle (e.g. its last episode just downloaded, which is exactly what
triggered the completion check to pass) — overwriting it with "skipped"/"complete" would
erase the one fact — "it downloaded" — the dashboard most needs to show for this cycle. A
"downloaded" outcome is kept as "downloaded", with the completion appended onto its existing
reason instead.

## Do not

Don't store this in `ani.json` — that's the watchlist store the dashboard also writes, and
sharing it would create write contention. Don't make a reader assume the `"entries"` key or
any optional per-entry sub-key is always present — both must degrade gracefully on an older
`run_state.json`. Don't let `_record_complete_outcome` overwrite a "downloaded" outcome
recorded earlier in the same cycle — append to it instead, or the dashboard loses the one
fact it most needs to show.
