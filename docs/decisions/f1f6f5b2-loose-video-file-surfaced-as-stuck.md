# A loose video file is surfaced as stuck, never guessed into a folder

`web/app.py` — the mover's per-directory loop, the "loose file" branch.

A video file sitting loose in the download root (JDownloader without package subfolders
enabled has nowhere else to put it) breaks every folder-oriented assumption the move logic
makes: the recency scan, archive/junk cleanup, subtitle sidecars, and empty-dir removal all
key off `dir_path`, and `match_anime_entry`'s reliable signals (`download_folder_pattern`,
`customPackage`) are keyed off a package folder name that doesn't exist here. Rather than
guess at a destination, it's surfaced as stuck for a human to either enable package
subfolders or move the file into one by hand.

## Do not

Don't try to route a loose video file (no package subfolder) through the normal
folder-oriented move logic — none of its signals (`download_folder_pattern`, `customPackage`,
the recency/archive/subtitle/empty-dir steps) apply to a bare file. Surface it as stuck
instead of guessing a destination.
