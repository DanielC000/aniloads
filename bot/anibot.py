import logging
import subprocess, sys, json, time, os, re, random
from logging.handlers import TimedRotatingFileHandler

from getpass import getpass

from datetime import datetime, date, timedelta, timezone

LOG_DIR = os.environ.get("LOG_DIR", "/config/logs")
LOG_FILE = os.path.join(LOG_DIR, "anibot.log")
LOGLEVEL = getattr(logging, os.environ.get("LOGLEVEL", "INFO").upper(), logging.INFO)

# Days before a skip_until date to start scraping anyway. The bot defers
# scraping a "Continuing" series until skip_until (the TVDB-predicted airdate),
# but anime-loads.org sometimes publishes an episode early. Once today is within
# this many days of skip_until, the bot scrapes anyway to catch the early
# release. 0 disables (strict skip_until honoring). See should_scrape_despite_skip().
EARLY_SCRAPE_DAYS = int(os.environ.get("EARLY_SCRAPE_DAYS", "1"))

# Hours between re-checks when TVDB's predicted airdate has already passed but
# anime-loads.org hasn't published the episode yet. Without this throttle, a
# "Continuing" series with a past-due airdate has no future skip_until to defer
# to and gets scraped every poll cycle until the episode appears.
TVDB_PASTDUE_RECHECK_HOURS = float(os.environ.get("TVDB_PASTDUE_RECHECK_HOURS", "2"))

_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setFormatter(logging.Formatter("%(message)s"))
_handlers = [_stdout_handler]

_file_log_error = None
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        os.chmod(LOG_DIR, 0o777)
    except OSError:
        pass
    _file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=14, encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _handlers.append(_file_handler)
    try:
        os.chmod(LOG_FILE, 0o666)
    except OSError:
        pass
except OSError as e:
    _file_log_error = e

logging.basicConfig(level=LOGLEVEL, handlers=_handlers)
_log = logging.getLogger("anibot")
if _file_log_error:
    _log.warning("File logging disabled: %s", _file_log_error)

try:
    from pushbullet import Pushbullet
except ImportError:
    Pushbullet = None

from tvdb import TVDBClient

import anistore
import config_defaults

import notify

import animeloads as animeloads_module

from animeloads import animeloads, ALLinkExtractionException

arglen = len(sys.argv)

import myjdapi

pb = ""

botfile = "config/ani.json"
botfolder = "config/"

def is_docker():
  if not os.path.isfile("/proc/" + str(os.getpid()) + "/cgroup"): return False
  with open("/proc/" + str(os.getpid()) + "/cgroup") as f:
    for line in f:
      if re.match(r"\d+:[\w=]+:/docker(-[ce]e)?/\w+", line):
        return True
    return False

def log(message, pushbullet):
    # Pushbullet no longer gets a push per call (that pushed every attempt,
    # not just outcomes) — it now receives the same one-per-cycle summary as
    # the other notify targets, sent from _notify_cycle(). `pushbullet` is
    # kept as a parameter for call-site compatibility but is unused here.
    _log.info(message)

def _pushkey_set(pushkey):
    return isinstance(pushkey, str) and pushkey.strip() != ""

def init_pushbullet(pushkey):
    """Build the Pushbullet client, or "" when disabled/unusable.

    The Pushbullet constructor validates the key over the network, so an
    invalid/revoked key, a null/missing key in ani.json, or a boot-time
    network error must not crash the bot into a container restart loop.
    """
    if not _pushkey_set(pushkey):
        return ""
    try:
        return Pushbullet(pushkey)
    except Exception as e:
        _log.warning("Pushbullet disabled: %s", e)
        return ""

def compare(inputstring, validlist):
    for v in validlist:
        if(v.lower() in inputstring.lower()):
            return True
    return False

def printException(e):
    exc_type, exc_obj, exc_tb = sys.exc_info()
    fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
    _log.error("Error: %s %s %s", exc_type, fname, exc_tb.tb_lineno)

def should_scrape_despite_skip(skip_until_str, today, window_days):
    """Decide whether to scrape now even though a skip_until date is set.

    The bot writes skip_until = <TVDB-predicted airdate> for a "Continuing"
    series and skips scraping until then. But anime-loads.org sometimes
    publishes an episode before its TVDB airdate, which the strict skip would
    miss. So once `today` is within `window_days` of skip_until — i.e. it's the
    airdate-eve or later — scrape anyway to catch an early release, while still
    skipping when skip_until is well in the future (preserving rate-limit
    protection).

    Returns True (scrape) when today >= skip_until - window_days, when there is
    no skip_until, or when skip_until is unparseable (a bad date must not
    silently suppress scraping). Returns False (honor the skip) otherwise.
    """
    if not skip_until_str:
        return True
    try:
        skip_date = date.fromisoformat(skip_until_str)
    except (ValueError, TypeError):
        return True
    return today >= skip_date - timedelta(days=window_days)

def tvdb_skip_decision(series_status, tvdb_season, tvdb_ep_count, airdate, episodes,
                        missing_count, today, now, skip_recheck_at, early_scrape_days,
                        pastdue_recheck_hours):
    """Pure decision logic for the anibot Step 4 TVDB skip/complete checks.

    `episodes` is already in watchlist/TVDB numbering — the mover's
    `new_ep = site_ep + episode_offset` return-leg translation (c0b7ab5,
    `_match_batch_episodes` in animeloads.py) means the highest watchlist
    episode is the same "episode within tvdb_season" numbering TVDB's own
    season/episode APIs use. No further `episode_offset` translation belongs
    here — applying it again would double-translate.

    Returns a dict:
      - "action": "complete" | "skip" | "retry" | "early" | "none"
      - "terminal": True when the caller should count this as skipped and
        `continue` (complete/skip); False when it should fall through to the
        normal scrape (retry/early/none).
      - "updates": {field: value} to merge into the anime entry (and persist)
        before returning to the caller, or {} if nothing changed.
      - "log": (level, message) or None.
    """
    no_change = {"action": "none", "terminal": False, "updates": {}, "log": None}

    if series_status == "Ended":
        if tvdb_ep_count and episodes >= tvdb_ep_count and missing_count == 0:
            return {"action": "complete", "terminal": True, "updates": {"complete": True},
                    "log": ("info", "series ended, all episodes downloaded")}
        return no_change

    if series_status == "Continuing" and tvdb_season:
        if airdate:
            try:
                air_date_obj = date.fromisoformat(airdate)
            except (ValueError, TypeError):
                return no_change

            if air_date_obj > today:
                updates = {"skip_until": airdate, "skip_real_airdate": True}
                if should_scrape_despite_skip(airdate, today, early_scrape_days):
                    return {"action": "early", "terminal": False, "updates": updates,
                            "log": ("info", "next episode airs " + airdate
                                    + ", scraping within " + str(early_scrape_days) + "d for early release")}
                if missing_count == 0:
                    return {"action": "skip", "terminal": True, "updates": updates,
                            "log": ("info", "next episode airs " + airdate)}
                return {"action": "retry", "terminal": False, "updates": updates,
                        "log": ("info", "next episode airs " + airdate + " but "
                                + str(missing_count) + " missing episodes to retry")}

            # Airdate already passed but not yet published (site running late).
            # There's no future skip_until to defer to, so without a throttle
            # this falls through to a full scrape every cycle until it appears.
            recheck_dt = None
            if skip_recheck_at:
                try:
                    recheck_dt = datetime.fromisoformat(skip_recheck_at)
                except (ValueError, TypeError):
                    recheck_dt = None
            if recheck_dt and now < recheck_dt:
                msg = ("next episode airdate " + airdate + " has passed but is not yet published")
                if missing_count == 0:
                    return {"action": "skip", "terminal": True, "updates": {},
                            "log": ("info", msg + ", re-checking after " + skip_recheck_at)}
                return {"action": "retry", "terminal": False, "updates": {},
                        "log": ("info", msg + " (recheck throttled) but "
                                + str(missing_count) + " missing episodes to retry")}
            next_recheck = (now + timedelta(hours=pastdue_recheck_hours)).isoformat()
            return {"action": "none", "terminal": False, "updates": {"skip_recheck_at": next_recheck},
                    "log": None}

        # No known airdate — synthetic throttle date (regenerates daily), honored
        # strictly (skip_real_airdate=False); the early-scrape window applies
        # only to real predicted airdates.
        default_skip = (today + timedelta(days=1)).isoformat()
        updates = {"skip_until": default_skip, "skip_real_airdate": False}
        if missing_count == 0:
            return {"action": "skip", "terminal": True, "updates": updates,
                    "log": ("info", "no airdate known, re-check " + default_skip)}
        return {"action": "retry", "terminal": False, "updates": updates,
                "log": ("info", "no airdate known, re-check " + default_skip + " but "
                        + str(missing_count) + " missing episodes to retry")}

    return no_change

# Field-level merge ownership for ani.json anime entries (see
# anistore.merge_entry_fields, used by startbot()'s per-entry save_ani()).
#
# Every field listed here is written ONLY by the bot — verified by grepping
# every `animeentry[...] =` / `.update(` / `.pop(` in this module. A scalar
# field is safe to overwrite wholesale on each save because nothing else
# ever writes it. Fields the dashboard can also edit through its POST
# handlers — customPackage, tvdb_id, tvdb_season, episode_offset, name, url,
# releaseID, pref_* overrides, settings — must NEVER be added here: the
# bot's stale per-cycle snapshot would otherwise silently revert a
# concurrent dashboard edit on its next save.
#
# `missing` is the one field BOTH sides mutate (the dashboard adds/removes a
# single retry episode; the bot removes a downloaded one and adds a failed
# one on the same list), so it is handled as a DELTA against the fresh
# on-disk list, never a wholesale replacement — see BOT_OWNED_LIST_FIELDS
# and compute_entry_delta() below.
#
# `episodes` is shared too: the dashboard sets it when the user says "I
# already have episodes up to N". It stays in the scalar list, but the bot
# writes it only through compute_entry_delta(), i.e. only when the bot itself
# changed it this cycle (advanced it after a download, or rolled it back after
# an episode turned out unavailable) — and then its value wins, even going
# DOWN. The bot never writes back a value it merely read. startbot() re-reads
# the entry fresh at the top of each entry (refresh_entry) and re-reads
# `episodes` again right before deciding what to download
# (sync_user_episodes), so a dashboard edit made up to that point is honored
# this same cycle.
#
# `paused` is user-owned like releaseID and the pref_* overrides: the bot
# only reads it (fresh, at the top of each entry) and never clears it.
BOT_OWNED_SCALAR_FIELDS = (
    "episodes", "skip_until", "skip_real_airdate", "skip_recheck_at",
    "al_status", "al_max_episodes", "al_available_max", "al_available_max_set_at",
    "complete", "media_type", "year", "display_title", "download_folder_pattern",
    "tvdb_series_status",
)
BOT_OWNED_LIST_FIELDS = ("missing",)

_UNSET = object()

def compute_entry_delta(before, after, scalar_fields=BOT_OWNED_SCALAR_FIELDS,
                         list_fields=BOT_OWNED_LIST_FIELDS):
    """Pure diff between `before` (the entry as last persisted this cycle)
    and `after` (the live in-memory entry the bot has since mutated).

    Returns (fields, unset, list_deltas) shaped for
    ``anistore.merge_entry_fields`` — only what actually changed since the
    last save, never the whole entry. A scalar field that disappeared from
    `after` (e.g. a popped `al_available_max` cap) goes into `unset` rather
    than `fields`, so the merge drops it from the fresh on-disk entry too.
    A list field (`missing`) is reduced to its added/removed sets so the
    caller can apply the DELTA onto a fresh copy instead of replacing it.
    """
    fields = {}
    unset = []
    for f in scalar_fields:
        cur = after.get(f, _UNSET)
        prev = before.get(f, _UNSET)
        if cur == prev:
            continue
        if cur is _UNSET:
            unset.append(f)
        else:
            fields[f] = cur

    list_deltas = {}
    for f in list_fields:
        prev_set = set(before.get(f) or [])
        cur_set = set(after.get(f) or [])
        added = cur_set - prev_set
        removed = prev_set - cur_set
        if added or removed:
            list_deltas[f] = (added, removed)

    return fields, unset, list_deltas


