# A dashboard-set episode offset is guarded against undershoot

`web/app.py` — the mover's series-path routing, applying a manually assigned season/offset to
a stuck "parse"/"unmatched" item.

A season/offset set from the dashboard (no `tvdb_id` required — see `apply_entry_edit`) can
carry an `episode_offset` that undershoots a low parsed episode number (e.g. offset `-12` on
`E05`). The result is guarded rather than filing a bogus `E00` or a negative episode.

## Do not

Don't apply a dashboard-set `episode_offset` to a parsed episode number without clamping the
result — an offset that undershoots a low episode (e.g. `-12` on `E05`) must not file as `E00`
or a negative episode.
