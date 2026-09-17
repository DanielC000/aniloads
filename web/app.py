#!/usr/bin/env python3
"""
Anime-Loads Dashboard — web UI for managing watchlist and monitoring bot activity.
Manages the ani.json watchlist for the pfuenzle/anime-loads bot.
Reads bot logs and triggers runs via Docker socket.
"""

import collections
import http.client
import json
import os
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, urlencode

import logging
from logging.handlers import TimedRotatingFileHandler

LOG_DIR = os.environ.get("LOG_DIR", "/config/logs")
LOG_FILE = os.path.join(LOG_DIR, "anime-web.log")
LOGLEVEL = getattr(logging, os.environ.get("LOGLEVEL", "INFO").upper(), logging.INFO)

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
_handlers = [_stream_handler]

_file_log_error = None
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    _file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=14, encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _handlers.append(_file_handler)
except OSError as e:
    _file_log_error = e

logging.basicConfig(level=LOGLEVEL, handlers=_handlers)
_log = logging.getLogger("anime-web")
if _file_log_error:
    _log.warning("File logging disabled: %s", _file_log_error)

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
ANI_JSON = os.path.join(CONFIG_DIR, "ani.json")
PREFS_FILE = os.path.join(CONFIG_DIR, "web-prefs.json")
RUN_STATE_FILE = os.path.join(CONFIG_DIR, "run_state.json")
# Soft run-now trigger consumed by bot/anibot.py's sleep_until_next_cycle() /
# consume_run_now_trigger() — replaces restarting the bot container (see
# trigger_run_now() below). RUN_NOW_STATE_FILE is dashboard-only bookkeeping
# for the cooldown: the bot deletes RUN_NOW_FILE within a few seconds of
# consuming it, well before the cooldown window is up, so the cooldown can't
# be tracked off that file's presence/mtime alone.
RUN_NOW_FILE = os.path.join(CONFIG_DIR, "run_now")
RUN_NOW_STATE_FILE = os.path.join(CONFIG_DIR, "run_now_last.json")
RUN_NOW_COOLDOWN_SECONDS = int(os.environ.get("RUN_NOW_COOLDOWN_SECONDS", "120"))
MOVE_HISTORY_FILE = os.path.join(CONFIG_DIR, "move_history.json")
PORT = int(os.environ.get("PORT", "8080"))
BOT_CONTAINER = os.environ.get("BOT_CONTAINER", "anime-loads")
DOCKER_SOCK = "/var/run/docker.sock"
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/data/downloads/anime")
MEDIA_DIR = os.environ.get("MEDIA_DIR", "/data/media/anime")
MOVIE_MEDIA_DIR = os.environ.get("MOVIE_MEDIA_DIR", "/data/media/anime movies")
MIN_AGE_MINUTES = int(os.environ.get("MIN_AGE_MINUTES", "5"))
MOVE_POLL_SECONDS = int(os.environ.get("MOVE_POLL_SECONDS", "300"))
MOVE_STARTUP_DELAY = int(os.environ.get("MOVE_STARTUP_DELAY", "30"))
DOCKER_SOCKET_TIMEOUT = int(os.environ.get("DOCKER_SOCKET_TIMEOUT", "10"))
RESOLVE_PENDING_STARTUP_DELAY = int(os.environ.get("RESOLVE_PENDING_STARTUP_DELAY", "10"))
RESOLVE_PENDING_EMPTY_INTERVAL = int(os.environ.get("RESOLVE_PENDING_EMPTY_INTERVAL", "60"))
RESOLVE_PENDING_PER_ENTRY_DELAY = int(os.environ.get("RESOLVE_PENDING_PER_ENTRY_DELAY", "5"))
RESOLVE_PENDING_BATCH_INTERVAL = int(os.environ.get("RESOLVE_PENDING_BATCH_INTERVAL", "120"))

# Try to import animeloads library
AL_AVAILABLE = False
try:
    sys.path.insert(0, "/usr/src/app/anime-loads")
    from animeloads import animeloads as AL
    AL_AVAILABLE = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Docker API client (stdlib only, talks to Unix socket)
# ---------------------------------------------------------------------------

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(DOCKER_SOCKET_TIMEOUT)
        self.sock.connect(self.socket_path)


class DockerAPI:
    def __init__(self, sock_path=DOCKER_SOCK):
        self.sock_path = sock_path
        self.available = os.path.exists(sock_path)

    def _request(self, method, path):
        if not self.available:
            return None
        try:
            conn = UnixHTTPConnection(self.sock_path)
            conn.request(method, path)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
            return data
        except Exception:
            return None

    def get_status(self, container=BOT_CONTAINER):
        data = self._request("GET", "/containers/{}/json".format(container))
        if not data:
            return {"status": "unknown", "started": "", "running": False}
        try:
            info = json.loads(data)
            state = info.get("State", {})
            return {
                "status": state.get("Status", "unknown"),
                "started": state.get("StartedAt", ""),
                "running": state.get("Running", False),
            }
        except (json.JSONDecodeError, KeyError):
            return {"status": "error", "started": "", "running": False}

    def get_logs(self, container=BOT_CONTAINER, tail=500):
        data = self._request(
            "GET",
            "/containers/{}/logs?stdout=1&stderr=1&tail={}&timestamps=1".format(
                container, tail
            ),
        )
        if not data:
            return []
        # Docker multiplexed log stream: 8-byte header per frame
        lines = []
        offset = 0
        while offset < len(data):
            if offset + 8 > len(data):
                break
            header = data[offset : offset + 8]
            size = struct.unpack(">I", header[4:8])[0]
            offset += 8
            if offset + size > len(data):
                chunk = data[offset:]
            else:
                chunk = data[offset : offset + size]
            offset += size
            try:
                text = chunk.decode("utf-8", errors="replace").strip()
                if text:
                    lines.append(text)
            except Exception:
                pass
        return lines

docker = DockerAPI()

# Move-completed state (thread-safe)
MOVE_HISTORY_MAX = 100
STUCK_ITEMS_MAX = 200

_move_lock = threading.Lock()
_move_history = collections.deque(maxlen=MOVE_HISTORY_MAX)
_move_last_run = None
_move_running = False
_move_trigger = threading.Event()
# Serializes save_move_state's tmp-write + replace — called from both the
# mover worker thread and POST handlers, so two concurrent saves must not
# race on the same tmp path (the dashboard is moving to a threading server).
_move_state_write_lock = threading.Lock()
# Downloads the mover can't resolve on its own (parse failure, already-exists,
# no watchlist match) keyed by a path+reason string — see _stuck_key. Persisted
# alongside history so "Ignore" and history both survive a web restart.
_stuck_items = {}


