# Self-healing boot path instead of crash-looping the container

`bot/anibot.py` — `startbot()`'s non-interactive (Docker) boot sequence: the config wait
loop, the `animeloads()` Selenium init retry, the MyJDownloader-password wait loop, and the
JD-port wait loop. Also `_boot_backoff()`.

Under `restart: unless-stopped`, letting the process `sys.exit`/raise on a transient boot
failure crash-loops the container, and Docker's own restart backoff can leave it "not
running" for long stretches — which is exactly the "the bot didn't start automatically"
symptom an owner would see. Several distinct boot-time failures are each handled the same
way: log the problem, back off (`_boot_backoff`, capped exponential: 5, 10, 20, 40, 80, 160,
300, 300, ... seconds), and retry in-process instead of exiting — so a transient cause (the
`/config` volume not mounted yet on host boot, a stale geckodriver/profile hiccup, a password
or port genuinely not set yet) self-heals once it clears, and the container — and the
dashboard's status/controls — stay live throughout:

- No download backend configured (`jdhost`/`myjd_user` both unset): wait and re-read config.
- The `animeloads()` constructor launching headless Firefox/geckodriver can fail on a cold
  start (resource contention while the host is still bringing services up, a stale
  profile/geckodriver hiccup) — retry with backoff instead of exiting.
- Neither a local JD host nor a MyJDownloader password: wait and re-read config rather than
  requiring a manual restart once the owner fixes it.
- No JD port set (`jd_deprecated` but no `jd_deprecatedport`): same wait-and-retry, instead of
  the interactive path's `sys.exit(1)`.
- No anime configured yet in the main cycle loop (fresh deploy, or none added via the
  dashboard): do NOT `return` — that exits the process (exit 0) and stops/tight-loops the
  container under `restart: unless-stopped`, which reads as "the bot won't stay running".
  Stay alive and re-check after the poll interval so entries added later via the dashboard
  are picked up without a manual container restart.

This only applies to the non-interactive path — the interactive CLI path still exits/raises
immediately, since there's a human present to fix it right away.

## Do not

Don't `sys.exit`/`return`/let an exception propagate out of any of these non-interactive
paths —
that crash-loops the container under `restart: unless-stopped` and can leave it "not running"
for an extended stretch. Keep retrying in-process with `_boot_backoff()` instead, and keep
this self-healing behavior scoped to the non-interactive path only — the interactive CLI path
is expected to exit/raise since a human is present to respond.
