# Anime-Loads Stack

Automated anime downloading from [anime-loads.org](https://www.anime-loads.org/) with a web-based watchlist manager.

## Architecture

```
              ┌──────────────┐     ┌──────────────┐
              │  Docker API  │     │   TVDB API   │
              │ (logs,restart│     │(season data, │
              └──────▲───────┘     │ status, air  │
                     │             │   dates)     │
                     │             └──────▲───────┘
                     │                    │
┌─────────────┐     ┌┴────────────┐     ┌─┴────────────┐     ┌──────────────┐
│  anime-web  │────>│   ani.json  │<────│  anime-loads  │────>│  jdownloader │
│   :8085     │     │ (watchlist) │     │    (bot)      │     │   :5800      │
└─────────────┘     └─────────────┘     └───────────────┘     └──────────────┘
                          │                                          │
                          │ tvdb_season                        ┌─────v────────┐
                          │ episode_offset                     │  /downloads  │
                          │ complete, skip_until               │   /anime     │
                          │                                    └─────┬────────┘
                          │              ┌──────────────┐            │
                          └─────────────>│move-completed│<───────────┘
                                         │  (watcher)   │
                                         └──────┬───────┘
                                         ┌──────v───────┐
                                         │ /media/anime │
                                         │   (Plex)     │
                                         └──────────────┘
```

## Services

| Service | Container | Port | Description |
|---------|-----------|------|-------------|
| **anime-web** | `anime-web` | 8085 | Dashboard — watchlist management, bot activity, run history |
| **anime-loads** | `anime-loads` | — | Bot that monitors watchlist, scrapes anime-loads.org, pushes links to JDownloader |
| **jdownloader** | `jdownloader` | 5800 | Download manager with web UI (VNC) |
| **move-completed** | `anime-move-completed` | — | Organises finished downloads into Plex-style `AnimeName/SXX/` folders |

## Setup

### 1. Configure Environment

```bash
cp .env.example .env
# Edit .env and add your TVDB API key (optional, enables season correlation)
```

The TVDB API key is free — get one at https://thetvdb.com/dashboard/account/apikey.
If omitted, the stack works without TVDB features.

### 2. Deploy

```bash
./deploy.sh
```

This copies all source files to the server, builds the bot Docker image from `bot/`, and starts the stack. On first deploy, `.env` is auto-created from `.env.example` if it doesn't exist on the server.

### 3. JDownloader Initial Setup

Open http://SERVER_IP:5800 and configure:

- **Settings > LinkGrabber**: Enable "Auto Confirm" and "Auto Start Downloads"
- **Settings > Advanced > ExternInterface**: Set `localhostonly = false`
- **Settings > Advanced > RemoteAPI**: Set `externinterfacelocalhostonly = false`

The ExternInterface settings are required for the anime-loads bot container to reach JDownloader's CNL endpoint (port 9666) over the Docker network.

### 4. Anime-Loads Bot Config

The bot config lives at `/mnt/Media/Configs/anime-loads/ani.json`.

Settings in `ani.json > settings`:

| Field | Value | Description |
|-------|-------|-------------|
| `browserengine` | `0` | Firefox (bundled in image) |
| `hoster` | `1` | 0 = DDownload, 1 = Rapidgator |
| `jdhost` | `jdownloader` | Docker network hostname |
| `timedelay` | `600` | Poll interval in seconds |
| `al_user` | *(optional)* | anime-loads.org login — recommended for reliable downloads |
| `al_pass` | *(optional)* | anime-loads.org password |

### 5. Dashboard

Open http://SERVER_IP:8085. Features:

- **Bot Activity** — live status, last run time, next run estimate
- **Run Now** — trigger an immediate bot check
- **Run History** — colour-coded feed of recent runs (downloads, errors, checks)
- **Watchlist** — add/remove anime, see episode counts, retry status, TVDB season mapping, and completion status
- **Preferences** — default language, resolution, auto-select
- **Add Anime** — paste an anime-loads.org URL or search by name, with TVDB season correlation
- **TVDB Linking** — link/unlink existing watchlist entries to TVDB series and seasons
- **Smart Skip Badges** — shows "Complete" (green), "Next: date" (orange), and anime-loads status per entry. "Mark Incomplete" button to force re-checking.

## ani.json Schema

The bot and web UI share `ani.json`. Anime entries:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Display name of the anime |
| `url` | string | anime-loads.org media URL |
| `releaseID` | int | 1-indexed release ID on the media page |
| `episodes` | int | Number of episodes the bot has processed |
| `missing` | int[] | Episode numbers that failed and need retry |
| `customPackage` | string | JDownloader package name (determines download folder name) |
| `tvdb_id` | int? | TVDB series ID (set via web UI TVDB correlation) |
| `tvdb_season` | int? | Correct season number for this entry (overrides filename SxxExx) |
| `episode_offset` | int? | Added to episode numbers when moving files (default 0) |
| `complete` | bool? | Set automatically when series is finished and all episodes downloaded. Bot skips this entry. |
| `skip_until` | string? | ISO date (e.g. `"2026-04-22"`). Bot skips checking until this date — set from TVDB next-episode airdate. |
| `skip_real_airdate` | bool? | Whether `skip_until` is a real TVDB-predicted airdate (`true`) vs a synthetic throttle date (`false`). Only real airdates get early-release scraping (`EARLY_SCRAPE_DAYS` window); synthetic dates are honored strictly. |
| `tvdb_series_status` | string? | Cached TVDB series status (`"Ended"`, `"Continuing"`, etc.) |
| `al_status` | string? | Cached anime-loads.org status (`"Abgeschlossen"`, `"Laufend"`, etc.) |
| `al_max_episodes` | int? | Cached max episode count from anime-loads.org |
| `media_type` | string? | Cached from anime-loads.org Description tab — `"series"`, `"movie"`, `"ova"`, `"special"`, `"web"`, `"bonus"`. Only `"movie"` changes mover routing. |
| `year` | int? | Cached release year from anime-loads.org — used for Plex movie folder naming `Title (Year)`. |
| `display_title` | string? | Cached best title from the Description tab (German > English > Japanese) — used for movie folder names. |

### TVDB Integration

Each anime-loads.org URL represents one season of a series. Release groups often
mislabel season numbers in filenames (e.g., `S01E01` for what is actually Season 2),
causing `move-completed` to place files in the wrong season folder.

The TVDB fields solve this by letting the user map each anime-loads entry to the
correct TVDB series and season. `move-completed` then overrides the filename's season
number when organising files.

**Example:** "Sousou no Frieren Dai 2 Ki" on anime-loads.org is Season 2, but
releases may name files `Frieren.S01E01.mkv`. With `tvdb_season: 2` set, the file
is moved to `Frieren/S02/` instead of `Frieren/S01/`.

**How it works:**

1. When adding anime, the dashboard auto-searches TVDB and shows matching series
2. Select the series, then pick the correct season — the UI auto-suggests based on
   matching episode counts between the anime-loads release and TVDB seasons
3. For existing entries, use the "Link TVDB" button on the watchlist card
4. `move-completed` reads `tvdb_season` and `episode_offset` from `ani.json` and
   overrides the season/episode numbers parsed from filenames

**`episode_offset`** handles cases where anime-loads numbers episodes from 1 but
TVDB continues from a previous season (e.g., offset 12 turns ep 1 into E13). Most
anime restart at E01 per season, so the default of 0 is usually correct.

If no TVDB API key is configured, all TVDB features are hidden and the stack works
exactly as before.

### Smart Skip-Checking

The bot avoids unnecessary Selenium scrapes by checking completion status before
launching a browser. Each cycle, for every entry:

1. **Already complete?** — entries with `complete: true` and no missing episodes are
   skipped entirely (no Selenium, no HTTP)
2. **Next episode not aired?** — if `skip_until` is set and in the future, the entry
   is deferred until that date. For a real TVDB airdate (`skip_real_airdate: true`) the
   bot scrapes from `EARLY_SCRAPE_DAYS` (default 1) before the date, to catch episodes
   anime-loads.org publishes early; synthetic throttle dates are honored strictly.
3. **TVDB status check** (lightweight HTTP, no Selenium) — if the entry has a `tvdb_id`:
   - Series status is `"Ended"` and all episodes downloaded → mark `complete`, skip
   - Series status is `"Continuing"` → fetch next episode airdate, set `skip_until`
4. **Normal check** — Selenium scrape runs, and after processing, the bot caches
   `al_status` and `al_max_episodes`. If anime-loads.org reports the series as
   `"Abgeschlossen"`/`"Completed"` and all episodes are downloaded, the entry is
   marked `complete`.

Completion is automatic. To re-enable checking (e.g. surprise continuation), use the
"Mark Incomplete" button on the dashboard.

## File Paths

| Path | Purpose |
|------|---------|
| `/mnt/Media/Configs/anime-loads/ani.json` | Bot config + watchlist |
| `/mnt/Media/Configs/anime-loads/web-prefs.json` | Web UI preferences |
| `/mnt/Media/data/downloads/anime/` | JDownloader download output |
| `/mnt/Media/data/media/anime/` | Plex anime series library (move target for series/OVA/special/web) |
| `/mnt/Media/data/media/anime movies/` | Plex anime movies library (move target for movies, `MOVIE_MEDIA_DIR`) |
| `/mnt/Media/setup/anime-loads/` | Deployed stack on server |
| `/mnt/Media/setup/anime-loads/.env` | TVDB API key (created from `.env.example` on first deploy) |

## move-completed Behaviour

The watcher runs every 5 minutes. For each download directory:

1. **Skips** if any file was modified in the last 5 minutes (download still active)
2. **Skips** if `.part` files exist (incomplete downloads)
3. **Cleans** leftover `.rar`/`.r00` archives once video files exist
4. **Resolves** the download dir to an `ani.json` entry via `customPackage`/`name`
5. **Branches** on the matched entry's `media_type`:
   - **Movie** → moves the video to `MOVIE_MEDIA_DIR/<Title> (<Year>)/<Title> (<Year>).<ext>` (Plex Movies convention). Uses `display_title` + `year` cached on the entry. No `SxxExx` parsing.
   - **Series / default** → parses `SxxExx` per video, applies `tvdb_season`/`episode_offset` overrides, moves into `MEDIA_DIR/<AnimeName>/S<xx>/`
6. **Cleans up** empty download directories

Set `DRY_RUN=1` in docker-compose to log actions without moving anything.

## Testing

A lightweight, hermetic test suite covers the pure-logic functions (release
matching, language prefs, filename/season parsing, the early-release skip
helper). It uses stdlib `unittest` only — no extra runtime deps, no network,
Selenium, Docker, or filesystem dependence.

```bash
PYTHONPATH=bot python -m unittest discover tests
```

The tests import `web/app.py` and `bot/anibot.py` directly; both are
import-safe (their server/CLI entry points are guarded by `__name__ ==
"__main__"`), and the suite stubs the bot's optional heavy deps so they need
not be installed.

## Bot Fork Details

The original `pfuenzle/anime-loads` image (Dec 2021) no longer works because:

- The site's server-side adblock detection blocks headless browsers that don't load ad scripts
- The captcha fallback API changed (`captcha-idhf` parameter no longer accepts static values)

The local fork (`bot/`) fixes this by:

- **Browser-based page fetching**: `updateInfo()` uses Selenium instead of plain HTTP requests, avoiding IP-based adblock flagging
- **Shared browser session**: The browser from `updateInfo()` is reused by `downloadEpisode()` — only one browser session per anime, keeping the ad-loaded context intact
- **CNL timeout handling**: `addToJD()` treats timeouts as success since JDownloader's endpoint processes links without responding
- **Modernised stack**: Python 3.11, current Firefox ESR, latest geckodriver, updated Selenium API

## Known Quirks

- **Login recommended**: The site's adblock detection is aggressive for anonymous users. Adding `al_user`/`al_pass` to settings improves reliability.
- **CNL timeout**: The bot logs "request timed out (links likely added)" — this is expected. JDownloader's addcrypted2 endpoint processes the links but doesn't always send a response.
- **Search speed**: Dashboard search uses headless Firefox to bypass DDoS-Guard. First search takes ~60s (Firefox startup).
- **Pending entries**: When adding via URL, entries go to a `pending` queue. A background resolver fetches release info and auto-selects the best release.

## Dependencies

- `bot/` — private fork of Pfuenzle/anime-loads (built locally via Dockerfile)
- `jlesage/jdownloader-2` — JDownloader with VNC web UI
- `alpine:latest` — move-completed watcher (with `jq` installed at runtime)
- Docker network `arr-net` (shared with Sonarr/Radarr stack)
