# Add-anime flow: validate-or-rescrape, the TVDB-step gate, and bot-owned scalars

`web/app.py` — the `/add-url` / `/add-release` handler chain.

## Validate posted selection fields, don't trust or blindly reject them

The `release_id` / `media_type` / episode-count carried as hidden fields from the
release-selection page (or the TVDB step that followed it) are validated against a fresh
re-derivation rather than trusted outright — see `_resolve_release_selection`'s docstring for
the validate-or-rescrape rule this applies. No `release_id` at all is a legitimate call shape
(adding without ever going through release selection) — only a *posted-but-unconfirmable* one
(tampered, or stale beyond recovery) is rejected outright.

## The TVDB step is a mandatory gate before saving, when available

If TVDB is available and the user hasn't been through the TVDB step yet, the correlation page
is shown instead of saving immediately. For movies, the "through the TVDB step" signal is
either `tvdb_skip` or a posted `tvdb_id` — there's no `tvdb_season` field to look for.

## Bot-owned scalars are set early so the first render is correct

New entries start with the global prefs as their per-entry prefs, same as badges an entry
resolved earlier carries. Bot-owned scalars already known from the release fetch are set now
so, e.g., a movie shows its Movie badge before the bot's first cycle — the bot's own delta save
only overwrites them later when its freshly-scraped value actually differs.

## Do not

Don't trust a posted `release_id`/`media_type`/episode-count outright, and don't reject a
request that simply has no `release_id` at all (that's the no-release-selection call shape) —
only a posted-but-unconfirmable one is rejected. Don't save past the TVDB step when TVDB is
available and the user hasn't been through it yet. Don't leave a new entry's bot-owned scalars
unset until the bot's first cycle when the release fetch already determined them.
