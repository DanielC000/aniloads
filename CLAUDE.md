# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this is

Aniloads is a homelab anime-download automation stack for anime-loads.org. **Public repo** (`github.com/DanielC000/aniloads`), vendored into the owner's homelab as the `anime-loads/app` submodule. Pushing is an outward action — commit locally; **push is owner-gated**. Don't touch the live homelab or run deploys; verify locally. Secrets stay env-only, never committed.

## Structure

- `bot/` — Python/Selenium scraper: `animeloads.py`, `anibot.py`, `downloader.py`, `tvdb.py` (captcha + Click-n-Load → JDownloader). Dockerized via `bot/Dockerfile`.
- `web/` — stdlib-Python dashboard (`web/app.py`, port 8085; raw `http.server`, not Flask).
- `tests/` — pytest suite (`test_anibot.py`, `test_app.py`, `support.py`).
- `docker-compose.yml` — service composition.

## Commit Convention

Commits follow **Conventional Commits**: `type(scope): summary` — lowercase type, imperative summary, no trailing period, subject ≤72 chars. Loom lands one squash commit per board task whose subject is the **card title**, so title board cards in this same form.

**Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`.

**Scopes (this repo):**
- `bot` — the scraper/downloader · `web` — the dashboard · `tests` — the test suite.
- `docker` — Dockerfile / compose · `deps` — `requirements.txt` · `meta` — repo config, this file.

A scope is required when a change clearly belongs to one area. Examples: `fix(bot): retry phantom-episode download loop`, `feat(web): add per-series status filter`.
