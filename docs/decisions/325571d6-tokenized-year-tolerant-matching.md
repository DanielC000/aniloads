# @decision 325571d6 — tokenized, year-tolerant watchlist matching

## Problem

A real stuck download: `Tokyo.Revengers.2021.S04E01.German.ML.AAC.1080p.WebDL.x264-PuddingSama.mkv`
never matched the watchlist entry "Tokyo Revengers", and the mover offered
only "Ignore" / "Move anyway" (which would have created a bogus "Tokyo
Revengers 2021" folder instead of filing into the real one).

Tracing `match_anime_entry` (web/app.py) showed the bug was broader than "the
release year breaks the match." Before this fix, three of its four lookup
strategies compared whole strings instead of tokens:

- `customPackage.lower() in dir_basename.lower()` and
  `name.lower() in dir_basename.lower()` are whole-string substring checks.
  They silently depend on both sides using the SAME separator — a
  space-separated `customPackage`/`name` ("Tokyo Revengers") is never a
  substring of a dot-separated JDownloader folder name
  ("tokyo.revengers.2021..."), even with no year involved at all. The
  existing test `test_custompackage_fallback` only ever passed because its
  synthetic dir string happened to already contain a space at the right
  spot — not representative of a real release name.
- `entry["name"].lower() == parsed_name.lower()` is exact equality, and
  `parsed_name` keeps the release year as a literal token
  (`parse_season_episode` doesn't strip it), so "tokyo revengers 2021" never
  equals "tokyo revengers".

Only the `download_folder_pattern` path (`_token_prefix_score`) already
tokenized before comparing, which is why it's immune to both problems — but
it requires a 3+-token pattern to be set on the entry, which isn't
guaranteed.

## Fix

Both gaps are fixed structurally, not by special-casing the year string:

1. **Tokenize before comparing.** `_tokens_contained` (containment, for the
   customPackage/name-in-dir checks) and `_tokens_equal_year_tolerant`
   (equality, for the parsed-name check) both operate on token lists
   (`_tokenize`, already separator-agnostic: splits on `.`/`_`/`-`/space),
   so a dotted release name matches a space-separated watchlist name
   regardless of which separator either side happens to use. Because
   containment now actually fires on real dotted folder names (it almost
   never did before), the two containment steps collect every matching
   entry and pick the one with the most needle tokens via
   `_most_specific_entries` (ties broken by `tvdb_season == parsed_season`,
   else list order) — otherwise a watchlist holding both "Bleach" and
   "Bleach Thousand Year Blood War" would file a TYBW release under
   whichever entry is listed first, a real past incident.

2. **Asymmetric year tolerance**, in `_tokens_equal_year_tolerant`: a
   year-like token (`(19|20)\d{2}`) is only ever dropped from a side when
   the OTHER side has no year token of its own. If both sides carry a
   year-like token and the lists aren't already equal, the years differ (or
   something else does) and the match fails. This means:
   - a release year with no counterpart on the watchlist entry's side is
     tolerated (`Tokyo.Revengers.2021` → "Tokyo Revengers" matches);
   - an entry whose own name legitimately contains the year still matches a
     same-year release exactly as before (no regression);
   - two differently-dated releases of similarly-named entries (e.g. "Foo
     2021" vs. a `Foo.2023...` release) never collide — the year is dropped
     only when there's nothing to compare it against, never when there's a
     mismatch to paper over.

3. **Single-token containment is anchored at the first token.**
   `_tokens_contained` requires a needle of exactly one token (e.g. a short
   watchlist name like "86" or "Re") to match `haystack_tokens[0]`, not
   anywhere in the sequence — otherwise a short/generic name would match any
   unrelated release that happens to contain that token mid-name, which the
   OLD raw substring check never allowed either (no loosening the matcher
   for the short-name case while fixing the dotted/year case).

`find_existing_media_folder` itself needed no change: once `match_anime_entry`
returns the corrected `folder_name` ("Tokyo Revengers"), its existing
case-insensitive directory listing lookup already finds the real folder.

## Scope note: why "Move anyway" became "assign to a watchlist entry"

The fix above only helps a download whose entry can be algorithmically
inferred from the filename/folder. An `unmatched` item with genuinely no
matching entry (a typo'd title, a show not yet in the watchlist at
download time, etc.) still needs a human to pick the right one — that's
the "assign" control added to the `unmatched` stuck-item card, reusing
`stuck_assign`'s existing security machinery unchanged (source path only
from the stuck store, `find_entry_by_url` resolution, bounded
season/episode, movies excluded). Season/episode are derived fresh at
RENDER time from `parse_season_episode(path)` rather than persisted on the
stuck record, so an older record (written before this existed) still
renders correctly. The TVDB season/offset recompute when a series is
picked is done client-side (JS), purely as UX — the submitted
season/episode values are still what `stuck_assign` treats as literal
finals, so its contract and tests are unchanged; with JS disabled the raw
parsed values submit as-is.
