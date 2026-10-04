# Bounds for manual library-placement fields

`web/app.py` — the manual season/offset edit form (editing library placement without TVDB).

A season is a folder number (`S00` specials up to a year-style season); the offset shifts a
release's episode number onto the library's (`release + offset = library`, the same rule the
mover and the bot's batch matcher apply), so it never needs to exceed a long-runner's absolute
episode count.

## Do not

Don't widen the season/offset input bounds without re-checking the mover's and the bot's batch
matcher's own `release + offset = library` arithmetic — all three must agree on what a given
offset means, or a manually set value silently misfiles once the bot or mover apply it.
