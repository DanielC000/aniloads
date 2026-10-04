# Stuck-item mover: known-junk cleanup scope and the existing-folder check

`web/app.py` — the mover's cleanup-after-move step and the series-path existing-folder check.

## Known-junk cleanup is a narrow allowlist

Known-junk leftovers (release nfo/readme, NZB/torrent leftovers, poster thumbnails) are the
only things deleted after a move. Archives are handled separately, earlier in the cycle.
Anything else — an unrecognized extension — is left in place rather than guessed at.

## An unmatched download with an existing library folder still files normally

The existing-folder check (case-insensitive) exists because an unmatched download whose
parsed name already has a folder in the media library (a show removed from the watchlist, or
a manual JDownloader add of something already in Plex) should still file normally. The
stuck/unmatched path is only for a download that would otherwise SILENTLY CREATE a brand-new,
likely-duplicate folder.

## Do not

Don't widen the known-junk delete list beyond release nfo/readme, NZB/torrent leftovers, and
poster thumbnails — an unrecognized extension must be left in place, not guessed at and
deleted. Don't route an unmatched download straight to "stuck" without first checking for an
existing library folder by name — only a download that would otherwise silently create a new
folder belongs on the stuck path.