def load_move_state():
    """Read the persisted move history + stuck/ignored items. Returns {} when
    absent or unreadable, so callers fall back to the empty in-memory state."""
    try:
        with open(MOVE_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    # ValueError also covers UnicodeDecodeError (a corrupt/non-UTF-8 file);
    # see load_run_state's comment for why this is a deliberate widening.
    except (FileNotFoundError, ValueError):
        return {}


def save_move_state():
    """Persist move history + stuck/ignored items atomically (tmp + os.replace)
    so a web restart doesn't lose them (history was previously an in-memory
    deque only) and a crash mid-write can't corrupt the file.

    Called from both the mover worker thread and POST handlers, so the
    tmp-write + replace is serialized by its own lock (a fixed tmp path shared
    across concurrent callers could otherwise interleave). Uses mkstemp for a
    collision-proof tmp name in the same directory (so os.replace stays an
    atomic rename on the same filesystem), with its mode fixed to 0o644 since
    mkstemp defaults to the more restrictive 0o600.
    """
    with _move_lock:
        payload = {
            "history": list(_move_history)[-MOVE_HISTORY_MAX:],
            "stuck": dict(list(_stuck_items.items())[-STUCK_ITEMS_MAX:]),
        }
    with _move_state_write_lock:
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(MOVE_HISTORY_FILE) or ".", prefix=".move_history-")
            os.chmod(tmp_path, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
            os.replace(tmp_path, MOVE_HISTORY_FILE)
        except OSError as e:
            _log.error("[mover] Failed to persist move history: %s", e)
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


def _restore_move_state():
    state = load_move_state()
    for ev in state.get("history", [])[-MOVE_HISTORY_MAX:]:
        _move_history.append(ev)
    _stuck_items.update(state.get("stuck") or {})


_restore_move_state()


# ---------------------------------------------------------------------------
# TVDB API v4 client (shared module in bot/tvdb.py)
# ---------------------------------------------------------------------------

from tvdb import TVDBClient, TVDB_API_KEY  # noqa: E402 — path set above
import anistore  # noqa: E402 — path set above

tvdb = TVDBClient()


# ---------------------------------------------------------------------------
# Bot log parser
# ---------------------------------------------------------------------------

def parse_bot_logs(raw_lines):
    """Parse bot log lines into structured runs."""
    runs = []
    current_run = None

    for line in raw_lines:
        # Strip Docker timestamp prefix (RFC3339 format)
        ts_match = re.match(r"(\d{4}-\d{2}-\d{2}T[\d:.]+Z?)\s*(.*)", line)
        docker_ts = ""
        content = line
        if ts_match:
            docker_ts = ts_match.group(1)
            content = ts_match.group(2)

        # Run start: [HH:MM:SS] Prüfe {name} auf updates
        check_match = re.match(r"\[(\d{2}:\d{2}:\d{2})\]\s*Prüfe (.+?) auf updates", content)
        if check_match:
            if current_run:
                runs.append(current_run)
            current_run = {
                "time": check_match.group(1),
                "docker_ts": docker_ts,
                "anime": check_match.group(2),
                "events": [],
            }
            continue

        # Standalone events ([SKIP]/[COMPLETE]/[THROTTLE]) can appear between
        # cycles with no active run. Handle them before the no-run guard below so
        # they always create their own entry — these never attach to current_run,
        # so hoisting them keeps active-run behavior identical while no longer
        # dropping them when current_run is None.
        if content.startswith("[SKIP]"):
            skip_msg = content[len("[SKIP]"):].strip()
            runs.append({
                "time": "",
                "docker_ts": docker_ts,
                "anime": "",
                "events": [{"type": "skip", "msg": skip_msg}],
            })
            continue
        elif content.startswith("[COMPLETE]"):
            complete_msg = content[len("[COMPLETE]"):].strip()
            runs.append({
                "time": "",
                "docker_ts": docker_ts,
                "anime": "",
                "events": [{"type": "complete", "msg": complete_msg}],
            })
            continue
        elif content.startswith("[THROTTLE]"):
            throttle_msg = content[len("[THROTTLE]"):].strip()
            runs.append({
                "time": "",
                "docker_ts": docker_ts,
                "anime": "",
                "events": [{"type": "throttle", "msg": throttle_msg}],
            })
            continue

        if not current_run:
            # Check for "Erfolgreich eingeloggt" or startup messages
            if "eingeloggt" in content.lower() or "Erfolgreich" in content:
                runs.append({
                    "time": "",
                    "docker_ts": docker_ts,
                    "anime": "",
                    "events": [{"type": "info", "msg": content.strip()}],
                })
            continue

        # Classify log lines
        etype, msg = None, None
        if content.startswith("[DOWNLOAD]"):
            etype, msg = "download", content[len("[DOWNLOAD]"):].strip()
        elif content.startswith("[ERROR]"):
            etype, msg = "error", content[len("[ERROR]"):].strip()
        elif content.startswith("[BATCH]"):
            etype, msg = "batch", content[len("[BATCH]"):].strip()
        elif content.startswith("[INFO]"):
            etype, msg = "info", content[len("[INFO]"):].strip()
        elif content.startswith("Schlafe"):
            current_run["events"].append({
                "type": "sleep",
                "msg": content.strip(),
                "docker_ts": docker_ts,
            })
            runs.append(current_run)
            current_run = None
            continue

        if etype is None:
            # Skip raw API responses, captcha output, etc.
            continue

        # The bot sometimes glues the anime name to the status text
        # (e.g. "Dorohedoro: Staffel 2hat fehlende Episode(n)").
        # Strip the anime name prefix and clean up.
        anime = current_run.get("anime", "")
        if anime and msg.startswith(anime):
            msg = msg[len(anime):].strip()
            if msg:
                msg = msg[0].upper() + msg[1:]

        current_run["events"].append({"type": etype, "msg": msg})

    if current_run:
        runs.append(current_run)

    return runs


def load_run_state():
    """Read the bot's persisted run-state record (written each cycle by
    bot/anibot.py next to ani.json). Returns {} when absent or unreadable, so
    callers fall back to log-tail parsing.

    This is the authoritative source for last_run / next_run timing — immune to
    the 500-line log-tail rollover that drops the German run markers under
    verbose logging and made the dashboard show "No runs yet" / "—" while the
    bot was running."""
    try:
        with open(RUN_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    # ValueError also covers UnicodeDecodeError (a corrupt/non-UTF-8 file):
    # both it and json.JSONDecodeError subclass ValueError, so this degrades
    # a bad file to the empty state instead of a 500, without swallowing an
    # unrelated bug (e.g. a TypeError) as if it were a corrupt file.
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _humanize_eta(next_time, now, delay):
    """Render a next-run ETA string from a target datetime. Mirrors the wording
    of the log-tail next-run math below (kept separate so that delicate,
    TZ-correct block stays untouched). Both datetimes are UTC-naive; `delay` is
    the sleep interval in seconds, used to tell "imminent" from "overdue"."""
    if next_time > now:
        diff = next_time - now
        mins = int(diff.total_seconds() // 60)
        return "~{} min".format(mins) if mins > 0 else "<1 min"
    overdue = now - next_time
    if overdue.total_seconds() <= delay:
        return "any moment"
    over_min = int(overdue.total_seconds() // 60)
    if over_min >= 1440:
        return "overdue ~{}d".format(over_min // 1440)
    if over_min >= 120:
        return "overdue ~{}h".format(over_min // 60)
    return "overdue ~{} min".format(over_min)


def _parse_state_ts(ts):
    """Parse a run-state UTC timestamp ("YYYY-MM-DDTHH:MM:SSZ") to a naive
    datetime, or None if missing/garbage."""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.rstrip("Z").split(".")[0])
    except (ValueError, TypeError):
        return None


# Dates in the feed. Timestamps are stored as naive UTC and only turned into
# local wall time here, at render time, so every time, day header and
# "Today"/"Yesterday" is drawn on one clock: the process's own zone. None means
# exactly that: astimezone() goes through libc, which honours TZ, and compose
# hands the bot the same TZ, so the feed reads like the bot's own log lines. A
# TZ libc cannot resolve falls back to UTC, which is the old output. Tests pin
# the zone so they don't depend on the host's. `now` is always the current UTC
# instant (naive) and is injectable so tests can pin it too.
_display_tz = None


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_local(dt):
    """A UTC datetime (naive means UTC) as naive local wall time. A value the
    platform cannot convert (a garbage year far outside the epoch) stays in UTC
    rather than failing the page."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        dt = dt.astimezone(_display_tz)
    except (OverflowError, OSError, ValueError):
        pass
    return dt.replace(tzinfo=None)


def _local_ts(ts):
    """A run-state UTC timestamp as naive local wall time, or None."""
    dt = _parse_state_ts(ts)
    return _to_local(dt) if dt else None


def format_day(day, now=None):
    """A calendar day as a reader says it: "Today", "Yesterday", else
    "Tue 8 Sep" (plus the year once it is not this year's). ``day`` is a local
    date; ``now`` is the UTC instant that decides which day is today."""
    today = _to_local(now or _utc_now()).date()
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    # %a/%b rather than %-d, which is not portable to Windows' strftime.
    label = "{} {} {}".format(day.strftime("%a"), day.day, day.strftime("%b"))
    if day.year != today.year:
        label += " {}".format(day.year)
    return label


def format_day_time(dt, fmt="%H:%M:%S", now=None):
    """A local wall time with a day qualifier only when it is not today:
    "18:44:05", "Yesterday 18:44:05", "Tue 8 Sep 18:44:05"."""
    label = format_day(dt.date(), now)
    clock = dt.strftime(fmt)
    return clock if label == "Today" else "{} {}".format(label, clock)


def render_run_day(day, now=None):
    """The muted header a run-history day's lines sit under."""
    return '<div class="run-day">{}</div>'.format(escape(format_day(day, now)))


def format_next_run_display(state_last):
    """Next-run ETA string from the persisted run-state record, or "" if it
    cannot be derived."""
    next_time = _parse_state_ts(state_last.get("next_run_ts"))
    if next_time is None:
        return ""
    try:
        delay = int(state_last.get("timedelay") or 0)
    except (ValueError, TypeError):
        delay = 0
    return _humanize_eta(next_time, datetime.now(timezone.utc).replace(tzinfo=None), delay)


def format_last_run_display(state_last, now=None):
    """Last-run line from the persisted run-state record: the cycle's local
    finish time plus a brief count summary (e.g. "19:20:05 &mdash; checked 5/8,
    2 downloaded"). The time carries a day qualifier when the run was not today
    ("Yesterday 19:20:05"), since the bot may only cycle once a day. Returns ""
    when nothing renderable is present, so the caller falls back to the
    log-tail display."""
    fin = _local_ts(state_last.get("finished_ts"))
    hhmmss = format_day_time(fin, now=now) if fin else ""

    counts = state_last.get("counts") or {}
    entries = counts.get("entries")
    checked = counts.get("checked")
    downloaded = counts.get("downloaded")
    parts = []
    if checked is not None and entries is not None:
        parts.append("checked {}/{}".format(checked, entries))
    elif checked is not None:
        parts.append("checked {}".format(checked))
    if downloaded:
        parts.append("{} downloaded".format(downloaded))
    summary = ", ".join(parts)

    if hhmmss and summary:
        return "{} &mdash; {}".format(hhmmss, escape(summary))
    if hhmmss:
        return hhmmss
    return escape(summary) if summary else ""


def build_run_summary(record):
    """Build one concise summary line for a single persisted run-state record.

    Sourced from the run-state counts (log-independent, like the rest of the
    run_state design) rather than the rolling log tail, so the run-history feed
    shows exactly one calm line per cycle instead of every parsed log event.

    Returns plain text like
    "19:40 — checked 2/18 · 16 skipped · 2 downloaded · 1 error". The skip
    breakdown is what stops a bare "checked 2/18" from reading as a mystery: it
    accounts for the entries the cycle passed over. Zero-noise segments are
    omitted, but downloads and errors are always surfaced when present. A cycle
    that did nothing renders as just its time. Returns "" when nothing is
    renderable (garbage record / no time and no counts) so the caller can skip
    it.

    ``skipped`` / ``unavailable`` are optional at read time — an older record, a
    fresh deploy, or a not-yet-redeployed bot writes neither, and such a record
    must render exactly as it did before."""
    if not isinstance(record, dict):
        return ""
    fin = _local_ts(record.get("finished_ts"))
    hhmm = fin.strftime("%H:%M") if fin else ""

    counts = record.get("counts") or {}
    entries = counts.get("entries")
    checked = counts.get("checked")
    downloaded = counts.get("downloaded")
    errors = counts.get("errors")
    skipped = counts.get("skipped")
    unavailable = counts.get("unavailable")

    parts = []
    if entries:
        parts.append("checked {}/{}".format(checked or 0, entries))
    elif checked:
        parts.append("checked {}".format(checked))
    if skipped:
        parts.append("{} skipped".format(skipped))
    if unavailable:
        parts.append("{} unavailable".format(unavailable))
    if downloaded:
        parts.append("{} downloaded".format(downloaded))
    if errors:
        parts.append("{} {}".format(errors, "error" if errors == 1 else "errors"))

    body = " · ".join(parts)
    if hhmm and body:
        return "{} — {}".format(hhmm, body)
    return hhmm or body


def _run_summary_tone(record):
    """Pick the feed tone for a run summary: errors dominate (danger), then
    downloads (ok), else routine (muted) — so a noisy run stands out and a quiet
    cycle stays dim."""
    counts = record.get("counts") or {} if isinstance(record, dict) else {}
    if counts.get("errors"):
        return "danger"
    if counts.get("downloaded"):
        return "ok"
    return "muted"


def get_activity():
    """Get current bot activity summary."""
    status = docker.get_status()
    raw_logs = docker.get_logs(tail=500)
    runs = parse_bot_logs(raw_logs)

    last_run = runs[-1] if runs else None
    last_sleep = None
    if runs:
        for run in reversed(runs):
            for ev in run.get("events", []):
                if ev["type"] == "sleep":
                    last_sleep = run
                    break
            if last_sleep:
                break

    # Estimate next run
    next_run_estimate = ""
    if last_sleep and last_sleep.get("docker_ts"):
        for ev in last_sleep["events"]:
            if ev["type"] == "sleep":
                delay_match = re.search(r"(\d+)\s*Sekunden", ev["msg"])
                if delay_match:
                    delay = int(delay_match.group(1))
                    try:
                        # Anchor on the sleep line's own timestamp; fall back to the
                        # run-start timestamp for older logs that predate it.
                        ts_src = ev.get("docker_ts") or last_sleep["docker_ts"]
                        ts = ts_src.rstrip("Z").split(".")[0]
                        last_time = datetime.fromisoformat(ts)
                        next_time = last_time + timedelta(seconds=delay)
                        now = datetime.now(timezone.utc).replace(tzinfo=None)
                        if next_time > now:
                            diff = next_time - now
                            mins = int(diff.total_seconds() // 60)
                            next_run_estimate = "~{} min".format(mins) if mins > 0 else "<1 min"
                        else:
                            # Past due. Within one sleep interval it's genuinely imminent;
                            # well beyond that the bot has missed its cycle (hung/stopped),
                            # so surface how overdue it is instead of a perpetual "any moment".
                            overdue = now - next_time
                            if overdue.total_seconds() <= delay:
                                next_run_estimate = "any moment"
                            else:
                                over_min = int(overdue.total_seconds() // 60)
                                if over_min >= 1440:
                                    next_run_estimate = "overdue ~{}d".format(over_min // 1440)
                                elif over_min >= 120:
                                    next_run_estimate = "overdue ~{}h".format(over_min // 60)
                                else:
                                    next_run_estimate = "overdue ~{} min".format(over_min)
                    except Exception:
                        pass

    result = {
        "status": status,
        "runs": runs,
        "last_run": last_run,
        "next_run": next_run_estimate,
    }

    # The persisted run-state record (when present) is authoritative for
    # last_run / next_run — it survives the log-tail rollover that the block
    # above is vulnerable to. The log-parsed `runs` still drive the event feed
    # below as a fallback. `run_state` is also surfaced so a sibling run-history
    # UI can render one summary per run from it.
    run_state = load_run_state()
    state_last = run_state.get("last_run") if isinstance(run_state, dict) else None
    if isinstance(state_last, dict):
        last_run_display = format_last_run_display(state_last)
        next_run_display = format_next_run_display(state_last)
        if last_run_display:
            result["last_run_display"] = last_run_display
        if next_run_display:
            result["next_run"] = next_run_display
        result["run_state"] = run_state

    return result


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_ani():
    """Load ani.json under the lock shared with the bot.

    Unlike the old bare-except version, a corrupt file is NOT silently
    swallowed into the empty default here — that was the bug: the next save
    from a POST handler would then persist that empty default over the real
    watchlist. Corrupt files propagate as anistore.CorruptStoreError; callers
    (do_GET/do_POST below) catch it centrally and show an error banner
    instead of rendering/saving over a wiped-looking watchlist."""
    with anistore.locked(ANI_JSON):
        return anistore.load(ANI_JSON, default={"settings": {}, "anime": []})


def save_ani(data):
    with anistore.locked(ANI_JSON):
        anistore.save(ANI_JSON, data)


def update_ani(fn):
    """Read-modify-write ani.json in ONE lock hold.

    A handler doing load_ani() ... mutate ... save_ani(data) acquires and
    releases the lock twice, leaving a window between them where a bot save
    can land and then be silently overwritten by the handler's now-stale
    in-memory copy. fn(data) must be pure local mutation (in place, or by
    returning a replacement dict) — no network/scrape calls inside it, since
    it runs while the lock is held; do any scraping before calling this."""
    return anistore.update(ANI_JSON, fn, default={"settings": {}, "anime": []})


# ---------------------------------------------------------------------------
# Soft run-now / per-entry check-now
# ---------------------------------------------------------------------------

# Guards trigger_run_now's cooldown check + state write as one atomic step —
# under the threaded server, two concurrent /run-now or /check-now POSTs
# (e.g. two browser tabs, or a double-click) could otherwise both read the
# cooldown as elapsed before either one's write lands, both passing a check
# meant to allow only one.
_run_now_lock = threading.Lock()


def _load_run_now_last():
    try:
        with open(RUN_NOW_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    # ValueError also covers UnicodeDecodeError (a corrupt/non-UTF-8 file);
    # see load_run_state's comment for why this is a deliberate widening.
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _write_small_json(path, data):
    """Atomically write a small dashboard-only JSON file (tmp + os.replace).
    Unlike anistore.save, this is never read/written by the bot container, so
    it needs no cross-container permission matching — a fixed 0o644 mode
    (same as save_move_state) is enough."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=d, prefix=".dashboard-")
        os.chmod(tmp_path, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
        return True
    except OSError as e:
        _log.error("[run-now] Failed to persist %s: %s", path, e)
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False


def run_now_cooldown_remaining(now=None):
    """Seconds remaining before another run-now/check-now request is
    allowed, or 0 if the cooldown has elapsed (or none has ever run).

    Tracked in RUN_NOW_STATE_FILE rather than off the run_now trigger
    file's own presence/mtime — the bot deletes that file within a few
    seconds of consuming it (see sleep_until_next_cycle's slice_seconds),
    well before the RUN_NOW_COOLDOWN_SECONDS window is actually up."""
    now = now or datetime.utcnow()
    last = _load_run_now_last().get("last_requested_at")
    if not last:
        return 0
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return 0
    remaining = RUN_NOW_COOLDOWN_SECONDS - (now - last_dt).total_seconds()
    return max(0, int(remaining))


def trigger_run_now(entry_url=None):
    """Queue a run-now (entry_url=None) or per-entry "Check now" request,
    subject to the shared cooldown.

    Writes the RUN_NOW_FILE trigger the bot's inter-cycle sleep wakes early
    on (see bot/anibot.py's sleep_until_next_cycle / consume_run_now_trigger)
    — a soft, non-destructive replacement for restarting the bot container:
    it never kills an in-flight cycle (possibly mid JDownloader hand-off),
    and it doesn't force a browser re-init + re-login.

    For a per-entry request, also sets force_check=True on that watchlist
    entry (cleared by the bot after one scrape — see anibot.resolve_force_check)
    so that entry's skip logic (airdate/complete/skip_until/TVDB) is bypassed
    for this one triggered cycle; the global trigger file is still needed so
    the bot actually wakes up to run that cycle.

    Returns (ok, message)."""
    # The cooldown check and its state write must happen as one atomic step
    # — see _run_now_lock's comment for why (two concurrent callers under
    # the threaded server could otherwise both pass the check).
    with _run_now_lock:
        remaining = run_now_cooldown_remaining()
        if remaining > 0:
            return False, "Cooling down — try again in {}s".format(remaining)

        name = None
        if entry_url:
            outcome = {}

            def _set_force_check(data):
                anime_list = data.get("anime", [])
                _, entry = find_entry_by_url(anime_list, entry_url)
                if entry is None:
                    outcome["result"] = "not_found"
                    return
                entry["force_check"] = True
                outcome["result"] = "ok"
                outcome["name"] = entry.get("name", "?")

            update_ani(_set_force_check)
            if outcome.get("result") != "ok":
                return False, "Entry not found"
            name = outcome.get("name")

        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        if not _write_run_now_trigger(now_iso):
            return False, "Failed to write run-now trigger"
        _write_small_json(RUN_NOW_STATE_FILE, {"last_requested_at": now_iso})

    if name:
        return True, "Check now queued for {} — starts within a few seconds".format(name)
    return True, "Run now queued — starts after the current cycle, within a few seconds"


def _write_run_now_trigger(timestamp):
    """Write RUN_NOW_FILE's content as plain text (an ISO timestamp), not
    JSON — the bot only checks for the file's existence and reads its
    content as an opaque string (anibot.consume_run_now_trigger)."""
    d = os.path.dirname(RUN_NOW_FILE) or "."
    os.makedirs(d, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=d, prefix=".run-now-")
        os.chmod(tmp_path, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(timestamp)
        os.replace(tmp_path, RUN_NOW_FILE)
        return True
    except OSError as e:
        _log.error("[run-now] Failed to write trigger file: %s", e)
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False


def load_prefs():
    defaults = {
        "audio_language": "german",
        "sub_language": "any",
        "min_resolution": 1080,
        "auto_select": True,
    }
    try:
        with open(PREFS_FILE, "r", encoding="utf-8") as f:
            stored = json.load(f)
        # Back-compat: old single `language` pref maps to audio_language.
        if "language" in stored and "audio_language" not in stored:
            stored["audio_language"] = stored["language"]
        stored.pop("language", None)
        defaults.update(stored)
    # ValueError also covers UnicodeDecodeError (a corrupt/non-UTF-8 file);
    # see load_run_state's comment for why this is a deliberate widening.
    except (FileNotFoundError, ValueError):
        pass
    return defaults


def save_prefs(prefs):
    """Write web-prefs.json atomically (tmp + os.replace) so two concurrent
    /save-prefs POSTs (now possible under the threaded server) can't
    interleave their writes into a half-written, corrupt file — the last one
    to finish simply wins, same as before threading."""
    d = os.path.dirname(PREFS_FILE) or "."
    fd, tmp_path = tempfile.mkstemp(dir=d, prefix=".web-prefs-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
        os.replace(tmp_path, PREFS_FILE)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Health panel — JDownloader / TVDB / site login / disk / bot staleness.
#
# The dashboard polls /api/status every 10s (see the refresh() script below),
# so a naive per-poll network probe would hammer JDownloader/TVDB constantly.
# Each network-touching check is memoized in _HEALTH_CACHE for its own TTL;
# the login/staleness checks are free (pure reads of already-loaded state) and
# skip the cache entirely.
# ---------------------------------------------------------------------------

JD_HEALTH_PORT = 9666  # JDownloader's Flashgot/CNL interface (see bot/animeloads.py utils.addToJD)
JD_HEALTH_TIMEOUT = float(os.environ.get("JD_HEALTH_TIMEOUT", "2"))
JD_HEALTH_CACHE_SECONDS = int(os.environ.get("JD_HEALTH_CACHE_SECONDS", "60"))
DISK_HEALTH_CACHE_SECONDS = int(os.environ.get("DISK_HEALTH_CACHE_SECONDS", "60"))
TVDB_HEALTH_CACHE_SECONDS = int(os.environ.get("TVDB_HEALTH_CACHE_SECONDS", "600"))

_health_cache_lock = threading.Lock()
_HEALTH_CACHE = {}  # key -> (time.monotonic() at check, result dict)


def _cached_health(key, ttl, compute):
    """Memoize a health probe for ttl seconds so the 10s dashboard poll never
    re-triggers a network call on every request. `compute` must be side-effect
    free beyond its own probe and must not raise."""
    now = time.monotonic()
    with _health_cache_lock:
        cached = _HEALTH_CACHE.get(key)
        if cached and now - cached[0] < ttl:
            return cached[1]
    result = compute()
    with _health_cache_lock:
        _HEALTH_CACHE[key] = (now, result)
    return result


def check_jdownloader_health():
    """Reachability probe for the configured local JDownloader host, against
    its CNL/Flashgot port — a plain TCP connect, so it never triggers JD's own
    add-link handling. A MyJDownloader-only setup (no local jdhost) has
    nothing to probe here, so it reads as unknown rather than failed."""
    def compute():
        try:
            jdhost = (load_ani().get("settings") or {}).get("jdhost")
        except anistore.CorruptStoreError:
            return {"state": "unknown", "detail": "ani.json unreadable"}
        if not jdhost:
            return {
                "state": "unknown",
                "detail": "No local JDownloader host configured",
                "hint": "Using MyJDownloader? Verify its connection in the MyJDownloader web UI — "
                        "this check only covers a local jdhost.",
            }
        try:
            sock = socket.create_connection((jdhost, JD_HEALTH_PORT), timeout=JD_HEALTH_TIMEOUT)
            sock.close()
            return {"state": "ok", "detail": "JDownloader reachable"}
        except OSError as e:
            return {
                "state": "fail",
                "detail": "JDownloader unreachable: {}".format(e),
                "hint": "Check JDownloader is running with its Click'n'Load interface enabled, "
                        "then reload the dashboard.",
            }
    return _cached_health("jdownloader", JD_HEALTH_CACHE_SECONDS, compute)


def check_tvdb_health():
    """TVDB reachability: verifies the configured API key actually
    authenticates (not just that it's non-empty, which is all `tvdb.available`
    means)."""
    def compute():
        if not tvdb.available:
            return {
                "state": "unknown",
                "detail": "No TVDB API key configured",
                "hint": "Set TVDB_API_KEY in .env, then redeploy, for TVDB-assisted matching.",
            }
        ok, detail = tvdb.check_health()
        if ok:
            return {"state": "ok", "detail": detail}
        return {
            "state": "fail",
            "detail": detail,
            "hint": "Check TVDB_API_KEY is a valid TheTVDB v4 API key, then redeploy.",
        }
    return _cached_health("tvdb", TVDB_HEALTH_CACHE_SECONDS, compute)


def _disk_row(label, path):
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"label": label, "state": "unknown", "detail": "{}: not accessible".format(label)}
    free_gb = usage.free / (1024 ** 3)
    pct_free = (usage.free / usage.total * 100) if usage.total else 0
    if free_gb < 2 or pct_free < 3:
        state = "fail"
    elif free_gb < 10 or pct_free < 8:
        state = "warn"
    else:
        state = "ok"
    return {
        "label": label, "state": state,
        "detail": "{}: {:.1f} GB free ({:.0f}%)".format(label, free_gb, pct_free),
    }


def check_disk_health():
    """Free space on the media + download dirs. Warns well before "full" so
    there's time to react — a JDownloader box that fills up silently stalls
    every download with no per-episode error to explain why."""
    def compute():
        rows = [_disk_row("Media", MEDIA_DIR), _disk_row("Downloads", DOWNLOAD_DIR)]
        order = {"fail": 3, "warn": 2, "unknown": 1, "ok": 0}
        worst = max(rows, key=lambda r: order[r["state"]])
        result = {"state": worst["state"], "detail": " · ".join(r["detail"] for r in rows)}
        if worst["state"] == "fail":
            result["hint"] = "Free up space on the affected volume — downloads will stall until then."
        elif worst["state"] == "warn":
            result["hint"] = "Disk space is getting low — plan to free up space soon."
        return result
    return _cached_health("disk", DISK_HEALTH_CACHE_SECONDS, compute)


def check_login_health(run_state=None):
    """Site login state, as last recorded by the bot at startup
    (bot/anibot.py's write_login_state). Absent entirely on a run_state.json
    that predates this feature, or before the bot's first login attempt."""
    run_state = run_state if run_state is not None else load_run_state()
    login = run_state.get("login") if isinstance(run_state, dict) else None
    if not isinstance(login, dict):
        return {"state": "unknown", "detail": "No login attempt recorded yet"}
    if not login.get("user_configured"):
        return {
            "state": "warn",
            "detail": "Running anonymously (no site credentials configured)",
            "hint": "Set AL_USER and AL_PASS in .env, then redeploy, for full access to multi-episode releases.",
        }
    if login.get("ok"):
        detail = "Logged in"
        if login.get("vip"):
            detail += " (VIP)"
        return {"state": "ok", "detail": detail}
    return {
        "state": "fail",
        "detail": "Login failed: {}".format(login.get("error") or "unknown error"),
        "hint": "Check AL_USER/AL_PASS are correct, then redeploy. Until then the bot runs anonymously "
                "and multi-episode fetches will keep erroring with \"Login required\".",
    }


def check_bot_staleness(run_state=None):
    """Warn when no cycle has finished in well over its own interval — the
    bot container may be hung, crashed, or stuck retrying something."""
    run_state = run_state if run_state is not None else load_run_state()
    last = run_state.get("last_run") if isinstance(run_state, dict) else None
    if not isinstance(last, dict):
        return {"state": "unknown", "detail": "No completed cycle recorded yet"}
    finished = _parse_state_ts(last.get("finished_ts"))
    if finished is None:
        return {"state": "unknown", "detail": "No completed cycle recorded yet"}
    try:
        delay = int(last.get("timedelay") or 0)
    except (TypeError, ValueError):
        delay = 0
    elapsed = (_utc_now() - finished).total_seconds()
    mins = max(int(elapsed // 60), 0)
    threshold = max(delay, 60) * 2
    if delay and elapsed > threshold:
        return {
            "state": "warn",
            "detail": "No cycle finished in {} min (expected every ~{} min)".format(mins, delay // 60),
            "hint": "Check the bot container is running — it may be hung or crash-looping.",
        }
    return {"state": "ok", "detail": "Last cycle finished {} min ago".format(mins)}


def get_health():
    """Assemble every health row. Cheap to call on every /api/status poll —
    each row is either free (login/staleness) or backed by its own cache.

    A single check raising (e.g. an unexpected error reaching a probed
    service) must never blank the whole card or break the 10s poll — it
    degrades that one row to "unknown" and the rest still render."""
    run_state = load_run_state()
    checks = [
        ("Site Login", lambda: check_login_health(run_state)),
        ("JDownloader", check_jdownloader_health),
        ("TVDB", check_tvdb_health),
        ("Disk Space", check_disk_health),
        ("Bot Cycles", lambda: check_bot_staleness(run_state)),
    ]
    rows = []
    for label, check in checks:
        try:
            rows.append((label, check()))
        except Exception as e:
            rows.append((label, {"state": "unknown", "detail": "Check failed: {}".format(e)}))
    return rows


_HEALTH_BADGE = {"ok": "badge-ok", "warn": "badge-warn", "fail": "badge-danger", "unknown": "badge-neutral"}
_HEALTH_LABEL = {"ok": "OK", "warn": "Warn", "fail": "Fail", "unknown": "Unknown"}


def render_health_card():
    """Render the Health panel: one row per check, a status badge, and (for
    anything not ok) a one-line actionable hint. Never renders a credential,
    username, or raw host/port — only booleans, counts, and generic detail
    strings."""
    rows = []
    for label, result in get_health():
        state = result.get("state", "unknown")
        badge_cls = _HEALTH_BADGE.get(state, "badge-neutral")
        badge_label = _HEALTH_LABEL.get(state, "Unknown")
        hint_html = ""
        if state != "ok" and result.get("hint"):
            hint_html = '<div class="hint">{}</div>'.format(escape(result["hint"]))
        rows.append(
            '<div class="health-row">'
            '<div class="health-row-main">'
            '<span class="health-label">{label}</span> '
            '<span class="badge {badge_cls}">{badge_label}</span>'
            '<span class="health-detail">{detail}</span>'
            '</div>{hint}'
            '</div>'.format(
                label=escape(label), badge_cls=badge_cls, badge_label=badge_label,
                detail=escape(result.get("detail", "")), hint=hint_html,
            )
        )
    return '<div class="health-grid">{}</div>'.format("".join(rows))


# Alias-tolerant language matching. Site labels are German ("Deutsch",
# "Japanisch", "Englisch"); each pref maps to a set of substrings to look for.
LANG_ALIASES = {
    "german": ("deutsch", "german", "ger"),
    "japanese": ("japan", "jap"),
    "english": ("englisch", "english", "eng"),
}


def lang_in_list(pref_lang, langs):
    """True if pref_lang matches any entry in langs (a list of language labels).

    pref_lang is one of german|japanese|english|any. "any" (and any unknown
    value) always matches. Matching is alias-tolerant substring membership so
    German site labels like "Japanisch" match the japanese pref.
    """
    if pref_lang == "any" or pref_lang not in LANG_ALIASES:
        return True
    aliases = LANG_ALIASES[pref_lang]
    return any(
        any(alias in (entry or "").lower() for alias in aliases)
        for entry in (langs or [])
    )


def pick_best_release(releases, prefs):
    """Pick the best matching release, or None if nothing matches (STRICT).

    A release matches iff audio_language is "any" or its alias is in the
    release's dubs list, AND sub_language is "any" or its alias is in the subs
    list. min_resolution is a hard filter. Matches rank by resolution+episodes.
    If no release matches, return None — the caller must not auto-select.
    """
    min_res = prefs.get("min_resolution", 1080)
    audio_pref = prefs.get("audio_language", "german")
    sub_pref = prefs.get("sub_language", "any")

    scored = []
    for rel in releases:
        try:
            res = int(rel.get("resolution", 0) or 0)
        except (ValueError, TypeError):
            res = 0
        try:
            eps = int(rel.get("episodes", 0) or 0)
        except (ValueError, TypeError):
            eps = 0

        if res < min_res:
            continue
        if not lang_in_list(audio_pref, rel.get("dubs", [])):
            continue
        if not lang_in_list(sub_pref, rel.get("subs", [])):
            continue

        scored.append((res + eps, rel))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def search_anime(query):
    if not AL_AVAILABLE:
        return None, "animeloads library not available"
    try:
        al = AL(browser=AL.FIREFOX)
        results = al.search(query)
        out = []
        for r in results:
            try:
                out.append({
                    "name": r.getName(),
                    "url": r.getUrl(),
                    "type": r.getTyp(),
                    "episodes": "{}/{}".format(r.getCurrentEpisodeCount(), r.getMaxEpisodeCount()),
                    "genre": r.getGenre(),
                    "dubs": ", ".join(r.getDubLang() or []),
                    "subs": ", ".join(r.getSubLang() or []),
                })
            except Exception:
                _log.warning("search_anime: failed to read a search result", exc_info=True)
        return out, None
    except Exception as e:
        return None, str(e)


def get_releases(url):
    if not AL_AVAILABLE:
        return None, "animeloads library not available"
    anime = None
    _t_total = time.time()
    _log.debug("get_releases: starting for %s", url)
    try:
        _t0 = time.time()
        al = AL(browser=AL.FIREFOX)
        _log.debug("get_releases: AL() init took %.1fs", time.time() - _t0)
        _t1 = time.time()
        anime = al.getAnime(url)
        _log.debug("get_releases: getAnime() took %.1fs", time.time() - _t1)
        releases = anime.getReleases()
        out = []
        for rel in releases:
            try:
                out.append({
                    "id": rel.getID(),
                    "resolution": rel.getResolution(),
                    "dubs": rel.getDubs(),
                    "subs": rel.getSubs(),
                    "episodes": rel.getEpisodeCount(),
                    "size_mb": rel.getSize(),
                    "group": rel.getGroup(),
                })
            except Exception:
                pass
        _log.debug("get_releases: built %d releases in %.1fs (total %.1fs)",
                   len(out), time.time() - _t1, time.time() - _t_total)
        display = (getattr(anime, 'gerName', '') or
                   getattr(anime, 'engName', '') or
                   getattr(anime, 'japName', ''))
        return {
            "name": anime.getName(),
            "url": anime.getURL(),
            "releases": out,
            "media_type": getattr(anime, 'type', '') or "series",
            "year": getattr(anime, 'year', 0) or None,
            "display_title": display or anime.getName(),
        }, None
    except Exception as e:
        _log.debug("get_releases: FAILED after %.1fs: %s", time.time() - _t_total, e)
        return None, str(e)
    finally:
        if anime and hasattr(anime, '_driver') and anime._driver:
            try:
                anime._driver.quit()
            except Exception:
                pass


_MEDIA_TYPES = ("series", "movie")
_MAX_SANE_EPISODE_COUNT = 100000


def _resolve_release_selection(url, params):
    """Validate the release_id / media_type / episode-count carried as
    hidden fields from render_releases through the release -> TVDB ->
    season forms, so /add-release and /tvdb-seasons never need their own
    re-scrape to re-derive or double check them.

    These are user-controlled POST fields, so they're trusted only when
    they check out: release_id must be a member of the ``release_ids`` set
    render_releases put on the page (every id actually offered for this
    anime), and episodes must parse as a small non-negative int. Whenever
    that can't be confirmed — a missing/malformed hidden field (an old page
    from before this existed, or one stripped in transit) as much as an
    outright tampered value — this falls back to exactly one fresh scrape
    to establish the truth, rather than trusting or guessing at the posted
    value either way.

    Returns (release_id, media_type, episodes). release_id is "" when
    nothing was selected, or when even a fresh scrape can't corroborate the
    posted one (it genuinely isn't one of this anime's releases) — callers
    must treat that as a rejected selection, not silently substitute a
    default.
    """
    release_id = params.get("release_id", "")
    media_type = params.get("media_type", "")
    episodes_raw = params.get("episodes", "")

    if not release_id:
        return "", (media_type if media_type in _MEDIA_TYPES else "series"), 0

    valid_ids = {tok for tok in
                 (t.strip() for t in params.get("release_ids", "").split(","))
                 if tok}
    try:
        episodes = int(episodes_raw)
    except ValueError:
        episodes = -1
    episodes_ok = 0 <= episodes <= _MAX_SANE_EPISODE_COUNT

    if release_id in valid_ids and media_type in _MEDIA_TYPES and episodes_ok:
        return release_id, media_type, episodes

    # Something didn't check out — re-derive from a fresh scrape instead of
    # trusting (or blindly rejecting) the posted value.
    info, _err = get_releases(url)
    if not info:
        return "", (media_type if media_type in _MEDIA_TYPES else "series"), 0
    resolved_media_type = info.get("media_type", "series") or "series"
    for rel in info.get("releases", []):
        if str(rel.get("id")) == release_id:
            return release_id, resolved_media_type, rel.get("episodes", 0) or 0
    return "", resolved_media_type, 0


# ---------------------------------------------------------------------------
# Move-completed logic
# ---------------------------------------------------------------------------

_SEASON_EP_RE = re.compile(r'(.*?)[._][Ss](\d+)[Ee](\d+)')
_VIDEO_EXTS = {'.mkv', '.mp4', '.avi'}
_ARCHIVE_RE = re.compile(r'\.(rar|r\d\d)$')
# External subtitle sidecars: moved alongside their video, never deleted.
_SUBTITLE_EXTS = {'.srt', '.ass', '.ssa', '.sup', '.vtt'}
# Known-junk leftovers safe to delete after a move (release nfo/readme,
# NZB/torrent leftovers, poster thumbnails). Archives are handled separately,
# earlier in the cycle. Anything else (an unrecognized extension) is left in
# place rather than guessed at.
_JUNK_EXTS = {'.nfo', '.txt', '.url', '.jpg', '.jpeg', '.png'}


def parse_season_episode(filename):
    """Extract (name_part, season, episode) from a filename like 'Anime.Name.S01E05.mkv'."""
    m = _SEASON_EP_RE.match(filename)
    if not m:
        return None
    name_part = m.group(1)
    season = int(m.group(2))
    episode = int(m.group(3))
    return name_part, season, episode


def _rename_season_episode(filename, new_season, new_episode):
    """Rewrite the SxxExx token in filename to new_season/new_episode.

    Only the matched season/episode digit spans are touched (via the
    regex match's own offsets), so the rest of the filename — including
    any other S##/E## substring inside the title — is left untouched.
    Each number's original zero-padding width is preserved, with a
    minimum of 2 digits (e.g. 'E001' stays 3-wide; a bare 'S1' becomes
    'S02'-width to match the 'S{:02d}' season folder convention).
    Returns filename unchanged if it doesn't match _SEASON_EP_RE.
    """
    m = _SEASON_EP_RE.match(filename)
    if not m:
        return filename
    season_start, season_end = m.span(2)
    episode_start, episode_end = m.span(3)
    season_width = max(season_end - season_start, 2)
    episode_width = max(episode_end - episode_start, 2)
    new_season_str = '{:0{width}d}'.format(new_season, width=season_width)
    new_episode_str = '{:0{width}d}'.format(new_episode, width=episode_width)
    return (
        filename[:season_start] + new_season_str +
        filename[season_end:episode_start] + new_episode_str +
        filename[episode_end:]
    )


def _lookup_anime_entries():
    """Load the anime list once per cycle for move lookups."""
    return load_ani().get("anime", [])


def _entry_to_match(entry, folder_name):
    """Build a match dict from an ani.json entry.

    Runs folder_name through _safe_folder_segment so a legacy/hand-edited
    customPackage saved before save-time sanitization existed (e.g. a stored
    "a/b" or "../../etc") maps to the same flat, contained folder a fresh
    save would now produce, instead of creating nested or escaping
    directories in the mover. A customPackage that's already a safe single
    segment — including one with ``:``/``?``/etc, common in anime-loads
    release names — passes through unchanged, so it keeps matching its
    existing library folder.
    """
    return {
        "folder_name": _safe_folder_segment(folder_name),
        "tvdb_season": entry.get("tvdb_season"),
        "episode_offset": entry.get("episode_offset", 0) or 0,
        "media_type": entry.get("media_type", "series"),
        "year": entry.get("year"),
        "display_title": entry.get("display_title") or entry.get("name", ""),
        "matched": True,
    }


def match_anime_entry(parsed_name, dir_basename, anime_list, parsed_season=None):
    """Match a download to an ani.json entry and return its settings.

    Tries matching by: (1) dir_basename against customPackage, (2) dir_basename
    against name, (3) parsed filename name against name. Returns a dict with
    folder_name, tvdb_season, episode_offset, media_type, year, display_title.

    `parsed_season` (when provided by the caller after SxxExx parsing) is used
    as a tiebreaker: when multiple entries share the same generic prefix, the
    one whose `tvdb_season` matches the file's parsed season wins.
    """
    dir_lower = dir_basename.lower()
    parsed_lower = parsed_name.lower()

    # Try download_folder_pattern first — auto-derived by the bot from the actual
    # release filename, so matches the JD-created folder even when JD ignores
    # customPackage (which is the user's chosen Plex output folder, not the DL folder).
    # Pick the entry whose pattern shares the longest leading-token run with the
    # dir name, so two entries about the same series (e.g. S01 + S03 of one show)
    # don't both win on the generic prefix.
    candidates = []
    for entry in anime_list:
        pattern = entry.get("download_folder_pattern", "")
        if not pattern:
            continue
        score = _token_prefix_score(pattern, dir_basename)
        if score >= 3:
            candidates.append((score, entry))
    if candidates:
        max_score = max(s for s, _ in candidates)
        top = [e for s, e in candidates if s == max_score]
        # Tiebreak by parsed season: when the JD folder name carries no season
        # token (e.g. user named both seasons "Mob Psycho 100"), the leading-
        # prefix score ties across entries. The filename's SxxExx is still
        # authoritative — prefer the entry whose tvdb_season matches.
        if parsed_season is not None and len(top) > 1:
            season_hits = [e for e in top if e.get("tvdb_season") == parsed_season]
            if season_hits:
                top = season_hits
        chosen = top[0]
        return _entry_to_match(chosen, chosen.get("customPackage", ""))

    # Try matching download dir against customPackage (legacy fallback for entries
    # without a download_folder_pattern, where the user happened to pick a name
    # that JD also used for the folder).
    for entry in anime_list:
        cp = entry.get("customPackage", "")
        if cp and cp.lower() in dir_lower:
            return _entry_to_match(entry, cp)

    # Try matching download dir against entry name
    for entry in anime_list:
        name = entry.get("name", "")
        if name and name.lower() in dir_lower:
            return _entry_to_match(entry, entry.get("customPackage", name))

    # Try matching parsed filename name against entry name
    for entry in anime_list:
        if entry.get("name", "").lower() == parsed_lower:
            return _entry_to_match(entry, entry.get("customPackage", entry["name"]))

    return {
        "folder_name": parsed_name,
        "tvdb_season": None,
        "episode_offset": 0,
        "media_type": "series",
        "year": None,
        "display_title": parsed_name,
        "matched": False,
    }


def _sanitize_folder(name):
    """Strip characters that are illegal in folder names on common filesystems."""
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


def _safe_folder_segment(name):
    """Make ``name`` safe to use as a single path segment under the media dir.

    Unlike ``_sanitize_folder`` (Plex movie naming — deletes every
    Windows-illegal character), this only removes what could ever let a
    stored folder name escape or nest: path separators (``/`` ``\\``), NUL
    and other control characters. It deliberately LEAVES ``: ? * " < > |``
    alone — those are legal on the Linux filesystem the library actually
    lives on, and anime-loads release names routinely contain them
    (``Re:ZERO -Starting Life in Another World-``, `` ...Girls in a
    Dungeon?``); stripping them would silently rename an existing library
    folder and split it in two. Separators are deleted rather than replaced,
    so what's left is always a single path segment — that's what keeps a
    traversal attempt (``../../etc``) from ever containing another separator
    to traverse with. The one case that doesn't neutralize is a segment
    that's ALL dots (``.`` or ``..``), which is still a traversal segment on
    its own — reject those (and anything that sanitizes to nothing), same as
    other invalid input.
    """
    cleaned = re.sub(r'[/\\\x00-\x1f]', '', name).strip()
    if re.fullmatch(r'\.+', cleaned):
        return ""
    return cleaned


_TOKEN_SPLIT_RE = re.compile(r'[._\s\-]+')


def _tokenize(s):
    return [t for t in _TOKEN_SPLIT_RE.split((s or '').lower()) if t]


def _token_prefix_score(pattern, dir_name):
    """Count how many leading tokens of `pattern` appear in `dir_name` (consecutively).

    Used to pick the most specific match when several entries share a generic
    prefix (e.g. multiple seasons of one series). Stops at the first missing token
    so a non-matching middle token doesn't get rescued by a later coincidence.
    """
    p_tokens = _tokenize(pattern)
    d_lower = (dir_name or '').lower()
    score = 0
    for t in p_tokens:
        if t in d_lower:
            score += 1
        else:
            break
    return score


def _movie_target_name(display_title, year):
    """Plex movie naming: 'Title (Year)' if year known, else 'Title'."""
    base = _sanitize_folder(display_title) or "Unknown"
    if year:
        return "{} ({})".format(base, year)
    return base


def _is_within_media_dir(target_dir, base_dir):
    """True if target_dir resolves to somewhere inside base_dir.

    Last-line defense for the mover: name sanitization should already keep
    every target inside base_dir, but this catches anything that slips
    through (a legacy entry saved before sanitization existed, an unforeseen
    edge case) before a single file gets moved.
    """
    base = os.path.realpath(base_dir)
    real = os.path.realpath(target_dir)
    try:
        return os.path.commonpath([base, real]) == base
    except ValueError:
        # commonpath raises when the paths don't share a root (e.g. different
        # drives on Windows) — definitely not contained.
        return False


def find_existing_media_folder(anime_name):
    """Find an existing media folder by case-insensitive match.

    Returns the existing folder name (preserving casing) or empty string.
    """
    target_lower = anime_name.lower()
    try:
        for name in os.listdir(MEDIA_DIR):
            if os.path.isdir(os.path.join(MEDIA_DIR, name)) and name.lower() == target_lower:
                return name
    except FileNotFoundError:
        pass
    return ""


def _find_files(directory, predicate):
    """Walk directory recursively, returning paths where predicate(filename) is true."""
    results = []
    for root, _dirs, files in os.walk(directory):
        for f in files:
            if predicate(f):
                results.append(os.path.join(root, f))
    return results


def _stuck_key(rel_path, reason):
    return "{}::{}".format(rel_path, reason)


def _stuck_touch(rel_path, reason, entry_name, msg):
    """Record (or refresh) a stuck item, keyed by its path under DOWNLOAD_DIR
    and the reason it's stuck. Returns (is_new, ignored, move_anyway) —
    move_anyway is a one-shot flag consumed here, so clicking "Move anyway"
    only applies to the very next cycle."""
    key = _stuck_key(rel_path, reason)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    with _move_lock:
        rec = _stuck_items.get(key)
        is_new = rec is None
        if is_new:
            rec = {
                "key": key,
                "reason": reason,
                "dir": entry_name,
                "path": rel_path,
                "ignored": False,
                "first_seen": now_iso,
            }
            _stuck_items[key] = rec
        rec["msg"] = msg
        rec["last_seen"] = now_iso
        move_anyway = rec.pop("move_anyway", False)
        ignored = rec.get("ignored", False)
    return is_new, ignored, move_anyway


def _stuck_resolve(rel_path, reason):
    """Drop a stuck record once it's been resolved by the mover itself
    (e.g. a "Move anyway" that succeeded)."""
    key = _stuck_key(rel_path, reason)
    with _move_lock:
        _stuck_items.pop(key, None)


def _prune_stuck_items():
    """Drop stuck records whose file no longer exists under DOWNLOAD_DIR —
    resolved some other way (a manual delete, a renamed file)."""
    with _move_lock:
        stale = [k for k, rec in _stuck_items.items()
                 if not os.path.exists(os.path.join(DOWNLOAD_DIR, rec["path"]))]
        for k in stale:
            del _stuck_items[k]


def _safe_download_path(rel_path):
    """Resolve a stuck item's stored relative path to an absolute path,
    refusing to return anything outside DOWNLOAD_DIR (defense in depth: the
    path is one this process computed itself via os.path.relpath, but any
    web action that deletes a file confirms containment first)."""
    root = os.path.realpath(DOWNLOAD_DIR)
    candidate = os.path.realpath(os.path.join(DOWNLOAD_DIR, rel_path))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate


def stuck_ignore(key):
    """Mark a stuck item ignored: it stops being surfaced/re-logged each
    cycle, but the file itself is left untouched in the download dir."""
    with _move_lock:
        rec = _stuck_items.get(key)
        if rec is None:
            return None
        rec["ignored"] = True
        msg = rec.get("msg", key)
    save_move_state()
    return msg


def stuck_delete_download(key):
    """Delete the downloaded copy behind an 'already exists' stuck item.
    Refuses anything not confined to DOWNLOAD_DIR, and any reason other than
    'exists' (this is a destructive action, not a general-purpose delete)."""
    with _move_lock:
        rec = _stuck_items.get(key)
    if rec is None or rec.get("reason") != "exists":
        return None
    path = _safe_download_path(rec["path"])
    if path is None:
        return None
    try:
        os.unlink(path)
    except OSError as e:
        return "error:{}".format(e)
    with _move_lock:
        _stuck_items.pop(key, None)
    save_move_state()
    return rec.get("msg", key)


def stuck_move_anyway(key):
    """Flag an 'unmatched' stuck item so the next move cycle moves it using
    the parsed (auto-created folder) name instead of skipping it."""
    with _move_lock:
        rec = _stuck_items.get(key)
        if rec is None or rec.get("reason") != "unmatched":
            return None
        rec["move_anyway"] = True
        msg = rec.get("msg", key)
    save_move_state()
    _move_trigger.set()
    return msg


def _move_subtitles(dir_path, video_stem, target_dir, new_stem):
    """Move subtitle sidecars sharing the video's base filename alongside it
    in target_dir, keeping any language suffix (e.g. '.en') and applying the
    same rename the video itself just got."""
    stem_lower = video_stem.lower()
    for sub_path in _find_files(dir_path, lambda f: os.path.splitext(f)[1].lower() in _SUBTITLE_EXTS):
        sub_name = os.path.basename(sub_path)
        sub_stem, ext = os.path.splitext(sub_name)
        if not sub_stem.lower().startswith(stem_lower):
            continue
        # The stem match must land on a whole-name boundary — otherwise
        # "Show.S01E1" (a prefix of "Show.S01E10") would wrongly claim
        # "Show.S01E10.en.srt" as its own subtitle.
        suffix = sub_stem[len(video_stem):]
        if suffix and not suffix.startswith('.'):
            continue
        dest_path = os.path.join(target_dir, new_stem + suffix + ext)
        if os.path.exists(dest_path):
            continue
        try:
            shutil.move(sub_path, dest_path)
        except OSError:
            pass


def run_move_cycle():
    """Scan download directory and move completed anime to media library.

    Returns a list of event dicts for the history log.
    """
    events = []

    if not os.path.isdir(DOWNLOAD_DIR):
        return events

    _prune_stuck_items()

    anime_list = _lookup_anime_entries()
    now = time.time()
    age_threshold = now - MIN_AGE_MINUTES * 60

    for entry_name in os.listdir(DOWNLOAD_DIR):
        dir_path = os.path.join(DOWNLOAD_DIR, entry_name)
        if not os.path.isdir(dir_path):
            continue

        # --- Safety checks ---

        # Skip if any file was modified recently
        recent = False
        for root, _dirs, files in os.walk(dir_path):
            for f in files:
                fpath = os.path.join(root, f)
                try:
                    if os.path.getmtime(fpath) > age_threshold:
                        recent = True
                        break
                except OSError:
                    pass
            if recent:
                break
        if recent:
            events.append({"type": "wait", "msg": "{} \u2014 files still being modified".format(entry_name)})
            continue

        # Skip if partial downloads exist
        parts = _find_files(dir_path, lambda f: f.endswith('.part'))
        if parts:
            events.append({"type": "wait", "msg": "{} \u2014 incomplete downloads (.part files)".format(entry_name)})
            continue

        # Find video files
        video_files = _find_files(dir_path, lambda f: os.path.splitext(f)[1].lower() in _VIDEO_EXTS)

        if not video_files:
            archives = _find_files(dir_path, lambda f: _ARCHIVE_RE.search(f))
            if archives:
                events.append({"type": "wait", "msg": "{} \u2014 archives present, no extracted video yet".format(entry_name)})
            continue

        # --- Clean up archives alongside extracted videos ---
        archives = _find_files(dir_path, lambda f: _ARCHIVE_RE.search(f))
        if archives:
            for arc in archives:
                try:
                    os.unlink(arc)
                except OSError:
                    pass
            events.append({"type": "cleanup", "msg": "Cleaned {} archive(s) from {}".format(len(archives), entry_name)})

        # Resolve the download directory to an ani.json entry once per directory.
        # Used to decide between series (Plex AnimeName/SXX/) and movie routing.
        dir_match = match_anime_entry("", entry_name, anime_list)
        if dir_match["media_type"] == "movie":
            # Prefer customPackage (user-chosen Plex folder); fall back to display_title.
            movie_folder_base = dir_match["folder_name"] or dir_match["display_title"]
            movie_folder = _movie_target_name(movie_folder_base, dir_match["year"])
            target_dir = os.path.join(MOVIE_MEDIA_DIR, movie_folder)
            if not _is_within_media_dir(target_dir, MOVIE_MEDIA_DIR):
                msg = "{} — unsafe folder name, refusing to move outside the media library".format(entry_name)
                for filepath in video_files:
                    rel_path = os.path.relpath(filepath, DOWNLOAD_DIR)
                    is_new, ignored, _ = _stuck_touch(rel_path, "unsafe_folder", entry_name, msg)
                    if is_new and not ignored:
                        events.append({"type": "error", "msg": msg})
                continue
            for filepath in video_files:
                src_name = os.path.basename(filepath)
                ext = os.path.splitext(src_name)[1].lower()
                # Single-file movie: rename to match the folder for Plex.
                # If a release ships multiple video files (rare), keep the
                # original filename for the extras to avoid clobbering.
                if len(video_files) == 1:
                    dest_name = movie_folder + ext
                else:
                    dest_name = src_name
                target_path = os.path.join(target_dir, dest_name)
                rel_path = os.path.relpath(filepath, DOWNLOAD_DIR)
                if os.path.exists(target_path):
                    msg = "{} \u2014 already exists".format(dest_name)
                    is_new, ignored, _ = _stuck_touch(rel_path, "exists", entry_name, msg)
                    if is_new and not ignored:
                        events.append({"type": "skip", "msg": msg})
                    continue
                try:
                    os.makedirs(target_dir, exist_ok=True)
                    shutil.move(filepath, target_path)
                    video_stem = os.path.splitext(src_name)[0]
                    new_stem = os.path.splitext(dest_name)[0]
                    _move_subtitles(dir_path, video_stem, target_dir, new_stem)
                    events.append({"type": "moved", "msg": "{} \u2192 {}/{}".format(src_name, movie_folder, dest_name)})
                    _stuck_resolve(rel_path, "exists")
                except Exception as e:
                    events.append({"type": "error", "msg": "Failed to move {}: {}".format(src_name, e)})

        else:
            # --- Series path: parse SxxExx per video and route into AnimeName/SXX/ ---
            for filepath in video_files:
                orig_filename = os.path.basename(filepath)
                filename = orig_filename
                rel_path = os.path.relpath(filepath, DOWNLOAD_DIR)
                parsed = parse_season_episode(filename)
                if not parsed:
                    msg = "Cannot parse season/episode: {}".format(filename)
                    is_new, ignored, _ = _stuck_touch(rel_path, "parse", entry_name, msg)
                    if is_new and not ignored:
                        events.append({"type": "error", "msg": msg})
                    continue

                name_part, season, episode = parsed

                # Convert dots/underscores to spaces for matching
                parsed_name = name_part.replace('.', ' ').replace('_', ' ')

                # Match to ani.json entry — gets folder name, TVDB season, and episode offset.
                # Pass parsed season so multiple entries sharing one generic dir prefix
                # (e.g. S01/S02/S03 of the same show) are tiebroken by tvdb_season.
                match = match_anime_entry(parsed_name, entry_name, anime_list, parsed_season=season)

                # Check for existing folder in media library (case-insensitive).
                # An unmatched download whose parsed name already has a folder
                # in the library (a show removed from the watchlist, a manual
                # JDownloader add of something already in Plex) still files
                # normally — the stuck/unmatched path is only for a download
                # that would otherwise SILENTLY CREATE a brand-new folder.
                existing = find_existing_media_folder(match["folder_name"])

                move_anyway = False
                if not match.get("matched", True) and not existing:
                    msg = "{} — no watchlist match (would create '{}')".format(
                        filename, match["folder_name"])
                    is_new, ignored, move_anyway = _stuck_touch(rel_path, "unmatched", entry_name, msg)
                    if not move_anyway:
                        if is_new and not ignored:
                            events.append({"type": "error", "msg": msg})
                        continue

                anime_name = existing or match["folder_name"]

                orig_season, orig_episode = season, episode

                # TVDB season override
                if match["tvdb_season"] is not None:
                    season = match["tvdb_season"]

                # TVDB episode offset
                ep_offset = match["episode_offset"]
                if ep_offset:
                    episode += ep_offset

                # Rebuild the SxxExx token in the filename itself whenever the
                # season or episode was overridden — Plex's scanner reads
                # SxxExx from the filename, not just the folder, so leaving
                # a stale S01 token behind would still file it under season 1.
                if season != orig_season or episode != orig_episode:
                    filename = _rename_season_episode(filename, season, episode)

                season_dir = 'S{:02d}'.format(season)
                target_dir = os.path.join(MEDIA_DIR, anime_name, season_dir)
                target_path = os.path.join(target_dir, filename)

                if not _is_within_media_dir(target_dir, MEDIA_DIR):
                    msg = "{} — unsafe folder name, refusing to move outside the media library".format(filename)
                    is_new, ignored, _ = _stuck_touch(rel_path, "unsafe_folder", entry_name, msg)
                    if is_new and not ignored:
                        events.append({"type": "error", "msg": msg})
                    continue

                if os.path.exists(target_path):
                    msg = "{} \u2014 already exists".format(filename)
                    is_new, ignored, _ = _stuck_touch(rel_path, "exists", entry_name, msg)
                    if is_new and not ignored:
                        events.append({"type": "skip", "msg": msg})
                    continue

                try:
                    os.makedirs(target_dir, exist_ok=True)
                    shutil.move(filepath, target_path)
                    video_stem = os.path.splitext(orig_filename)[0]
                    new_stem = os.path.splitext(filename)[0]
                    _move_subtitles(dir_path, video_stem, target_dir, new_stem)
                    dest_short = "{}/{}".format(anime_name, season_dir)
                    events.append({"type": "moved", "msg": "{} \u2192 {}".format(filename, dest_short)})
                    _stuck_resolve(rel_path, "exists")
                    _stuck_resolve(rel_path, "unmatched")
                except Exception as e:
                    events.append({"type": "error", "msg": "Failed to move {}: {}".format(filename, e)})

        # --- Cleanup download directory ---
        # Delete only known junk (release nfo/readme, thumbnails, ...); leave
        # subtitles (already moved above, if matched) and any unrecognized
        # extension in place rather than destroying something we don't know.
        for root, _dirs, files in os.walk(dir_path):
            for f in files:
                if os.path.splitext(f)[1].lower() in _JUNK_EXTS:
                    try:
                        os.unlink(os.path.join(root, f))
                    except OSError:
                        pass
        # Remove empty directories bottom-up
        for root, dirs, files in os.walk(dir_path, topdown=False):
            if not files and not dirs:
                try:
                    os.rmdir(root)
                except OSError:
                    pass

    return events


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Anime-Loads Dashboard</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23161922'/%3E%3Cpath d='M16 6v11m0 0l-4.5-4.5M16 17l4.5-4.5' fill='none' stroke='%237985e0' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'/%3E%3Cpath d='M9 23.5h14' stroke='%237985e0' stroke-width='2.4' stroke-linecap='round'/%3E%3C/svg%3E">
<style>
  /* ---- Design tokens: one place for color, spacing, type ---- */
  :root {
    /* cool-tinted neutral surfaces (never pure gray) */
    --bg: #0d0f14;
    --surface: #161922;
    --surface-2: #1e222d;
    --surface-3: #11141b;
    --border: #272c39;
    --border-light: #313847;

    --text: #e4e7ee;
    --text-heading: #f3f5fa;
    --text-muted: #9aa3b2;
    --text-faint: #828ca0;  /* AA 4.5:1: 5.19 on surface, 5.67 on bg, 4.70 on surface-2 */

    /* one disciplined accent (muted indigo) — links / interactive only */
    --accent: #7985e0;
    --accent-hover: #9aa4ef;
    --accent-bg: #5b66c9;
    --accent-bg-hover: #6c77d6;

    /* semantic tones, desaturated for dark surfaces */
    --ok-bg: #18301f;       --ok-text: #8fd0a6;
    --warn-bg: #33291a;     --warn-text: #e0bd86;
    --danger-bg: #361f23;   --danger-text: #e79aa0;
    --neutral-bg: #252a36;  --neutral-text: #aab3c2;
    --accent-soft-bg: #232847; --accent-soft-text: #abb3f2;

    --danger-btn: #b3433f;  --danger-btn-hover: #c24f4a;
    --success-btn: #3f7d4a; --success-btn-hover: #4a8c55;
    --warning-btn: #a85d33; --warning-btn-hover: #ac5f35; /* desaturated caution tone — AA 4.5:1 white (4.91 base / 4.73 hover) */

    /* 4pt spacing scale */
    --s1: 4px; --s2: 8px; --s3: 12px; --s4: 16px; --s5: 24px; --s6: 32px; --s7: 48px;

    /* modular type scale (base 16px, ratio ~1.25) */
    --fs-xs: 0.8rem; --fs-sm: 0.9rem; --fs-base: 1rem;
    --fs-lg: 1.125rem; --fs-h2: 1.25rem; --fs-h1: 1.6rem;

    --radius: 8px; --radius-sm: 6px;
    --tr: 0.12s ease;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); padding: var(--s5) var(--s4); max-width: 900px; margin: 0 auto; line-height: 1.55; }
  h1 { color: var(--text-heading); margin-bottom: var(--s5); font-size: var(--fs-h1); font-weight: 650; letter-spacing: -0.01em; line-height: 1.2; }
  h2 { color: var(--text-heading); margin: var(--s5) 0 var(--s3); font-size: var(--fs-h2); font-weight: 600; line-height: 1.2; }
  h3 { color: var(--text-heading); font-size: var(--fs-lg); font-weight: 600; }
  a { color: var(--accent); }

  .muted { color: var(--text-muted); }
  .faint { color: var(--text-faint); }
  .hint { color: var(--text-muted); font-size: var(--fs-xs); }

  .card { background: var(--surface); border-radius: var(--radius); padding: var(--s4); margin-bottom: var(--s3); border: 1px solid var(--border); }
  .card-accent { border-color: var(--accent); }
  .card-selected { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
  .anime-name { font-weight: 600; font-size: var(--fs-lg); color: var(--text-heading); }
  .anime-meta { color: var(--text-muted); font-size: var(--fs-xs); margin-top: var(--s2); display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .anime-url { color: var(--accent); font-size: var(--fs-xs); word-break: break-all; }

  .btn { display: inline-flex; align-items: center; justify-content: center; min-height: 38px; padding: 9px 16px; border-radius: var(--radius-sm); border: none; cursor: pointer; font-size: var(--fs-sm); font-weight: 500; text-decoration: none; transition: background-color var(--tr), border-color var(--tr), color var(--tr); }
  .btn-primary { background: var(--accent-bg); color: #fff; }
  .btn-primary:hover { background: var(--accent-bg-hover); }
  .btn-danger { background: var(--danger-btn); color: #fff; }
  .btn-danger:hover { background: var(--danger-btn-hover); }
  .btn-success { background: var(--success-btn); color: #fff; }
  .btn-success:hover { background: var(--success-btn-hover); }
  .btn-warning { background: var(--warning-btn); color: #fff; }
  .btn-warning:hover { background: var(--warning-btn-hover); }
  .btn-sm { min-height: 32px; padding: 6px 12px; font-size: var(--fs-xs); }
  .btn-ghost { background: var(--surface-2); color: var(--text-muted); border: 1px solid var(--border); }
  .btn-ghost:hover { background: var(--border); color: var(--text); }

  input[type=text], input[type=url], input[type=number], select { width: 100%; padding: 10px 14px; border-radius: var(--radius-sm); border: 1px solid var(--border-light); background: var(--surface-2); color: var(--text); font-size: var(--fs-sm); margin-bottom: var(--s3); transition: border-color var(--tr); }
  select { appearance: none; -webkit-appearance: none; }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  .form-row { display: flex; gap: var(--s3); align-items: end; }
  .form-row input, .form-row select { flex: 1; margin-bottom: 0; }
  .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--s3); }
  .form-group label { display: block; color: var(--text-muted); font-size: var(--fs-xs); margin-bottom: var(--s1); }
  .empty { color: var(--text-muted); font-style: italic; padding: var(--s5); text-align: center; }

  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: var(--fs-xs); font-weight: 500; line-height: 1.5; }
  /* default badges are neutral; color is reserved for state that matters */
  .badge-neutral, .badge-lang, .badge-sub, .badge-res, .badge-ep { background: var(--neutral-bg); color: var(--neutral-text); }
  .badge-ok { background: var(--ok-bg); color: var(--ok-text); }
  .badge-warn { background: var(--warn-bg); color: var(--warn-text); }
  .badge-danger, .badge-retry { background: var(--danger-bg); color: var(--danger-text); }
  .badge-accent, .badge-auto { background: var(--accent-soft-bg); color: var(--accent-soft-text); }

  .release-row { display: flex; gap: var(--s2); align-items: center; flex-wrap: wrap; padding: var(--s2) 0; border-bottom: 1px solid var(--border); }
  .release-row:last-child { border-bottom: none; }
  .status-msg { padding: var(--s3); border-radius: var(--radius-sm); margin-bottom: var(--s4); }
  .status-ok { background: var(--ok-bg); color: var(--ok-text); }
  .status-err { background: var(--danger-bg); color: var(--danger-text); }
  .section { margin-bottom: var(--s6); }
  .toggle { display: flex; align-items: center; gap: var(--s2); }
  .toggle input[type=checkbox] { width: 18px; height: 18px; accent-color: var(--accent); }
  .prefs-current { display: flex; gap: var(--s2); flex-wrap: wrap; margin-top: var(--s2); }
  details summary { cursor: pointer; color: var(--text-heading); font-size: var(--fs-base); font-weight: 600; }
  details summary:hover { color: var(--accent); }
  details[open] summary { margin-bottom: var(--s3); }

  /* Activity section */
  .activity-grid { display: grid; grid-template-columns: 1fr 1fr 1fr auto; gap: var(--s4); align-items: center; }
  .activity-label { color: var(--text-muted); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; }
  .activity-value { font-size: var(--fs-base); font-weight: 500; margin-top: 2px; }
  .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .status-dot.running { background: #56b870; }
  .status-dot.stopped { background: #d9534f; }
  .status-dot.unknown { background: var(--text-faint); }

  /* Health panel */
  .health-grid { display: flex; flex-direction: column; gap: var(--s3); }
  .health-row { padding: var(--s2) 0; border-bottom: 1px solid var(--border); }
  .health-row:last-child { border-bottom: none; padding-bottom: 0; }
  .health-row-main { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; }
  .health-label { font-weight: 600; font-size: var(--fs-sm); min-width: 110px; }
  .health-detail { color: var(--text-muted); font-size: var(--fs-sm); }

  /* Run / move history feed */
  .run-entry { padding: var(--s3) 0; border-bottom: 1px solid var(--border); }
  .run-entry:last-child { border-bottom: none; }
  .run-header { display: flex; justify-content: space-between; align-items: center; gap: var(--s2); }
  .run-time { color: var(--text-faint); font-size: var(--fs-xs); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .run-anime { color: var(--text-heading); font-weight: 600; font-size: var(--fs-sm); }
  .run-events { margin-top: var(--s2); display: flex; flex-direction: column; gap: 1px; }
  /* Day header: the feed's HH:MM lines are grouped under the calendar day they
     ran on, in the same quiet label voice as the activity stats. The entry
     divider above already separates the days, so the label carries no rule of
     its own and sits closer to its lines than to the previous day. */
  .run-day { color: var(--text-muted); font-size: 0.72rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; padding: var(--s5) 0 0; }
  .run-day:first-child { padding-top: 0; }

  /* one event line: clear label + message; downloads & errors dominate,
     routine sleep/skip lines stay quiet so the feed reads calm */
  .event { display: flex; gap: var(--s2); align-items: baseline; padding: 2px 0; font-size: var(--fs-xs); line-height: 1.45; }
  .event-label { flex: 0 0 70px; font-size: 0.7rem; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; }
  .event-msg { flex: 1; color: var(--text); word-break: break-word; }
  .event--ok .event-label { color: var(--ok-text); }
  .event--danger .event-label, .event--danger .event-msg { color: var(--danger-text); }
  .event--warn .event-label { color: var(--warn-text); }
  .event--warn .event-msg { color: var(--text-muted); }
  .event--muted { opacity: 0.9; }
  .event--muted .event-label { color: var(--text-faint); font-weight: 500; }
  .event--muted .event-msg { color: var(--text-faint); }

  /* A quiet-cycle group and a cycle's low-signal detail collapse behind the
     same dependency-free toggle as the OK-episode panel. Rows inside are
     server-rendered, so opening one only flips a class. */
  .run-entry--quiet .event-msg { color: var(--text-faint); }
  .run-detail-toggle { padding: var(--s1) 0 0; }
  .run-detail-btn { background: none; border: none; cursor: pointer; color: var(--accent); font-size: var(--fs-xs); font-family: inherit; padding: var(--s1) 0; }
  .run-detail-btn:hover { color: var(--accent-hover); }
  .run-detail { display: none; }
  .run-detail.run-detail-open { display: block; margin-top: var(--s1); padding-left: var(--s3); border-left: 2px solid var(--border-light); }

  /* Episode management panel */
  .ep-panel { margin-top: var(--s3); border-top: 1px solid var(--border); padding-top: var(--s2); }
  .ep-panel summary { font-size: var(--fs-xs); color: var(--text-muted); font-weight: 500; }
  .ep-panel summary:hover { color: var(--accent); }
  .ep-panel[open] summary { margin-bottom: var(--s2); }
  .ep-row { display: flex; align-items: center; gap: var(--s2); padding: var(--s1) 0; border-bottom: 1px solid var(--border); }
  .ep-row:last-child { border-bottom: none; }
  /* OK episodes collapse behind this toggle; rows are built on demand in JS */
  .ep-ok-toggle { padding: var(--s1) 0; }
  .ep-expand-btn { background: none; border: none; cursor: pointer; color: var(--accent); font-size: var(--fs-xs); font-family: inherit; padding: var(--s1) 0; }
  .ep-expand-btn:hover { color: var(--accent-hover); }
  .ep-ok-group { display: none; }
  .ep-ok-group.ep-ok-open { display: block; }
  .ep-num { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: var(--fs-xs); min-width: 50px; color: var(--text-muted); }
  .ep-add-row { display: flex; gap: var(--s2); margin-top: var(--s2); padding-top: var(--s2); border-top: 1px solid var(--border); align-items: center; }
  .ep-add-row input[type=number] { width: 90px; padding: 6px 8px; border-radius: 4px; border: 1px solid var(--border-light); background: var(--surface-2); color: var(--text); font-size: var(--fs-xs); margin: 0; min-height: 32px; }

  .folder-row { margin-top: var(--s1); font-size: var(--fs-xs); color: var(--text-muted); }
  .folder-input { background: var(--surface-3); border: 1px solid var(--border-light); color: var(--text); padding: 5px 8px; border-radius: 4px; font-size: var(--fs-xs); width: 220px; margin: 0; }

  @media (max-width: 600px) {
    .activity-grid { grid-template-columns: 1fr 1fr; gap: var(--s3); }
    .form-row { flex-wrap: wrap; }
    .form-row select { flex: 1 1 100%; }
    .folder-input { width: 100%; }
  }
  @media (prefers-reduced-motion: reduce) {
    * { transition: none !important; animation: none !important; }
  }
</style>
</head>
<body>
<h1>Anime-Loads Dashboard</h1>

%%STATUS_MSG%%

<div class="section">
  <h2>Bot Activity</h2>
  <div class="card">
    <div class="activity-grid">
      <div>
        <div class="activity-label">Status</div>
        <div class="activity-value" id="bot-status">%%BOT_STATUS%%</div>
      </div>
      <div>
        <div class="activity-label">Last Run</div>
        <div class="activity-value" id="last-run">%%LAST_RUN%%</div>
      </div>
      <div>
        <div class="activity-label">Next Run</div>
        <div class="activity-value" id="next-run">%%NEXT_RUN%%</div>
      </div>
      <div>
        <form method="POST" action="/run-now" style="margin:0;">
          <button type="submit" class="btn btn-warning">Run Now</button>
        </form>
      </div>
    </div>
  </div>
</div>

<div class="section">
  <h2>Health</h2>
  <div class="card" id="health">
    %%HEALTH%%
  </div>
</div>

<div class="section">
  <details open>
    <summary>Run History</summary>
    <div class="card" id="run-history">
      %%RUN_HISTORY%%
    </div>
  </details>
</div>

<div class="section">
  <h2>File Mover</h2>
  <div class="card">
    <div class="activity-grid" style="grid-template-columns: 1fr 1fr auto;">
      <div>
        <div class="activity-label">Status</div>
        <div class="activity-value" id="move-status">%%MOVE_STATUS%%</div>
      </div>
      <div>
        <div class="activity-label">Last Run</div>
        <div class="activity-value" id="move-last-run">%%MOVE_LAST_RUN%%</div>
      </div>
      <div>
        <form method="POST" action="/move-now" style="margin:0;">
          <button type="submit" class="btn btn-warning">Move Now</button>
        </form>
      </div>
    </div>
  </div>
  <details open>
    <summary>Stuck Downloads</summary>
    <div class="card" id="move-stuck">
      %%MOVE_STUCK%%
    </div>
  </details>
  <details>
    <summary>Move History</summary>
    <div class="card" id="move-history">
      %%MOVE_HISTORY%%
    </div>
  </details>
</div>

<div class="section">
  <details %%PREFS_OPEN%%>
    <summary>Preferences</summary>
    <div class="card">
      <form method="POST" action="/save-prefs">
        <div class="form-grid">
          <div class="form-group">
            <label>Audio Language (Dub)</label>
            <select name="audio_language">
              <option value="german" %%AUDIO_GER%%>German (Deutsch)</option>
              <option value="japanese" %%AUDIO_JAP%%>Japanese</option>
              <option value="english" %%AUDIO_ENG%%>English</option>
              <option value="any" %%AUDIO_ANY%%>Any</option>
            </select>
          </div>
          <div class="form-group">
            <label>Subtitle Language (Sub)</label>
            <select name="sub_language">
              <option value="german" %%SUB_GER%%>German (Deutsch)</option>
              <option value="japanese" %%SUB_JAP%%>Japanese</option>
              <option value="english" %%SUB_ENG%%>English</option>
              <option value="any" %%SUB_ANY%%>Any</option>
            </select>
          </div>
          <div class="form-group">
            <label>Minimum Resolution</label>
            <select name="min_resolution">
              <option value="480" %%RES_480%%>480p</option>
              <option value="720" %%RES_720%%>720p</option>
              <option value="1080" %%RES_1080%%>1080p</option>
            </select>
          </div>
        </div>
        <div class="toggle" style="margin-top:12px;">
          <input type="checkbox" name="auto_select" id="auto_select" %%AUTO_CHECKED%%>
          <label for="auto_select" style="font-size:0.9rem;">Auto-select best matching release when adding</label>
        </div>
        <div style="margin-top:14px;">
          <button type="submit" class="btn btn-success">Save Preferences</button>
        </div>
      </form>
      <div class="prefs-current">
        <span class="badge badge-lang">Dub: %%PREF_AUDIO_DISPLAY%%</span>
        <span class="badge badge-sub">Sub: %%PREF_SUB_DISPLAY%%</span>
        <span class="badge badge-res">&ge; %%PREF_RES%%p</span>
        %%AUTO_BADGE%%
      </div>
    </div>
  </details>
</div>

<div class="section">
  <h2>Add Anime</h2>
  <div class="card">
    <form method="POST" action="/add-url" onsubmit="return scrapeBusy(this, 'Fetching releases… this can take up to a minute');">
      <label class="hint">Paste an anime-loads.org URL to see available releases:</label>
      <div class="form-row" style="margin-top:6px;">
        <input type="url" name="url" placeholder="https://www.anime-loads.org/media/..." required>
        <button type="submit" class="btn btn-primary">Fetch Releases</button>
      </div>
    </form>
  </div>
  <div class="card">
    <form method="POST" action="/search" onsubmit="return scrapeBusy(this, 'Searching… this can take up to a minute');">
      <label class="hint">Or search by name:</label>
      <div class="form-row" style="margin-top:6px;">
        <input type="text" name="q" placeholder="Search anime..." required>
        <button type="submit" class="btn btn-primary">Search anime</button>
      </div>
    </form>
  </div>
</div>

%%SEARCH_RESULTS%%

<div class="section">
  <h2>Watchlist (%%COUNT%% anime)</h2>
  %%WATCHLIST%%
</div>

<script>
// Scrape-backed forms (add-url, search) hit Selenium server-side and can
// take up to ~a minute — disable the button and relabel it on submit so the
// page doesn't look hung while a normal (non-AJAX) form POST is in flight.
// The threaded server keeps the /api/status poll below updating throughout.
function scrapeBusy(form, label) {
  var btn = form.querySelector('button[type=submit]');
  if (btn && !btn.disabled) {
    btn.disabled = true;
    btn.textContent = label;
  }
  return true;
}

(function() {
  var ids = ['bot-status','last-run','next-run','run-history','health',
             'move-status','move-last-run','move-history','move-stuck'];
  function refresh() {
    fetch('/api/status')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        ids.forEach(function(id) {
          var key = id.replace(/-/g, '_');
          var el = document.getElementById(id);
          if (el && d[key] !== undefined) {
            // The feed re-renders every 10s. Remember which run-detail panels
            // the reader had open (by their stable per-cycle key) and re-open
            // them, or a poll would silently collapse what they were reading.
            var open = openRunKeys(el);
            el.innerHTML = d[key];
            restoreRunKeys(el, open);
          }
        });
        if (d.bot_running) {
          var msg = document.getElementById('status-msg');
          if (msg) msg.remove();
        }
      })
      .catch(function() {});
  }
  setInterval(refresh, 10000);
})();

// Build the OK-episode rows on demand the first time the toggle is opened, then
// just show/hide them. Keeps long-series watchlist cards light on first paint.
function expandEps(btn) {
  var group = btn.parentNode.nextElementSibling;
  if (!group) return;
  if (!btn.dataset.built) {
    var key = btn.dataset.key, eps = parseInt(btn.dataset.eps, 10) || 0;
    var miss = {};
    try {
      JSON.parse(btn.dataset.missing || '[]').forEach(function(e) { miss[e] = 1; });
    } catch (e) {}
    // Built as real DOM nodes, not an HTML string — key is the attribute-
    // decoded watchlist URL, and concatenating it into markup would let a
    // URL containing HTML metacharacters inject content into the page.
    var frag = document.createDocumentFragment();
    for (var n = 1; n <= eps; n++) {
      if (miss[n]) continue;
      var row = document.createElement('div');
      row.className = 'ep-row';
      var numSpan = document.createElement('span');
      numSpan.className = 'ep-num';
      numSpan.textContent = 'Ep ' + n;
      row.appendChild(numSpan);
      row.appendChild(document.createTextNode(' '));
      var okBadge = document.createElement('span');
      okBadge.className = 'badge badge-ok';
      okBadge.textContent = 'OK';
      row.appendChild(okBadge);
      row.appendChild(document.createTextNode(' '));
      var form = document.createElement('form');
      form.setAttribute('method', 'POST');
      form.setAttribute('action', '/ep-add');
      form.style.margin = '0';
      var keyInput = document.createElement('input');
      keyInput.type = 'hidden';
      keyInput.name = 'key';
      keyInput.value = key;
      form.appendChild(keyInput);
      var epInput = document.createElement('input');
      epInput.type = 'hidden';
      epInput.name = 'ep';
      epInput.value = n;
      form.appendChild(epInput);
      var submitBtn = document.createElement('button');
      submitBtn.type = 'submit';
      submitBtn.className = 'btn btn-ghost btn-sm';
      submitBtn.textContent = 'Retry';
      form.appendChild(submitBtn);
      row.appendChild(form);
      frag.appendChild(row);
    }
    group.appendChild(frag);
    btn.dataset.built = '1';
  }
  var open = group.classList.toggle('ep-ok-open');
  var count = btn.dataset.count;
  btn.textContent = (open ? 'Hide ' : 'Show ') + count + ' OK episode' + (count === '1' ? '' : 's');
}

// Show/hide a run cycle's detail panel: the low-signal events (unavailable /
// complete) and the cycles folded into a quiet group. Everything inside the
// panel is server-rendered and already escaped, so this only flips a class —
// no feed data ever reaches a JS string literal.
function setRunDetail(btn, open) {
  var panel = btn.parentNode.nextElementSibling;
  if (!panel) return;
  if (open) panel.classList.add('run-detail-open');
  else panel.classList.remove('run-detail-open');
  btn.textContent = (open ? 'Hide ' : 'Show ') + btn.dataset.label;
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}
function expandRun(btn) {
  var panel = btn.parentNode.nextElementSibling;
  setRunDetail(btn, !(panel && panel.classList.contains('run-detail-open')));
}
// Keys are only ever compared, never spliced into a selector.
function openRunKeys(el) {
  var keys = [], nodes = el.querySelectorAll('.run-detail.run-detail-open');
  for (var i = 0; i < nodes.length; i++) {
    if (nodes[i].dataset.runKey) keys.push(nodes[i].dataset.runKey);
  }
  return keys;
}
function restoreRunKeys(el, keys) {
  if (!keys.length) return;
  var nodes = el.querySelectorAll('.run-detail');
  for (var i = 0; i < nodes.length; i++) {
    var k = nodes[i].dataset.runKey;
    if (!k || keys.indexOf(k) === -1) continue;
    var head = nodes[i].previousElementSibling;
    var btn = head ? head.querySelector('button') : null;
    if (btn) setRunDetail(btn, true);
  }
}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def render_activity(activity, now=None):
    """Render the activity status bar values."""
    status = activity["status"]
    bot_running = status.get("running", False)
    dot_class = "running" if bot_running else "stopped"
    status_text = '<span class="status-dot {}"></span>{}'.format(
        dot_class, "Running" if bot_running else "Stopped"
    )

    # Prefer the persisted run-state line (immune to log-tail rollover); fall
    # back to the log-parsed last run, then to the empty/unavailable states.
    last_run_display = activity.get("last_run_display")
    last_run = activity.get("last_run")
    if last_run_display:
        last_text = last_run_display
    elif last_run and last_run.get("time"):
        last_text = last_run["time"]
        day = _log_run_day(last_run)
        if day:
            label = format_day(day, now)
            if label != "Today":
                last_text = "{} {}".format(label, last_text)
        if last_run.get("anime"):
            last_text += " &mdash; {}".format(escape(last_run["anime"]))
    elif not docker.available:
        last_text = '<span class="faint">Docker socket unavailable</span>'
    else:
        last_text = '<span class="faint">No runs yet</span>'

    next_text = activity.get("next_run") or '<span class="faint">&mdash;</span>'

    return status_text, last_text, next_text


# Presentation for a single feed event: (tone, label). Tone drives color/weight
# so downloads and errors visually dominate while routine sleep/skip/cleanup
# lines stay quiet. This is the one place feed events get their look — shared by
# the bot run history and the file-mover history.
_EVENT_PRESENTATION = {
    "download": ("ok", "Downloaded"),
    "batch": ("ok", "Batch"),
    "moved": ("ok", "Moved"),
    "complete": ("ok", "Complete"),
    "error": ("danger", "Error"),
    "mismatch": ("warn", "Numbering"),
    "throttle": ("warn", "Throttled"),
    "wait": ("warn", "Waiting"),
    "skip": ("muted", "Skipped"),
    "unavailable": ("warn", "Unavailable"),
    "sleep": ("muted", "Sleeping"),
    "info": ("muted", "Info"),
    "cleanup": ("muted", "Cleaned"),
}


def render_event(etype, msg):
    """Render one feed event line: a legible tone label + the raw message."""
    tone, label = _EVENT_PRESENTATION.get(etype, ("muted", "Info"))
    return ('<div class="event event--{tone}">'
            '<span class="event-label">{label}</span>'
            '<span class="event-msg">{msg}</span></div>').format(
        tone=tone, label=label, msg=escape(msg))


# A cycle's events split by signal: downloads, errors and numbering mismatches
# are the news and always get their own visible line; unavailable/complete are
# background detail that hides behind the expand toggle so the feed stays
# readable. A "mismatch" (a release numbering its files 41-46 while the
# watchlist wants 1-7) is the most actionable line the feed can show — it names
# the config change that fixes it — so it is LOUD: never behind the toggle, and
# never folded into a "nothing new" group.
_LOUD_EVENT_KINDS = ("download", "error", "mismatch")
_QUIET_EVENT_KINDS = ("unavailable", "complete")
_RUN_EVENT_KINDS = _LOUD_EVENT_KINDS + _QUIET_EVENT_KINDS


def run_state_events(record):
    """The per-cycle events of a run-state record, defensively filtered.

    ``events`` is optional at read time — an older record, a fresh deploy, or a
    bot that has not been redeployed yet carries none — and an unknown kind is
    dropped rather than rendered as a mystery line."""
    if not isinstance(record, dict):
        return []
    events = record.get("events")
    if not isinstance(events, list):
        return []
    return [ev for ev in events
            if isinstance(ev, dict) and ev.get("kind") in _RUN_EVENT_KINDS]


def format_run_event(event):
    """One event as PLAIN TEXT: "One Piece — episodes 1112, 1113", "Gantz —
    JDownloader nicht erreichbar". Deliberately not HTML — series names are
    watchlist data (``Fate/stay night & Heaven's Feel``), so the single escape
    happens once at render time in render_event(). Returns "" for an event with
    nothing to say."""
    anime = str(event.get("anime") or "").strip()
    episodes = [ep for ep in (event.get("episodes") or [])
                if isinstance(ep, int) and not isinstance(ep, bool)]
    detail = str(event.get("detail") or "").strip()

    parts = []
    if anime:
        parts.append(anime)
    if episodes:
        parts.append("episode{} {}".format(
            "" if len(episodes) == 1 else "s",
            ", ".join(str(ep) for ep in episodes)))
    if detail:
        parts.append(detail)
    return " — ".join(parts)


def render_run_cycle_events(record):
    """Render one cycle's events, split by signal.

    Returns ``(loud_html, quiet_html, quiet_count)`` — already-escaped HTML for
    the always-visible download/error lines (plus the truncation notice, which
    is a caveat about completeness and must never hide), and for the
    unavailable/complete lines that live behind the toggle."""
    events = run_state_events(record)
    loud = ""
    quiet = ""
    quiet_count = 0
    for event in events:
        kind = event.get("kind")
        text = format_run_event(event)
        if not text:
            continue
        line = render_event(kind, text)
        if kind in _LOUD_EVENT_KINDS:
            loud += line
        else:
            quiet += line
            quiet_count += 1

    if isinstance(record, dict) and record.get("events_truncated"):
        # Count the RAW recorded list, not the filtered one: an unrecognised
        # kind (a newer bot than this dashboard) is dropped from rendering, but
        # it was still recorded — and the one line whose whole job is saying
        # "this list is incomplete" must not itself state a wrong number.
        raw = record.get("events")
        recorded = len(raw) if isinstance(raw, list) else len(events)
        loud += render_event("info", "event list truncated — only the first {} "
                                     "of this cycle were recorded".format(recorded))
    return loud, quiet, quiet_count


def _run_detail_key(record):
    """A stable per-cycle key for the detail panel, so the 10s feed refresh can
    re-open whatever the reader had open. The finished timestamp is the record's
    own identity; a record without one simply never restores."""
    if not isinstance(record, dict):
        return ""
    return str(record.get("finished_ts") or "")


def render_run_detail(key, label, inner_html):
    """The show/hide pair the run feed uses — the same dependency-free pattern as
    the OK-episode toggle. ``inner_html`` is server-rendered and already escaped,
    so the script only flips a class and no feed data reaches a JS literal."""
    return ('<div class="run-detail-toggle">'
            '<button type="button" class="run-detail-btn" data-label="{label}" '
            'aria-expanded="false" onclick="expandRun(this)">Show {label}</button>'
            '</div><div class="run-detail" data-run-key="{key}">{inner}</div>').format(
        label=escape(label, quote=True), key=escape(key, quote=True), inner=inner_html)


def render_run_cycle(record):
    """One cycle on its own line: the summary, its download/error lines, and a
    toggle for the lower-signal detail."""
    summary = build_run_summary(record)
    loud, quiet, quiet_count = render_run_cycle_events(record)

    html = ('<div class="run-entry"><div class="event event--{tone}">'
            '<span class="event-msg">{msg}</span></div>').format(
        tone=_run_summary_tone(record), msg=escape(summary))
    if loud:
        html += '<div class="run-events">{}</div>'.format(loud)
    if quiet:
        html += render_run_detail(
            _run_detail_key(record),
            "{} more detail{}".format(quiet_count, "" if quiet_count == 1 else "s"),
            '<div class="run-events">{}</div>'.format(quiet))
    return html + "</div>"


def cycle_is_quiet(record):
    """True when a cycle brought no news — nothing downloaded, no errors, and no
    event worth its own line. Only these fold into a group; a cycle with a
    download or an error ALWAYS keeps its own line."""
    counts = record.get("counts") or {} if isinstance(record, dict) else {}
    if counts.get("downloaded") or counts.get("errors"):
        return False
    return not any(ev.get("kind") in _LOUD_EVENT_KINDS
                   for ev in run_state_events(record))


def build_quiet_group_summary(records, now=None):
    """The one line that stands in for a run of consecutive quiet cycles:
    "18:47 – 19:06 · 18 quiet cycles — checked 0–3/18, nothing new".

    The time span and the checked range are what the folded lines actually said,
    so nothing the reader needs is invented or lost. The feed splits folds at
    day boundaries so the span sits under its day header; should a caller hand
    in a group spanning days anyway, both ends name their day rather than
    passing "23:50 – 00:10" off as ten minutes."""
    times = []
    checks = []
    entry_counts = set()
    for record in records:
        fin = _local_ts(record.get("finished_ts"))
        if fin:
            times.append(fin)
        counts = record.get("counts") or {}
        checked = counts.get("checked")
        if isinstance(checked, int) and not isinstance(checked, bool):
            checks.append(checked)
        entries = counts.get("entries")
        if isinstance(entries, int) and not isinstance(entries, bool) and entries:
            entry_counts.add(entries)

    head = ""
    if times:
        first, last = min(times), max(times)
        if first.date() == last.date():
            oldest, newest = first.strftime("%H:%M"), last.strftime("%H:%M")
        else:
            oldest = "{} {}".format(format_day(first.date(), now), first.strftime("%H:%M"))
            newest = "{} {}".format(format_day(last.date(), now), last.strftime("%H:%M"))
        head = oldest if oldest == newest else "{} – {}".format(oldest, newest)

    tail = "nothing new"
    if checks:
        low, high = min(checks), max(checks)
        span = str(low) if low == high else "{}–{}".format(low, high)
        # Only claim a denominator when every folded cycle agreed on one.
        if len(entry_counts) == 1:
            span = "{}/{}".format(span, next(iter(entry_counts)))
        tail = "checked {}, nothing new".format(span)

    lead = " · ".join(part for part in (head, "{} quiet cycles".format(len(records))) if part)
    return "{} — {}".format(lead, tail)


def render_quiet_group(records, now=None):
    """Fold 2+ consecutive quiet cycles into ONE muted line — the fix for the
    wall of near-identical routine lines burying the cycles that mattered. A
    lone quiet cycle is left alone (one line is not a wall). The folded cycles
    stay reachable behind the toggle, so collapsing hides nothing."""
    if not records:
        return ""
    if len(records) == 1:
        return render_run_cycle(records[0])

    inner = '<div class="run-events">'
    for record in records:
        loud, quiet, _count = render_run_cycle_events(record)
        inner += ('<div class="event event--muted"><span class="event-msg">{}</span>'
                  '</div>').format(escape(build_run_summary(record)))
        inner += loud + quiet
    inner += "</div>"

    # Newest first, so records[-1] is the oldest end of the span.
    key = "{}..{}".format(_run_detail_key(records[-1]), _run_detail_key(records[0]))
    return ('<div class="run-entry run-entry--quiet">'
            '<div class="event event--muted"><span class="event-msg">{msg}</span></div>'
            '{detail}</div>').format(
        msg=escape(build_quiet_group_summary(records, now)),
        detail=render_run_detail(key, "all {}".format(len(records)), inner))


def render_run_state_history(state_runs, max_runs=20, now=None):
    """Render the run-history feed from the bot's persisted run-state records.
    Newest first, grouped under a header per calendar day ("Today",
    "Yesterday", "Tue 8 Sep") so each line can keep its compact HH:MM.

    A cycle that did something — a download, an error — gets its own line with
    those events spelled out (which series, which episodes, what went wrong).
    Consecutive cycles that did nothing fold into a single muted grouped line,
    so a wall of identical routine lines can no longer bury the ones that
    mattered. A fold never crosses a day header: a new day closes the open
    group first. A record whose timestamp is missing or garbage stays under the
    current header. Returns "" when nothing is renderable, so the caller can
    fall back to the log-parsed event feed."""
    display = list(reversed(state_runs))[:max_runs]
    html = ""
    quiet_group = []
    current_day = None
    for record in display:
        if not build_run_summary(record):
            continue
        fin = _local_ts(record.get("finished_ts"))
        if fin and fin.date() != current_day:
            html += render_quiet_group(quiet_group, now)
            quiet_group = []
            current_day = fin.date()
            html += render_run_day(current_day, now)
        if cycle_is_quiet(record):
            quiet_group.append(record)
            continue
        html += render_quiet_group(quiet_group, now)
        quiet_group = []
        html += render_run_cycle(record)
    return html + render_quiet_group(quiet_group, now)


def _log_run_day(run):
    """The local calendar day a log-parsed run happened on, or None when its
    line carried no usable Docker timestamp. Docker's prefix is UTC; the local
    day matches the bot's printed ``[HH:MM:SS]`` because both containers get
    the same TZ, so a 00:30 line sits under the day it says it is."""
    ts = _local_ts(run.get("docker_ts")) if isinstance(run, dict) else None
    return ts.date() if ts else None


def render_run_history(runs, state_runs=None, max_runs=20, now=None):
    """Render the run history feed.

    Prefers the bot's persisted run-state records (`state_runs`) — one concise
    summary line per cycle, independent of the rolling log tail. Falls back to
    the log-parsed event feed (`runs`) when no run-state records exist, so the
    no-record case (older bot, fresh deploy) does not regress. Both feeds group
    their lines under calendar-day headers; a log line without a Docker
    timestamp simply stays under the current one."""
    if state_runs:
        state_html = render_run_state_history(state_runs, max_runs=max_runs, now=now)
        if state_html:
            return state_html

    if not runs:
        if not docker.available:
            return '<div class="empty">Docker socket not mounted — cannot read bot logs</div>'
        return '<div class="empty">No bot runs recorded yet</div>'

    # Show most recent runs first, limit count
    display_runs = list(reversed(runs))[:max_runs]

    html = ""
    current_day = None
    for run in display_runs:
        time_str = run.get("time", "")
        anime = run.get("anime", "")
        # [COMPLETE] entries are run-history noise the owner doesn't want surfaced.
        # Drop them from the rendered feed only — the bot still logs [COMPLETE] and
        # parse_bot_logs() still parses it; this is a presentation-layer suppression.
        events = [ev for ev in run.get("events", []) if ev.get("type") != "complete"]

        if not anime and not events:
            continue

        day = _log_run_day(run)
        if day and day != current_day:
            current_day = day
            html += render_run_day(day, now)

        html += '<div class="run-entry">'
        # Only show a header when there's a real anime name or a run time. Routine
        # standalone lines (a lone skip/throttle with no Pr\u00fcfe) drop the empty
        # "System" label and stand alone as one calm muted line.
        if anime or time_str:
            html += '<div class="run-header">'
            if anime:
                html += '<span class="run-anime">{}</span>'.format(escape(anime))
            else:
                html += '<span class="run-anime faint">System</span>'
            if time_str:
                html += '<span class="run-time">{}</span>'.format(time_str)
            html += "</div>"

        if events:
            html += '<div class="run-events">'
            for ev in events:
                html += render_event(ev.get("type", "info"), ev.get("msg", ""))
            html += "</div>"
        html += "</div>"

    return html


def render_move_status(now=None):
    """Render move status and last run time (day-qualified when not today)."""
    if _move_running:
        status_html = '<span class="status-dot running"></span>Running'
    else:
        status_html = '<span class="status-dot unknown"></span>Idle'

    with _move_lock:
        if _move_last_run:
            # The worker stamps an aware UTC time; shown on the local clock
            # like every other feed timestamp.
            last_html = format_day_time(_to_local(_move_last_run), now=now)
        elif not os.path.isdir(DOWNLOAD_DIR):
            last_html = '<span class="faint">Download dir not mounted</span>'
        else:
            last_html = '<span class="faint">Not yet</span>'

    return status_html, last_html


def render_move_history(max_entries=30):
    """Render the move history feed."""
    with _move_lock:
        entries = list(_move_history)

    if not entries:
        if not os.path.isdir(DOWNLOAD_DIR):
            return '<div class="empty">Download directory not mounted</div>'
        return '<div class="empty">No move activity yet</div>'

    display = list(reversed(entries))[:max_entries]
    html = '<div class="run-events">'
    for ev in display:
        html += render_event(ev.get("type", "info"), ev.get("msg", ""))
    html += "</div>"
    return html


_STUCK_REASON_LABELS = {
    "parse": "Can't parse season/episode",
    "exists": "Already exists in library",
    "unmatched": "No watchlist match",
    "unsafe_folder": "Unsafe folder name (blocked)",
}


def render_move_stuck():
    """Render the stuck-downloads list: parse failures, already-exists
    conflicts, and unmatched downloads the mover won't touch again on its
    own until a user picks an action."""
    with _move_lock:
        items = [dict(v) for v in _stuck_items.values() if not v.get("ignored")]

    if not items:
        return '<div class="empty">No stuck downloads</div>'

    items.sort(key=lambda r: r.get("first_seen", ""))

    html = ""
    for rec in items:
        key = rec.get("key", "")
        reason = rec.get("reason", "")
        label = _STUCK_REASON_LABELS.get(reason, "Stuck")
        msg = rec.get("msg", "")
        key_input = '<input type="hidden" name="key" value="{}">'.format(escape(key))

        actions = """
              <form method="POST" action="/move-stuck-ignore" style="display:inline;margin:0;">
                {key_input}
                <button type="submit" class="btn btn-sm">Ignore</button>
              </form>""".format(key_input=key_input)

        if reason == "exists":
            delete_confirm = confirm_attr(
                "Delete the downloaded copy of {}? This cannot be undone.".format(rec.get("path", "")))
            actions += """
              <form method="POST" action="/move-stuck-delete" style="display:inline;margin:0;">
                {key_input}
                <button type="submit" class="btn btn-danger btn-sm" onclick="{confirm}">Delete download copy</button>
              </form>""".format(key_input=key_input, confirm=delete_confirm)
        elif reason == "unmatched":
            actions += """
              <form method="POST" action="/move-stuck-anyway" style="display:inline;margin:0;">
                {key_input}
                <button type="submit" class="btn btn-warning btn-sm">Move anyway</button>
              </form>""".format(key_input=key_input)

        html += """
        <div class="card card-accent">
          <div style="display:flex;justify-content:space-between;align-items:start;gap:12px;">
            <div>
              <div class="anime-name">{label}</div>
              <div class="anime-meta">{msg}</div>
            </div>
            <div>{actions}</div>
          </div>
        </div>""".format(label=escape(label), msg=escape(msg), actions=actions)

    return html


def confirm_attr(message):
    """Build a safe ``onclick="return confirm(...)"`` attribute value.

    ``json.dumps`` produces a valid JS string literal (escaping quotes,
    backslashes, etc.), and ``escape(..., quote=True)`` then makes it safe inside
    the double-quoted HTML attribute. Without this, a name containing an
    apostrophe (e.g. "Frieren: Beyond Journey's End") was only HTML-escaped, so
    the inline ``confirm('Remove Journey&#x27;s End?')`` failed to compile and
    Remove submitted with no confirmation."""
    return escape("return confirm({})".format(json.dumps(message)), quote=True)


def find_entry_by_url(entries, url):
    """Locate a watchlist entry by its (unique) URL.

    Returns ``(index, entry)`` for the first match, or ``(-1, None)`` if no
    entry has that URL (including when ``url`` is empty).

    Mutations key off URL rather than array index: the ``resolve_pending``
    background thread pops resolved entries concurrently, so an index captured
    at page-render time can point at a different entry by the time the form is
    submitted (TOCTOU). URLs are unique — ``/add-url`` dedups by URL across both
    the anime and pending lists — so matching on URL hits the intended entry.
    """
    if not url:
        return -1, None
    for i, entry in enumerate(entries):
        if entry.get("url") == url:
            return i, entry
    return -1, None


def render_watchlist(anime_list, pending_list=None):
    if not anime_list and not pending_list:
        return '<div class="empty">No anime in watchlist. Add some above!</div>'
    html = ""

    if pending_list:
        for i, a in enumerate(pending_list):
            name = a.get("name", "Unknown")
            url = a.get("url", "")
            remove_confirm = confirm_attr("Remove {}?".format(name))
            pref_audio = a.get("pref_audio_language", a.get("pref_language", ""))
            pref_sub = a.get("pref_sub_language", "")
            pref_res = a.get("pref_resolution", "")
            pref_badges = ""
            if pref_audio:
                pref_badges += '<span class="badge badge-lang">Dub: {}</span> '.format(escape(pref_audio.title()))
            if pref_sub:
                pref_badges += '<span class="badge badge-sub">Sub: {}</span> '.format(escape(pref_sub.title()))
            if pref_res:
                pref_badges += '<span class="badge badge-res">{}p</span> '.format(escape(str(pref_res)))

            if a.get("no_match"):
                status_badge = '<span class="badge badge-warn">No match</span>'
                no_match_line = (
                    '<div class="anime-meta muted">No release matches your '
                    'language preference &mdash; adjust Preferences, or remove.</div>'
                )
            else:
                status_badge = '<span class="badge badge-accent">Resolving</span>'
                no_match_line = ""

            html += """
            <div class="card card-accent">
              <div style="display:flex;justify-content:space-between;align-items:start;gap:12px;">
                <div>
                  <div class="anime-name">{name} {status_badge}</div>
                  <div class="anime-url">{url}</div>
                  <div class="anime-meta">{pref_badges}</div>
                  {no_match_line}
                </div>
                <form method="POST" action="/remove-pending" style="margin:0;">
                  <input type="hidden" name="key" value="{key}">
                  <button type="submit" class="btn btn-danger btn-sm" onclick="{remove_confirm}">Remove</button>
                </form>
              </div>
            </div>""".format(name=escape(name), url=escape(url), pref_badges=pref_badges,
                             status_badge=status_badge, no_match_line=no_match_line, key=escape(url),
                             remove_confirm=remove_confirm)

    for i, a in enumerate(anime_list):
        name = a.get("name", "Unknown")
        url = a.get("url", "")
        # Mutations target this entry by its unique URL (see find_entry_by_url),
        # not by array index — the resolver shifts indices concurrently.
        key = escape(url)
        eps = a.get("episodes", a.get("episodes_downloaded", 0))
        missing = a.get("missing", [])
        remove_confirm = confirm_attr("Remove {}?".format(name))

        url_html = '<div class="anime-url">{}</div>'.format(escape(url)) if url else ""

        pref_audio = a.get("pref_audio_language", a.get("pref_language", ""))
        pref_sub = a.get("pref_sub_language", "")
        pref_res = a.get("pref_resolution", "")
        pref_badges = ""
        if pref_audio:
            pref_badges += '<span class="badge badge-lang">Dub: {}</span> '.format(escape(pref_audio.title()))
        if pref_sub:
            pref_badges += '<span class="badge badge-sub">Sub: {}</span> '.format(escape(pref_sub.title()))
        if pref_res:
            pref_badges += '<span class="badge badge-res">{}p</span> '.format(escape(str(pref_res)))

        missing_badge = ""
        if missing:
            missing_badge = ' <span class="badge badge-danger">{} retry</span>'.format(len(missing))

        # Movie entries route to MOVIE_MEDIA_DIR, not AnimeName/SXX/ — a neutral
        # badge makes that routing visible at a glance (UI-6).
        type_badge = ""
        if a.get("media_type") == "movie":
            type_badge = ' <span class="badge badge-neutral">Movie</span>'

        # TVDB badges — neutral tone (informational, not state that needs the eye)
        tvdb_badges = ""
        if a.get("tvdb_id"):
            if a.get("tvdb_season"):
                tvdb_badges += ' <span class="badge badge-neutral">S{:02d}</span>'.format(
                    a["tvdb_season"])
            else:
                tvdb_badges += ' <span class="badge badge-neutral">TVDB</span>'
            if a.get("episode_offset", 0) != 0:
                tvdb_badges += ' <span class="badge badge-neutral">Offset {:+d}</span>'.format(
                    a["episode_offset"])
            tvdb_badges += (' <form method="POST" action="/tvdb-unlink" style="margin:0;display:inline;">'
                            '<input type="hidden" name="key" value="{}">'
                            '<button type="submit" class="btn btn-ghost btn-sm" '
                            'onclick="return confirm(\'Remove TVDB link?\')">Unlink TVDB</button>'
                            '</form>').format(key)
        elif tvdb.available:
            tvdb_badges += (' <form method="POST" action="/tvdb-link" style="margin:0;display:inline;">'
                            '<input type="hidden" name="key" value="{}">'
                            '<button type="submit" class="btn btn-ghost btn-sm">Link TVDB</button>'
                            '</form>').format(key)

        # Completion / skip badges — color reserved for meaningful state
        status_badges = ""
        if a.get("complete"):
            status_badges += ' <span class="badge badge-ok">Complete</span>'
            status_badges += (' <form method="POST" action="/mark-incomplete" style="margin:0;display:inline;">'
                              '<input type="hidden" name="key" value="{}">'
                              '<button type="submit" class="btn btn-ghost btn-sm">Mark incomplete</button>'
                              '</form>').format(key)
        elif a.get("skip_until"):
            status_badges += ' <span class="badge badge-warn">Next: {}</span>'.format(
                escape(str(a["skip_until"])))
        if a.get("al_status"):
            status_badges += ' <span class="badge badge-neutral">{}</span>'.format(
                escape(a["al_status"]))

        # Folder name (editable)
        folder = a.get("customPackage", name)
        folder_html = (
            '<div class="folder-row">'
            'Folder: <form method="POST" action="/update-folder" style="display:inline;margin:0;">'
            '<input type="hidden" name="key" value="{key}">'
            '<input type="text" name="folder" value="{folder}" class="folder-input">'
            ' <button type="submit" class="btn btn-ghost btn-sm">Save folder</button>'
            '</form></div>').format(key=key, folder=escape(folder))

        # Build episode detail panel
        eps_count = int(eps) if isinstance(eps, (int, float)) else 0
        missing_set = set(missing)
        retry_label = ", {} retrying".format(len(missing)) if missing else ""

        # Retrying episodes are always rendered. OK episodes are collapsed
        # behind an "expand" toggle and built on demand in JS, so a 1000+ episode
        # series emits only its retrying rows up front instead of 1000+ DOM rows
        # (UI-2). Nothing is lost: any OK episode can still be re-queued via the
        # Add-to-retry box below.
        ep_rows = ""
        ok_count = 0
        for ep_num in range(1, eps_count + 1):
            if ep_num in missing_set:
                action = ('<form method="POST" action="/ep-remove" style="margin:0;">'
                          '<input type="hidden" name="key" value="{}"><input type="hidden" name="ep" value="{}">'
                          '<button type="submit" class="btn btn-ghost btn-sm">Skip</button>'
                          '</form>').format(key, ep_num)
                ep_rows += ('<div class="ep-row"><span class="ep-num">Ep {}</span> '
                            '<span class="badge badge-retry">Retrying</span> {}</div>').format(ep_num, action)
            else:
                ok_count += 1

        # Show missing episodes beyond the current count (manually added)
        for ep_num in sorted(missing):
            if ep_num > eps_count:
                ep_rows += ('<div class="ep-row"><span class="ep-num">Ep {}</span>'
                            ' <span class="badge badge-retry">Retrying</span>'
                            ' <span class="badge badge-auto">Queued</span>'
                            ' <form method="POST" action="/ep-remove" style="margin:0;">'
                            '<input type="hidden" name="key" value="{}"><input type="hidden" name="ep" value="{}">'
                            '<button type="submit" class="btn btn-ghost btn-sm">Skip</button>'
                            '</form></div>').format(ep_num, key, ep_num)

        ok_toggle = ""
        if ok_count:
            missing_in_range = json.dumps(sorted(m for m in missing_set if 1 <= m <= eps_count))
            ok_toggle = (
                '<div class="ep-ok-toggle">'
                '<button type="button" class="ep-expand-btn" data-key="{key}" '
                'data-eps="{eps}" data-count="{n}" data-missing=\'{miss}\' '
                'onclick="expandEps(this)">Show {n} OK episode{s}</button>'
                '</div><div class="ep-ok-group"></div>'
            ).format(key=key, eps=eps_count, n=ok_count, miss=missing_in_range,
                     s="" if ok_count == 1 else "s")

        ep_add_form = ('<div class="ep-add-row">'
                       '<form method="POST" action="/ep-add" style="margin:0;display:flex;gap:8px;align-items:center;">'
                       '<input type="hidden" name="key" value="{}">'
                       '<input type="number" name="ep" min="1" placeholder="Ep #" required>'
                       '<button type="submit" class="btn btn-primary btn-sm">Add to retry</button>'
                       '</form></div>').format(key)

        ep_panel = ('<details class="ep-panel"><summary>Episodes ({} total{})</summary>'
                    '{}{}{}</details>').format(eps_count, retry_label, ep_rows, ok_toggle, ep_add_form)

        html += """
        <div class="card">
          <div style="display:flex;justify-content:space-between;align-items:start;gap:12px;">
            <div>
              <div class="anime-name">{name}</div>
              {url_html}
              <div class="anime-meta">
                <span class="badge badge-ep">{eps} eps</span>{type_badge}{missing_badge}
                {pref_badges}{tvdb_badges}{status_badges}
              </div>
              {folder_html}
            </div>
            <div style="display:flex;gap:8px;">
              <form method="POST" action="/check-now" style="margin:0;">
                <input type="hidden" name="key" value="{key}">
                <button type="submit" class="btn btn-ghost btn-sm">Check now</button>
              </form>
              <form method="POST" action="/remove" style="margin:0;">
                <input type="hidden" name="key" value="{key}">
                <button type="submit" class="btn btn-danger btn-sm" onclick="{remove_confirm}">Remove</button>
              </form>
            </div>
          </div>
          {ep_panel}
        </div>""".format(
            name=escape(name), url_html=url_html, eps=escape(str(eps)),
            type_badge=type_badge, missing_badge=missing_badge, pref_badges=pref_badges,
            tvdb_badges=tvdb_badges, status_badges=status_badges,
            folder_html=folder_html, key=key, ep_panel=ep_panel,
            remove_confirm=remove_confirm,
        )
    return html


def render_search_results(results):
    if not results:
        return ""
    html = '<div class="section"><h2>Search Results</h2>'
    for r in results:
        lang_bits = []
        if r.get("dubs"):
            lang_bits.append("Dub: " + escape(r["dubs"]))
        if r.get("subs"):
            lang_bits.append("Sub: " + escape(r["subs"]))
        lang_line = ('<div class="anime-meta">' + " &middot; ".join(lang_bits) + '</div>') if lang_bits else ""

        fields = {k: escape(str(v)) for k, v in r.items()}
        fields["lang_line"] = lang_line
        html += """
        <div class="card">
          <div style="display:flex;justify-content:space-between;align-items:start;gap:12px;">
            <div>
              <div class="anime-name">{name}</div>
              <div class="anime-meta">{type} &middot; {episodes} episodes &middot; {genre}</div>
              {lang_line}
              <div class="anime-url">{url}</div>
            </div>
            <form method="POST" action="/add-url" style="margin:0;">
              <input type="hidden" name="url" value="{url}">
              <button type="submit" class="btn btn-primary btn-sm">Add to watchlist</button>
            </form>
          </div>
        </div>""".format(**fields)
    html += "</div>"
    return html


def render_releases(anime_info, best_id=None):
    if not anime_info:
        return ""
    html = '<div class="section"><h2>Select Release for: {}</h2>'.format(escape(anime_info["name"]))
    html += """
    <div class="card" style="margin-bottom:16px;">
      <label class="hint">Folder name in /anime library:</label>
      <input type="text" id="release-folder" value="{name}" style="margin-top:4px;margin-bottom:0;">
    </div>""".format(name=escape(anime_info["name"]))

    media_type = anime_info.get("media_type", "series") or "series"
    # Every release id actually offered on this page — carried as a hidden
    # field alongside the chosen release_id so /add-release and /tvdb-seasons
    # can validate the selection is one the site really offered, without
    # re-scraping to re-derive that set.
    valid_ids = ",".join(str(rel["id"]) for rel in anime_info["releases"])
    for rel in anime_info["releases"]:
        dubs = escape(", ".join(rel["dubs"])) if rel["dubs"] else "&mdash;"
        subs = escape(", ".join(rel["subs"])) if rel["subs"] else "&mdash;"
        is_best = rel["id"] == best_id
        highlight = "card-selected" if is_best else ""
        best_label = ' <span class="badge badge-accent">Best match</span>' if is_best else ""

        html += """
        <div class="card {highlight}">
          <div class="release-row">
            <span class="badge badge-res">{res}p</span>
            <span class="badge badge-neutral">Dub: {dubs}</span>
            <span class="badge badge-neutral">Sub: {subs}</span>
            <span class="badge badge-ep">{eps} eps</span>
            <span class="hint">{size}MB &middot; {group}</span>
            {best_label}
            <form method="POST" action="/add-release" style="margin:0;margin-left:auto;"
                  onsubmit="this.querySelector('[name=custom_folder]').value=document.getElementById('release-folder').value;">
              <input type="hidden" name="url" value="{url}">
              <input type="hidden" name="name" value="{name}">
              <input type="hidden" name="release_id" value="{rid}">
              <input type="hidden" name="release_ids" value="{valid_ids}">
              <input type="hidden" name="episodes" value="{eps}">
              <input type="hidden" name="custom_folder" value="">
              <input type="hidden" name="media_type" value="{mt}">
              <button type="submit" class="btn btn-primary btn-sm">Add this release</button>
            </form>
          </div>
        </div>""".format(
            res=rel["resolution"], dubs=dubs, subs=subs, eps=rel["episodes"],
            size=rel["size_mb"], group=escape(rel["group"]), url=escape(anime_info["url"]),
            name=escape(anime_info["name"]), rid=rel["id"], highlight=highlight,
            best_label=best_label, mt=escape(media_type), valid_ids=escape(valid_ids),
        )
    html += "</div>"
    return html


def render_tvdb_step(anime_name, url, release_id, custom_folder,
                     search_results=None, seasons=None, selected_tvdb_id="",
                     selected_tvdb_name="", ep_count=0, edit_key=None,
                     media_type="series", release_ids="", episodes=0):
    """Render the TVDB correlation page shown between release selection and saving.

    When edit_key is set (the existing entry's URL), this is editing an existing
    entry — forms POST to /tvdb-save instead of /add-release, carrying the URL as
    the stable ``key`` so the save resolves the right entry even if the resolver
    shifted indices in the meantime.

    When media_type == "movie", the UI searches TVDB's movie catalogue and
    drops the season picker (movies have no seasons). Clicking "Link" on a
    result saves the tvdb_id and returns to the watchlist.

    ``release_ids``/``episodes`` are the same release-selection metadata
    render_releases first put on the page, carried forward through every
    form on this multi-step flow so /add-release and /tvdb-seasons never
    need to re-scrape to validate or re-derive them.
    """
    is_movie = (media_type == "movie")
    save_action = "/tvdb-save" if edit_key is not None else "/add-release"

    # Hidden fields carried through every form on this page
    hidden = (
        '<input type="hidden" name="url" value="{url}">'
        '<input type="hidden" name="name" value="{name}">'
        '<input type="hidden" name="release_id" value="{rid}">'
        '<input type="hidden" name="release_ids" value="{valid_ids}">'
        '<input type="hidden" name="episodes" value="{eps}">'
        '<input type="hidden" name="custom_folder" value="{folder}">'
        '<input type="hidden" name="media_type" value="{mt}">'
    ).format(url=escape(url), name=escape(anime_name),
             rid=escape(str(release_id)), folder=escape(custom_folder),
             mt=escape(media_type), valid_ids=escape(release_ids),
             eps=int(episodes) if str(episodes).lstrip("-").isdigit() else 0)
    if edit_key is not None:
        hidden += '<input type="hidden" name="key" value="{}">'.format(escape(edit_key))

    html = '<div class="section"><h2>TVDB Correlation: {}</h2>'.format(escape(anime_name))
    if is_movie:
        html += '<p class="hint">Detected as <strong>Anime Movie</strong> — searching TVDB movies. No season selection needed.</p>'
    else:
        html += '<p class="hint">Link this anime to a TVDB series and season so downloads are placed in the correct season folder.</p>'

    # Skip button — save without TVDB
    skip_label = (
        "save without TVDB link" if is_movie
        else ("save without season mapping" if edit_key is None else "cancel")
    )
    html += """
    <form method="POST" action="{action}" style="margin-bottom:16px;">
      {hidden}
      <input type="hidden" name="tvdb_skip" value="1">
      <button type="submit" class="btn btn-ghost">Skip TVDB &mdash; {skip_label}</button>
    </form>""".format(hidden=hidden, action=save_action, skip_label=skip_label)

    # Search box
    search_placeholder = "Search TVDB movies..." if is_movie else "Search TVDB..."
    search_button = "Search TVDB Movies" if is_movie else "Search TVDB"
    html += """
    <div class="card" style="margin-bottom:16px;">
      <form method="POST" action="/tvdb-search" style="display:flex;gap:8px;align-items:center;margin:0;">
        {hidden}
        <input type="text" name="query" value="{query}" placeholder="{placeholder}" style="flex:1;margin:0;">
        <button type="submit" class="btn btn-primary">{button}</button>
      </form>
    </div>""".format(hidden=hidden, query=escape(anime_name),
                      placeholder=search_placeholder, button=search_button)

    # Search results
    if search_results is not None:
        if not search_results:
            html += '<div class="status-msg status-err">No TVDB results found.</div>'
        else:
            # Movies post directly to save_action with tvdb_id;
            # series post to /tvdb-seasons to pick a season first.
            result_action = save_action if is_movie else "/tvdb-seasons"
            result_button = "Link Movie" if is_movie else "Select"
            for r in search_results[:8]:
                year_str = " ({})".format(r["year"]) if r.get("year") else ""
                overview = r.get("overview", "")
                if len(overview) > 150:
                    overview = overview[:150] + "..."
                is_selected = str(r["tvdb_id"]) == str(selected_tvdb_id)
                border = "card-selected" if is_selected else ""
                html += """
                <div class="card {border}">
                  <div style="display:flex;justify-content:space-between;align-items:start;gap:12px;">
                    <div>
                      <div class="anime-name">{name}{year}</div>
                      <div class="hint" style="margin-top:2px;">{overview}</div>
                    </div>
                    <form method="POST" action="{action}" style="margin:0;">
                      {hidden}
                      <input type="hidden" name="tvdb_id" value="{tid}">
                      <input type="hidden" name="tvdb_name" value="{name}">
                      <button type="submit" class="btn btn-primary btn-sm">{button}</button>
                    </form>
                  </div>
                </div>""".format(
                    name=escape(r["name"]), year=year_str,
                    overview=escape(overview), tid=r["tvdb_id"],
                    hidden=hidden, border=border,
                    action=result_action, button=result_button)

    # Season picker (shown after selecting a series — never for movies)
    if seasons is not None and not is_movie:
        html += '<div class="card card-accent" style="margin-top:16px;">'
        html += '<h3 style="margin:0 0 8px;">Seasons for: {}</h3>'.format(escape(selected_tvdb_name))

        # Auto-suggest: pick the season whose ep count is closest to the release ep count
        best_season = None
        if ep_count > 0:
            best_diff = float("inf")
            for s in seasons:
                diff = abs(s["episode_count"] - ep_count)
                if diff < best_diff:
                    best_diff = diff
                    best_season = s["season_number"]

        for s in seasons:
            is_suggested = s["season_number"] == best_season
            suggest_label = ' <span class="badge badge-accent">Likely match ({} eps)</span>'.format(ep_count) if is_suggested else ""
            highlight = "background:var(--accent-soft-bg);border-radius:6px;padding-left:8px;padding-right:8px;" if is_suggested else ""
            html += """
            <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;{highlight}">
              <span>
                <strong>Season {snum}</strong>
                <span class="badge badge-ep">{eps} eps</span>
                {suggest}
              </span>
              <form method="POST" action="{save_action}" style="margin:0;">
                {hidden}
                <input type="hidden" name="tvdb_id" value="{tid}">
                <input type="hidden" name="tvdb_season" value="{snum}">
                <input type="hidden" name="episode_offset" value="0">
                <button type="submit" class="btn btn-primary btn-sm">Use Season {snum}</button>
              </form>
            </div>""".format(
                snum=s["season_number"], eps=s["episode_count"],
                suggest=suggest_label, hidden=hidden,
                tid=selected_tvdb_id, highlight=highlight,
                save_action=save_action)

        # Advanced: manual offset input
        html += """
        <details style="margin-top:12px;">
          <summary class="hint" style="cursor:pointer;">Advanced: episode offset</summary>
          <div style="margin-top:8px;">
            <p class="hint" style="margin:0 0 8px;">
              Set this if anime-loads numbers episodes from 1 but TVDB continues from a previous season
              (e.g., offset 12 means ep 1 becomes E13).
            </p>
            <form method="POST" action="{save_action}" style="display:flex;gap:8px;align-items:center;margin:0;">
              {hidden}
              <input type="hidden" name="tvdb_id" value="{tid}">
              <label>Season:</label>
              <input type="number" name="tvdb_season" min="1" value="{suggested}" style="width:60px;margin:0;" required>
              <label>Offset:</label>
              <input type="number" name="episode_offset" value="0" style="width:60px;margin:0;">
              <button type="submit" class="btn btn-primary btn-sm">Save season</button>
            </form>
          </div>
        </details>""".format(hidden=hidden, tid=selected_tvdb_id,
                             suggested=best_season or 1,
                             save_action=save_action)
        html += '</div>'

    html += "</div>"
    return html


def render_page(status="", search_html="", prefs_open=False, ani_data=None):
    data = ani_data if ani_data is not None else load_ani()
    anime_list = data.get("anime", [])
    pending_list = data.get("pending", [])
    prefs = load_prefs()
    total = len(anime_list) + len(pending_list)

    activity = get_activity()
    bot_status_html, last_run_html, next_run_html = render_activity(activity)
    history_html = render_run_history(
        activity["runs"], activity.get("run_state", {}).get("runs"))

    move_status_html, move_last_html = render_move_status()
    move_history_html = render_move_history()
    move_stuck_html = render_move_stuck()

    health_html = render_health_card()

    lang_names = {"german": "German", "japanese": "Japanese", "english": "English", "any": "Any"}
    audio_pref = prefs.get("audio_language", "german")
    sub_pref = prefs.get("sub_language", "any")
    audio_display = lang_names.get(audio_pref, "Any")
    sub_display = lang_names.get(sub_pref, "Any")

    page = HTML_TEMPLATE
    page = page.replace("%%STATUS_MSG%%", status)
    page = page.replace("%%BOT_STATUS%%", bot_status_html)
    page = page.replace("%%LAST_RUN%%", last_run_html)
    page = page.replace("%%NEXT_RUN%%", next_run_html)
    page = page.replace("%%RUN_HISTORY%%", history_html)
    page = page.replace("%%HEALTH%%", health_html)
    page = page.replace("%%MOVE_STATUS%%", move_status_html)
    page = page.replace("%%MOVE_LAST_RUN%%", move_last_html)
    page = page.replace("%%MOVE_HISTORY%%", move_history_html)
    page = page.replace("%%MOVE_STUCK%%", move_stuck_html)
    page = page.replace("%%SEARCH_RESULTS%%", search_html)
    page = page.replace("%%WATCHLIST%%", render_watchlist(anime_list, pending_list))
    page = page.replace("%%COUNT%%", str(total))
    page = page.replace("%%PREFS_OPEN%%", "open" if prefs_open else "")
    page = page.replace("%%AUDIO_GER%%", 'selected' if audio_pref == "german" else "")
    page = page.replace("%%AUDIO_JAP%%", 'selected' if audio_pref == "japanese" else "")
    page = page.replace("%%AUDIO_ENG%%", 'selected' if audio_pref == "english" else "")
    page = page.replace("%%AUDIO_ANY%%", 'selected' if audio_pref == "any" else "")
    page = page.replace("%%SUB_GER%%", 'selected' if sub_pref == "german" else "")
    page = page.replace("%%SUB_JAP%%", 'selected' if sub_pref == "japanese" else "")
    page = page.replace("%%SUB_ENG%%", 'selected' if sub_pref == "english" else "")
    page = page.replace("%%SUB_ANY%%", 'selected' if sub_pref == "any" else "")
    page = page.replace("%%RES_480%%", 'selected' if prefs["min_resolution"] == 480 else "")
    page = page.replace("%%RES_720%%", 'selected' if prefs["min_resolution"] == 720 else "")
    page = page.replace("%%RES_1080%%", 'selected' if prefs["min_resolution"] == 1080 else "")
    page = page.replace("%%AUTO_CHECKED%%", 'checked' if prefs.get("auto_select") else "")
    page = page.replace("%%PREF_AUDIO_DISPLAY%%", audio_display)
    page = page.replace("%%PREF_SUB_DISPLAY%%", sub_display)
    page = page.replace("%%PREF_RES%%", str(prefs["min_resolution"]))
    page = page.replace("%%AUTO_BADGE%%",
        '<span class="badge badge-auto">Auto-select ON</span>' if prefs.get("auto_select") else "")

    return page


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _respond(self, code, html):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _redirect(self, url):
        self.send_response(303)
        self.send_header("Location", url)
        self.end_headers()

    def _redirect_msg(self, msg, level=None):
        """Redirect home with a status banner message, URL-encoded so a name
        containing ``& = # %`` survives intact — parse_qs decodes it on the GET
        side. A raw ``/?msg=...`` truncated everything after the first ``&``.

        ``level`` ("ok"/"err") lets a caller state the banner tone explicitly
        instead of the GET side guessing it from a leading "Error" — a
        failure message that doesn't start with that word (e.g. "Could not
        fetch releases: ...") otherwise renders as a green success. Omitted,
        do_GET falls back to the old prefix sniff, so an un-migrated caller
        keeps its previous behavior."""
        params = {"msg": msg}
        if level is not None:
            params["level"] = level
        self._redirect("/?" + urlencode(params))

    def _read_post(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        params = {}
        for pair in body.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[unquote(k.replace("+", " "))] = unquote(v.replace("+", " "))
        return params

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/status":
            activity = get_activity()
            bot_status_html, last_run_html, next_run_html = render_activity(activity)
            history_html = render_run_history(
                activity["runs"], activity.get("run_state", {}).get("runs"))
            move_status_html, move_last_html = render_move_status()
            move_history_html = render_move_history()
            move_stuck_html = render_move_stuck()
            health_html = render_health_card()
            payload = json.dumps({
                "bot_status": bot_status_html,
                "last_run": last_run_html,
                "next_run": next_run_html,
                "run_history": history_html,
                "health": health_html,
                "move_status": move_status_html,
                "move_last_run": move_last_html,
                "move_history": move_history_html,
                "move_stuck": move_stuck_html,
                "bot_running": activity["status"].get("running", False),
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
            return

        status = ""

        qs = parse_qs(parsed.query)
        if "msg" in qs:
            msg = qs["msg"][0]
            level = qs.get("level", [None])[0]
            if level not in ("ok", "err"):
                level = "ok" if not msg.startswith("Error") else "err"
            cls = "status-ok" if level == "ok" else "status-err"
            status = '<div class="status-msg {}" id="status-msg">{}</div>'.format(cls, escape(msg))

        try:
            page = render_page(status=status)
        except anistore.CorruptStoreError as e:
            _log.error("[watchlist] ani.json is corrupt, refusing to render it: %s", e)
            status = '<div class="status-msg status-err" id="status-msg">Error: ani.json is corrupt — the watchlist can\'t be shown or edited until it is fixed or restored.</div>'
            page = render_page(status=status, ani_data={"settings": {}, "anime": []})

        self._respond(200, page)

    def do_POST(self):
        parsed = urlparse(self.path)
        params = self._read_post()
        try:
            self._dispatch_post(parsed, params)
        except anistore.CorruptStoreError as e:
            # Refuse to save over a corrupt file — a POST handler that got
            # this far already found the file unreadable before it could
            # mutate/save anything, so nothing was written.
            _log.error("[watchlist] ani.json is corrupt, refused POST %s: %s", parsed.path, e)
            self._redirect_msg(
                "Error: ani.json is corrupt — refused to save. Fix or restore the file, then reload.",
                level="err")

    def _dispatch_post(self, parsed, params):
        if parsed.path == "/run-now":
            ok, msg = trigger_run_now()
            if ok:
                _log.info("[bot] Run now requested via dashboard")
                self._redirect_msg(msg)
            else:
                _log.warning("[bot] Run now request rejected: %s", msg)
                self._redirect_msg("Error: {}".format(msg), level="err")

        elif parsed.path == "/check-now":
            entry_url = params.get("key", "")
            ok, msg = trigger_run_now(entry_url=entry_url)
            if ok:
                _log.info("[bot] Check now requested via dashboard: %s", entry_url)
                self._redirect_msg(msg)
            else:
                _log.warning("[bot] Check now request rejected: %s", msg)
                self._redirect_msg("Error: {}".format(msg), level="err")

        elif parsed.path == "/move-now":
            _log.info("[mover] Move Now triggered via dashboard")
            _move_trigger.set()
            self._redirect_msg("Move cycle triggered")

        elif parsed.path == "/move-stuck-ignore":
            key = params.get("key", "")
            msg = stuck_ignore(key)
            if msg is not None:
                _log.info("[mover] Ignoring stuck item: %s", msg)
                self._redirect_msg("Ignoring: {}".format(msg))
            else:
                self._redirect_msg("Error: stuck item not found", level="err")

        elif parsed.path == "/move-stuck-delete":
            key = params.get("key", "")
            result = stuck_delete_download(key)
            if result is None:
                self._redirect_msg("Error: stuck item not found", level="err")
            elif result.startswith("error:"):
                self._redirect_msg("Error: {}".format(result[len("error:"):]), level="err")
            else:
                _log.info("[mover] Deleted downloaded copy: %s", result)
                self._redirect_msg("Deleted download copy: {}".format(result))

        elif parsed.path == "/move-stuck-anyway":
            key = params.get("key", "")
            msg = stuck_move_anyway(key)
            if msg is not None:
                _log.info("[mover] Will move anyway on next cycle: %s", msg)
                self._redirect_msg("Will move on next cycle: {}".format(msg))
            else:
                self._redirect_msg("Error: stuck item not found", level="err")

        elif parsed.path == "/save-prefs":
            try:
                min_resolution = int(params.get("min_resolution", "1080"))
            except ValueError:
                self._redirect_msg(
                    "Error: minimum resolution must be a number", level="err")
                return
            prefs = {
                "audio_language": params.get("audio_language", "german"),
                "sub_language": params.get("sub_language", "any"),
                "min_resolution": min_resolution,
                "auto_select": "auto_select" in params,
            }
            save_prefs(prefs)
            self._redirect_msg("Preferences saved")

        elif parsed.path == "/add-url":
            url = params.get("url", "").strip()
            if not url or "anime-loads.org" not in url:
                self._redirect_msg("Error: Invalid URL")
                return

            # Cheap pre-check so re-adding something already present skips
            # the scrape below entirely. Not the authoritative check — that
            # happens inside update_ani's single lock hold further down, so
            # a concurrent add (or a bot/resolver write landing while this
            # request's scrape is in flight) is never missed or clobbered.
            existing = load_ani()
            all_entries = existing.get("anime", []) + existing.get("pending", [])
            if any(a.get("url") == url for a in all_entries):
                self._redirect_msg("Already in watchlist")
                return

            # Fetch releases from site so user can see what's available. No
            # anistore lock is held across this network/Selenium call — see
            # update_ani's docstring for why that matters now the server is
            # threaded.
            anime_info, err = get_releases(url)
            if err or not anime_info or not anime_info.get("releases"):
                # Fallback: queue to pending if fetch fails. Dedupe + append
                # happen in ONE lock hold so a write that landed during the
                # multi-second scrape above (e.g. the bot, or another /add-url)
                # can't be silently overwritten by this request's stale
                # pre-scrape snapshot.
                slug = url.rstrip("/").split("/")[-1]
                name = slug.replace("-", " ").title()
                already_present = False

                def _add_pending(data):
                    nonlocal already_present
                    all_entries = data.get("anime", []) + data.get("pending", [])
                    if any(a.get("url") == url for a in all_entries):
                        already_present = True
                        return
                    data.setdefault("pending", []).append(
                        {"url": url, "name": name, "status": "pending"})

                update_ani(_add_pending)
                if already_present:
                    self._redirect_msg("Already in watchlist")
                    return
                msg = "Could not fetch releases{}, added to pending queue".format(
                    ": " + err if err else "")
                self._redirect_msg(msg)
                return

            # Show release selection page
            prefs = load_prefs()
            best = pick_best_release(anime_info["releases"], prefs)
            best_id = best["id"] if best else None
            search_html = render_releases(anime_info, best_id)
            self._respond(200, render_page(search_html=search_html))

        elif parsed.path == "/add-release":
            url = params.get("url", "").strip()
            name = params.get("name", "Unknown")
            custom_folder = params.get("custom_folder", "").strip()

            data = load_ani()
            for a in data.get("anime", []):
                if a.get("url") == url:
                    self._redirect_msg("Already in watchlist")
                    return

            # Validate the release_id / media_type / episode-count carried
            # as hidden fields from the release-selection page (or the TVDB
            # step that followed it) rather than trusting them outright —
            # see _resolve_release_selection's docstring for the
            # validate-or-rescrape rule this applies. No release_id at all
            # is a legitimate call shape (adding without ever going through
            # release selection) — only a *posted-but-unconfirmable* one
            # (tampered, or stale beyond recovery) is rejected outright.
            posted_release_id = params.get("release_id", "")
            release_id, media_type, episodes = _resolve_release_selection(url, params)
            if posted_release_id and not release_id:
                self._redirect_msg(
                    "Error: invalid release selection — please fetch releases again")
                return

            # If TVDB is available and user hasn't been through the TVDB step yet,
            # show the correlation page instead of saving immediately.
            # For movies the "through the TVDB step" signal is either tvdb_skip
            # or a posted tvdb_id — there's no tvdb_season field to look for.
            has_tvdb_data = (
                "tvdb_season" in params
                or "tvdb_skip" in params
                or (media_type == "movie" and "tvdb_id" in params)
            )
            if tvdb.available and not has_tvdb_data:
                results = tvdb.search(
                    name,
                    content_type="movie" if media_type == "movie" else "series")
                search_html = render_tvdb_step(
                    name, url, release_id, custom_folder,
                    search_results=results, media_type=media_type,
                    release_ids=params.get("release_ids", ""), episodes=episodes)
                self._respond(200, render_page(search_html=search_html))
                return

            folder_name_raw = custom_folder if custom_folder else name
            folder_name = _safe_folder_segment(folder_name_raw)
            if not folder_name:
                self._redirect_msg("Error: invalid folder name")
                return

            entry = {
                "url": url,
                "name": name,
                "episodes": 0,
                "missing": [],
                "customPackage": folder_name,
            }
            if release_id:
                try:
                    entry["releaseID"] = int(release_id)
                except ValueError:
                    entry["releaseID"] = release_id

            # Add TVDB fields if provided
            tvdb_id = params.get("tvdb_id", "")
            tvdb_season = params.get("tvdb_season", "")
            episode_offset = params.get("episode_offset", "")
            if tvdb_id:
                try:
                    entry["tvdb_id"] = int(tvdb_id)
                except ValueError:
                    pass
            if tvdb_season:
                try:
                    entry["tvdb_season"] = int(tvdb_season)
                except ValueError:
                    pass
            if episode_offset:
                try:
                    offset = int(episode_offset)
                    if offset != 0:
                        entry["episode_offset"] = offset
                except ValueError:
                    pass

            # No scraping happens below this point in this request (the TVDB
            # correlation branch above already returned) — safe to do the
            # final duplicate re-check + append inside one lock hold.
            already_present = False

            def _add_release(data):
                nonlocal already_present
                for a in data.get("anime", []):
                    if a.get("url") == url:
                        already_present = True
                        return
                data.setdefault("anime", []).append(entry)

            update_ani(_add_release)
            if already_present:
                self._redirect_msg("Already in watchlist")
                return

            season_info = ""
            if entry.get("tvdb_season"):
                season_info = ", season {}".format(entry["tvdb_season"])
            _log.info("[watchlist] Added: %s (folder=%s, tvdb_id=%s%s)",
                      name, folder_name, entry.get("tvdb_id", "-"), season_info)
            folder_display = (
                folder_name if folder_name == folder_name_raw
                else "{} (saved as '{}')".format(folder_name_raw, folder_name))
            self._redirect_msg("Added: {} (folder: {}{})".format(
                name, folder_display, season_info))

        elif parsed.path == "/tvdb-search":
            url = params.get("url", "").strip()
            name = params.get("name", "Unknown")
            release_id = params.get("release_id", "")
            custom_folder = params.get("custom_folder", "").strip()
            query = params.get("query", "").strip() or name
            media_type = params.get("media_type", "series")
            edit_key = params.get("key")

            content_type = "movie" if media_type == "movie" else "series"
            results = tvdb.search(query, content_type=content_type) if tvdb.available else []
            search_html = render_tvdb_step(
                name, url, release_id, custom_folder,
                search_results=results, edit_key=edit_key,
                media_type=media_type,
                release_ids=params.get("release_ids", ""),
                episodes=params.get("episodes", 0))
            self._respond(200, render_page(search_html=search_html))

        elif parsed.path == "/tvdb-seasons":
            url = params.get("url", "").strip()
            name = params.get("name", "Unknown")
            custom_folder = params.get("custom_folder", "").strip()
            tvdb_id = params.get("tvdb_id", "")
            tvdb_name = params.get("tvdb_name", "")
            edit_key = params.get("key")

            # Fetch seasons for the selected series
            seasons = tvdb.get_seasons(tvdb_id) if tvdb.available and tvdb_id else []

            # Re-run the search so results stay visible
            results = tvdb.search(name) if tvdb.available else []

            # Validate the release_id / media_type / episode-count carried
            # from the release step (for the season auto-suggestion) rather
            # than re-scraping to look them up — see
            # _resolve_release_selection's docstring for the
            # validate-or-rescrape rule this applies. An invalid/tampered
            # release_id resolves to "" here (no auto-suggestion); the
            # actual save at /add-release rejects it outright.
            release_id, media_type, ep_count = _resolve_release_selection(url, params)

            # Editing an existing entry has no release_id, so fall back to the
            # entry's stored episode count — otherwise the "Likely match" season
            # suggestion never appears on the Link-TVDB-from-watchlist path (UI-3).
            if not ep_count and edit_key is not None:
                try:
                    watchlist = load_ani().get("anime", [])
                    _, w_entry = find_entry_by_url(watchlist, edit_key)
                    if w_entry is not None:
                        ep_count = int(w_entry.get("episodes", 0) or 0)
                except (ValueError, TypeError):
                    pass

            search_html = render_tvdb_step(
                name, url, release_id, custom_folder,
                search_results=results, seasons=seasons,
                selected_tvdb_id=tvdb_id, selected_tvdb_name=tvdb_name or name,
                ep_count=ep_count, edit_key=edit_key,
                media_type=media_type,
                release_ids=params.get("release_ids", ""), episodes=ep_count)
            self._respond(200, render_page(search_html=search_html))

        elif parsed.path == "/search":
            query = params.get("q", "").strip()
            if not query:
                self._redirect_msg("Error: Empty search", level="err")
                return

            results, err = search_anime(query)

            if err:
                search_html = '<div class="section"><div class="status-msg status-err">Search error: {}</div></div>'.format(escape(err))
            elif not results:
                search_html = '<div class="section"><div class="status-msg status-err">No results for &quot;{}&quot;</div></div>'.format(escape(query))
            else:
                search_html = render_search_results(results)

            self._respond(200, render_page(search_html=search_html))

        elif parsed.path == "/remove-pending":
            entry_url = params.get("key", "")
            removed = None

            def _remove_pending(data):
                nonlocal removed
                pending = data.get("pending", [])
                idx, entry = find_entry_by_url(pending, entry_url)
                if entry is not None:
                    pending.pop(idx)
                    data["pending"] = pending
                    removed = entry

            update_ani(_remove_pending)
            if removed is not None:
                _log.info("[watchlist] Removed pending: %s", removed.get("name") or removed.get("url", "?"))
                self._redirect_msg("Removed: {}".format(removed.get("name", "?")))
            else:
                self._redirect_msg("Error: entry not found", level="err")

        elif parsed.path == "/remove":
            entry_url = params.get("key", "")
            removed = None

            def _remove(data):
                nonlocal removed
                anime_list = data.get("anime", [])
                idx, entry = find_entry_by_url(anime_list, entry_url)
                if entry is not None:
                    anime_list.pop(idx)
                    removed = entry

            update_ani(_remove)
            if removed is not None:
                _log.info("[watchlist] Removed anime: %s", removed.get("name", "?"))
                self._redirect_msg("Removed: {}".format(removed.get("name", "?")))
            else:
                self._redirect_msg("Error: entry not found", level="err")

        elif parsed.path == "/ep-add":
            entry_url = params.get("key", "")
            try:
                ep = int(params.get("ep", -1))
            except ValueError:
                self._redirect_msg(
                    "Error: episode number must be numeric", level="err")
                return
            outcome = {}

            def _ep_add(data):
                anime_list = data.get("anime", [])
                _, entry = find_entry_by_url(anime_list, entry_url)
                if entry is None:
                    outcome["result"] = "invalid"
                    return
                # Bound to the site's known ANNOUNCED total (al_max_episodes)
                # — NOT entry["episodes"] (highest already downloaded) and
                # NOT al_available_max (the currently-published cap): adding
                # the next, not-yet-published episode number is the
                # documented manual override for one that went up early —
                # by definition beyond al_available_max — so bounding on
                # either of those would block exactly that. animeloads.py
                # uses 999999 as its "unknown announced total" sentinel, so
                # that value means unknown here too, falling through to a
                # generous sanity cap.
                al_max_episodes = entry.get("al_max_episodes")
                if isinstance(al_max_episodes, (int, float)) and 0 < al_max_episodes < 999999:
                    ep_max = al_max_episodes
                else:
                    ep_max = 5000
                if ep <= 0 or ep > ep_max:
                    outcome["result"] = "invalid"
                    return
                outcome["name"] = entry.get("name", "?")
                missing = entry.get("missing", [])
                if ep in missing:
                    outcome["result"] = "already"
                    return
                missing.append(ep)
                missing.sort()
                entry["missing"] = missing
                outcome["result"] = "added"

            update_ani(_ep_add)
            if outcome["result"] == "added":
                self._redirect_msg("Added episode {} to retry queue for {}".format(ep, outcome["name"]))
            elif outcome["result"] == "already":
                self._redirect_msg("Episode {} already in retry queue".format(ep))
            else:
                self._redirect_msg("Error: entry not found or invalid episode", level="err")

        elif parsed.path == "/ep-remove":
            entry_url = params.get("key", "")
            try:
                ep = int(params.get("ep", -1))
            except ValueError:
                self._redirect_msg(
                    "Error: episode number must be numeric", level="err")
                return
            outcome = {}

            def _ep_remove(data):
                anime_list = data.get("anime", [])
                _, entry = find_entry_by_url(anime_list, entry_url)
                if entry is None or ep <= 0:
                    outcome["result"] = "invalid"
                    return
                outcome["name"] = entry.get("name", "?")
                missing = entry.get("missing", [])
                if ep not in missing:
                    outcome["result"] = "not_queued"
                    return
                missing.remove(ep)
                entry["missing"] = missing
                outcome["result"] = "removed"

            update_ani(_ep_remove)
            if outcome["result"] == "removed":
                self._redirect_msg("Removed episode {} from retry queue for {}".format(ep, outcome["name"]))
            elif outcome["result"] == "not_queued":
                self._redirect_msg("Episode {} not in retry queue".format(ep))
            else:
                self._redirect_msg("Error: entry not found or invalid episode", level="err")

        elif parsed.path == "/tvdb-link":
            entry_url = params.get("key", "")
            data = load_ani()
            anime_list = data.get("anime", [])
            _, entry = find_entry_by_url(anime_list, entry_url)
            if entry is not None and tvdb.available:
                name = entry.get("name", "Unknown")
                url = entry.get("url", "")
                media_type = entry.get("media_type", "series")
                content_type = "movie" if media_type == "movie" else "series"
                results = tvdb.search(name, content_type=content_type)
                search_html = render_tvdb_step(
                    name, url, "", "",
                    search_results=results, edit_key=url,
                    media_type=media_type)
                self._respond(200, render_page(search_html=search_html))
            else:
                self._redirect_msg("Error: entry not found or TVDB unavailable", level="err")

        elif parsed.path == "/tvdb-save":
            entry_url = params.get("key", "")

            if "tvdb_skip" in params:
                # Cancelling a TVDB edit makes no changes — say so, otherwise the
                # bare redirect home looks like the click did nothing (UI-5).
                # Read-only lookup: no mutation, so no need for update_ani here.
                _, entry = find_entry_by_url(load_ani().get("anime", []), entry_url)
                if entry is not None:
                    self._redirect_msg("Cancelled — {} unchanged".format(
                        entry.get("name", "?")))
                else:
                    self._redirect_msg("Cancelled")
                return

            tvdb_id = params.get("tvdb_id", "")
            tvdb_season = params.get("tvdb_season", "")
            episode_offset = params.get("episode_offset", "")
            outcome = {}

            def _tvdb_save(data):
                anime_list = data.get("anime", [])
                _, entry = find_entry_by_url(anime_list, entry_url)
                if entry is None:
                    outcome["result"] = "not_found"
                    return
                if tvdb_id:
                    try:
                        entry["tvdb_id"] = int(tvdb_id)
                    except ValueError:
                        pass
                if tvdb_season:
                    try:
                        entry["tvdb_season"] = int(tvdb_season)
                    except ValueError:
                        pass
                if episode_offset:
                    try:
                        offset = int(episode_offset)
                        if offset != 0:
                            entry["episode_offset"] = offset
                        elif "episode_offset" in entry:
                            del entry["episode_offset"]
                    except ValueError:
                        pass
                outcome["result"] = "saved"
                outcome["name"] = entry.get("name", "?")
                outcome["season_str"] = (
                    " S{:02d}".format(entry.get("tvdb_season", 0)) if entry.get("tvdb_season") else "")

            update_ani(_tvdb_save)
            if outcome.get("result") == "saved":
                self._redirect_msg("TVDB linked: {}{}".format(outcome["name"], outcome["season_str"]))
            else:
                self._redirect_msg("Error: entry not found", level="err")

        elif parsed.path == "/tvdb-unlink":
            entry_url = params.get("key", "")
            outcome = {}

            def _tvdb_unlink(data):
                anime_list = data.get("anime", [])
                _, entry = find_entry_by_url(anime_list, entry_url)
                if entry is None:
                    outcome["result"] = "not_found"
                    return
                for field in ("tvdb_id", "tvdb_season", "episode_offset"):
                    entry.pop(field, None)
                outcome["result"] = "unlinked"
                outcome["name"] = entry.get("name", "?")

            update_ani(_tvdb_unlink)
            if outcome.get("result") == "unlinked":
                self._redirect_msg("TVDB unlinked: {}".format(outcome["name"]))
            else:
                self._redirect_msg("Error: entry not found", level="err")

        elif parsed.path == "/update-folder":
            entry_url = params.get("key", "")
            folder_raw = params.get("folder", "").strip()
            folder = _safe_folder_segment(folder_raw)
            outcome = {}

            def _update_folder(data):
                anime_list = data.get("anime", [])
                _, entry = find_entry_by_url(anime_list, entry_url)
                if entry is None or not folder:
                    outcome["result"] = "invalid"
                    return
                entry["customPackage"] = folder
                outcome["result"] = "updated"
                outcome["name"] = entry.get("name", "?")

            update_ani(_update_folder)
            if outcome.get("result") == "updated":
                shown = folder if folder == folder_raw else "{} (saved as '{}')".format(folder_raw, folder)
                self._redirect_msg("Folder updated: {} -> {}".format(outcome["name"], shown))
            else:
                self._redirect_msg("Error: entry not found or empty folder", level="err")

        elif parsed.path == "/mark-incomplete":
            entry_url = params.get("key", "")
            outcome = {}

            def _mark_incomplete(data):
                anime_list = data.get("anime", [])
                _, entry = find_entry_by_url(anime_list, entry_url)
                if entry is None:
                    outcome["result"] = "not_found"
                    return
                for field in ("complete", "skip_until"):
                    entry.pop(field, None)
                outcome["result"] = "marked"
                outcome["name"] = entry.get("name", "?")

            update_ani(_mark_incomplete)
            if outcome.get("result") == "marked":
                self._redirect_msg("Marked incomplete: {}".format(outcome["name"]))
            else:
                self._redirect_msg("Error: entry not found", level="err")

        else:
            self._redirect("/")


def apply_resolved_pending(data, resolved_entries, no_match_urls=()):
    """Merge resolve_pending()'s per-cycle scrape results onto a FRESH
    ani.json snapshot, instead of saving the whole stale "pending"/"anime"
    snapshot the resolver started its (multi-second, per-entry) scrape pass
    with. Callers must load `data` fresh under the lock right before calling
    this (see resolve_pending(), which scrapes entirely OUTSIDE the lock and
    only takes it for this merge) — mirrors anistore.merge_entry, but this
    resolve step moves an entry between two collections rather than editing
    fields on one.

    `resolved_entries`: ready-to-insert anime-list entries (each with its
    own "url"). For each one, the matching URL is dropped from the fresh
    `pending` list — a no-op, not an error, if it's no longer there (the
    dashboard already removed it, or a previous pass already migrated it) —
    and the entry is appended to `anime`, unless one with that URL is
    already present there (avoids a duplicate on a retried resolve).

    `no_match_urls`: URLs whose fresh `pending` entry should be flagged
    `no_match = True` (silently skipped if no longer pending).

    A `pending` entry the dashboard added after the scrape started, and any
    entry named in neither argument, is left untouched.
    """
    resolved_by_url = {e["url"]: e for e in resolved_entries}
    fresh_pending = data.get("pending", [])
    fresh_anime = data.setdefault("anime", [])
    existing_urls = {e.get("url") for e in fresh_anime}

    remaining = []
    for p in fresh_pending:
        url = p.get("url")
        if url in resolved_by_url:
            if url not in existing_urls:
                fresh_anime.append(resolved_by_url[url])
                existing_urls.add(url)
            continue
        if url in no_match_urls:
            p["no_match"] = True
        remaining.append(p)
    data["pending"] = remaining
    return data


def resolve_pending():
    """Background thread: resolve pending entries and move to anime list."""
    time.sleep(RESOLVE_PENDING_STARTUP_DELAY)

    while True:
        try:
            data = load_ani()
            pending = data.get("pending", [])

            if not pending:
                time.sleep(RESOLVE_PENDING_EMPTY_INTERVAL)
                continue

            prefs = load_prefs()
            resolved_entries = []
            no_match_urls = set()

            for entry in pending:
                url = entry.get("url", "")
                if not url:
                    continue

                _log.info("[resolver] Resolving: %s", entry.get("name", url))
                try:
                    info, err = get_releases(url)
                    if err or not info or not info.get("releases"):
                        _log.warning("[resolver] Failed for %s: %s", url, err)
                        continue

                    entry_prefs = dict(prefs)
                    audio_override = entry.get("pref_audio_language", entry.get("pref_language"))
                    if audio_override:
                        entry_prefs["audio_language"] = audio_override
                    if entry.get("pref_sub_language"):
                        entry_prefs["sub_language"] = entry["pref_sub_language"]
                    if entry.get("pref_resolution"):
                        entry_prefs["min_resolution"] = entry["pref_resolution"]

                    best = pick_best_release(info["releases"], entry_prefs)
                    if best:
                        anime_entry = {
                            "url": info["url"],
                            "name": info["name"],
                            "releaseID": best["id"],
                            "episodes": 0,
                            "missing": [],
                            "customPackage": info["name"],
                        }
                        resolved_entries.append(anime_entry)
                        _log.info("[resolver] Resolved %s -> release %d (%sp, %s)",
                            info["name"], best["id"], best["resolution"],
                            ", ".join(best.get("dubs", [])))
                    elif not entry.get("no_match"):
                        # Releases exist but none match the strict language prefs.
                        # Surface this so the entry doesn't sit unresolved forever.
                        no_match_urls.add(url)
                        _log.info("[resolver] No release matches prefs for %s", info["name"])
                except Exception as e:
                    _log.error("[resolver] Error resolving %s: %s", url, e)

                time.sleep(RESOLVE_PENDING_PER_ENTRY_DELAY)

            if resolved_entries or no_match_urls:
                update_ani(lambda d: apply_resolved_pending(d, resolved_entries, no_match_urls))
                if resolved_entries:
                    _log.info("[resolver] Moved %d entries to anime list", len(resolved_entries))

        except Exception as e:
            _log.error("[resolver] Error: %s", e)

        time.sleep(RESOLVE_PENDING_BATCH_INTERVAL)


def move_completed_worker():
    """Background thread: move completed downloads to media library."""
    time.sleep(MOVE_STARTUP_DELAY)
    global _move_last_run, _move_running

    while True:
        try:
            _move_running = True
            events = run_move_cycle()
            with _move_lock:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                for ev in events:
                    ev["time"] = ts
                    _move_history.append(ev)
                _move_last_run = datetime.now(timezone.utc)
            _move_running = False
            save_move_state()

            if events:
                counts = {"moved": 0, "error": 0, "skip": 0, "wait": 0, "cleanup": 0}
                for ev in events:
                    t = ev["type"]
                    counts[t] = counts.get(t, 0) + 1
                    msg = ev.get("msg", "")
                    if t == "moved":
                        _log.info("[mover] %s", msg)
                    elif t == "error":
                        _log.warning("[mover] %s", msg)
                    else:
                        _log.debug("[mover] [%s] %s", t, msg)
                summary = "Cycle: moved=%d errors=%d skipped=%d waiting=%d cleanup=%d" % (
                    counts["moved"], counts["error"], counts["skip"],
                    counts["wait"], counts["cleanup"])
                if counts["moved"] or counts["error"]:
                    _log.info("[mover] %s", summary)
                else:
                    _log.debug("[mover] %s", summary)
        except Exception as e:
            _move_running = False
            _log.error("[mover] Error: %s", e)

        _move_trigger.wait(timeout=MOVE_POLL_SECONDS)
        _move_trigger.clear()


if __name__ == "__main__":
    _log.info("Anime-Loads Dashboard starting on port %d", PORT)

    resolver = threading.Thread(target=resolve_pending, daemon=True)
    resolver.start()

    mover = threading.Thread(target=move_completed_worker, daemon=True)
    mover.start()

    # ThreadingHTTPServer (daemon_threads=True by default) so a slow
    # Selenium-backed handler (add-anime's get_releases, up to ~a minute)
    # can't freeze the whole dashboard, including the 10s /api/status poll,
    # for every other concurrent request.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
