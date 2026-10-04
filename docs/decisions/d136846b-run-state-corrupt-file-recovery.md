# Degrading a corrupt run_state.json instead of losing the write

`bot/anibot.py` — `write_run_state()`'s read of the previous state file.

`ValueError` also covers `UnicodeDecodeError` (a corrupt/non-UTF-8 file): both it and
`json.JSONDecodeError` subclass `ValueError`, so this degrades a bad previous-state file to a
fresh history instead of losing the whole write, without swallowing an unrelated bug as if it
were a corrupt file.

## Do not

Don't narrow this except clause to just `json.JSONDecodeError` — a non-UTF-8 previous
`run_state.json` raises `UnicodeDecodeError`, which only `ValueError` (its superclass) also
catches. And don't widen it to a bare `except Exception` — that would also swallow unrelated
bugs as if they were a corrupt file.
