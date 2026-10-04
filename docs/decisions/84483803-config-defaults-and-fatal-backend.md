# ani.json defaults and the one fatal config requirement

`bot/anibot.py` — `loadconfig()`. `bot/config_defaults.py` — `DEFAULT_SETTINGS`.

## Seeding and optional-key fallback

`loadconfig()` seeds a fresh, fully-defaulted `ani.json` the first time anyone (bot or
dashboard) looks for it — never overwrites a real, existing file (see
`anistore.seed_if_missing`). Without this, a missing file used to mean "no/bad config"
forever until someone hand-wrote one.

Missing OPTIONAL keys (anything a hand-edited or partially upgraded `ani.json` can simply
omit) fall back to `config_defaults.DEFAULT_SETTINGS` instead of the old "Fehlerhafte
ani.json Konfiguration" hard failure — only a missing download backend is actually fatal.

The one thing `loadconfig()` truly cannot default: a download backend. Either a local
JDownloader host, or a MyJDownloader user (its password can still be entered interactively at
startup, see `startbot()`'s own `jdhost==""` and `myjd_pass==""` handling) must be configured.

## Where each default value comes from

- `hoster`: the (value, label) pairs are duplicated in `config_defaults.py` rather than
  imported from `animeloads.py`, because `animeloads.py` pulls in selenium at module scope —
  `config_defaults.py` must stay import-safe without it. The README's documented example
  `ani.json` uses `hoster=1` (Rapidgator).
- `browserengine`: Firefox is the only browser bundled in the bot image (`bot/Dockerfile`
  installs firefox-esr + geckodriver, no Chrome/chromedriver) — also the README's documented
  default.
- `timedelay`: what `anibot.startbot()`'s own recheck fallback already uses whenever
  `timedelay` isn't a positive int, and what `editconfig()`'s own prompt recommends
  ("Empfohlen: 600 Sekunden").
- `jdhost` / `myjd_user` etc.: empty, matching `downloadEpisode`/`downloadBatchCNL`'s own
  default parameter value — a fresh install has no download backend configured yet, and
  `loadconfig()` requires the owner to set one of these before the bot can actually download
  anything.

## Do not

Don't turn a missing optional `ani.json` settings key back into a hard failure — fall back to
`config_defaults.DEFAULT_SETTINGS` instead. Don't default a download backend (`jdhost` /
`myjd_user`) to anything non-empty — that's the one setting `loadconfig()` must treat as
fatal when absent. Don't import the hoster value/label pairs from `animeloads.py` into
`config_defaults.py` — that would pull selenium into a module that must stay import-safe
without it.
