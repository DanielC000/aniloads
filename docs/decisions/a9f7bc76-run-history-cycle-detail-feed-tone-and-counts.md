# Run-history feed: which events get a tone and the raw-vs-filtered count rule

`web/app.py` — the per-cycle run-history feed rendering.

## Downloads, errors and mismatches are always loud

A cycle's events split by signal: downloads, errors and numbering mismatches are the news and
always get their own visible line; unavailable/complete are background detail that hides
behind the expand toggle so the feed stays readable. A "mismatch" (a release numbering its
files 41-46 while the watchlist wants 1-7) is the most actionable line the feed can show — it
names the config change that fixes it — so it is never behind the toggle, and never folded
into a "nothing new" group.

## A count caveat must count the raw list, not the filtered one

Where the feed shows a "this list may be incomplete" caveat, it counts the RAW recorded list,
not the filtered-for-rendering one: an unrecognised kind (a newer bot than this dashboard) is
dropped from rendering, but it was still recorded — and the one line whose whole job is saying
the list is incomplete must not itself state a wrong (under-counted) number.

## Do not

Don't move a download/error/mismatch event behind the expand toggle — those three are always
shown. Don't derive an "incomplete list" count from the already-filtered/rendered events — it
must count the raw recorded list, or the caveat itself understates how much was dropped.