def _peek_entry(path, url):
    """A fresh on-disk copy of `url`'s anime entry, read under the lock.

    Returns the entry dict, None when no entry has that URL (the dashboard
    removed it), or _UNSET when ani.json can't be read right now (corrupt),
    so callers can keep what they already have instead of guessing."""
    try:
        with anistore.locked(path):
            data = anistore.load(path)
    except anistore.CorruptStoreError:
        return _UNSET
    for entry in data.get('anime', []) or []:
        if entry.get('url') == url:
            return entry
    return None


def refresh_entry(path, animeentry):
    """Replace the cycle-start snapshot of one entry with its fresh on-disk
    copy, in place, right before the bot starts on it.

    The cycle-start snapshot can be minutes old by the time the loop reaches
    a later entry; the dashboard may have paused it, changed its release,
    prefs, folder or `episodes` since. The bot hasn't touched this entry yet
    this cycle, so the fresh copy is authoritative. Returns False when the
    entry is gone (removed mid-cycle: skip it, never resurrect it). An
    unreadable file keeps the snapshot and returns True."""
    fresh = _peek_entry(path, animeentry.get('url'))
    if fresh is None:
        return False
    if fresh is not _UNSET:
        animeentry.clear()
        animeentry.update(fresh)
    return True


def tvdb_checks_apply(animeentry, force_check, tvdb_available):
    """Whether Step 4's TVDB lookups run for this entry. Keyed off `tvdb_id`
    alone: a `tvdb_season` / `episode_offset` set by hand in the dashboard,
    with no TVDB link, only steers the mover and batch matching and never
    triggers a lookup."""
    return (not force_check and bool(animeentry.get('tvdb_id')) and tvdb_available
            and animeentry.get('media_type') != 'movie')


def paused_skip_reason(animeentry):
    """Why this entry is skipped before any other check, or None. Paused is
    set and cleared only in the dashboard; the bot never unpauses."""
    if not animeentry.get('paused'):
        return None
    return (str(animeentry.get('name') or animeentry.get('url') or "?")
            + " — paused in the dashboard, skipping")


def skip_if_paused(animeentry, run_counts, entry_outcomes):
    """The paused early-skip at the top of startbot()'s per-entry loop: log
    it, count it as skipped and record a "paused" outcome for run_state.
    Returns True when the caller must skip the entry."""
    reason = paused_skip_reason(animeentry)
    if not reason:
        return False
    _log.info("[PAUSED] " + reason)
    run_counts["skipped"] += 1
    _record_entry_outcome(entry_outcomes, animeentry.get('url'), "paused", "paused from dashboard")
    return True


def sync_user_episodes(path, animeentry, saved_state):
    """Adopt a dashboard edit of `episodes` made while the bot was busy on
    this entry (e.g. during the scrape), right before the bot decides which
    episodes to download.

    Only when the bot hasn't changed `episodes` itself since its last save:
    a pending bot change (a rollback to the available cap) stands and is
    written by the next save_ani(). The adopted value also becomes the
    saved baseline, so it's never echoed back as a bot delta. Returns the
    episode count to plan with."""
    current = animeentry.get('episodes', 0)
    if current != saved_state.get('episodes', _UNSET):
        return current
    fresh = _peek_entry(path, animeentry.get('url'))
    if not isinstance(fresh, dict) or 'episodes' not in fresh:
        return current
    value = fresh['episodes']
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return current
    if value != current:
        animeentry['episodes'] = value
        saved_state['episodes'] = value
    return value


def wanted_episodes(missing, episodes, cur_episodes):
    """What to download: retries first, then every episode after the
    `episodes` the entry already has, up to what's online now."""
    wanted_missing = list(missing)
    episodes = int(episodes)
    wanted_new = list(range(episodes + 1, cur_episodes + 1)) if episodes < cur_episodes else []
    return wanted_missing, wanted_new

# Soft run-now: the dashboard drops this trigger file next to ani.json (see
# web/app.py's trigger_run_now()) instead of restarting the bot container.
# The bot wakes its inter-cycle sleep early when it appears (sleep_until_next_cycle)
# and consumes (deletes) it at the start of the cycle it triggers
# (consume_run_now_trigger) — a request arriving mid-cycle just sits there until
# that point, so it is always honored *after* the running cycle, never by
# interrupting it.
RUN_NOW_FILE = "run_now"
RUN_NOW_SLEEP_SLICE = int(os.environ.get("RUN_NOW_SLEEP_SLICE", "5"))

def _run_now_path():
    """Path to the run_now trigger file, alongside the watchlist (ani.json).
    Derived from botfile like _run_state_path()."""
    return os.path.join(os.path.dirname(botfile) or ".", RUN_NOW_FILE)

def sleep_until_next_cycle(seconds, trigger_path, slice_seconds=RUN_NOW_SLEEP_SLICE,
                            sleep_fn=time.sleep, time_fn=time.monotonic):
    """Sleep for `seconds`, waking early if `trigger_path` appears.

    Sleeps in slices of at most `slice_seconds` instead of one long
    time.sleep() call, so a run-now request created mid-sleep (or already
    sitting there from a cycle that just finished) is noticed within
    `slice_seconds` instead of waiting out the rest of the inter-cycle
    delay. Never called from inside a cycle — this only runs between
    cycles, so a trigger can never interrupt in-flight work (e.g. a
    JDownloader hand-off).

    Returns True if woken early by the trigger file, False if `seconds`
    elapsed naturally without one appearing. `sleep_fn`/`time_fn` are
    injectable so tests can exercise this without a real sleep."""
    if not isinstance(seconds, int) or seconds <= 0:
        return os.path.exists(trigger_path)
    deadline = time_fn() + seconds
    while True:
        if os.path.exists(trigger_path):
            return True
        remaining = deadline - time_fn()
        if remaining <= 0:
            return False
        sleep_fn(min(slice_seconds, remaining))

