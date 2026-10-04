# Force-check bypass and the single-day al_available_max cap reset

`bot/anibot.py` — `startbot()`'s per-entry loop, around `resolve_force_check()` and the
`al_available_max` cap-reset block.

## Smart-skip force_check bypass

`force_check` is a dashboard "Check now" click on this one entry. It's peeked fresh (not
this cycle's snapshot) and cleared unconditionally right in `resolve_force_check()` — a
one-shot bypass of Steps 1-4 of the smart-skip logic, honored at most once even if the scrape
that follows fails.

## al_available_max cap revalidation

`curEpisodes` is capped by the known-available max (the site DOM may over-report if tabs have
no links). The cap is single-day: a stale cap from a prior day must not permanently hide
episodes the site has since added. It's revalidated by clearing yesterday's cap when the DOM
reports more episodes than the cap allows — if those new episodes are still phantom, the
batch/single-episode download paths further down re-set the cap with today's date.

## match_anime_entry's download_folder_pattern tiebreak (web/app.py)

`web/app.py` — `match_anime_entry()`'s `download_folder_pattern` path, `_token_prefix_score`.

Tried first because it's auto-derived by the bot from the actual release filename, so it
matches the JD-created folder even when JD ignores `customPackage` (the user's chosen Plex
output folder, not the download folder). Among candidates, the entry whose pattern shares the
longest leading-token run with the dir name wins, so two entries about the same series (e.g.
S01 + S03 of one show) don't both win on the generic prefix. When the JD folder name carries
no season token of its own (e.g. the user named both seasons "Mob Psycho 100"), the
leading-prefix score ties across entries — the filename's `SxxExx` is still authoritative, so
the tie is broken by preferring the entry whose `tvdb_season` matches.

## Do not

Don't let a `force_check` bypass apply more than once per request — it must be cleared
unconditionally in `resolve_force_check()` regardless of whether the scrape that follows
succeeds. Don't make the `al_available_max` cap permanent — it must be re-checked daily
(`cap_set_at != today_iso`) and cleared when the DOM reports more episodes than it allows, or
genuinely-new episodes stay hidden forever behind a stale cap. Don't let
`download_folder_pattern`'s longest-leading-token-run tiebreak ignore a parsed `SxxExx` when
prefix scores tie — the filename's season is still authoritative.
