# A manually overridden season/episode rewrites the filename's SxxExx token

`web/app.py` — the mover's series-path routing, after a dashboard season/episode override is
applied to a stuck item.

The `SxxExx` token in the filename itself is rebuilt whenever the season or episode was
overridden from the dashboard — Plex's scanner reads `SxxExx` from the filename, not just the
destination folder, so leaving a stale `S01` token behind would still file the release under
season 1 regardless of which folder it landed in.

## Do not

Don't apply a season/episode override to only the destination folder — Plex's scanner reads
`SxxExx` from the filename too, so the filename's token must be rebuilt to match or the
release still shows up under the old season in Plex.
