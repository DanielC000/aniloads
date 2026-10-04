# Catching ValueError also degrades a corrupt small JSON file, not just bad JSON

`web/app.py` — `load_run_state()`, `load_move_state()`, `_load_run_now_last()`, and
`load_prefs()`'s small-file readers.

Each of these catches `(FileNotFoundError, [OSError,] ValueError)` around its `json.load()`.
`ValueError` also covers `UnicodeDecodeError` (a corrupt/non-UTF-8 file): both it and
`json.JSONDecodeError` subclass `ValueError`, so this degrades a bad file to the empty/default
state instead of a 500, without swallowing an unrelated bug (e.g. a `TypeError`) as if it were
a corrupt file.

## Do not

Don't narrow this to `json.JSONDecodeError` or widen it to bare `Exception` — `ValueError`
is the deliberate boundary: it catches `JSONDecodeError` and `UnicodeDecodeError` (both corrupt
file shapes) while still letting an unrelated bug (e.g. `TypeError`) surface as a real error
instead of being silently treated as a corrupt file.