def consume_run_now_trigger(path):
    """Atomically consume (delete) the run_now trigger file if present.

    Returns its content (the ISO timestamp the dashboard wrote) if a
    trigger was pending, or None if there was none. A race where the file
    vanishes between the read and the remove (only the bot ever deletes it,
    so this should not happen in practice) is swallowed — the request was
    already effectively consumed either way."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except (FileNotFoundError, OSError):
        return None
    try:
        os.remove(path)
    except OSError:
        pass
    return content or "manual"

def resolve_force_check(path, url):
    """Peek a FRESH on-disk read of `url`'s entry for a pending
    `force_check` request (dashboard "Check now" on one card), and
    unconditionally clear it — a one-shot override honored at most once,
    even if the scrape that follows fails.

    Reads fresh (not the cycle-start snapshot passed around the rest of
    startbot()'s loop) so a request made after this cycle began is still
    honored this same cycle. `force_check` is deliberately NOT in
    BOT_OWNED_SCALAR_FIELDS — it is dashboard-set/bot-cleared, so clearing
    it must be this explicit merge_entry_fields(unset=...) call, never a
    side effect of the bot's own per-cycle field-level save_ani().

    Returns True if a request was pending (now cleared), False otherwise —
    including when the entry was removed by the dashboard between the
    cycle-start snapshot and this peek: it is simply not found in the fresh
    read, so nothing is cleared and (per merge_entry's contract) nothing is
    resurrected."""
    try:
        with anistore.locked(path):
            data = anistore.load(path)
    except anistore.CorruptStoreError:
        return False
    pending = False
    for entry in data.get('anime', []) or []:
        if entry.get('url') == url:
            pending = bool(entry.get('force_check'))
            break
    if pending:
        anistore.merge_entry_fields(path, "anime", url, unset=["force_check"])
    return pending

def pre_scrape_skip_decision(animeentry, name, missing_count, episodes, force_check, today):
    """Steps 1-3 of the smart-skip logic (complete flag, cached al_status
    completion, skip_until airdate throttle) as one pure decision, so the
    per-entry `force_check` bypass has a single gate to test instead of
    three separate inline conditions each needing their own guard.

    `force_check=True` (a dashboard "Check now" request already consumed by
    resolve_force_check()) bypasses all three steps for this one scrape —
    no skip, no log line here (the caller logs the check-now request
    itself).

    Returns {"skip": bool, "mark_complete": bool, "log": (level, tag, msg) or None}.
    `mark_complete` tells the caller to set animeentry['complete'] = True
    and persist it (Step 2) — a side effect this pure function cannot
    perform itself."""
    if force_check:
        return {"skip": False, "mark_complete": False, "log": None}

    # Step 1: already marked complete and no missing episodes.
    if animeentry.get('complete') and missing_count == 0:
        return {"skip": True, "mark_complete": False,
                "log": ("info", "SKIP", name + " is complete")}

    # Step 2: cached anime-loads.org status (no network call).
    al_status = animeentry.get('al_status', '')
    al_max = animeentry.get('al_max_episodes')
    if al_status in ("Abgeschlossen", "Completed", "Complete") \
            and al_max and episodes is not None and episodes >= al_max \
            and missing_count == 0:
        return {"skip": True, "mark_complete": True,
                "log": ("info", "COMPLETE", name + " — al_status: " + al_status
                        + ", all " + str(al_max) + " episodes downloaded")}

    # Step 3: waiting for next episode airdate (skip_until).
    skip_until = animeentry.get('skip_until', '')
    if skip_until:
        if animeentry.get('skip_real_airdate'):
            honor_skip = not should_scrape_despite_skip(skip_until, today, EARLY_SCRAPE_DAYS)
        else:
            try:
                honor_skip = today < date.fromisoformat(skip_until)
            except (ValueError, TypeError):
                honor_skip = False
        if honor_skip:
            if missing_count == 0:
                return {"skip": True, "mark_complete": False,
                        "log": ("info", "SKIP", name + " — next episode airs " + skip_until)}
            else:
                return {"skip": False, "mark_complete": False,
                        "log": ("info", "RETRY", name + " — next episode airs " + skip_until
                                + " but " + str(missing_count) + " missing episodes to retry")}

    return {"skip": False, "mark_complete": False, "log": None}

def _boot_backoff(attempt, cap=300):
    """Capped exponential backoff (seconds) for in-process boot retries.

    Keeps the container alive and self-healing on a transient boot failure
    instead of exiting and relying on Docker's restart backoff. Under
    `restart: unless-stopped` a crash-loop accumulates an exponential delay
    that can leave the container "not running" for long stretches — which is
    exactly the "the bot didn't start automatically" symptom. Retrying inside
    the process keeps the container up and the dashboard's status/controls live.

    Sequence: 5, 10, 20, 40, 80, 160, 300, 300, ... seconds (capped)."""
    return min(cap, 5 * (2 ** min(attempt, 6)))

# Persisted run-state record. The dashboard derives last_run / next_run from
# this file instead of scraping the rolling container-log tail (the German run
# markers "Prüfe …"/"Schlafe N Sekunden" roll off the 500-line window under
# verbose logging, which made the UI show "No runs yet" / "—" while the bot was
# running fine). Written next to ani.json so the bot and the dashboard share one
# config dir. The `runs` history is bounded and holds one summary per cycle, so
# a future run-history UI can render one summary per run from it.
RUN_STATE_FILE = "run_state.json"
RUN_STATE_HISTORY_MAX = 50
EVENTS_CAP = 40

def _utcnow_iso():
    """UTC timestamp as RFC3339-ish ISO8601 with a trailing Z. Matches the
    dashboard's UTC clock (datetime.now(timezone.utc)) so its next-run math stays correct."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _run_state_path():
    """Path to run_state.json, alongside the watchlist (ani.json). Derived from
    botfile so a --configfile override keeps the record next to the config."""
    return os.path.join(os.path.dirname(botfile) or ".", RUN_STATE_FILE)

def _record_event(events, kind, anime, episodes=None, detail=None):
    """Append one bounded run-state event to `events` (mutated in place).

    Kept deliberately simple (no try/except) — event collection runs inline in
    the hot scrape loop and must never introduce an exception path into a
    cycle. `detail` is truncated to 200 chars; callers must never pass a URL,
    credential, or JD host/port (run_state.json is dashboard-visible)."""
    event = {"kind": kind, "anime": anime}
    if episodes:
        event["episodes"] = list(episodes)
    if detail:
        event["detail"] = str(detail)[:200]
    events.append(event)

# Per-entry check outcome, persisted in run_state.json's additive top-level
# "entries" key (NOT ani.json — the watchlist store the dashboard also
# writes; keeping this in run_state.json avoids write contention with it).
# Answers "why didn't X download?" (UX audit finding 3, card 7bc5a4f0) without
# scraping logs. Shape, keyed by entry URL:
#
#   "entries": {
#     "<url>": {
#       "checked_ts": "2026-09-17T02:00:00Z",   # this cycle's check, RFC3339 UTC
#       "result": "skipped",                     # see ENTRY_RESULTS below
#       "reason": "waiting for airdate 2026-09-20",  # human-readable, <=200 chars
#       "episode": 12,                            # optional: the episode a
#                                                  # downloaded/unavailable/error
#                                                  # result concerns
#       "last_error": {                           # optional: survives a LATER
#         "reason": "JDownloader unreachable",    # non-error result, so "last
#         "checked_ts": "2026-09-16T14:00:00Z"    # error" stays visible after
#       }                                          # a subsequent clean skip
#     }, ...
#   }
#
# One entry per URL (latest outcome only, not a list) — bounded to the
# current watchlist size since entries no longer on the watchlist are pruned
# on every write (see _merge_entry_outcomes). Additive: a reader (the web
# dashboard's later card) must tolerate both the key's absence (older
# run_state.json) and any per-entry sub-key's absence (episode/last_error are
# optional).
ENTRY_RESULTS = ("downloaded", "skipped", "unavailable", "error", "mismatch", "paused")

def _record_entry_outcome(entry_outcomes, url, result, reason, episode=None):
    """Record one entry's outcome for this cycle into `entry_outcomes`
    (mutated in place, keyed by watchlist entry URL — see ENTRY_RESULTS
    above for the shape written to run_state.json).

    Called once per entry per cycle, at whichever branch is the entry's
    final outcome for the cycle (an early skip/error `continue`, or the
    bottom of the per-entry loop body for anything that scrapes through).
    `reason` is truncated to 200 chars; never pass a URL, credential, or JD
    host/port (run_state.json is dashboard-visible)."""
    outcome = {"checked_ts": _utcnow_iso(), "result": result, "reason": str(reason)[:200]}
    if episode is not None:
        outcome["episode"] = episode
    entry_outcomes[url] = outcome

def _record_complete_outcome(entry_outcomes, url, movie=False):
    """Record a completion newly detected at the bottom of the per-entry
    loop (the movie / anime-loads-status auto-complete checks).

    Unlike a plain `_record_entry_outcome` call, this one must NOT blindly
    overwrite: when the SAME entry already recorded a "downloaded" outcome
    earlier in THIS cycle (e.g. its last episode just downloaded, which is
    exactly what triggered the completion check to pass), overwriting it
    with "skipped"/"complete" would erase the one fact — "it downloaded" —
    the dashboard most needs to show for this cycle. So a "downloaded"
    outcome is kept as "downloaded", with the completion appended onto its
    existing reason instead; only when nothing else was recorded this cycle
    (nothing to lose) does this fall back to "skipped"/"complete"."""
    existing = entry_outcomes.get(url)
    if existing and existing.get("result") == "downloaded":
        suffix = "movie complete" if movie else "series complete"
        _record_entry_outcome(entry_outcomes, url, "downloaded",
                               existing["reason"] + "; " + suffix,
                               episode=existing.get("episode"))
    else:
        _record_entry_outcome(entry_outcomes, url, "skipped", "complete")

def _merge_entry_outcomes(prev_entries, entry_outcomes, watchlist_urls):
    """Fold this cycle's `entry_outcomes` onto the previously-persisted
    per-entry map, producing the map to persist this write.

    - Pruned to `watchlist_urls` (the current watchlist) — an entry removed
      from the watchlist is dropped instead of accumulating forever.
    - A URL not visited this cycle (not in `entry_outcomes`) keeps its
      previous record unchanged, so a partial cycle never blanks entries it
      didn't reach.
    - `last_error` survives a later non-error outcome: a fresh "error"
      result sets it from this cycle's own outcome; otherwise it carries
      forward from the previous record untouched.
    """
    merged = {}
    for url in watchlist_urls:
        prev_record = prev_entries.get(url) if isinstance(prev_entries, dict) else None
        new_outcome = entry_outcomes.get(url) if entry_outcomes else None
        if new_outcome is None:
            if isinstance(prev_record, dict):
                merged[url] = prev_record
            continue
        record = dict(new_outcome)
        if new_outcome["result"] == "error":
            record["last_error"] = {"reason": new_outcome["reason"],
                                     "checked_ts": new_outcome["checked_ts"]}
        elif isinstance(prev_record, dict) and isinstance(prev_record.get("last_error"), dict):
            record["last_error"] = prev_record["last_error"]
        merged[url] = record
    return merged

def _format_cycle_summary(events, login_error=None):
    """Build one English notification message from a cycle's `events` list,
    or return None when nothing noteworthy happened (a quiet cycle).

    Noteworthy: downloads, errors, mismatches, or a login failure at
    startup. Plain "checked"/"skipped"/"unavailable" counts are not."""
    downloads = [e for e in events if e["kind"] == "download"]
    errors = [e for e in events if e["kind"] == "error"]
    mismatches = [e for e in events if e["kind"] == "mismatch"]

    if not (downloads or errors or mismatches or login_error):
        return None

    parts = []
    if downloads:
        total_eps = sum(len(e.get("episodes") or []) for e in downloads) or len(downloads)
        names = ", ".join(
            "{} ({})".format(e["anime"], ", ".join(str(x) for x in e["episodes"]))
            if e.get("episodes") else e["anime"]
            for e in downloads
        )
        noun = "episode" if total_eps == 1 else "episodes"
        parts.append("{} {} downloaded — {}".format(total_eps, noun, names))
    if mismatches:
        parts.append("{} mismatch{}".format(len(mismatches), "" if len(mismatches) == 1 else "es"))
    if errors:
        first = "; ".join(
            "{}: {}".format(e["anime"], e.get("detail") or "error") for e in errors[:3]
        )
        parts.append("{} error{}: {}".format(len(errors), "" if len(errors) == 1 else "s", first))
    if login_error:
        parts.append("login failed: {}".format(login_error))

    return "Aniloads: " + " · ".join(parts)


def _notify_cycle(targets, pushbullet, events, login_error=None):
    """Send at most one cycle-summary notification to every configured
    ntfy/Discord/Gotify target plus Pushbullet — quiet cycles send nothing.
    Best-effort: never raises into the caller."""
    message = _format_cycle_summary(events, login_error=login_error)
    if not message:
        return
    if targets:
        notify.send_all(targets, "Aniloads", message)
    if pushbullet:
        try:
            pushbullet.push_note("Aniloads", message)
        except Exception:
            pass


def write_run_state(started_ts, finished_ts, timedelay, counts, events=None, trigger=None,
                     entry_outcomes=None, watchlist_urls=None):
    """Persist one per-cycle run-state record and append it to a bounded history.

    Best-effort: a write failure must never break the bot loop, so all errors
    are swallowed. Written atomically (tmp + os.replace) so the dashboard never
    reads a half-written file. `counts` is a free-form dict (entries/checked/
    downloaded/errors/skipped/unavailable today). `events` is an ordered list
    of what actually happened this cycle (download/error/unavailable/complete),
    capped at EVENTS_CAP entries — the first EVENTS_CAP are kept and
    `events_truncated` is set when the cap clipped the list; `counts` stays
    authoritative regardless of clipping. `trigger` is additive: omitted (the
    default) for a routine timer-driven cycle, or "manual" when this cycle was
    woken early by the dashboard's run-now trigger file — see
    consume_run_now_trigger().

    `entry_outcomes`/`watchlist_urls` are additive (see ENTRY_RESULTS /
    _record_entry_outcome / _merge_entry_outcomes above for the persisted
    "entries" shape). `watchlist_urls` is the deliberate signal for whether
    to touch the "entries" key at all: omitted (the default, `None`) means
    "the caller has no reliable watchlist this call" (e.g. a corrupt-ani.json
    or pre-login cycle) — the previous "entries" map is carried forward
    untouched. Pass an explicit list (empty or not) once the watchlist is
    known, and the map is pruned to exactly those URLs, merging in
    `entry_outcomes` for this cycle (see _merge_entry_outcomes)."""
    try:
        next_run_ts = ""
        if isinstance(timedelay, int) and timedelay > 0:
            try:
                fin = datetime.strptime(finished_ts, "%Y-%m-%dT%H:%M:%SZ")
                next_run_ts = (fin + timedelta(seconds=timedelay)).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, TypeError):
                next_run_ts = ""
        events = events or []
        record = {
            "started_ts": started_ts,
            "finished_ts": finished_ts,
            "timedelay": timedelay,
            "next_run_ts": next_run_ts,
            "counts": counts,
            "events": events[:EVENTS_CAP],
        }
        if trigger:
            record["trigger"] = trigger
        if len(events) > EVENTS_CAP:
            record["events_truncated"] = True
        path = _run_state_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                prev = json.load(f)
            if not isinstance(prev, dict):
                prev = {}
            runs = prev.get("runs")
            if not isinstance(runs, list):
                runs = []
        # ValueError also covers UnicodeDecodeError (a corrupt/non-UTF-8 file):
        # both it and json.JSONDecodeError subclass ValueError, so this degrades
        # a bad previous-state file to a fresh history instead of losing the
        # whole write, without swallowing an unrelated bug as if it were a
        # corrupt file.
        except (FileNotFoundError, OSError, ValueError):
            prev = {}
            runs = []
        runs.append(record)
        if len(runs) > RUN_STATE_HISTORY_MAX:
            runs = runs[-RUN_STATE_HISTORY_MAX:]
        state = {"schema": 1, "last_run": record, "runs": runs}
        # `login` is an independent top-level key (write_login_state) written
        # once per bot startup rather than once per cycle — preserve it across
        # every per-cycle rewrite instead of silently dropping it.
        if "login" in prev:
            state["login"] = prev["login"]
        # `entries` (per-entry check outcomes) is likewise independent of the
        # per-cycle record above — see this function's docstring for when it
        # is touched vs. carried forward untouched.
        if watchlist_urls is not None:
            prev_entries = prev.get("entries")
            if not isinstance(prev_entries, dict):
                prev_entries = {}
            state["entries"] = _merge_entry_outcomes(prev_entries, entry_outcomes, watchlist_urls)
        elif "entries" in prev:
            state["entries"] = prev["entries"]
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        # Run-state bookkeeping is never allowed to take down the bot loop.
        pass

def write_login_state(user_configured, ok, error=None, vip=None):
    """Persist the anime-loads.org login outcome as a top-level `login` key in
    run_state.json, alongside (not inside) the per-cycle `last_run`/`runs`
    records write_run_state maintains — login normally happens once at bot
    startup (and again at a cycle boundary if AL_USER/AL_PASS change, see
    relogin_if_credentials_changed), not once per cycle, so it survives until
    the next login attempt overwrites it.

    Additive: existing readers that only look at `last_run`/`runs` are
    unaffected, and a reader must tolerate this key's absence (e.g. an older
    run_state.json, or one written before the first login attempt completes).

    Best-effort like write_run_state: a write failure must never break the bot
    loop. Never pass a credential or username — `user_configured` is a plain
    bool and `error` must stay a generic, non-identifying message."""
    try:
        record = {
            "user_configured": bool(user_configured),
            "ok": bool(ok),
            "checked_ts": _utcnow_iso(),
        }
        if vip is not None:
            record["vip"] = bool(vip)
        if error:
            record["error"] = str(error)[:200]
        path = _run_state_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                state = {}
        except (FileNotFoundError, OSError, ValueError):
            state = {}
        state.setdefault("schema", 1)
        state["login"] = record
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        # Run-state bookkeeping is never allowed to take down the bot loop.
        pass

def load_ani_cycle_start(path):
    """Load ani.json at the start of a bot cycle, under the lock shared with
    the dashboard. Returns (data, None) on success, or (None, error) if the
    file is corrupt — a torn read (the dashboard mid-write) must not crash
    the loop and restart-loop the container; the caller logs the error and
    retries next cycle instead."""
    try:
        with anistore.locked(path):
            return anistore.load(path), None
    except anistore.CorruptStoreError as e:
        return None, e


def loadconfig():
    try:
        os.makedirs(os.path.dirname(botfolder), exist_ok=True)
        # Seed a fresh, fully-defaulted ani.json the first time anyone (bot or
        # dashboard) looks for it — never overwrites a real, existing file
        # (see anistore.seed_if_missing). Without this, a missing file used to
        # mean "no/bad config" forever until someone hand-wrote one.
        anistore.seed_if_missing(botfile, config_defaults.default_ani_data)
        infile = open(botfile, "r", encoding="utf-8")
        data = json.load(infile)
        infile.close()
    except Exception as e:
        printException(e)
        print("ani.json nicht gefunden, ")
        return False, False, False, False, False, False, False, False, False, False, False, False, False
    # Sentinel defaults: if the file has no "settings" block at all, fall through
    # to a clean False-tuple (→ caller treats it as "no/bad config" and retries)
    # instead of raising UnboundLocalError on the return below.
    jdhost = hoster = browser = browserlocation = pushkey = timedelay = False
    myjd_user = myjd_pass = myjd_device = jd_deprecated = jd_deprecatedport = False
    al_user = al_pass = False
    for key in data:
        if(key == "settings"):
            value = data[key]
            if not isinstance(value, dict):
                _log.error("ani.json 'settings' ist kein Objekt / is not an object")
                return False, False, False, False, False, False, False, False, False, False, False, False, False

            # Missing OPTIONAL keys (anything a hand-edited or partially
            # upgraded ani.json can simply omit) fall back to
            # config_defaults.DEFAULT_SETTINGS instead of the old
            # "Fehlerhafte ani.json Konfiguration" hard failure — only a
            # missing download backend (below) is actually fatal.
            filled, missing = config_defaults.fill_settings_defaults(value)
            if missing:
                _log.info(
                    "ani.json settings: fehlende optionale Schluessel, nutze Standardwerte / "
                    "missing optional key(s), using defaults: %s",
                    ", ".join(sorted(missing)),
                )

            jdhost = filled['jdhost']
            hoster = filled['hoster']
            browser = filled['browserengine']
            browserlocation = filled['browserlocation']
            pushkey = filled['pushbullet_apikey']
            timedelay = filled['timedelay']
            myjd_user = filled['myjd_user']
            myjd_pass = filled['myjd_pw']
            myjd_device = filled['myjd_device']
            jd_deprecated = filled['jd_deprecated']
            jd_deprecatedport = filled['jd_deprecatedport']

            # The one thing loadconfig() truly cannot default: a download
            # backend. Either a local JDownloader host, or a MyJDownloader
            # user (its password can still be entered interactively at
            # startup, see startbot()'s own jdhost=="" and myjd_pass==""
            # handling) must be configured.
            if not jdhost and not myjd_user:
                _log.error(
                    "ani.json settings: kein Download-Ziel konfiguriert, setze 'jdhost' "
                    "(lokaler JDownloader) oder 'myjd_user' fuer MyJDownloader / "
                    "no download backend configured, set 'jdhost' (local JDownloader) "
                    "or 'myjd_user' (MyJDownloader)"
                )
                return False, False, False, False, False, False, False, False, False, False, False, False, False

            # anime-loads.org login: prefer the environment (AL_USER/AL_PASS from
            # .env), fall back to ani.json settings for backward compatibility.
            al_user = os.environ.get('AL_USER') or value.get('al_user')
            al_pass = os.environ.get('AL_PASS') or value.get('al_pass')
    return jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device,jd_deprecated,jd_deprecatedport, al_user, al_pass

def editconfig():
    try:
        os.makedirs(os.path.dirname(botfolder), exist_ok=True)
        infile = open(botfile, "r", encoding="utf-8")
        data = json.load(infile)
        infile.close()
        for key in data:
            if(key == "settings"):
                value = data[key]
                jdhost = value['jdhost']       
                hoster = value['hoster']
                browser = value['browserengine']
                browserlocation = value['browserlocation']
                pushkey = value['pushbullet_apikey']
                timedelay = value['timedelay']
                myjd_user = value['myjd_user']
                myjd_pass = value['myjd_pw']
                myjd_device = value['myjd_device']
                jd_deprecated = value['jd_deprecated']
                jd_deprecatedport = value['jd_deprecatedport']
    except:
        jdhost = ""
        hoster = ""
        browser = ""
        browserlocation = ""
        pushkey = ""
        timedelay = ""
        myjd_user = ""
        myjd_pw = ""
        myjd_device = ""
        jd_deprecated = ""
        jd_deprecatedport = ""

    if(hoster == 1):
        hosterstr = "rapidgator"
    elif(hoster == 0):
        hosterstr = "ddownload"
    changehoster  = True
    if(hoster != ""):
        if(compare(input("Dein gewählter hoster: " + hosterstr + ", möchtest du ihn wechseln? [J/N]: "), {"j", "ja", "yes", "y"}) == False):
            changehoster = False
    if(changehoster):
        while(True):
            host = input("Welchen hoster bevorzugst du? rapidgator oder ddownload: ")
            if("ddownload" in host):
                hoster = animeloads.DDOWNLOAD
                break
            elif("rapidgator" in host):
                hoster = animeloads.RAPIDGATOR
                break
            else:
                print("Bitte gib entweder rapidgator oder ddowwnload ein")

    change_jdhost = True


    jd_device = ""
    jd_user = ""
    jd_pass = ""

    jd_choice = input("Läuft Jdownloader auf deinem lokalen Rechner[1] oder möchtest du MyJDownloader nutzen[2]?  (1 oder 2): ")
    if(jd_choice == "1"):
        if(jdhost != ""):
            if(compare(input("Deine Adresse des Computers, auf dem JDownloader läuft lautet: " + jdhost + ", möchtest du ihn wechseln? [J/N]: "), {"j", "ja", "yes", "y"}) == False):
                change_jhdhost = False
      
        if(change_jdhost):
            if(input("Läuft dein JD2 auf deinem Lokalen Computer? Dann Eingabe leer lassen und bestätigen, falls nicht, gib die Adresse des Zeilrechners an: ") != ""):
                jdhost = input
            else:
                jdhost = "127.0.0.1"
        jd_device = ""
        jd_pass = ""
        jd_user = ""
    
    else:
        jd_choice = 2
        jd=myjdapi.Myjdapi()
        jd.set_app_key("animeloads")
        
        logincorrect = False
        while(logincorrect == False):
            jd_user = input("MyJdownloader Nutzername: ")
            jd_pass = getpass("MyJdownloader Passwort: ")
            
            try:
              jd.connect(jd_user, jd_pass)
              logincorrect = True
            except:
                print("Fehlerhafte Logindaten")
        
        print("Logindaten sind korrekt")
        jd.update_devices()
        devices = jd.list_devices() 
        
        print("Deine verbundenen Geräte: ")
        for dev in devices:
            print(dev['name'])
        
        foundDevice = False
        while(foundDevice == False):
            jd_device = input("Gib den Namen des Gerätes, welches du benutzen willst ein: ")
            for dev in devices:
                devname = dev['name']
                if(jd_device == devname):
                    foundDevice = True
                    break
            if(foundDevice == False):
                print("Gerät nicht gefunden...")
        
        print("Nutze Gerät: " + jd_device)
        
        if(compare(input("Möchtest du das MyJDownloader passwort speichern (unverschlüsselt!!!)? Andernfalls musst du es jeden Programmstart eingeben [J/N]: "), {"j", "ja", "yes", "y"}) == False):
            jd_pass = ""

        jdhost = ""

    if(browser == 0):
        browserstring = "Firefox"
    elif(browser == 1):
        browserstring = "Chrome"

    if("--docker" in sys.argv):
        browser = animeloads.FIREFOX
        print("Überspringe Browserwahl, da in Docker")
    else:
        changebrowser = True
        if(browser != ""):
            if(compare(input("Dein gewählter Browser: " + browserstring + ", möchtest du ihn wechseln? [J/N]: "), {"j", "ja", "yes", "y"}) == False):
                changebrowser = False
        if(changebrowser):
            while(True):
                browser = input("Welchen Browser möchtest du nutzen? Darunter fallen auch forks der jeweiligen Browser (Chrome/Firefox)? Achte darauf, dass Chromedriver (Chrome) oder Geckodriver (Firefox) im gleichen Ordern wie das Script liegt: ")
                if(browser == "Chrome"):
                    browser = animeloads.CHROME
                    break
                elif(browser == "Firefox"):
                    browser = animeloads.FIREFOX
                    break
                else:
                    print("Fehlerhafter Input, entweder Chrome oder Firefox")
                    
            if(compare(input("Ist dein Browser ein fork von chrome/firefox oder an einem anderen als dem standardpfad installiert? [J/N]: "), {"j", "ja", "yes", "y"})):
                browserloc = input("Dann gib jetzt den Pfad der Browserdatei an (inklusive Endung): ")


    change_pushbullet = True

    if(pushkey != ""):
        if(compare(input("Dein Pushbullet API-Key ist: " +  pushkey + ", möchtest du ihn wechseln? [J/N]: "), {"j", "ja", "yes", "y"}) == False):
            change_pushbullet == False
    
    if(change_pushbullet):
        print("Hier kannst du deinen Pushbullet Account verbinden, damit du benachrichtigt wirst, wenn neue Folgen verfügbar sind und runtergeladen werden")
        if(compare(input("Möchtest du Pushbullet verwenden? [J/N]: "), {"j", "ja", "yes", "y"})):
            pushkey = input("Dann gib hier deinen Access Token ein (https://www.pushbullet.com/#settings): ")
        else:
            pushkey = ""

    change_timedelay = True
    if(timedelay != ""):
        if(compare(input("Deine Pause zwischen den Episodenupdates ist: " +  str(timedelay) + ", möchtest du sie ändern? [J/N]: "), {"j", "ja", "yes", "y"}) == False):
            change_timedelay = False

    if(change_timedelay):
        while(True):
            print("Hier kannst du deine Zeit, die zwischen der Suche nach neuen Episoden gewartet wird, einstellen.")
            timedelay_str = input("Wielange möchtest du warten? (In Sekunden. Empfohlen: 600 Sekunden (10 minuten)): ")
            try:
                timedelay = int(timedelay_str)
                break
            except:
                print("Bitte gib eine korrekte Zahl ein")

    settingsdata = {
        "hoster": hoster,
        "browserengine": browser,
        "pushbullet_apikey": pushkey,
        "browserlocation": browserlocation,
        "jdhost": jdhost,
        "timedelay": timedelay,
        "myjd_user": jd_user,
        "myjd_pw": jd_pass,
        "myjd_device": jd_device,
        "jd_deprecated": jd_deprecated,
        "jd_deprecatedport" : jd_deprecatedport
    }

    ani_exists = True

    try:
        os.makedirs(os.path.dirname(botfolder), exist_ok=True)
        f = open(botfile, "r", encoding="utf-8")
        data = json.load(f)
        infile.close()
    except:
        ani_exists = False

    if(ani_exists):
        data['settings'] = settingsdata
        os.makedirs(os.path.dirname(botfolder), exist_ok=True)
        jfile = open(botfile, "w", encoding="utf-8")
        jfile.write(json.dumps(data, indent=4, sort_keys=True))
        jfile.flush()
        jfile.close
    else:
        settingsdata = {"settings": settingsdata}
        os.makedirs(os.path.dirname(botfolder), exist_ok=True)
        jfile = open(botfile, "w", encoding="utf-8")
        jfile.write(json.dumps(settingsdata, indent=4, sort_keys=True))
        jfile.flush()
        jfile.close

def addAnime():
    jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device,jd_deprecated, jd_deprecatedport, al_user, al_pass = loadconfig()
 
    while(jdhost == False):
        print("Noch keine oder Fehlerhafte konfiguration, leite weiter zu Einstellungen")
        editconfig()
        jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, al_user, al_pass = loadconfig()

    al = animeloads(browser=browser, browserloc=browserlocation)
    exit = False
    search = False

    while(exit == False):
        search = False
        print("Gib nun entweder eine URL zu einem Anime-Eintrag oder einen Namen, nach dem du suchen willst ein")
        aniquery = input("URL/Anime (Du kannst jederzeit \"suche\" eingeben, um zurück zur Suche zu kommen oder \"exit\", um das Programm zu beenden): ")
        if(aniquery == "exit"):
            break
        if("https://www.anime-loads.org/media/" in aniquery):
            print("Hole Anime von URL: " + aniquery)
            anime = al.getAnime(aniquery)

            releases = anime.getReleases()
        
            print("\n\nReleases:\n")
        
            for rel in releases:
                print(rel.tostring())
    
            print("\n")
            relchoice = ""
            while(True):
                relchoice = input("Wähle eine Release ID: ")
                if(relchoice == "exit"):
                    exit = True
                    break
                elif(relchoice == "suche"):
                    search = True
                    break
                try:
                    relchoice = int(relchoice)
                    if(relchoice <= len(releases)):
                        break
                    else:
                        raise Exception()
                except:
                    print("Fehlerhafte Eingabe, versuche erneut")
    
            if(search or exit):
                continue

            release = releases[relchoice-1]
            print("Du hast folgendes Release gewählt: " + str(release.tostring()))
    
            print("\n")

            print("Das Release hat " + str(release.getEpisodeCount()) + " Episode(n)")
            curEpisodes = -1
            while(curEpisodes == -1):
                epi_in = input("Wieviel Episoden hast du bereits runtergeladen? Die restlichen verfügbaren werden dann automatisch heruntergeladen (Leerlassen, wenn nur neue Episoden runterladen willst): ")
                if(epi_in == "exit"):
                    exit = True
                    break
                elif(epi_in == "suche"):
                    search = True
                    break
                try:
                    if(epi_in == ""):
                        curEpisodes = release.getEpisodeCount()
                    else:
                        epi_in_int = int(epi_in)
                        if(epi_in_int > release.getEpisodeCount()):
                            print("Deine Episodenzahl darf nicht größer als verfügbare Episoden sein")
                        else:
                            curEpisodes = epi_in_int
                except:
                    print("Fehlerhafte Eingabe, muss eine Zahl sein")

            print("\n")

            customPackage = ""
            if(compare(input("Möchtest du dem Anime einen spezifischen Paketnamen geben? Andernfalls wird der Name des Anime genutzt [J/N]: "), {"j", "ja", "yes", "y"}) == True):
                customPackage = input("Packagename: ")

            destinationFolder = ""
            if jd_deprecated:
                if (compare(input("Möchtest du dem Anime an einen bestimmten Ort speichern? (z.B. \"C://anime/s2\" ) [J/N]: "), {"j", "ja", "yes", "y"}) == True):
                    destinationFolder = input("Pfad: ")

            animedata = {
                "name": anime.getName(),
                "missing": [],
                "releaseID": relchoice,
                "episodes": curEpisodes,
                "url": anime.getURL(),
                "customPackage": customPackage,
                "destinationFolder": destinationFolder
            }
        
            os.makedirs(os.path.dirname(botfolder), exist_ok=True)
            f = open(botfile, "r", encoding="utf-8")
            data = json.load(f)
            f.close()

            haveAddedAnime = False

            try:
                anidata = data['anime']
            except:
                print("Erster Anime in Liste, füge hinzu")
                fullanimedata = []
                fullanimedata.append(animedata)
                data['anime'] = fullanimedata 
                haveAddedAnime = True
                os.makedirs(os.path.dirname(botfolder), exist_ok=True)
                jfile = open(botfile, "w", encoding="utf-8")
                jfile.write(json.dumps(data, indent=4, sort_keys=True))
                jfile.flush()
                jfile.close()
                print("Anime wurde hinzugefügt")

            if(haveAddedAnime == False):              #Füge zu liste hinzu
                isNewAnime = True
                for animeentry in anidata:
                    url = animeentry['url']
                    release = animeentry['releaseID']
                    if(url == anime.getURL() and release == relchoice):
                        print("Anime mit gleichem Release ist bereits in Liste, gehe zurück zur Suche")
                        isNewAnime = False
                if(isNewAnime):
                    print("Füge Anime zu liste hinzu")
                    fullanimedata = data['anime']
                    fullanimedata.append(animedata)
                    data['anime'] = fullanimedata 
#                animedata = {"anime": animedata}
#                data.append(animedata)


                    os.makedirs(os.path.dirname(botfolder), exist_ok=True)
                    jfile = open(botfile, "w", encoding="utf-8")
                    jfile.write(json.dumps(data, indent=4, sort_keys=True))
                    jfile.flush()
                    jfile.close()
                    print("Anime wurde hinzugefügt")

            print("\n\n\n")

        elif(aniquery != "suche"):
            results = al.search(aniquery)
        
            if(len(results) == 0):
                print("Keine Ergebnisse")
                search = True
                break

            print("Ergebnisse: ")
    
            for idx, result in enumerate(results):
                print("[" + str(idx + 1) + "] " + result.tostring())
    
            while(True):
                anichoice = input("Wähle einen Anime (Zahl links daneben eingeben): ")
                if(anichoice == "exit"):
                    exit = True
                    break
                elif(anichoice == "suche"):
                    search = True
                    break
                try:
                    anichoice = int(anichoice)
                    anime = results[anichoice - 1].getAnime()
                    break
                except:
                    print("Fehlerhafte eingabe, versuche erneut")
    
            if(search or exit):
                continue

            releases = anime.getReleases()
        
            print("\n\nReleases:\n")
        
            for rel in releases:
                print(rel.tostring())
    
            print("\n")
            relchoice = ""
            while(True):
                relchoice = input("Wähle eine Release ID: ")
                if(relchoice == "exit"):
                    exit = True
                    break
                elif(relchoice == "suche"):
                    search = True
                    break
                try:
                    relchoice = int(relchoice)
                    if(relchoice <= len(releases)):
                        break
                    else:
                        raise Exception()
                except:
                    print("Fehlerhafte Eingabe, versuche erneut")
    
            if(search or exit):
                continue

            release = releases[relchoice-1]
            print("Du hast folgendes Release gewählt: " + str(release.tostring()))
    
            print("\n")

            print("Das Release hat " + str(release.getEpisodeCount()) + " Episode(n)")
            curEpisodes = -1
            while(curEpisodes == -1):
                epi_in = input("Wieviel Episoden hast du bereits runtergeladen? Die restlichen verfügbaren werden dann automatisch heruntergeladen (Leerlassen, wenn nur neue Episoden runterladen willst): ")
                if(epi_in == "exit"):
                    exit = True
                    break
                elif(epi_in == "suche"):
                    search = True
                    break
                try:
                    if(epi_in == ""):
                        curEpisodes = release.getEpisodeCount()
                    else:
                        epi_in_int = int(epi_in)
                        if(epi_in_int > release.getEpisodeCount()):
                            print("Deine Episodenzahl darf nicht größer als verfügbare Episoden sein")
                        else:
                            curEpisodes = epi_in_int
                except:
                    print("Fehlerhafte Eingabe, muss eine Zahl sein")

            print("\n")

            customPackage = ""

            if(compare(input("Möchtest du dem Anime einen spezifischen Paketnamen geben? Andernfalls wird der Name des Anime genutzt [J/N]: "), {"j", "ja", "yes", "y"}) == True):
                customPackage = input("Packagename: ")

            animedata = {
                "name": anime.getName(),
                "missing": [],
                "releaseID": relchoice,
                "episodes": curEpisodes,
                "url": anime.getURL(),
                "customPackage": customPackage
            }

            if jd_deprecated:
                destinationFolder = ""
                if (compare(input("Möchtest du dem Anime an einen bestimmten Ort speichern? (z.B. \"C://anime/s2\" ) [J/N]: "), {"j", "ja", "yes", "y"}) == True):
                    destinationFolder = input("Pfad: ")

                animedata = {
                    "name": anime.getName(),
                    "missing": [],
                    "releaseID": relchoice,
                    "episodes": curEpisodes,
                    "url": anime.getURL(),
                    "customPackage": customPackage,
                    "destinationFolder": destinationFolder
                }

            os.makedirs(os.path.dirname(botfolder), exist_ok=True)
            f = open(botfile, "r", encoding="utf-8")
            data = json.load(f)
            f.close()

            haveAddedAnime = False

            try:
                anidata = data['anime']
            except:
                print("Erster Anime in Liste, füge hinzu")
                fullanimedata = []
                fullanimedata.append(animedata)
                data['anime'] = fullanimedata 
                haveAddedAnime = True
                os.makedirs(os.path.dirname(botfolder), exist_ok=True)
                jfile = open(botfile, "w", encoding="utf-8")
                jfile.write(json.dumps(data, indent=4, sort_keys=True))
                jfile.flush()
                jfile.close()
                print("Anime wurde hinzugefügt")

            if(haveAddedAnime == False):              #Füge zu liste hinzu
                isNewAnime = True
                for animeentry in anidata:
                    url = animeentry['url']
                    release = animeentry['releaseID']
                    if(url == anime.getURL() and release == relchoice):
                        print("Anime mit gleichem Release ist bereits in Liste, gehe zurück zur Suche")
                        isNewAnime = False
                if(isNewAnime):
                    print("Füge Anime zu liste hinzu")
                    fullanimedata = data['anime']
                    fullanimedata.append(animedata)
                    data['anime'] = fullanimedata 
#                animedata = {"anime": animedata}
#                data.append(animedata)
                    os.makedirs(os.path.dirname(botfolder), exist_ok=True)
                    jfile = open(botfile, "w", encoding="utf-8")
                    jfile.write(json.dumps(data, indent=4, sort_keys=True))
                    jfile.flush()
                    jfile.close()
                    print("Anime wurde hinzugefügt")

            print("\n\n\n")


def handle_failed_batch(batch_result, all_wanted, animeentry, run_counts,
                        today_iso, name, push, log_fn=log, events=None):
    """Apply the outcome of a failed ``downloadBatchCNL`` to bot state.

    Distinguishes two cases that ``downloadBatchCNL`` cannot tell apart on its
    own:

    * **All-phantom** — the batch failed *only* because every wanted episode
      lies beyond the actually-available max (``available_max``). The site DOM
      over-reported the episode count, so none of the wanted episodes exist in
      any release. This is the benign UNAVAILABLE case (the same reality the
      single-ep path handles): logged at INFO, NOT counted as an error.
    * **Numbering mismatch** — ``downloadBatchCNL`` flagged the result with
      ``reason_code == "episode_numbering_mismatch"``: every wanted episode
      and every release-provided episode are disjoint, but not because the
      wanted episodes are beyond ``available_max`` (that's all-phantom,
      below) — the release numbers its files in a different scheme (e.g.
      absolute numbering continuing across cours). This is a third, distinct
      condition; it must never be confused with all-phantom or genuine
      failure. Logged as ``[MISMATCH]`` (actionable, names the suggested
      ``episode_offset``), NOT counted as an error — a dedicated
      ``run_counts["mismatch"]`` tally instead.
    * **Genuine failure** — no ``available_max`` in the response (e.g. a
      MyJD/JD error or the ``except`` path's caller), or a *partial* phantom
      where some wanted episodes are still in range. Logged as ``[ERROR]`` and
      counted in ``run_counts["errors"]``.

    Either way, if the CNL response carried an ``available_max`` the cap is
    refreshed on ``animeentry`` (``al_available_max`` + ``al_available_max_set_at``)
    so the daily revalidation keeps working and genuinely-new episodes recover.

    Returns ``True`` when the cap was refreshed (caller should ``save_ani()``),
    ``False`` otherwise. Mutates ``animeentry``, ``run_counts`` and (when
    passed) ``events`` in place; performs no I/O of its own, which keeps it
    unit-testable.
    """
    reason = batch_result.get("reason", "unbekannt")
    batch_max = batch_result.get("available_max")
    reason_code = batch_result.get("reason_code")
    all_phantom = (batch_max is not None and
                   all(ep > batch_max for ep in all_wanted))
    if reason_code == "episode_numbering_mismatch":
        log_fn("[MISMATCH] " + name + ": " + reason, push)
        run_counts["mismatch"] = run_counts.get("mismatch", 0) + 1
        if events is not None:
            _record_event(events, "mismatch", name, episodes=all_wanted, detail=reason)
    elif all_phantom:
        log_fn("[UNAVAILABLE] " + name + ": keine Downloadlinks für gewünschte "
               "Episoden — verfügbar bis Episode " + str(batch_max)
               + ", markiere als nicht verfügbar", push)
        run_counts["unavailable"] = run_counts.get("unavailable", 0) + 1
        if events is not None:
            _record_event(events, "unavailable", name, episodes=all_wanted,
                          detail="No download links available (max episode " + str(batch_max) + ")")
    else:
        run_counts["errors"] += 1
        log_fn("[ERROR] Batch-CNL fehlgeschlagen für " + name + ": " + reason + " — überspringe", push)
        if events is not None:
            _record_event(events, "error", name, episodes=all_wanted, detail=reason)
    if batch_max is not None:
        animeentry['al_available_max'] = batch_max
        animeentry['al_available_max_set_at'] = today_iso
        return True
    return False


def reload_settings_for_cycle(current):
    """Re-read ani.json's settings block at the start of a cycle and apply
    everything that's safe to change without restarting the bot: the poll
    interval, hoster, pushbullet key, and the JD/MyJDownloader connection
    fields (jdhost/myjd_user/myjd_pw/myjd_device/jd_deprecated*). Those
    connection fields aren't held open anywhere -- animeloads.downloadEpisode/
    downloadBatchCNL take them as plain parameters and connect fresh on every
    call (see utils.addToMYJD/addToJD) -- so simply returning the
    freshly-loaded values here is all a "reconnect" requires; the next
    download call picks them up.

    The returned al_user/al_pass are also freshly loaded, but unlike the JD
    fields they are NOT self-applying: the anime-loads.org login lives on the
    long-lived `animeloads` instance's session, not in a value passed per
    call, so returning a new al_user/al_pass here changes nothing by itself.
    The caller (startbot()'s loop) is responsible for noticing they changed
    and calling `al.login()` again -- see relogin_if_credentials_changed.

    `browserengine`/`browserlocation` are intentionally carried over
    unchanged from `current` -- switching them needs a new Selenium
    driver, which this function does not attempt, so those two still
    require a container restart.

    `current` is the 13-tuple loadconfig() shape currently in effect (the
    tuple startbot() already threads through its loop). Returns
    (updated, reloaded, reason): `reloaded` is False when the file is
    corrupt/torn or no longer configures a download backend -- loadconfig()
    can't tell those two apart, and both are transient, so `current` is
    returned unchanged (with `reason` describing what happened) rather
    than crashing the loop.
    """
    (jdhost, hoster, browser, browserlocation, pushkey, timedelay,
     myjd_user, myjd_pass, myjd_device, jd_deprecated, jd_deprecatedport,
     al_user, al_pass) = current

    reloaded_values = loadconfig()
    if reloaded_values[0] == False:
        return current, False, ("settings reload failed (corrupt ani.json or no "
                                 "download backend configured) -- keeping previous settings")

    (new_jdhost, new_hoster, new_browser, new_browserlocation, new_pushkey,
     new_timedelay, new_myjd_user, new_myjd_pass, new_myjd_device,
     new_jd_deprecated, new_jd_deprecatedport, new_al_user, new_al_pass) = reloaded_values

    if new_browser != browser or new_browserlocation != browserlocation:
        _log.warning(
            "browserengine/browserlocation wurden geaendert, das erfordert einen Neustart "
            "des Bot-Containers / browserengine/browserlocation changed -- restart the bot "
            "container to apply this change"
        )

    updated = (
        new_jdhost, new_hoster, browser, browserlocation, new_pushkey, new_timedelay,
        new_myjd_user, new_myjd_pass, new_myjd_device, new_jd_deprecated,
        new_jd_deprecatedport, new_al_user, new_al_pass,
    )
    return updated, True, None


def relogin_if_credentials_changed(al, al_user, al_pass, last_al_user, last_al_pass, events=None):
    """Re-run the anime-loads.org login when AL_USER/AL_PASS (env) or
    ani.json's al_user/al_pass fallback changed since the last cycle.

    Unlike the JD/MyJDownloader fields (see reload_settings_for_cycle), the
    login isn't a value passed per call -- `al` is the single long-lived
    `animeloads` instance startbot() keeps for the whole run, and its session
    only changes when `al.login()` is actually invoked again. Swapping the
    local al_user/al_pass variables alone changes nothing: the batch-download
    path checks `al.username`, which keeps whatever it was set to at startup
    (or the last successful login) until this re-runs it.

    Returns True if it attempted a re-login (whether or not it succeeded),
    False if the credentials are unchanged and there was nothing to do.
    Never raises -- a failed re-login is logged and recorded as a run_state
    event, and the previous session (logged in or anonymous) carries on
    unchanged, exactly like the startup login's own failure handling.
    """
    if (al_user, al_pass) == (last_al_user, last_al_pass):
        return False

    if al_user and al_pass:
        try:
            al.login(al_user, al_pass)
        except Exception as e:
            _log.warning(
                "Anime-Loads Zugangsdaten geaendert, erneute Anmeldung fehlgeschlagen, "
                "vorherige Sitzung bleibt bestehen / anime-loads.org login changed, "
                "re-login failed -- keeping the previous session: %s", e
            )
            write_login_state(True, False, error=str(e))
            if events is not None:
                _record_event(events, "error", "settings",
                              detail="anime-loads.org re-login failed: " + type(e).__name__)
        else:
            _log.info(
                "Anime-Loads Zugangsdaten geaendert, erneute Anmeldung erfolgreich / "
                "anime-loads.org login changed, re-login succeeded"
            )
            write_login_state(True, True, vip=getattr(al, "isVIP", None))
    else:
        _log.info(
            "Anime-Loads Zugangsdaten entfernt -- die anonyme Sitzung bleibt bis zum "
            "naechsten Neustart bestehen / anime-loads.org login removed -- the "
            "anonymous session stays until the next restart"
        )
    return True


def startbot():

    jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, jd_deprecated, jd_deprecatedport, al_user, al_pass = loadconfig()
 
    interactive = "--docker" not in sys.argv
    if "--not-interactive" in sys.argv:
        interactive = False
    if "--interactive" in sys.argv:
        interactive = True

    config_attempt = 0
    while(jdhost == False):
        if(interactive):
            print("Noch keine oder Fehlerhafte konfiguration, leite weiter zu Einstellungen")
            editconfig()
            jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, jd_deprecated, jd_deprecatedport, al_user, al_pass = loadconfig()
        else:
            # Non-interactive (Docker): do NOT sys.exit — a transient cause such as
            # the /config volume not being mounted yet on host boot would otherwise
            # crash-loop the container under `restart: unless-stopped`. Stay alive
            # and re-read the config with backoff so it self-heals once available.
            config_attempt += 1
            delay = _boot_backoff(config_attempt)
            _log.error("Keine oder fehlerhafte Konfiguration (Versuch %d) — erneuter Versuch in %ds "
                       "(haeufige Ursache: /config noch nicht gemountet oder ani.json fehlt)",
                       config_attempt, delay)
            time.sleep(delay)
            jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, jd_deprecated, jd_deprecatedport, al_user, al_pass = loadconfig()

    pb = init_pushbullet(pushkey)
    last_pushkey = pushkey
    notify_targets = notify.parse_targets(os.environ.get("NOTIFY_URL", ""))

    # The animeloads() constructor launches headless Firefox/geckodriver to fetch
    # DDoS-Guard cookies. A cold-start Selenium failure (resource contention while
    # the host is still bringing services up, a stale profile/geckodriver hiccup)
    # raises here. Unguarded, that exception exits the process and crash-loops the
    # container under `restart: unless-stopped` — the most likely "didn't start
    # automatically" path. Retry in-process with backoff so a transient failure
    # self-recovers and the container stays up.
    al = None
    init_attempt = 0
    while al is None:
        try:
            al = animeloads(browser=browser, browserloc=browserlocation)
        except Exception as e:
            if interactive:
                raise
            init_attempt += 1
            delay = _boot_backoff(init_attempt)
            printException(e)
            _log.error("Browser/Selenium-Initialisierung fehlgeschlagen (Versuch %d) — "
                       "erneuter Versuch in %ds", init_attempt, delay)
            time.sleep(delay)
    tvdb = TVDBClient()

    if(interactive):
        if(compare(input("Möchtest du dich anmelden? [J/N]: "), {"j", "ja", "yes", "y"})):
            user = input("Username: ")
            password = getpass("Passwort: ")
            try:
                al.login(user, password)
                write_login_state(True, True, vip=al.isVIP)
            except Exception as e:
                print("Fehlerhafte Anmeldedaten, fahre mit anonymen Account fort")
                write_login_state(True, False, error=str(e))
                _notify_cycle(notify_targets, pb, [], login_error=str(e))
        else:
            print("Überspringe Anmeldung")
            write_login_state(False, False)
    else:
        if(al_user is not None and al_pass is not None):
            try:
                al.login(al_user, al_pass)
                _log.info("Erfolgreich bei Anime-Loads angemeldet")
                write_login_state(True, True, vip=al.isVIP)
            except Exception as e:
                _log.warning("Fehlerhafte Anmeldedaten, fahre mit anonymen Account fort")
                write_login_state(True, False, error=str(e))
                _notify_cycle(notify_targets, pb, [], login_error=str(e))
        else:
            _log.info("Keine Anmeldedaten für Anime-Loads hinterlegt, fahre mit anonymen Account fort")
            write_login_state(False, False)

    last_al_user, last_al_pass = al_user, al_pass

    if(jdhost == "" and myjd_pass == ""):
        if(interactive == False):
            # Misconfiguration: neither a local JD host nor a MyJDownloader
            # password. Don't sys.exit (crash-loops under unless-stopped); stay
            # alive and re-read the config with backoff so the owner can fix it
            # without manually restarting the container.
            jdpw_attempt = 0
            while(jdhost == "" and myjd_pass == ""):
                jdpw_attempt += 1
                delay = _boot_backoff(jdpw_attempt)
                _log.error("Kein MyJdownloader Passwort und kein JD-Host gesetzt — "
                           "Container bleibt aktiv, erneute Pruefung in %ds", delay)
                time.sleep(delay)
                jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, jd_deprecated, jd_deprecatedport, al_user, al_pass = loadconfig()
        else:
            print("Kein MyJdownloader Passwort gesetzt")  # interactive prompt
            logincorrect = False
            jd=myjdapi.Myjdapi()
            jd.set_app_key("animeloads")
            while(logincorrect == False):
                myjd_pass = getpass("MyJdownloader Passwort: ")

                try:
                  jd.connect(myjd_user, myjd_pass)
                  logincorrect = True
                except:
                    print("Fehlerhafte Logindaten")
    _log.info("Erfolgreich eingeloggt")
    port_attempt = 0
    while (jd_deprecated and jd_deprecatedport == ""):
        if interactive:
            _log.error("Kein JD port gesetzt. beende...")
            sys.exit(1)
        # Non-interactive: keep the container alive and re-read config so a fix
        # (or a late volume mount) is picked up without a manual restart.
        port_attempt += 1
        delay = _boot_backoff(port_attempt)
        _log.error("Kein JD port gesetzt — Container bleibt aktiv, erneute Pruefung in %ds", delay)
        time.sleep(delay)
        jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, jd_deprecated, jd_deprecatedport, al_user, al_pass = loadconfig()

    while(True):
        # Per-cycle run-state bookkeeping (persisted for the dashboard's
        # last_run/next_run, independent of the rolling log tail).
        # Consumed at the START of the cycle it triggers — a request that
        # arrived mid-cycle just sat in the file until now, so it is always
        # honored *after* the previous cycle finished, never by interrupting it.
        manual_trigger = consume_run_now_trigger(_run_now_path())
        trigger = "manual" if manual_trigger else None
        run_started = _utcnow_iso()
        run_counts = {"entries": 0, "checked": 0, "downloaded": 0, "errors": 0,
                      "skipped": 0, "unavailable": 0, "mismatch": 0}
        events = []
        entry_outcomes = {}

        # Apply dashboard settings changes at this cycle boundary (card
        # 2a89b409) -- see reload_settings_for_cycle's own docstring for
        # exactly what does/doesn't take effect without a restart.
        (jdhost, hoster, browser, browserlocation, pushkey, timedelay,
         myjd_user, myjd_pass, myjd_device, jd_deprecated, jd_deprecatedport,
         al_user, al_pass), settings_reloaded, settings_reload_reason = reload_settings_for_cycle(
            (jdhost, hoster, browser, browserlocation, pushkey, timedelay,
             myjd_user, myjd_pass, myjd_device, jd_deprecated, jd_deprecatedport,
             al_user, al_pass))
        if not settings_reloaded:
            _log.warning(settings_reload_reason)
            _record_event(events, "error", "settings", detail=settings_reload_reason)
        else:
            if pushkey != last_pushkey:
                pb = init_pushbullet(pushkey)
            if not interactive:
                relogin_if_credentials_changed(al, al_user, al_pass, last_al_user, last_al_pass,
                                                events=events)
        last_pushkey = pushkey
        last_al_user, last_al_pass = al_user, al_pass

        os.makedirs(os.path.dirname(botfolder), exist_ok=True)
        data, corrupt_err = load_ani_cycle_start(botfile)
        if corrupt_err is not None:
            _log.error("ani.json ist beschaedigt, ueberspringe Zyklus: %s", corrupt_err)
            recheck = timedelay if isinstance(timedelay, int) and timedelay > 0 else 600
            # watchlist_urls omitted: a corrupt ani.json means we don't actually
            # know the real watchlist this cycle — carry the persisted "entries"
            # map forward untouched rather than pruning against an empty list.
            write_run_state(run_started, _utcnow_iso(), recheck, run_counts, events, trigger=trigger)
            sleep_until_next_cycle(recheck, _run_now_path())
            continue

        anidata = ""
        try:
            anidata = data['anime']
        except:
            # No anime configured yet (fresh deploy, or none added via the
            # dashboard). Do NOT return — that exits the process (exit 0) and
            # stops/tight-loops the container under `restart: unless-stopped`,
            # which reads as "the bot won't stay running". Stay alive and
            # re-check after the poll interval so entries added later via the
            # dashboard are picked up without a manual container restart.
            recheck = timedelay if isinstance(timedelay, int) and timedelay > 0 else 600
            _log.info("Keine Anime in der Liste — erneute Pruefung in " + str(recheck) + " Sekunden")
            # watchlist_urls=[]: unlike the corrupt-file case above, we DO know
            # the watchlist here (it's genuinely empty) — prune "entries" to match.
            write_run_state(run_started, _utcnow_iso(), recheck, run_counts, events, trigger=trigger,
                             entry_outcomes=entry_outcomes, watchlist_urls=[])
            sleep_until_next_cycle(recheck, _run_now_path())
            continue

        if(anidata != ""):
            run_counts["entries"] = len(anidata)
            for idx, animeentry in enumerate(anidata):
                # Fresh copy first: user-owned fields (paused, releaseID,
                # prefs, folder, episodes) may have changed since cycle start.
                if not refresh_entry(botfile, animeentry):
                    continue
                if skip_if_paused(animeentry, run_counts, entry_outcomes):
                    continue
                name = animeentry['name']
                url = animeentry['url']
                releaseID = animeentry['releaseID']
                try:
                    customPackage = animeentry['customPackage']
                except:
                    customPackage = ""
                # JD package name must be unique per release so JDownloader does
                # not merge two seasons of the same show (which share customPackage
                # by design — that field defines the Plex destination folder).
                jdPackageName = (
                    animeentry.get('display_title')
                    or animeentry.get('name')
                    or customPackage
                )
                try:
                    destinationFolder = animeentry['destinationFolder']
                except:
                    destinationFolder = None
                missingEpisodes = animeentry['missing']
                episodes = animeentry['episodes']

                # Baseline for this entry's field-level merge: what's been
                # persisted so far this cycle (initially, what cycle-start
                # load_ani_cycle_start() read). save_ani() below diffs the
                # live `animeentry` against this on each call and writes only
                # what changed, onto a FRESH re-read of ani.json under the
                # lock — never the whole stale `data` snapshot.
                saved_state = {f: animeentry[f] for f in BOT_OWNED_SCALAR_FIELDS if f in animeentry}
                saved_state["missing"] = list(missingEpisodes)

                def save_ani():
                    """Persist only the bot-owned fields changed on
                    `animeentry` since the last save this cycle. Returns
                    False (and stops updating `saved_state`) if the
                    dashboard removed this entry mid-cycle — callers must
                    treat that as "stop processing this entry" for the rest
                    of the cycle instead of resurrecting it."""
                    nonlocal saved_state
                    fields, unset, list_deltas = compute_entry_delta(saved_state, animeentry)
                    if not fields and not unset and not list_deltas:
                        return True
                    found = anistore.merge_entry_fields(
                        botfile, "anime", url, fields=fields, unset=unset, list_deltas=list_deltas)
                    if found:
                        saved_state.update(fields)
                        for f in unset:
                            saved_state.pop(f, None)
                        for f in list_deltas:
                            saved_state[f] = list(animeentry.get(f) or [])
                    return found

                # --- Smart skip logic -------------------------------------------
                # force_check: a dashboard "Check now" click on this one entry.
                # Peeked fresh (not this cycle's snapshot) and cleared unconditionally
                # right here — a one-shot bypass of Steps 1-4 below, honored at most
                # once even if the scrape that follows fails.
                force_check = resolve_force_check(botfile, url)
                if force_check:
                    _log.info("[CHECK-NOW] " + name + " — forced check requested, bypassing skip logic")

                # Steps 1-3 (complete flag / cached al_status / skip_until throttle)
                decision = pre_scrape_skip_decision(
                    animeentry, name, len(missingEpisodes), episodes, force_check, date.today())
                if decision["log"]:
                    level, tag, msg = decision["log"]
                    getattr(_log, level)("[" + tag + "] " + msg)
                if decision["mark_complete"]:
                    animeentry['complete'] = True
                    if not save_ani(): continue
                if decision["skip"]:
                    run_counts["skipped"] += 1
                    # By this point Step 1 ("already complete") and Step 2
                    # ("al_status complete", mark_complete above) are the only
                    # ways `complete` can be set — Step 3 (skip_until) returns
                    # before ever touching it. So `complete` alone tells them apart.
                    if animeentry.get('complete'):
                        reason = "complete"
                    elif animeentry.get('skip_real_airdate') and animeentry.get('skip_until'):
                        reason = "waiting for airdate " + animeentry['skip_until']
                    else:
                        reason = "skip_until (" + str(animeentry.get('skip_until') or '') + ")"
                    _record_entry_outcome(entry_outcomes, url, "skipped", reason)
                    continue

                # Step 4: TVDB-based checks (lightweight HTTP, no Selenium)
                # Movies are not series — skip TVDB series status logic.
                tvdb_id = animeentry.get('tvdb_id')
                tvdb_season = animeentry.get('tvdb_season')
                if tvdb_checks_apply(animeentry, force_check, tvdb.available):
                    try:
                        series_status = tvdb.get_series_status(tvdb_id)
                        if series_status:
                            animeentry['tvdb_series_status'] = series_status

                        tvdb_ep_count = None
                        if series_status == "Ended" and tvdb_season:
                            tvdb_ep_count = tvdb.get_season_episode_count(tvdb_id, tvdb_season)
                        airdate = None
                        if series_status == "Continuing" and tvdb_season:
                            airdate = tvdb.get_next_episode_airdate(tvdb_id, tvdb_season, episodes)

                        decision = tvdb_skip_decision(
                            series_status, tvdb_season, tvdb_ep_count, airdate, episodes,
                            len(missingEpisodes), date.today(), datetime.now(),
                            animeentry.get('skip_recheck_at'), EARLY_SCRAPE_DAYS,
                            TVDB_PASTDUE_RECHECK_HOURS)

                        if decision["updates"]:
                            animeentry.update(decision["updates"])
                            if not save_ani(): continue
                        if decision["log"]:
                            level, message = decision["log"]
                            tag = {"complete": "COMPLETE", "skip": "SKIP",
                                   "retry": "RETRY", "early": "EARLY"}.get(decision["action"], "TVDB")
                            getattr(_log, level)("[" + tag + "] " + name + " — " + message)
                        if decision["terminal"]:
                            run_counts["skipped"] += 1
                            # decision["log"][1] is already a human reason for
                            # every terminal case tvdb_skip_decision can return
                            # (complete / waiting-for-airdate / TVDB past-due
                            # recheck throttle / no-airdate-known synthetic skip).
                            reason = decision["log"][1] if decision["log"] else "TVDB skip"
                            _record_entry_outcome(entry_outcomes, url, "skipped", reason)
                            continue
                    except Exception as e:
                        _log.warning("[TVDB] Error checking " + name + ": " + str(e))
                # --- End skip logic ----------------------------------------------

                try:
                    anime = al.getAnime(url)
                    release = anime.getReleases()[releaseID-1]
                except:
                    _log.warning("Failed to get Anime, skipping...")
                    run_counts["checked"] += 1
                    run_counts["errors"] += 1
                    _record_event(events, "error", name, detail="Failed to fetch anime data")
                    _record_entry_outcome(entry_outcomes, url, "error", "Failed to fetch anime data")
                    continue

                # The scrape above can take a while: pick up an `episodes`
                # edit made meanwhile before planning what to download.
                episodes = sync_user_episodes(botfile, animeentry, saved_state)

                now = datetime.now()
                run_counts["checked"] += 1
                _log.info("[" + now.strftime("%H:%M:%S") + "] Prüfe " + name + " auf updates")
                # updateInfo already called by getAnime — skip redundant call
                curEpisodes = release.getEpisodeCount()               #Anzahl der Episoden aktuell online

                # Cap curEpisodes by known-available max (DOM may over-report if tabs have no links).
                # The cap is single-day: a stale cap from a prior day must not permanently hide
                # episodes that the site has since added. Revalidate by clearing yesterday's cap
                # when DOM reports more — if those new episodes are still phantom, the batch/single-ep
                # paths below will re-set the cap with today's date.
                today_iso = date.today().isoformat()
                al_available_max = animeentry.get('al_available_max')
                cap_set_at = animeentry.get('al_available_max_set_at')
                if al_available_max is not None and al_available_max < curEpisodes and cap_set_at != today_iso:
                    _log.info("[CAP-RESET] " + name + ": clearing stale al_available_max="
                              + str(al_available_max) + " (set " + (cap_set_at or "never")
                              + ") — DOM reports " + str(curEpisodes))
                    animeentry.pop('al_available_max', None)
                    animeentry.pop('al_available_max_set_at', None)
                    al_available_max = None
                    if not save_ani(): continue
                if al_available_max is not None and al_available_max < curEpisodes:
                    curEpisodes = al_available_max
                    # Self-heal: drop missing/episodes values that exceed the real max
                    missingEpisodes = [m for m in missingEpisodes if m <= al_available_max]
                    animeentry['missing'] = missingEpisodes
                    if int(animeentry['episodes']) > al_available_max:
                        animeentry['episodes'] = al_available_max
                        episodes = al_available_max
                    if not save_ani(): continue

                # Cache anime-loads.org status for dashboard
                if anime.status:
                    animeentry['al_status'] = anime.status
                if anime.maxEpisodes != 999999:
                    animeentry['al_max_episodes'] = anime.maxEpisodes
                # Throttle: anime finished but release on site is still missing
                # episodes. Use freshly-scraped values; recheck once per day via Step 3.
                fresh_al_status = animeentry.get('al_status', '')
                fresh_al_max = animeentry.get('al_max_episodes')
                if fresh_al_status in ("Abgeschlossen", "Completed", "Complete") \
                        and fresh_al_max and curEpisodes < fresh_al_max \
                        and len(missingEpisodes) == 0:
                    throttle_date = (date.today() + timedelta(days=1)).isoformat()
                    _log.info("[THROTTLE] " + name + " — complete but release has "
                          + str(curEpisodes) + "/" + str(fresh_al_max) + " eps, re-check " + throttle_date)
                    animeentry['skip_until'] = throttle_date
                    # Synthetic throttle date — honored strictly, not early-scraped.
                    animeentry['skip_real_airdate'] = False
                    if not save_ani(): continue
                # Cache media type + naming metadata (used by mover to route movies
                # to a separate output folder with Plex "Title (Year)" convention).
                if anime.type:
                    animeentry['media_type'] = anime.type
                if getattr(anime, 'year', 0):
                    animeentry['year'] = anime.year
                display = (getattr(anime, 'gerName', '') or
                           getattr(anime, 'engName', '') or
                           getattr(anime, 'japName', ''))
                if display:
                    animeentry['display_title'] = display
                # Collect all wanted episodes (missing + new)
                wanted_missing, wanted_new = wanted_episodes(missingEpisodes, episodes, curEpisodes)
                all_wanted = wanted_missing + wanted_new
                if len(all_wanted) > 1:
                    # Multi-episode: CNL only, no per-episode fallback
                    if not al.username or al.username == "anonymous":
                        run_counts["errors"] += 1
                        log("[ERROR] " + name + ": Login erforderlich für Batch-CNL (" + str(len(all_wanted)) + " Episoden) — überspringe", pb)
                        _record_event(events, "error", name, episodes=all_wanted,
                                      detail="Login required for batch download")
                        _record_entry_outcome(entry_outcomes, url, "error", "Login required for batch download")
                    else:
                        try:
                            log("[BATCH] Versuche Batch-Download für " + str(len(all_wanted)) + " Episoden von " + name, pb)
                            batch_result = anime.downloadBatchCNL(
                                release, hoster, browser, browserlocation,
                                jdhost=jdhost, myjd_user=myjd_user, myjd_pw=myjd_pass,
                                myjd_device=myjd_device, jd_deprecated=jd_deprecated,
                                jd_deprecatedport=jd_deprecatedport,
                                pkgName=jdPackageName, destinationFolder=destinationFolder,
                                wanted_episodes=set(all_wanted),
                                episode_offset=animeentry.get('episode_offset', 0) or 0)
                            if batch_result["success"]:
                                batch_sent = set(batch_result["episodes_sent"])
                                run_counts["downloaded"] += len(batch_sent)
                                log("[BATCH] " + str(len(batch_sent)) + " Episoden von " + name + " zu JDownloader hinzugefügt", pb)
                                _record_event(events, "download", name, episodes=sorted(batch_sent))
                                _record_entry_outcome(entry_outcomes, url, "downloaded",
                                                       str(len(batch_sent)) + " episode(s) batch-downloaded")
                                # Record actual available max from CNL data (authoritative; DOM may over-report)
                                batch_max = batch_result.get("available_max")
                                if batch_max is not None:
                                    animeentry['al_available_max'] = batch_max
                                    animeentry['al_available_max_set_at'] = today_iso
                                # Update ani.json for batch-sent episodes
                                for ep in sorted(batch_sent):
                                    if ep in missingEpisodes:
                                        missingEpisodes.remove(ep)
                                    if ep > animeentry['episodes']:
                                        animeentry['episodes'] = ep
                                # Rebuild missing: drop batch-sent and anything beyond available_max
                                remaining_missing = [m for m in missingEpisodes if m not in batch_sent]
                                if batch_max is not None:
                                    remaining_missing = [m for m in remaining_missing if m <= batch_max]
                                animeentry['missing'] = remaining_missing
                                # Capture the actual JD download folder pattern so the mover can match it.
                                release_pattern = batch_result.get("release_pattern") or getattr(anime, '_last_release_pattern', '')
                                if release_pattern:
                                    animeentry['download_folder_pattern'] = release_pattern
                                if not save_ani(): continue
                                if batch_result["episodes_not_found"]:
                                    _log.warning("[BATCH] Episoden nicht im Batch gefunden: %s — werden beim nächsten Lauf erneut versucht",
                                                 batch_result["episodes_not_found"])
                            else:
                                # Decide error-vs-benign and refresh the cap in one place
                                # (see handle_failed_batch). save_ani() stays here so the
                                # helper remains pure/testable.
                                # Mirrors handle_failed_batch's own classification (kept in
                                # sync with it — see that function's docstring) so the
                                # persisted entry outcome matches the [MISMATCH]/
                                # [UNAVAILABLE]/[ERROR] tag it actually logged.
                                _batch_max = batch_result.get("available_max")
                                _all_phantom = (_batch_max is not None and
                                                all(_e > _batch_max for _e in all_wanted))
                                if batch_result.get("reason_code") == "episode_numbering_mismatch":
                                    _record_entry_outcome(entry_outcomes, url, "mismatch",
                                                           batch_result.get("reason", "episode numbering mismatch"))
                                elif _all_phantom:
                                    _record_entry_outcome(entry_outcomes, url, "unavailable",
                                                           "No download links available (max episode "
                                                           + str(_batch_max) + ")")
                                else:
                                    _record_entry_outcome(entry_outcomes, url, "error",
                                                           batch_result.get("reason", "batch download failed"))
                                if handle_failed_batch(batch_result, all_wanted, animeentry,
                                                       run_counts, today_iso, name, pb,
                                                       events=events):
                                    if not save_ani(): continue
                        except Exception as e:
                            printException(e)
                            run_counts["errors"] += 1
                            log("[ERROR] Batch-CNL fehlgeschlagen für " + name + ": " + str(e) + " — überspringe", pb)
                            _record_event(events, "error", name, episodes=all_wanted,
                                          detail="Batch-CNL failed: " + type(e).__name__)
                            _record_entry_outcome(entry_outcomes, url, "error",
                                                   "Batch-CNL failed: " + type(e).__name__)

                elif len(all_wanted) == 1:
                    # Single episode: use per-episode download
                    ep = all_wanted[0]
                    is_missing = ep in wanted_missing
                    log("[DOWNLOAD] Lade Episode " + str(ep) + " von " + name, pb)
                    ep_unavailable = False
                    try:
                        if(myjd_user != ""):
                            dl_ret = anime.downloadEpisode(ep, release, hoster, browser, browserlocation, myjd_user=myjd_user, myjd_pw=myjd_pass, myjd_device=myjd_device,jd_deprecated=jd_deprecated,jd_deprecatedport=jd_deprecatedport, pkgName=jdPackageName, destinationFolder=destinationFolder)
                        else:
                            dl_ret = anime.downloadEpisode(ep, release, hoster, browser, browserlocation, jdhost, jd_deprecated=jd_deprecated,jd_deprecatedport=jd_deprecatedport, pkgName=jdPackageName, destinationFolder=destinationFolder)
                    except ALLinkExtractionException as e:
                        # CNL returned no usable key/links — episode has no download data on the site
                        ep_unavailable = True
                        dl_ret = False
                    except Exception as e:
                        printException(e)
                        dl_ret = False
                    if(dl_ret == True):
                        run_counts["downloaded"] += 1
                        log("[DOWNLOAD] Episode " + str(ep) + " von " + name + " wurde zu JDownloader hinzugefügt", pb)
                        if is_missing:
                            if ep in missingEpisodes:
                                missingEpisodes.remove(ep)
                            animeentry['missing'] = missingEpisodes
                        if ep > animeentry['episodes']:
                            animeentry['episodes'] = ep
                        release_pattern = getattr(anime, '_last_release_pattern', '')
                        if release_pattern:
                            animeentry['download_folder_pattern'] = release_pattern
                        _record_event(events, "download", name, episodes=[ep])
                        _record_entry_outcome(entry_outcomes, url, "downloaded", "episode downloaded", episode=ep)
                        if not save_ani(): continue
                    elif ep_unavailable:
                        log("[UNAVAILABLE] Episode " + str(ep) + " von " + name + " — keine Downloadlinks, markiere als nicht verfügbar", pb)
                        run_counts["unavailable"] += 1
                        _record_event(events, "unavailable", name, episodes=[ep],
                                      detail="No download links available")
                        _record_entry_outcome(entry_outcomes, url, "unavailable",
                                               "No download links available", episode=ep)
                        # Cap al_available_max so we stop asking for this (or higher) ep
                        prev_max = animeentry.get('al_available_max')
                        new_max = ep - 1
                        if prev_max is None or prev_max > new_max:
                            animeentry['al_available_max'] = new_max
                        animeentry['al_available_max_set_at'] = today_iso
                        if ep in missingEpisodes:
                            missingEpisodes.remove(ep)
                            animeentry['missing'] = missingEpisodes
                        # Roll back episodes counter if it was advanced into this unavailable ep
                        if int(animeentry['episodes']) >= ep:
                            animeentry['episodes'] = new_max
                        if not save_ani(): continue
                    elif isinstance(dl_ret, Exception):
                        run_counts["errors"] += 1
                        log("[ERROR] Episode " + str(ep) + " von " + name + ": " + str(dl_ret), pb)
                        _record_event(events, "error", name, episodes=[ep],
                                      detail="Download failed: " + type(dl_ret).__name__)
                        _record_entry_outcome(entry_outcomes, url, "error",
                                               "Download failed: " + type(dl_ret).__name__, episode=ep)
                    else:
                        run_counts["errors"] += 1
                        log("[ERROR] Episode " + str(ep) + " von " + name + ": JDownloader nicht erreichbar?", pb)
                        _record_event(events, "error", name, episodes=[ep], detail="JDownloader unreachable")
                        _record_entry_outcome(entry_outcomes, url, "error", "JDownloader unreachable", episode=ep)
                        # Transient failure: don't mutate state — next run will retry naturally

                else:
                    _log.info("[INFO] " + name + " hat keine neuen Folgen verfügbar")
                    _record_entry_outcome(entry_outcomes, url, "skipped", "no new episode")

                # Auto-detect completion from anime-loads.org status
                updated_missing = animeentry.get('missing', [])
                if anime.type == "movie":
                    # Movies have no episode count on the site (maxEpisodes stays 999999).
                    # A single successful release download is the whole thing.
                    if animeentry.get('episodes', 0) >= 1 and len(updated_missing) == 0:
                        _log.info("[COMPLETE] " + name + " — movie release downloaded")
                        animeentry['complete'] = True
                        _record_event(events, "complete", name)
                        _record_complete_outcome(entry_outcomes, url, movie=True)
                        save_ani()
                elif anime.status in ("Abgeschlossen", "Completed") \
                        and anime.maxEpisodes != 999999 \
                        and animeentry['episodes'] >= anime.maxEpisodes \
                        and len(updated_missing) == 0:
                    _log.info("[COMPLETE] " + name + " — anime-loads status: " + anime.status)
                    animeentry['complete'] = True
                    _record_event(events, "complete", name)
                    _record_complete_outcome(entry_outcomes, url)
                    save_ani()
            write_run_state(run_started, _utcnow_iso(), timedelay, run_counts, events, trigger=trigger,
                             entry_outcomes=entry_outcomes,
                             watchlist_urls=[e['url'] for e in anidata])
            _notify_cycle(notify_targets, pb, events)
            _log.info("Schlafe " + str(timedelay) + " Sekunden")
            sleep_until_next_cycle(timedelay, _run_now_path())

def removeAnime():
    jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, al_user, al_pass = loadconfig()
 
    while(jdhost == False):
        print("Noch keine oder Fehlerhafte konfiguration, leite weiter zu Einstellungen")
        editconfig()
        jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, al_user, al_pass = loadconfig()

    os.makedirs(os.path.dirname(botfolder), exist_ok=True)
    f = open(botfile, "r", encoding="utf-8")
    data = json.load(f)
    f.close()

    anidata = ""
    try:
        anidata = data['anime']
    except:
        print("Du hast keine Anime in deiner Liste")


    if(anidata != ""):
        print("Deine Liste: ")
        while(True):
            for idx, animeentry in enumerate(anidata):
                print("[ID: " + str(idx+1) + "] " + animeentry['name'] + " mit Release " + str(animeentry['releaseID']))
            selection = input("Welchen Anime möchtest du löschen? (ID eingeben, \"exit\" zum beenden): ")
            if(selection == "exit"):
                print("Exit, beende...")
                break
            else:
                try:
                    sel_int = int(selection) - 1
                    data['anime'].pop(sel_int)
                    os.makedirs(os.path.dirname(botfolder), exist_ok=True)
                    jfile = open(botfile, "w", encoding="utf-8")
                    jfile.write(json.dumps(data, indent=4, sort_keys=True))
                    jfile.flush()
                    jfile.close()
                    print("Anime wurde gelöscht")
                except:
                    print("Fehler beim löschen des Eintrags")

def printhelp():
    print("anibot.py [edit | start | add | remove]")
    print("[edit]:    Ändere deine Einstellungen")
    print("[start]:   Starte Bot und lade Episoden runter")
    print("[add]:     Füge neue Anime zu deiner Liste hinzu")
    print("[remove]:  Lösche Anime aus deiner Liste")


# CLI dispatch. Guarded by __name__ == "__main__" so that `import anibot`
# (e.g. from the test suite) does NOT launch the bot, while running the module
# as a script — `python anibot.py [args]`, the Docker ENTRYPOINT/CMD — still
# dispatches identically. The if-blocks below don't create a new scope, so the
# module-level globals botfile/botfolder are reassigned exactly as before.
if __name__ == "__main__":
    commandSet = False
    if(arglen >= 2):
        for idx, arg in enumerate(sys.argv):
            if(arg == "--configfile"):
                try:
                    botfile = sys.argv[idx+1]
                    botfolder_arr = botfile.split("/")[:-1]
                    botfolder = ""
                    for p in botfolder_arr:
                        botfolder += p
                        botfolder += "/"
                    print("Config Datei: " + botfile)
                except Exception as e:
                    botfile = "config/ani.json"
                    botfolder = "config/"
                    print("--configfile gegeben, aber kein Pfad (oder fehlerhafter) danach, setze Pfad auf ./config/ani.json")
            if(arg == "start"):
                commandSet = True
                startbot()
            elif(arg == "edit"):
                commandSet = True
                editconfig()
                print("Einstellungen gespeichert")
            elif(arg == "add"):
                commandSet = True
                addAnime()
            elif(arg == "remove"):
              commandSet = True
              removeAnime()
            elif("help" in arg):
                printhelp()

    else:
        if(arglen == 1):
            startbot()
        printhelp()

    if(commandSet == False):
        startbot()

    #episodes = getEpisodes()
