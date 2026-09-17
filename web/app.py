#!/usr/bin/env python3
"""
Anime-Loads Dashboard — web UI for managing watchlist and monitoring bot activity.
Manages the ani.json watchlist for the pfuenzle/anime-loads bot.
Reads bot logs and triggers runs via Docker socket.
"""

import base64
import collections
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
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

# Optional dashboard auth: HTTP Basic, enabled only when BOTH are set — read
# once at import time (not per-request) so a test can still flip these on the
# module to exercise both states. Off (today's behavior) when either is unset.
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "")
AUTH_ENABLED = bool(DASHBOARD_USER and DASHBOARD_PASS)


def _normalize_origin(value):
    """A raw DASHBOARD_ALLOWED_ORIGINS entry as a lowercase "scheme://host:port"
    string, tolerating a trailing slash / stray whitespace. Falls back to a
    lowercased, stripped copy of the input if it doesn't parse as scheme+host,
    so a malformed entry just never matches instead of raising at import."""
    value = value.strip()
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return value.lower()
    return "{}://{}".format(parsed.scheme.lower(), parsed.netloc.lower())


# Explicit CSRF allowlist (full "scheme://host:port" origins, comma-separated)
# for a reverse-proxy setup where neither the request's own Host nor
# X-Forwarded-Host lines up with the browser's Origin/Referer — see
# Handler._check_csrf's docstring.
DASHBOARD_ALLOWED_ORIGINS = {
    _normalize_origin(o) for o in os.environ.get("DASHBOARD_ALLOWED_ORIGINS", "").split(",") if o.strip()
}

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
        """Container state, plus ``docker_available`` distinguishing "the socket
        is unreachable, so we simply don't know" from "we asked Docker and it
        said the container isn't running" — the caller must not conflate the
        two into one red "Stopped"."""
        data = self._request("GET", "/containers/{}/json".format(container))
        if not data:
            return {"status": "unavailable", "started": "", "running": False,
                    "docker_available": False}
        try:
            info = json.loads(data)
            state = info.get("State", {})
            return {
                "status": state.get("Status", "unknown"),
                "started": state.get("StartedAt", ""),
                "running": state.get("Running", False),
                "docker_available": True,
            }
        except (json.JSONDecodeError, KeyError):
            return {"status": "error", "started": "", "running": False,
                    "docker_available": True}

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
import notify  # noqa: E402 — path set above
import config_defaults  # noqa: E402 — path set above

NOTIFY_TARGETS = notify.parse_targets(os.environ.get("NOTIFY_URL", ""))

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


def format_next_run_display(state_last, now=None):
    """Next-run ETA string from the persisted run-state record: absolute local
    time plus the relative countdown, e.g. "18:44 (in ~31 min)" (or "Thu 18:44
    (in ~31 min)" once it is not today, "18:44 (overdue ~5 min)" past due).
    Returns "" if it cannot be derived. ``now`` is the UTC instant (naive) to
    measure the ETA from; injectable so tests don't depend on wall-clock
    drift."""
    next_time = _parse_state_ts(state_last.get("next_run_ts"))
    if next_time is None:
        return ""
    try:
        delay = int(state_last.get("timedelay") or 0)
    except (ValueError, TypeError):
        delay = 0
    now = now if now is not None else datetime.now(timezone.utc).replace(tzinfo=None)
    relative = _humanize_eta(next_time, now, delay)
    absolute = format_day_time(_to_local(next_time), fmt="%H:%M", now=now)
    rel = "in {}".format(relative) if next_time > now else relative
    return "{} ({})".format(absolute, rel)


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

    # Surfaced so render_activity can stay consistent with the Health panel's
    # own "Bot Cycles" row instead of independently claiming everything is
    # fine while Health already flagged a stale cycle.
    result["staleness"] = check_bot_staleness(run_state)

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
    instead of rendering/saving over a wiped-looking watchlist.

    Seeds a fresh, fully-defaulted ani.json the first time anyone (dashboard
    or bot) looks for it and finds none — never overwrites a real, existing
    file (see anistore.seed_if_missing). Without this, "no ani.json yet"
    used to mean the dashboard handed back {"settings": {}, ...}, a config
    the bot could never boot with."""
    anistore.seed_if_missing(ANI_JSON, config_defaults.default_ani_data)
    with anistore.locked(ANI_JSON):
        return anistore.load(ANI_JSON, default=config_defaults.default_ani_data())


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
    it runs while the lock is held; do any scraping before calling this.

    Seeds a fresh, fully-defaulted ani.json first if none exists yet (see
    load_ani's docstring) — a POST arriving before any GET has must not
    persist a bare {"settings": {}, ...} skeleton."""
    anistore.seed_if_missing(ANI_JSON, config_defaults.default_ani_data)
    return anistore.update(ANI_JSON, fn, default=config_defaults.default_ani_data())


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
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
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
                if entry.get("paused"):
                    outcome["result"] = "paused"
                    outcome["name"] = entry.get("name", "?")
                    return
                entry["force_check"] = True
                outcome["result"] = "ok"
                outcome["name"] = entry.get("name", "?")

            update_ani(_set_force_check)
            if outcome.get("result") == "paused":
                return False, "{} is paused. Resume it first".format(outcome["name"])
            if outcome.get("result") != "ok":
                return False, "Entry not found"
            name = outcome.get("name")

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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


def normalize_anime_url(url):
    """Comparison key for an anime-loads URL, used only for duplicate checks
    (the entry keeps the URL exactly as submitted).

    Folds the variants that point at the same show: scheme and host case,
    http vs https, a leading ``www.``, and a trailing slash. The path itself
    stays case-sensitive and the query string is kept."""
    raw = (url or "").strip()
    try:
        p = urlparse(raw)
        port = p.port
    except ValueError:
        return raw
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    scheme = p.scheme.lower()
    if scheme == "http":
        scheme = "https"
    return "{}://{}{}{}{}".format(
        scheme, host, ":{}".format(port) if port else "",
        p.path.rstrip("/"), "?" + p.query if p.query else "")


def find_duplicate_entry(data, url):
    """First entry in ``anime`` or ``pending`` whose URL normalizes to the
    same key as ``url`` (see normalize_anime_url), else None."""
    key = normalize_anime_url(url)
    if not key:
        return None
    for entry in data.get("anime", []) + data.get("pending", []):
        if normalize_anime_url(entry.get("url", "")) == key:
            return entry
    return None


def _duplicate_msg(entry):
    return "Already in watchlist: {}".format(
        entry.get("name") or entry.get("url") or "?")


# ---------------------------------------------------------------------------
# Landing anchors: where a redirect after an action puts the user back
# ---------------------------------------------------------------------------

# Page sections a redirect can land on (each has a matching id in
# HTML_TEMPLATE). Entry cards use entry_anchor_id() instead.
ANCHOR_BOT = "bot-activity"
ANCHOR_MOVER = "file-mover"
ANCHOR_PREFS = "preferences"
ANCHOR_SETTINGS = "settings"
ANCHOR_ADD_FLOW = "add-flow"
ANCHOR_WATCHLIST = "watchlist"
SECTION_ANCHORS = (ANCHOR_BOT, ANCHOR_MOVER, ANCHOR_PREFS, ANCHOR_SETTINGS,
                   ANCHOR_ADD_FLOW, ANCHOR_WATCHLIST)

# The disclosures on a watchlist card a redirect can re-open.
CARD_PANELS = ("episodes", "edit")

_ANCHOR_RE = re.compile(r"^[a-z][a-z0-9-]{0,80}$")


def entry_anchor_id(url):
    """Stable HTML id for a watchlist entry's card, shared by the card's
    ``<article id>`` and every redirect that lands on it.

    Derived from the entry URL (the unique key, see find_entry_by_url), never
    the display name: a readable ASCII slug of the last path segment plus a
    short hash of the normalized URL, so ``&``, quotes, slashes and unicode
    can't produce an invalid id or a CSS-selector-hostile one, and two
    spellings of the same URL (see normalize_anime_url) land on one card."""
    key = normalize_anime_url(url)
    tail = key.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"[^a-z0-9]+", "-", tail.lower()).strip("-")[:40].strip("-")
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    return "entry-{}-{}".format(slug, digest) if slug else "entry-" + digest


def status_url(msg, level=None, anchor=None, panel=None, runs=None):
    """``/?msg=...`` for a status banner, landing on ``anchor`` (a section
    anchor or an entry_anchor_id) with the card disclosure ``panel`` open.
    ``runs`` keeps the Run History depth the reader had paged to.

    The anchor rides twice: as the ``#fragment`` the browser scrolls to, and
    as ``at=`` so the server (which never sees a fragment) can render the
    banner next to it and re-open the panel the action came from."""
    params = {"msg": msg}
    if level is not None:
        params["level"] = level
    if runs:
        params["runs"] = runs
    fragment = ""
    if anchor and _ANCHOR_RE.match(anchor):
        params["at"] = anchor
        if panel in CARD_PANELS:
            params["open"] = panel
        fragment = "#" + anchor
    return "/?" + urlencode(params) + fragment


def render_status_banner(msg, level):
    """The dismissable result banner. Success fades on its own (see the page
    script); an error stays until closed."""
    return (
        '<div class="status-msg status-{tone}" id="status-msg" role="status" aria-live="polite">'
        '<span class="status-text">{msg}</span>'
        '<button type="button" class="status-close" aria-label="Dismiss message">'
        '<span aria-hidden="true">&times;</span></button></div>').format(
            tone="ok" if level == "ok" else "err", msg=escape(msg))


# Add-flow steps (search results, release picker, TVDB step) are rendered by a
# POST. Post/Redirect/Get: the POST stashes the rendered step here and
# redirects to ``/?flow=<token>``, so reloading re-renders it instead of
# re-submitting. Read-only on GET; bounded and short-lived, in memory only.
FLOW_TTL_SECONDS = 3600
FLOW_MAX = 32
_flow_lock = threading.Lock()
_flow_store = collections.OrderedDict()


def stash_flow(search_html, search_query=""):
    token = secrets.token_urlsafe(12)
    now = time.monotonic()
    with _flow_lock:
        for t in [t for t, v in _flow_store.items() if now - v[0] > FLOW_TTL_SECONDS]:
            del _flow_store[t]
        _flow_store[token] = (now, search_html, search_query)
        while len(_flow_store) > FLOW_MAX:
            _flow_store.popitem(last=False)
    return token


def load_flow(token):
    """``(search_html, search_query)`` for a live token, else None."""
    with _flow_lock:
        item = _flow_store.get(token)
        if item is None or time.monotonic() - item[0] > FLOW_TTL_SECONDS:
            return None
        return item[1], item[2]


def suggest_tvdb_season(seasons, ep_count):
    """Season number whose episode count is closest to ``ep_count``, or None.

    Season 0 (TVDB specials) is only a candidate when it is the only season
    listed. When two or more seasons are equally close there is no honest
    "likely match", so this returns None rather than picking one silently."""
    if not ep_count or not seasons:
        return None
    candidates = [s for s in seasons if s.get("season_number") != 0] or list(seasons)
    best, best_diff, tied = None, None, False
    for s in candidates:
        try:
            diff = abs(int(s.get("episode_count") or 0) - ep_count)
        except (ValueError, TypeError):
            continue
        if best_diff is None or diff < best_diff:
            best, best_diff, tied = s.get("season_number"), diff, False
        elif diff == best_diff:
            tied = True
    return None if tied else best


def _parse_year(raw):
    """A posted/scraped release year, or None unless it's a plausible one."""
    try:
        year = int(raw)
    except (ValueError, TypeError):
        return None
    return year if 1900 <= year <= 2100 else None


# ---------------------------------------------------------------------------
# Move-completed logic
# ---------------------------------------------------------------------------

_SEASON_EP_RE = re.compile(r'(.*?)[ ._-][Ss](\d+)[Ee](\d+)')
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
    """Extract (name_part, season, episode) from a filename like 'Anime.Name.S01E05.mkv'.

    The separator before SxxExx may be '.', '_', '-' or a space (so "Name -
    S01E05" and "Name S01E05" both parse), and the matched separator itself
    is stripped from the trailing edge of name_part (via rstrip) so a " - "
    separator doesn't leave a dangling hyphen on the parsed name.
    """
    m = _SEASON_EP_RE.match(filename)
    if not m:
        return None
    name_part = m.group(1).rstrip(' .-_')
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
            # A video file sitting loose in the download root (JDownloader
            # without package subfolders enabled has nowhere else to put
            # it). The move logic below is entirely folder-oriented (recency
            # scan, archive/junk cleanup, subtitle sidecars, empty-dir
            # removal all key off dir_path) and match_anime_entry's reliable
            # signals (download_folder_pattern, customPackage) are keyed off
            # a package folder name that doesn't exist here — so rather than
            # guess at a destination, surface it as stuck for a human to
            # enable package subfolders or move it into one by hand.
            if os.path.splitext(entry_name)[1].lower() not in _VIDEO_EXTS:
                continue
            file_path = dir_path
            try:
                if os.path.getmtime(file_path) > age_threshold:
                    events.append({"type": "wait", "msg": "{} — file still being modified".format(entry_name)})
                    continue
            except OSError:
                continue
            rel_path = os.path.relpath(file_path, DOWNLOAD_DIR)
            msg = ("{} — not in a package folder (enable JDownloader's package "
                   "subfolders, or move it into one)").format(entry_name)
            is_new, ignored, _ = _stuck_touch(rel_path, "loose", entry_name, msg)
            if is_new and not ignored:
                events.append({"type": "error", "msg": msg})
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

  input[type=text], input[type=url], input[type=search], input[type=number], input[type=password], select { width: 100%; padding: 10px 14px; border-radius: var(--radius-sm); border: 1px solid var(--border-light); background: var(--surface-2); color: var(--text); font-size: var(--fs-sm); margin-bottom: var(--s3); transition: border-color var(--tr); }
  input[disabled] { opacity: 0.6; cursor: not-allowed; }
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

  /* Add-anime flow: step indicator + Cancel above each step's heading */
  .flow-head { display: flex; justify-content: space-between; align-items: center; gap: var(--s3); flex-wrap: wrap; margin: var(--s5) 0 var(--s3); }
  .flow-head + h2 { margin-top: 0; }
  .steps { list-style: none; display: flex; align-items: center; flex-wrap: wrap; gap: var(--s2); font-size: var(--fs-xs); color: var(--text-faint); }
  .step { display: inline-flex; align-items: center; gap: 6px; }
  .step + .step::before { content: ""; width: 20px; height: 1px; background: var(--border-light); margin-right: var(--s1); }
  .step-num { display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px; border-radius: 50%; border: 1px solid var(--border-light); font-size: 0.7rem; font-weight: 600; }
  .step-done { color: var(--text-muted); }
  .step-done .step-num { background: var(--accent-soft-bg); border-color: transparent; color: var(--accent-soft-text); }
  .step-current { color: var(--text-heading); font-weight: 600; }
  .step-current .step-num { background: var(--accent-bg); border-color: var(--accent-bg); color: #fff; }
  .flow-note { margin: var(--s2) 0 0; }
  .flow-actions { display: flex; gap: var(--s2); flex-wrap: wrap; margin: var(--s3) 0 var(--s4); }
  .flow-actions form { margin: 0; }

  .release-row { display: flex; gap: var(--s2); align-items: center; flex-wrap: wrap; padding: var(--s2) 0; border-bottom: 1px solid var(--border); }
  .release-row:last-child { border-bottom: none; }
  .status-msg { display: flex; align-items: flex-start; gap: var(--s3); padding: var(--s3); border-radius: var(--radius-sm); margin-bottom: var(--s4); transition: opacity 0.18s cubic-bezier(0.2, 0, 0, 1); }
  .status-text { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .status-close { flex: none; display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; margin: -4px -4px -4px 0; border: none; border-radius: var(--radius-sm); background: transparent; color: inherit; font-size: 1.25rem; line-height: 1; cursor: pointer; opacity: 0.75; }
  .status-close:hover { opacity: 1; background: rgba(255, 255, 255, 0.06); }
  .status-msg.status-leaving { opacity: 0; }
  .wl-card .status-msg, .wl-pending .status-msg { margin: 0 0 var(--s3); font-size: var(--fs-sm); }

  /* Landing after an action: anchors clear the top edge, and the card the
     action touched gets a ring that fades once it has drawn the eye. */
  [id] { scroll-margin-top: var(--s4); }
  .wl-card, .wl-pending { position: relative; }
  .wl-card:target::after, .wl-pending:target::after { content: ""; position: absolute; inset: -1px; border-radius: var(--radius); box-shadow: 0 0 0 2px var(--accent); pointer-events: none; opacity: 0; animation: wl-target 2.4s cubic-bezier(0.2, 0, 0, 1); }
  @keyframes wl-target { 0%, 45% { opacity: 1; } 100% { opacity: 0; } }
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

  /* Watchlist card: title + Check now, one status line, fact badges, then
     two quiet disclosures (Episodes, Edit) that hold every other action. */
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
  .wl-head { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--s3); }
  .wl-title { min-width: 0; flex: 1 1 auto; }
  .wl-url { position: relative; display: block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-faint); font-size: var(--fs-xs); text-decoration: none; }
  a.wl-url:hover { color: var(--accent); text-decoration: underline; }
  .wl-check { margin: 0; flex: none; }
  .wl-status { display: flex; flex-wrap: wrap; align-items: baseline; column-gap: var(--s2); row-gap: 2px; margin-top: var(--s3); font-size: var(--fs-sm); color: var(--text-muted); }
  .wl-status-dot { align-self: center; flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--text-faint); }
  .wl-status-head { color: var(--text-heading); font-weight: 600; }
  .wl-status-detail + .wl-status-detail::before, .wl-status-head + .wl-status-detail::before { content: "·"; margin-right: var(--s2); color: var(--text-faint); }
  .wl-checked { margin-top: var(--s1); font-size: var(--fs-xs); color: var(--text-muted); overflow-wrap: anywhere; }
  .wl-status + .wl-checked { margin-top: var(--s2); }
  .wl-checked--danger { color: var(--danger-text); }
  .run-more { display: flex; flex-wrap: wrap; align-items: center; gap: var(--s2) var(--s3); margin-top: var(--s4); padding-top: var(--s3); border-top: 1px solid var(--border); }
  .run-more .hint { margin-right: auto; }
  .wl-status--ok .wl-status-dot { background: var(--ok-text); }
  .wl-status--ok .wl-status-head { color: var(--ok-text); }
  .wl-status--danger .wl-status-dot { background: var(--danger-text); }
  .wl-status--danger .wl-status-head { color: var(--danger-text); }
  /* Paused: a neutral pause glyph in place of the dot */
  .wl-status--paused .wl-status-dot { width: 8px; height: 10px; border-radius: 0; background: none; border-left: 3px solid var(--text-muted); border-right: 3px solid var(--text-muted); }
  .wl-status--pending .wl-status-dot { background: var(--accent); }
  .wl-card .anime-meta, .wl-pending .anime-meta { margin-top: var(--s2); }
  /* Pending: not a real entry yet, so a dashed edge and only Remove below */
  .wl-pending { border-style: dashed; }
  .wl-pending > .wl-remove { margin-top: var(--s3); }

  .wl-panels { display: flex; flex-wrap: wrap; column-gap: var(--s5); margin-top: var(--s3); padding-top: var(--s1); border-top: 1px solid var(--border); }
  .wl-panel[open] { flex-basis: 100%; }
  .wl-panel > summary { list-style: none; display: flex; align-items: center; gap: var(--s2); min-height: 36px; font-size: var(--fs-xs); font-weight: 500; color: var(--text-muted); }
  .wl-panel > summary::-webkit-details-marker { display: none; }
  .wl-panel > summary::before { content: ""; flex: none; width: 6px; height: 6px; border-right: 1.5px solid currentColor; border-bottom: 1.5px solid currentColor; transform: rotate(-45deg); margin-right: 2px; transition: transform var(--tr); }
  .wl-panel[open] > summary::before { transform: rotate(45deg); }
  .wl-panel > summary:hover { color: var(--accent); }
  .wl-panel[open] > summary { margin-bottom: 0; color: var(--text); }
  .wl-panel-detail { color: var(--text-faint); font-weight: 400; }
  .wl-panel-body { padding: var(--s1) 0 var(--s3); display: flex; flex-direction: column; gap: var(--s3); }

  .ep-ok-line { font-size: var(--fs-xs); color: var(--text); }
  .ep-label, .wl-edit-label { display: block; color: var(--text-muted); font-size: 0.72rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 2px; }
  .ep-ranges { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .ep-retry-list { list-style: none; }
  .ep-row { display: flex; align-items: center; gap: var(--s2); padding: var(--s1) 0; border-bottom: 1px solid var(--border); }
  .ep-row:last-child { border-bottom: none; }
  .ep-row-action { margin: 0 0 0 auto; }
  .ep-num { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: var(--fs-xs); min-width: 64px; color: var(--text); }
  .ep-add-row { display: flex; flex-wrap: wrap; gap: var(--s2); align-items: center; margin: 0; font-size: var(--fs-xs); color: var(--text-muted); }
  .ep-add-row input[type=number] { width: 96px; padding: 6px 8px; margin: 0; min-height: 32px; font-size: var(--fs-xs); }

  .wl-edit-row { margin: 0; }
  .wl-inline { display: flex; flex-wrap: wrap; align-items: center; gap: var(--s2); }
  .wl-inline form { margin: 0; }
  .wl-edit-note { font-size: var(--fs-xs); color: var(--text); }
  .wl-card .folder-input { flex: 1 1 220px; max-width: 360px; width: auto; padding: 6px 10px; margin: 0; min-height: 32px; font-size: var(--fs-xs); }
  .wl-edit-hint { font-size: var(--fs-xs); color: var(--text-faint); margin: 0; }
  input[type=number].wl-num { width: 96px; padding: 6px 8px; margin: 0; min-height: 32px; font-size: var(--fs-xs); }
  .wl-prefs { display: flex; flex-wrap: wrap; align-items: flex-end; gap: var(--s2) var(--s3); margin-bottom: var(--s1); }
  .wl-field { display: flex; flex-direction: column; gap: 2px; flex: 0 1 140px; font-size: var(--fs-xs); color: var(--text-muted); }
  .wl-field-num { flex: 0 0 auto; }
  select.wl-select { width: 100%; padding: 6px 28px 6px 10px; margin: 0; min-height: 32px; font-size: var(--fs-xs); background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%239aa3b2' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 10px center; }
  .flow-have { display: flex; flex-wrap: wrap; align-items: center; gap: var(--s2) var(--s3); }
  .card .flow-have:not(:only-child) { margin-top: var(--s3); }
  .wl-remove { padding-top: var(--s3); border-top: 1px solid var(--border); }
  .wl-remove > summary { list-style: none; width: fit-content; }
  .wl-remove > summary::-webkit-details-marker { display: none; }
  .btn-danger-quiet { background: transparent; color: var(--danger-text); border: 1px solid var(--danger-bg); }
  .btn-danger-quiet:hover { background: var(--danger-bg); color: var(--danger-text); }
  .wl-remove[open] > summary { display: none; }
  .wl-remove-confirm { display: flex; flex-direction: column; gap: var(--s2); padding: var(--s3); border-radius: var(--radius-sm); background: var(--danger-bg); font-size: var(--fs-sm); }
  .wl-remove-confirm strong { color: var(--text-heading); }

  /* Watchlist filter strip: status chips that double as counts, then a
     name filter and sort. Revealed by the script; hidden cards use [hidden]. */
  .wl-controls { display: flex; flex-direction: column; gap: var(--s3); margin-bottom: var(--s4); }
  .wl-controls[hidden], .wl-card[hidden], .wl-pending[hidden], #wl-none[hidden] { display: none; }
  .wl-chips { display: flex; flex-wrap: wrap; gap: var(--s2); }
  .wl-chip { display: inline-flex; align-items: center; gap: var(--s2); min-height: 32px; padding: 0 var(--s3); border-radius: var(--radius-sm); border: 1px solid var(--border); background: transparent; color: var(--text-muted); font: inherit; font-size: var(--fs-xs); font-weight: 500; cursor: pointer; transition: background-color var(--tr), border-color var(--tr), color var(--tr); }
  .wl-chip:hover { color: var(--text); border-color: var(--border-light); }
  .wl-chip-n { color: var(--text-faint); font-variant-numeric: tabular-nums; }
  .wl-chip[aria-pressed="true"] { background: var(--accent-soft-bg); border-color: transparent; color: var(--accent-soft-text); }
  .wl-chip[aria-pressed="true"] .wl-chip-n { color: inherit; }
  .wl-tools { display: flex; flex-wrap: wrap; align-items: flex-end; gap: var(--s2) var(--s3); }
  .wl-tool { display: flex; flex-direction: column; gap: 2px; flex: 0 1 180px; }
  .wl-tool-q { flex: 1 1 240px; max-width: 360px; }
  .wl-tool label { color: var(--text-muted); font-size: 0.72rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; }
  .wl-tool input, .wl-tool select { margin: 0; padding: 6px 10px; min-height: 36px; font-size: var(--fs-sm); }
  .wl-tool select { padding-right: 28px; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%239aa3b2' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 10px center; }
  .wl-shown { margin: 0; font-size: var(--fs-xs); color: var(--text-faint); }
  .wl-shown:empty { display: none; }
  #wl-none { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: var(--s3); }

  @media (max-width: 600px) {
    .activity-grid { grid-template-columns: 1fr 1fr; gap: var(--s3); }
    .form-row { flex-wrap: wrap; }
    .form-row select { flex: 1 1 100%; }
    /* 44px touch targets on phones */
    .wl-card .btn, .wl-pending .btn, .wl-panel > summary, .wl-card .folder-input, .ep-add-row input[type=number], input[type=number].wl-num, select.wl-select { min-height: 44px; }
    .wl-field { flex: 1 1 120px; }
    .wl-card .folder-input { max-width: none; flex-basis: 100%; }
    .wl-chip, .wl-tool input, .wl-tool select { min-height: 44px; }
    .wl-tool, .wl-tool-q { flex: 1 1 100%; max-width: none; }
    .status-close { width: 44px; height: 44px; margin: -12px -12px -12px 0; }
  }
  @media (prefers-reduced-motion: reduce) {
    * { transition: none !important; animation: none !important; }
    .wl-card:target::after, .wl-pending:target::after { opacity: 1; }
  }
</style>
</head>
<body>
<header>
  <h1>Anime-Loads Dashboard</h1>
</header>

<main>
%%STATUS_MSG%%

<div class="section" id="bot-activity">
  <h2>Bot Activity</h2>
  %%STATUS@bot-activity%%
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
  <details open id="run-history-panel">
    <summary>Run History</summary>
    <div class="card" id="run-history">
      %%RUN_HISTORY%%
    </div>
  </details>
  <script>
  // Phones: start Run History collapsed so the watchlist isn't a long scroll away.
  (function() {
    var d = document.getElementById('run-history-panel');
    if (d && window.matchMedia && window.matchMedia('(max-width: 600px)').matches) d.open = false;
  })();
  </script>
</div>

<div class="section" id="file-mover">
  <h2>File Mover</h2>
  %%STATUS@file-mover%%
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
        %%MOVE_NOW_BUTTON%%
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

<div class="section" id="preferences">
  <details %%PREFS_OPEN%%>
    <summary>Preferences</summary>
    %%STATUS@preferences%%
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

<div class="section" id="settings">
  <details %%SETTINGS_OPEN%%>
    <summary>Settings</summary>
    %%STATUS@settings%%
    <div class="card">
      %%SETTINGS_CARD%%
    </div>
  </details>
</div>

<div id="add-flow">
<div class="section">
  <h2>Add Anime</h2>
  %%STATUS@add-flow%%
  <div class="card">
    <form method="POST" action="/add-url" onsubmit="return scrapeBusy(this, 'Fetching releases… this can take up to a minute');">
      <label class="hint" for="add-url">Paste an anime-loads.org URL to see available releases:</label>
      <div class="form-row" style="margin-top:6px;">
        <input type="url" id="add-url" name="url" placeholder="https://www.anime-loads.org/media/..." required>
        <button type="submit" class="btn btn-primary">Fetch Releases</button>
      </div>
    </form>
  </div>
  <div class="card">
    <form method="POST" action="/search" onsubmit="return scrapeBusy(this, 'Searching… this can take up to a minute');">
      <label class="hint">Or search by name:</label>
      <div class="form-row" style="margin-top:6px;">
        <input type="text" name="q" value="%%SEARCH_QUERY%%" placeholder="Search anime..." aria-label="Search anime by name" required>
        <button type="submit" class="btn btn-primary">Search anime</button>
      </div>
    </form>
  </div>
</div>

%%SEARCH_RESULTS%%
</div>

<div class="section" id="watchlist">
  <h2>Watchlist (%%COUNT%%)</h2>
  %%STATUS@watchlist%%
  %%WATCHLIST_CONTROLS%%
  <div id="wl-list">%%WATCHLIST%%</div>
</div>
</main>

<script>
// Scrape-backed forms (add-url, search) hit Selenium server-side and can
// take up to ~a minute — disable the button and relabel it on submit so the
// page doesn't look hung while a normal (non-AJAX) form POST is in flight.
// The threaded server keeps the /api/status poll below updating throughout.
// "Already have episodes up to" fields show the episode the bot starts at.
document.addEventListener('input', function(e) {
  var hintId = e.target.getAttribute && e.target.getAttribute('data-next-hint');
  var hint = hintId && document.getElementById(hintId);
  if (!hint) return;
  var n = parseInt(e.target.value, 10);
  hint.textContent = (n >= 0) ? 'Next download: episode ' + (n + 1) : 'Enter 0 or more';
});
// Add flow: every form on the step carries a hidden have_episodes; fill it
// from the one visible field when any of them is submitted.
document.addEventListener('submit', function(e) {
  var src = document.getElementById('flow-have');
  var dst = e.target.querySelector('input[type=hidden][name=have_episodes]');
  if (src && dst) dst.value = src.value || '0';
}, true);

function scrapeBusy(form, label) {
  var btn = form.querySelector('button[type=submit]');
  if (btn && !btn.disabled) {
    btn.disabled = true;
    btn.textContent = label;
  }
  return true;
}

// Result banner: strip msg from the address bar so reload/bookmark doesn't
// re-show it (the #anchor stays), close on demand, and let a success fade on
// its own. Errors stay until dismissed.
(function() {
  try {
    var u = new URL(window.location.href);
    if (u.searchParams.has('msg')) {
      u.searchParams.delete('msg');
      u.searchParams.delete('level');
      var q = u.searchParams.toString();
      history.replaceState(null, '', u.pathname + (q ? '?' + q : '') + u.hash);
    }
  } catch (e) {}
  var msg = document.getElementById('status-msg');
  if (!msg) return;
  function dismiss() {
    if (!msg.parentNode) return;
    msg.classList.add('status-leaving');
    setTimeout(function() { if (msg.parentNode) msg.remove(); }, 200);
  }
  var close = msg.querySelector('.status-close');
  if (close) close.addEventListener('click', dismiss);
  if (msg.classList.contains('status-ok')) {
    var held = false;
    msg.addEventListener('mouseenter', function() { held = true; });
    msg.addEventListener('mouseleave', function() { held = false; });
    msg.addEventListener('focusin', function() { held = true; });
    msg.addEventListener('focusout', function() { held = false; });
    (function wait() { setTimeout(function() { if (held) wait(); else dismiss(); }, 6000); })();
  }
})();

// Watchlist filter + sort, client-side. Cards carry their state as data-*
// from the server; the 10s poll never touches #wl-list, so this only runs
// on load and on input. The chosen chip and sort persist per browser.
(function() {
  var box = document.getElementById('wl-controls');
  var list = document.getElementById('wl-list');
  if (!box || !list) return;
  var KEY = 'aniloads.watchlist-view';
  var q = document.getElementById('wl-q');
  var sortSel = document.getElementById('wl-sort');
  var shown = document.getElementById('wl-shown');
  var none = document.getElementById('wl-none');
  var chips = [].slice.call(box.querySelectorAll('.wl-chip'));
  var cards = [].slice.call(list.querySelectorAll('.wl-card, .wl-pending'));
  var view = {filter: 'all', sort: 'list'};
  try {
    var saved = JSON.parse(localStorage.getItem(KEY) || 'null');
    if (saved && typeof saved === 'object') {
      if (typeof saved.filter === 'string') view.filter = saved.filter;
      if (typeof saved.sort === 'string') view.sort = saved.sort;
    }
  } catch (e) {}
  // A saved chip whose count is now zero is not rendered: fall back to All.
  if (!chips.some(function(b) { return b.dataset.filter === view.filter; })) view.filter = 'all';
  if (![].some.call(sortSel.options, function(o) { return o.value === view.sort; })) view.sort = 'list';

  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(view)); } catch (e) {}
  }
  function matches(card) {
    var f = view.filter;
    if (f === 'all') return true;
    if (f === 'no-tvdb') return card.dataset.tvdb === '0';
    return card.dataset.state === f;
  }
  function apply() {
    var needle = q.value.trim().toLowerCase();
    var n = 0;
    cards.forEach(function(c) {
      var ok = matches(c) && (!needle || c.dataset.name.indexOf(needle) !== -1);
      c.hidden = !ok;
      if (ok) n++;
    });
    chips.forEach(function(b) {
      b.setAttribute('aria-pressed', b.dataset.filter === view.filter ? 'true' : 'false');
    });
    shown.textContent = n === cards.length ? '' : 'Showing ' + n + ' of ' + cards.length;
    none.hidden = n !== 0;
  }
  function arrange() {
    var pending = cards.filter(function(c) { return c.dataset.state === 'pending'; });
    var items = cards.filter(function(c) { return c.dataset.state !== 'pending'; });
    var added = function(c) { return +c.dataset.added; };
    items.sort(function(a, b) {
      if (view.sort === 'name') return a.dataset.name.localeCompare(b.dataset.name) || added(a) - added(b);
      if (view.sort === 'added') return added(b) - added(a);
      if (view.sort === 'next') {
        var x = a.dataset.next, y = b.dataset.next;
        if (x !== y) return !x ? 1 : !y ? -1 : (x < y ? -1 : 1);
      }
      return added(a) - added(b);
    });
    pending.concat(items).forEach(function(c) { list.appendChild(c); });
  }
  // A link to one card (#anchor) must land on it even when the saved
  // filter hides it: drop back to All so the target shows.
  function reveal() {
    var target;
    try { target = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1))); } catch (e) {}
    if (!target || !list.contains(target)) return;
    var card = target.closest('.wl-card, .wl-pending');
    if (!card || !card.hidden) return;
    view.filter = 'all';
    q.value = '';
    save();
    apply();
    target.scrollIntoView();
  }

  chips.forEach(function(b) {
    b.addEventListener('click', function() {
      view.filter = b.dataset.filter;
      save();
      apply();
    });
  });
  q.addEventListener('input', apply);
  sortSel.addEventListener('change', function() {
    view.sort = sortSel.value;
    save();
    arrange();
  });
  document.getElementById('wl-clear').addEventListener('click', function() {
    view.filter = 'all';
    q.value = '';
    save();
    apply();
    chips[0].focus();
  });
  window.addEventListener('hashchange', reveal);

  sortSel.value = view.sort;
  arrange();
  apply();
  box.hidden = false;
  reveal();
})();

(function() {
  var ids = ['bot-status','bot-status','last-run','next-run','run-history','health',
             'move-status','move-last-run','move-history','move-stuck'];
  function refresh() {
    // Keep the reader's "Show older cycles" depth across polls.
    var runs = /[?&]runs=([0-9]+)/.exec(location.search);
    fetch('/api/status' + (runs ? '?runs=' + runs[1] : ''))
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

    if status.get("docker_available") is False:
        # The container status comes only from Docker — when the socket isn't
        # reachable we genuinely don't know whether the bot is running, so a
        # red "Stopped" here would be a false signal. run_state-derived
        # Last/Next Run (below) stay authoritative regardless.
        status_text = ('<span class="status-dot unknown"></span>Unknown '
                        '<span class="hint">&mdash; Docker socket unavailable</span>')
    else:
        bot_running = status.get("running", False)
        dot_class = "running" if bot_running else "stopped"
        status_text = '<span class="status-dot {}"></span>{}'.format(
            dot_class, "Running" if bot_running else "Stopped"
        )
        # Health's own "Bot Cycles" row is the authority on staleness; don't
        # let a green "Running" dot silently contradict it.
        staleness = activity.get("staleness")
        if bot_running and isinstance(staleness, dict) and staleness.get("state") == "warn":
            status_text += ' <span class="hint">&mdash; {}</span>'.format(
                escape(staleness.get("detail", "")))

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


RUN_HISTORY_PAGE = 20
# The bot keeps 50 run-state records; the cap only bounds a hand-typed ?runs=.
RUN_HISTORY_MAX = 500


def parse_runs_param(qs):
    """How many cycles Run History shows, from a parsed ``?runs=`` query:
    the default page when absent or garbage, clamped to the allowed range."""
    try:
        n = int((qs.get("runs") or [""])[0])
    except (TypeError, ValueError):
        return RUN_HISTORY_PAGE
    return max(RUN_HISTORY_PAGE, min(n, RUN_HISTORY_MAX))


def render_run_history_more(total, max_runs):
    """The paging row under the feed: "Show older cycles" while records beyond
    ``max_runs`` exist, and a way back once the reader has paged. Plain links,
    so paging works without script and survives the 10s poll (which passes
    ``runs`` along)."""
    links = ""
    if total > max_runs:
        links += ('<a class="btn btn-ghost btn-sm" href="/?runs={}#run-history-panel">'
                  'Show older cycles</a>').format(max_runs + RUN_HISTORY_PAGE)
    if max_runs > RUN_HISTORY_PAGE:
        links += ('<a class="btn btn-ghost btn-sm" href="/#run-history-panel">'
                  'Show recent only</a>')
    if not links:
        return ""
    shown = min(total, max_runs)
    return ('<div class="run-more"><span class="hint">Showing the latest {} of {} cycles</span>'
            '{}</div>').format(shown, total, links)


def render_run_history(runs, state_runs=None, max_runs=RUN_HISTORY_PAGE, now=None):
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
            return state_html + render_run_history_more(len(state_runs), max_runs)

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

    return html + render_run_history_more(len(runs), max_runs)


def render_move_status(now=None):
    """Render move status and last run time (day-qualified when not today)."""
    mounted = os.path.isdir(DOWNLOAD_DIR)
    if _move_running:
        status_html = '<span class="status-dot running"></span>Running'
    elif not mounted:
        status_html = '<span class="status-dot unknown"></span>Not mounted'
    else:
        status_html = '<span class="status-dot unknown"></span>Idle'

    with _move_lock:
        if _move_last_run:
            # The worker stamps an aware UTC time; shown on the local clock
            # like every other feed timestamp.
            last_html = format_day_time(_to_local(_move_last_run), now=now)
        elif not mounted:
            last_html = '<span class="faint">Download dir not mounted</span>'
        else:
            last_html = '<span class="faint">Not yet</span>'

    return status_html, last_html


def render_move_now_button():
    """The Move Now button — disabled with a reason when DOWNLOAD_DIR isn't
    mounted, since a triggered cycle couldn't move anything anyway."""
    if os.path.isdir(DOWNLOAD_DIR):
        return ('<form method="POST" action="/move-now" style="margin:0;">'
                '<button type="submit" class="btn btn-warning">Move Now</button>'
                '</form>')
    return ('<form method="POST" action="/move-now" style="margin:0;">'
            '<button type="submit" class="btn btn-warning" disabled '
            'title="Download directory not mounted">Move Now</button>'
            '</form>')


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
    "loose": "Not in a package folder",
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


def pending_resolve_error(entry, now=None):
    """The pending card's failure line from ``resolve_error`` (written by
    resolve_pending): "Last attempt 18:44 failed: <reason>. Retrying
    automatically." Returns "" when the last attempt did not fail."""
    err = entry.get("resolve_error")
    if not isinstance(err, dict) or not err.get("reason"):
        return ""
    when = _local_ts(err.get("ts"))
    head = "Last attempt {} failed".format(
        format_day_time(when, fmt="%H:%M", now=now)) if when else "Last attempt failed"
    return "{}: {}. Retrying automatically.".format(
        head, " ".join(str(err["reason"]).split()).rstrip("."))


def render_watchlist(anime_list, pending_list=None, entry_outcomes=None, focus=None):
    """``entry_outcomes`` is run_state.json's per-entry map, keyed by URL;
    absent (an older bot) the cards simply carry no last-check line.

    ``focus`` (``{"anchor", "panel", "banner"}``, see render_page) marks the
    card a redirect landed on: it carries the banner and re-opens the panel."""
    if not isinstance(entry_outcomes, dict):
        entry_outcomes = {}
    focus = focus or {}
    if not anime_list and not pending_list:
        return '<div class="empty">No anime in watchlist. Add some above!</div>'
    html = ""

    for i, a in enumerate(pending_list or []):
        anchor = entry_anchor_id(a.get("url", ""))
        html += render_pending_card(
            i, a, banner=focus.get("banner", "") if focus.get("anchor") == anchor else "")

    for i, a in enumerate(anime_list):
        outcome = entry_outcomes.get(a.get("url"))
        if focus.get("anchor") == entry_anchor_id(a.get("url", "")):
            html += render_watchlist_card(i, a, outcome, open_panel=focus.get("panel"),
                                          banner=focus.get("banner", ""))
        else:
            html += render_watchlist_card(i, a, outcome)
    return html


# anime-loads.org reports a series' status in German ("Laufend"); the rest of
# the dashboard is English, so the card shows a translated label. Unknown
# values fall through verbatim rather than being hidden.
_AL_STATUS_LABELS = {
    "laufend": "Airing",
    "abgeschlossen": "Finished",
    "completed": "Finished",
    "complete": "Finished",
    "pausiert": "Paused",
    "abgebrochen": "Cancelled",
    "geplant": "Announced",
    "angekündigt": "Announced",
}

# Show at most this many "downloaded" ranges before summarizing the rest, so a
# long series with scattered retries can't grow the episode panel unbounded.
_MAX_EP_RANGES = 12


def translate_al_status(raw):
    """The site's raw status cell as dashboard wording ("Laufend" -> "Airing")."""
    raw = str(raw or "").strip()
    if not raw:
        return ""
    return _AL_STATUS_LABELS.get(raw.lower(), raw)


def _plural(n, word):
    return "{} {}{}".format(n, word, "" if n == 1 else "s")


def _episode_count(entry):
    eps = entry.get("episodes", entry.get("episodes_downloaded", 0))
    return int(eps) if isinstance(eps, (int, float)) and eps > 0 else 0


def _parse_airdate(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def format_airdate(day, today):
    """A date as "Sat 26 Sep", with the year only when it isn't this year."""
    label = "{} {} {}".format(day.strftime("%a"), day.day, day.strftime("%b"))
    if day.year != today.year:
        label += " {}".format(day.year)
    return label


def watchlist_status(entry, today=None):
    """The one human status line for a watchlist card.

    Returns ``(tone, headline, details)``: ``tone`` is ok / danger / neutral,
    ``headline`` the state in a few words and ``details`` a list of short
    supporting facts. Precedence follows what needs the reader: a pause the
    user set comes first (nothing else happens while it holds), then retries,
    movie, complete, never-downloaded, a known next date, and finally the
    site status."""
    if today is None:
        today = _to_local(_utc_now()).date()
    eps = _episode_count(entry)
    missing = entry.get("missing") or []
    site = translate_al_status(entry.get("al_status"))
    is_movie = entry.get("media_type") == "movie"

    if entry.get("paused"):
        details = ["the bot skips it until you resume"]
        if missing:
            details.append("{} waiting to retry".format(_plural(len(missing), "episode")))
        elif eps and not is_movie:
            details.append(_plural(eps, "episode"))
        return "paused", "Paused", details
    if missing:
        if is_movie:
            return "danger", "Download retrying", []
        return ("danger", "{} retrying".format(_plural(len(missing), "episode")),
                [_plural(eps, "episode")])
    if is_movie:
        year = _parse_year(entry.get("year"))
        headline = "Movie ({})".format(year) if year else "Movie"
        if eps:
            return "ok", headline, ["Downloaded"]
        return "neutral", headline, ["Waiting for download"]
    if entry.get("complete"):
        return "ok", "Complete", [_plural(eps, "episode")]
    if not eps:
        return "neutral", "Waiting for first download", (
            ["{} on site".format(site)] if site else [])
    if entry.get("skip_until"):
        day = _parse_airdate(entry["skip_until"])
        what = "next episode" if entry.get("skip_real_airdate") else "next check"
        if day is None:
            when = "{} {}".format(what, entry["skip_until"])
        elif day < today:
            when = "{} was due {}".format(what, format_airdate(day, today))
        else:
            when = "{} {}".format(what, format_airdate(day, today))
        return "neutral", "Airing", [when, _plural(eps, "episode")]
    return "neutral", site or "Watching", [_plural(eps, "episode")]


_ISO_DAY_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _humanize_reason(reason, today):
    """A bot-written reason as card copy: ISO dates read as "Sat 20 Sep", the
    two machine-shaped skip reasons get words, and a leading capital drops so
    the reason flows after "Checked 18:44 · " (an acronym-led "JDownloader"
    keeps its case)."""
    reason = " ".join(str(reason or "").split())
    if reason == "complete":
        reason = "series complete"
    m = re.fullmatch(r"skip_until \((.*)\)", reason)
    if m:
        reason = "next check {}".format(m.group(1)) if m.group(1) else "throttled"

    def day(match):
        parsed = _parse_airdate(match.group(1))
        return format_airdate(parsed, today) if parsed else match.group(1)

    reason = _ISO_DAY_RE.sub(day, reason)
    if len(reason) > 1 and reason[0].isupper() and reason[1].islower():
        reason = reason[0].lower() + reason[1:]
    return reason


def entry_check_lines(outcome, now=None):
    """The quiet "last check" lines under a watchlist card's status, from the
    bot's per-entry record in run_state.json (see ENTRY_RESULTS in
    bot/anibot.py). Returns ``(check, error)``, each plain text or "":

    - ``check``: "Checked 18:44 · waiting for airdate Sat 20 Sep".
    - ``error``: "Last error: JDownloader unreachable (Tue 8 Sep)", shown only
      when that error is not this check's own result (the check line already
      says it) and the latest check was not a download, which supersedes it.

    A missing or malformed record renders nothing, so an older bot or a
    never-checked entry keeps the card as it was. So does a "paused" result:
    the card's status line already says Paused."""
    if not isinstance(outcome, dict) or outcome.get("result") == "paused":
        return "", ""
    now = now or _utc_now()
    today = _to_local(now).date()
    result = outcome.get("result")
    checked = _local_ts(outcome.get("checked_ts"))

    reason = _humanize_reason(outcome.get("reason"), today)
    episode = outcome.get("episode")
    if not isinstance(episode, int) or isinstance(episode, bool):
        episode = None
    if result == "downloaded" and episode is not None:
        extra = reason.split(";", 1)[1].strip() if ";" in reason else ""
        reason = "downloaded episode {}".format(episode) + ("; " + extra if extra else "")
    elif episode is not None and "episode" not in reason.lower():
        reason = "{} (episode {})".format(reason, episode) if reason else "episode {}".format(episode)
    if not reason and isinstance(result, str):
        reason = result

    check = ""
    if reason:
        when = format_day_time(checked, fmt="%H:%M", now=now) if checked else ""
        check = "{} · {}".format("Checked " + when if when else "Last check", reason)

    error = ""
    last_error = outcome.get("last_error")
    if isinstance(last_error, dict) and last_error.get("reason") and result != "downloaded":
        same_check = (result == "error"
                      and last_error.get("checked_ts") == outcome.get("checked_ts"))
        if not same_check:
            err_day = _local_ts(last_error.get("checked_ts"))
            error = "Last error: {}".format(" ".join(str(last_error["reason"]).split()))
            if err_day:
                error += " ({})".format(format_day(err_day.date(), now))
    return check, error


def render_entry_check(outcome, now=None):
    check, error = entry_check_lines(outcome, now)
    html = ""
    if check:
        tone = " wl-checked--danger" if outcome.get("result") == "error" else ""
        html += '<p class="wl-checked{}">{}</p>'.format(tone, escape(check))
    if error:
        html += '<p class="wl-checked wl-checked--danger">{}</p>'.format(escape(error))
    return html


# Watchlist filter chips: (state, label). "pending" and "no-tvdb" are not
# card states; the client matches them on the card kind and data-tvdb.
_WL_FILTERS = (("all", "All"), ("airing", "Airing"), ("retrying", "Retrying"),
               ("complete", "Complete"), ("movie", "Movies"),
               ("paused", "Paused"), ("pending", "Pending"), ("no-tvdb", "No TVDB"))


def watchlist_filter_state(entry, today=None):
    """The card's filter state, read off watchlist_status so a chip always
    agrees with the status line: paused / retrying / movie / complete / new
    (waiting for first download) / airing."""
    tone, headline, _ = watchlist_status(entry, today)
    if tone == "paused":
        return "paused"
    if tone == "danger":
        return "retrying"
    if entry.get("media_type") == "movie":
        return "movie"
    if headline == "Complete":
        return "complete"
    if headline == "Waiting for first download":
        return "new"
    return "airing"


def watchlist_filter_attrs(i, entry, today=None):
    """data-* attributes the client-side filter and sort read from a card."""
    state = watchlist_filter_state(entry, today)
    day = _parse_airdate(entry.get("skip_until")) if state == "airing" else None
    return ('data-state="{}" data-tvdb="{}" data-name="{}" data-added="{}" '
            'data-next="{}"').format(
                state, 1 if entry.get("tvdb_id") else 0,
                escape(str(entry.get("name", "")).casefold()), i,
                day.isoformat() if day else "")


def render_watchlist_controls(anime_list, pending_list=None):
    """Filter chips with counts, a name filter and a sort choice. Rendered
    hidden: the script reveals it, so without JS nothing dead is on show."""
    pending_list = pending_list or []
    if not anime_list and not pending_list:
        return ""
    counts = {"all": len(anime_list) + len(pending_list), "pending": len(pending_list),
              "no-tvdb": sum(1 for a in anime_list if not a.get("tvdb_id"))}
    for a in anime_list:
        state = watchlist_filter_state(a)
        counts[state] = counts.get(state, 0) + 1
    chips = ""
    for key, label in _WL_FILTERS:
        n = counts.get(key, 0)
        if key != "all" and not n:
            continue
        chips += ('<button type="button" class="wl-chip" data-filter="{key}" '
                  'aria-pressed="{pressed}">{label}<span class="wl-chip-n">{n}</span>'
                  '</button>').format(key=key, label=label, n=n,
                                      pressed="true" if key == "all" else "false")
    return (
        '<div class="wl-controls" id="wl-controls" hidden>'
        '<div class="wl-chips" role="group" aria-label="Filter watchlist by status">{chips}</div>'
        '<div class="wl-tools">'
        '<div class="wl-tool wl-tool-q"><label for="wl-q">Filter by name</label>'
        '<input type="search" id="wl-q" autocomplete="off" spellcheck="false"></div>'
        '<div class="wl-tool"><label for="wl-sort">Sort</label>'
        '<select id="wl-sort">'
        '<option value="list">Watchlist order</option>'
        '<option value="name">Name</option>'
        '<option value="added">Recently added</option>'
        '<option value="next">Next episode</option>'
        '</select></div>'
        '</div>'
        '<p class="wl-shown" id="wl-shown" aria-live="polite"></p>'
        '</div>'
        '<p class="empty" id="wl-none" hidden>No anime match these filters. '
        '<button type="button" class="btn btn-ghost btn-sm" id="wl-clear">Show all</button></p>'
    ).format(chips=chips)


def watchlist_heading_count(anime_list, pending_list=None):
    """"12 anime" or "12 anime, 2 pending": entries and pending apart."""
    text = "{} anime".format(len(anime_list))
    if pending_list:
        text += ", {} pending".format(len(pending_list))
    return text


def compact_ranges(numbers):
    """Sorted ints as inclusive ``(start, end)`` runs: [1,2,3,5] -> [(1,3),(5,5)]."""
    ranges = []
    for n in sorted(set(numbers)):
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], n)
        else:
            ranges.append((n, n))
    return ranges


def _format_range(start, end):
    return str(start) if start == end else "{}–{}".format(start, end)


def ep_add_max(entry):
    """Highest episode number /ep-add accepts for this entry.

    Bound to the site's known ANNOUNCED total (al_max_episodes) — NOT
    entry["episodes"] (highest already downloaded) and NOT al_available_max
    (the currently-published cap): adding the next, not-yet-published episode
    number is the documented manual override for one that went up early — by
    definition beyond al_available_max — so bounding on either of those would
    block exactly that. animeloads.py uses 999999 as its "unknown announced
    total" sentinel, so that value means unknown here too, falling through to
    a generous sanity cap."""
    al_max_episodes = entry.get("al_max_episodes")
    if isinstance(al_max_episodes, (int, float)) and 0 < al_max_episodes < 999999:
        return int(al_max_episodes)
    return 5000


# Per-entry release preferences an edit may set. A blank choice removes the
# override so the entry follows the global Preferences again.
_PREF_LANGS = (("german", "German"), ("japanese", "Japanese"),
               ("english", "English"), ("any", "Any"))
_PREF_RESOLUTIONS = (480, 720, 1080)


def entry_effective_prefs(entry, prefs):
    """Global prefs with this entry's per-entry overrides applied: what
    pick_best_release should use for this one series."""
    effective = dict(prefs)
    audio_override = entry.get("pref_audio_language", entry.get("pref_language"))
    if audio_override:
        effective["audio_language"] = audio_override
    if entry.get("pref_sub_language"):
        effective["sub_language"] = entry["pref_sub_language"]
    if entry.get("pref_resolution"):
        effective["min_resolution"] = entry["pref_resolution"]
    return effective


def next_download_note(entry):
    """One sentence on what the bot does next with this entry, shown after an
    edit that changes it (episodes, release, pause)."""
    if entry.get("paused"):
        return "Paused, so the bot skips it until you resume."
    if entry.get("complete"):
        return "Marked complete, so the bot downloads nothing more until you mark it incomplete."
    if entry.get("media_type") == "movie":
        return "The bot downloads the movie on its next check." if not _episode_count(entry) else ""
    note = "The bot will download from episode {} on its next check".format(
        _episode_count(entry) + 1)
    missing = [m for m in (entry.get("missing") or []) if isinstance(m, int)]
    if missing:
        note += " and retry {}".format(_plural(len(missing), "episode"))
    return note + "."


def parse_have_episodes(raw, cap=_MAX_SANE_EPISODE_COUNT):
    """The "I already have episodes up to N" field: an int 0..cap, or None
    when it isn't one. Blank means 0 (have none)."""
    raw = str(raw if raw is not None else "").strip()
    if not raw:
        return 0
    if not re.fullmatch(r"\d{1,6}", raw):
        return None
    value = int(raw)
    return value if value <= cap else None


# Bounds for the manual library-placement fields. A season is a folder number
# (S00 specials up to a year-style season); the offset shifts release episode
# numbers onto library ones (release + offset = library, the same rule the
# mover and the bot's batch matcher apply), so it never needs to exceed a
# long-runner's absolute episode count.
_MAX_LIBRARY_SEASON = 9999
_MAX_EPISODE_OFFSET = 9999


def parse_library_placement(params):
    """The Edit panel's library season + episode offset fields as
    ({"tvdb_season": int or None, "episode_offset": int}, error). A blank
    season clears it (the mover keeps the file's own season); a blank offset
    means 0."""
    season_raw = str(params.get("tvdb_season", "")).strip()
    offset_raw = str(params.get("episode_offset", "")).strip().replace("−", "-")
    season = None
    if season_raw:
        if not re.fullmatch(r"\d{1,4}", season_raw):
            return None, "season must be a whole number from 0 to {}".format(_MAX_LIBRARY_SEASON)
        season = int(season_raw)
    offset = 0
    if offset_raw:
        if not re.fullmatch(r"[+-]?\d{1,4}", offset_raw):
            return None, "episode offset must be a whole number from -{0} to {0}".format(
                _MAX_EPISODE_OFFSET)
        offset = int(offset_raw)
    return {"tvdb_season": season, "episode_offset": offset}, None


def library_placement_note(value):
    """Where the mover files this entry's downloads, in words, for the save
    message."""
    season = value.get("tvdb_season")
    offset = value.get("episode_offset") or 0
    where = ("files downloads into season {}".format(season) if season is not None
             else "keeps the season from each file name")
    shift = ("adds {:+d} to release episode numbers".format(offset) if offset
             else "no episode offset")
    return "the mover {}, {}".format(where, shift)


def parse_entry_prefs(params):
    """The per-entry prefs form as {field: value-or-None}; None removes the
    override. Returns (prefs, error)."""
    langs = {code for code, _ in _PREF_LANGS}
    out = {}
    for field in ("pref_audio_language", "pref_sub_language"):
        value = params.get(field, "").strip().lower()
        if value and value not in langs:
            return None, "unknown language"
        out[field] = value or None
    res = params.get("pref_resolution", "").strip()
    if res:
        if not res.isdigit() or int(res) not in _PREF_RESOLUTIONS:
            return None, "unknown resolution"
        out["pref_resolution"] = int(res)
    else:
        out["pref_resolution"] = None
    return out, None


def apply_entry_edit(entry, edit, value):
    """Apply one validated dashboard edit to a watchlist entry, in place.

    ``edit`` is "episodes" (int), "paused" (bool), "prefs" (dict from
    parse_entry_prefs), "release" (a release id already confirmed against
    the site) or "library" (dict from parse_library_placement). Pure local mutation, so it can run inside update_ani's lock.
    Returns (result, detail): result is "saved", "unchanged" or "invalid"."""
    if edit == "episodes":
        if not isinstance(value, int) or value < 0 or value > ep_add_max(entry):
            return "invalid", "episodes must be between 0 and {}".format(ep_add_max(entry))
        if _episode_count(entry) == value and "episodes" in entry:
            return "unchanged", ""
        entry["episodes"] = value
        return "saved", ""
    if edit == "paused":
        if bool(entry.get("paused")) == bool(value):
            return "unchanged", ""
        if value:
            entry["paused"] = True
        else:
            entry.pop("paused", None)
        return "saved", ""
    if edit == "prefs":
        changed = False
        for field, new in value.items():
            if field == "pref_audio_language" and "pref_language" in entry:
                entry.pop("pref_language", None)
                changed = True
            if new is None:
                if field in entry:
                    entry.pop(field)
                    changed = True
            elif entry.get(field) != new:
                entry[field] = new
                changed = True
        return ("saved" if changed else "unchanged"), ""
    if edit == "release":
        try:
            release_id = int(value)
        except (TypeError, ValueError):
            return "invalid", "invalid release"
        if entry.get("releaseID") == release_id:
            return "unchanged", ""
        entry["releaseID"] = release_id
        # The available-episodes cap was measured on the old release.
        entry.pop("al_available_max", None)
        entry.pop("al_available_max_set_at", None)
        return "saved", ""
    if edit == "library":
        # Stored exactly as /tvdb-link stores them: tvdb_season an int (0 is
        # specials), episode_offset only when non-zero. Neither needs a
        # tvdb_id; the bot's TVDB checks key off tvdb_id alone.
        season = value.get("tvdb_season") if isinstance(value, dict) else None
        offset = value.get("episode_offset") if isinstance(value, dict) else None
        if season is not None and (not isinstance(season, int) or isinstance(season, bool)
                                   or not 0 <= season <= _MAX_LIBRARY_SEASON):
            return "invalid", "season must be a whole number from 0 to {}".format(
                _MAX_LIBRARY_SEASON)
        if (not isinstance(offset, int) or isinstance(offset, bool)
                or not -_MAX_EPISODE_OFFSET <= offset <= _MAX_EPISODE_OFFSET):
            return "invalid", "episode offset must be a whole number from -{0} to {0}".format(
                _MAX_EPISODE_OFFSET)
        if entry.get("tvdb_season") == season and (entry.get("episode_offset") or 0) == offset \
                and ("episode_offset" in entry) == (offset != 0):
            return "unchanged", ""
        if season is None:
            entry.pop("tvdb_season", None)
        else:
            entry["tvdb_season"] = season
        if offset:
            entry["episode_offset"] = offset
        else:
            entry.pop("episode_offset", None)
        return "saved", ""
    return "invalid", "unknown edit"


def _sr(text):
    """Visually hidden text that completes a control's accessible name."""
    return '<span class="sr-only">{}</span>'.format(escape(text))


def _key_input(key):
    return '<input type="hidden" name="key" value="{}">'.format(key)


def _watchlist_url_html(url):
    if not url:
        return ""
    shown = re.sub(r"^https?://(www\.)?", "", url)
    if urlparse(url).scheme.lower() in ("http", "https"):
        return ('<a class="wl-url" href="{href}" target="_blank" rel="noopener noreferrer" '
                'title="{href}">{shown}{sr}</a>').format(
                    href=escape(url), shown=escape(shown), sr=_sr(" (opens in new tab)"))
    return '<span class="wl-url" title="{0}">{0}</span>'.format(escape(url))


def _render_episode_panel(i, entry, key, name, is_open=False):
    eps = _episode_count(entry)
    missing = sorted({m for m in (entry.get("missing") or []) if isinstance(m, int)})
    missing_set = set(missing)
    ok_nums = [n for n in range(1, eps + 1) if n not in missing_set]
    ranges = compact_ranges(ok_nums)

    summary_bits = []
    if len(ranges) == 1:
        summary_bits.append("{} OK".format(_format_range(*ranges[0])))
    elif ok_nums:
        summary_bits.append("{} OK".format(len(ok_nums)))
    if missing:
        summary_bits.append("{} retrying".format(len(missing)))
    if not summary_bits:
        summary_bits.append("none downloaded yet")

    body = ""
    if ranges:
        shown = ", ".join(_format_range(s, e) for s, e in ranges[:_MAX_EP_RANGES])
        if len(ranges) > _MAX_EP_RANGES:
            shown += " and {} more ranges".format(len(ranges) - _MAX_EP_RANGES)
        body += ('<p class="ep-ok-line"><span class="ep-label">Downloaded</span> '
                 '<span class="ep-ranges">{}</span></p>').format(shown)

    if missing:
        rows = ""
        for ep_num in missing:
            queued = (' <span class="badge badge-auto" title="Not downloaded yet">Queued</span>'
                      if ep_num > eps else "")
            rows += (
                '<li class="ep-row"><span class="ep-num">Ep {n}</span>'
                '<span class="badge badge-retry">Retrying</span>{queued}'
                '<form method="POST" action="/ep-remove" class="ep-row-action">{key}'
                '<input type="hidden" name="ep" value="{n}">'
                '<button type="submit" class="btn btn-ghost btn-sm">Stop retrying{sr}</button>'
                '</form></li>').format(
                    n=ep_num, queued=queued, key=_key_input(key),
                    sr=_sr(" episode {} of {}".format(ep_num, name)))
        body += '<ul class="ep-retry-list" aria-label="Episodes retrying">{}</ul>'.format(rows)

    body += (
        '<form method="POST" action="/ep-add" class="ep-add-row">{key}'
        '<label for="ep-add-{i}">Retry episode</label>'
        '<input type="number" id="ep-add-{i}" name="ep" min="1" max="{max}" '
        'inputmode="numeric" placeholder="#" required>'
        '<button type="submit" class="btn btn-ghost btn-sm">Add to retry{sr}</button>'
        '</form>').format(key=_key_input(key), i=i, max=ep_add_max(entry),
                          sr=_sr(" for {}".format(name)))

    return ('<details class="wl-panel ep-panel"{}><summary>Episodes'
            '<span class="wl-panel-detail">{}</span></summary>'
            '<div class="wl-panel-body">{}</div></details>').format(
                " open" if is_open else "", escape(" · ".join(summary_bits)), body)


def _pref_select(field, i, label, options, current):
    """One labelled per-entry preference select; the blank option follows
    the global Preferences."""
    opts = '<option value="">Use global</option>'
    for value, text in options:
        opts += '<option value="{v}"{sel}>{t}</option>'.format(
            v=escape(str(value)), t=escape(text),
            sel=" selected" if str(current) == str(value) else "")
    return ('<div class="wl-field"><label for="{f}-{i}">{label}</label>'
            '<select id="{f}-{i}" name="{f}" class="wl-select">{opts}</select></div>').format(
                f=field, i=i, label=label, opts=opts)


def _render_download_rows(i, entry, key, name):
    """The top of the Edit panel: pause, episodes already owned, release and
    per-entry release preferences."""
    if entry.get("paused"):
        note, value, button = "Paused. The bot skips this series.", "0", "Resume downloads"
    else:
        note, value, button = "Active. Checked on every bot run.", "1", "Pause downloads"
    rows = (
        '<div class="wl-edit-row"><span class="wl-edit-label">Downloads</span>'
        '<div class="wl-inline"><span class="wl-edit-note">{note}</span>'
        '<form method="POST" action="/entry-edit">{key}'
        '<input type="hidden" name="edit" value="paused">'
        '<input type="hidden" name="paused" value="{value}">'
        '<button type="submit" class="btn btn-ghost btn-sm">{button}{sr}</button>'
        '</form></div></div>').format(
            note=note, value=value, button=button, key=_key_input(key),
            sr=_sr(" for {}".format(name)))

    if entry.get("media_type") != "movie":
        eps = _episode_count(entry)
        rows += (
            '<form method="POST" action="/entry-edit" class="wl-edit-row">{key}'
            '<input type="hidden" name="edit" value="episodes">'
            '<label class="wl-edit-label" for="have-{i}">Already have episodes up to</label>'
            '<div class="wl-inline">'
            '<input type="number" id="have-{i}" name="episodes" value="{eps}" min="0" max="{max}" '
            'inputmode="numeric" required class="wl-num" aria-describedby="have-hint-{i}" '
            'data-next-hint="have-hint-{i}">'
            '<button type="submit" class="btn btn-ghost btn-sm">Save episodes{sr}</button>'
            '<span class="wl-edit-hint" id="have-hint-{i}">Next download: episode {next}</span>'
            '</div></form>').format(
                key=_key_input(key), i=i, eps=eps, max=ep_add_max(entry), next=eps + 1,
                sr=_sr(" for {}".format(name)))

    release_id = entry.get("releaseID")
    rows += (
        '<div class="wl-edit-row"><span class="wl-edit-label">Release</span>'
        '<div class="wl-inline"><span class="wl-edit-note">{current}</span>'
        '<form method="POST" action="/entry-releases" '
        'onsubmit="return scrapeBusy(this, \'Fetching releases…\');">{key}'
        '<button type="submit" class="btn btn-ghost btn-sm">Change release{sr}</button>'
        '</form></div></div>').format(
            current="Release #{}".format(escape(str(release_id))) if release_id else "No release chosen",
            key=_key_input(key), sr=_sr(" for {}".format(name)))

    audio = entry.get("pref_audio_language", entry.get("pref_language", ""))
    rows += (
        '<form method="POST" action="/entry-edit" class="wl-edit-row">{key}'
        '<input type="hidden" name="edit" value="prefs">'
        '<span class="wl-edit-label" id="prefs-label-{i}">Release preferences</span>'
        '<div class="wl-prefs" role="group" aria-labelledby="prefs-label-{i}">{dub}{sub}{res}'
        '<button type="submit" class="btn btn-ghost btn-sm">Save preferences{sr}</button></div>'
        '<p class="wl-edit-hint">Used to pick the best match when you change release.</p>'
        '</form>').format(
            key=_key_input(key), i=i, sr=_sr(" for {}".format(name)),
            dub=_pref_select("pref_audio_language", i, "Dub", _PREF_LANGS, audio),
            sub=_pref_select("pref_sub_language", i, "Sub", _PREF_LANGS,
                             entry.get("pref_sub_language", "")),
            res=_pref_select("pref_resolution", i, "Min resolution",
                             [(r, "{}p".format(r)) for r in _PREF_RESOLUTIONS],
                             entry.get("pref_resolution", "")))
    return rows


def _render_remove_confirm(name, key, action, note):
    """Two-step remove, in the page rather than a window.confirm(): the first
    click only opens a panel that names the series; Cancel closes it again.
    ``key`` is the already-escaped entry URL."""
    return (
        '<details class="wl-remove"><summary class="btn btn-sm btn-danger-quiet">'
        'Remove from watchlist{sr}</summary>'
        '<div class="wl-remove-confirm" role="group" aria-label="Confirm removal of {name}">'
        '<p>Remove <strong>{name}</strong> from the watchlist? {note}</p>'
        '<div class="wl-inline">'
        '<form method="POST" action="{action}">{key}'
        '<button type="submit" class="btn btn-danger btn-sm">Remove {name}</button></form>'
        '<button type="button" class="btn btn-ghost btn-sm" '
        'onclick="this.closest(\'details\').open = false">Cancel</button>'
        '</div></div></details>').format(
            sr=_sr(" " + name), name=escape(name), key=_key_input(key),
            action=action, note=escape(note))


def _status_line_html(tone, headline, details=()):
    return (
        '<p class="wl-status wl-status--{tone}">'
        '<span class="wl-status-dot" aria-hidden="true"></span>'
        '<span class="wl-status-head">{head}</span>{details}</p>').format(
            tone=tone, head=escape(headline),
            details="".join('<span class="wl-status-detail">{}</span>'.format(escape(d))
                            for d in details))


def _pref_facts(entry):
    """(label, title) badges for an entry's per-series release preferences."""
    facts = []
    pref_audio = entry.get("pref_audio_language", entry.get("pref_language", ""))
    if pref_audio:
        facts.append(("Dub: {}".format(pref_audio.title()), "Preferred audio"))
    if entry.get("pref_sub_language"):
        facts.append(("Sub: {}".format(entry["pref_sub_language"].title()), "Preferred subtitles"))
    if entry.get("pref_resolution"):
        facts.append(("{}p".format(entry["pref_resolution"]), "Preferred resolution"))
    return facts


def _facts_html(facts):
    if not facts:
        return ""
    return '<div class="anime-meta">{}</div>'.format(" ".join(
        '<span class="badge badge-neutral" title="{}">{}</span>'.format(escape(t), escape(label))
        for label, t in facts))


def render_pending_card(i, a, banner=""):
    """A watchlist entry the resolver has not turned into a full entry yet.
    Same shape as render_watchlist_card, minus the actions that need a
    resolved release: a status line saying where resolving stands, and Remove."""
    name = a.get("name", "Unknown")
    url = a.get("url", "")
    failure = pending_resolve_error(a)
    if a.get("no_match"):
        status_html = _status_line_html("danger", "No matching release")
        note_html = ('<p class="wl-checked">No release matches your language preference. '
                     'Adjust Preferences, or remove it.</p>')
    elif failure:
        status_html = _status_line_html("danger", "Resolve failed")
        note_html = '<p class="wl-checked wl-checked--danger">{}</p>'.format(escape(failure))
    else:
        status_html = _status_line_html("pending", "Resolving", ["finding a release"])
        note_html = ""

    return """
        <article class="card wl-pending" data-state="pending" data-name="{fname}" id="{anchor}" aria-labelledby="wl-pending-name-{i}">
          {banner}
          <div class="wl-head">
            <div class="wl-title">
              <h3 class="anime-name" id="wl-pending-name-{i}">{name}</h3>
              {url_html}
            </div>
          </div>
          {status_html}{note_html}
          {facts_html}
          {remove_html}
        </article>""".format(
        i=i, fname=escape(str(name).casefold()), anchor=entry_anchor_id(url), banner=banner,
        name=escape(name), url_html=_watchlist_url_html(url),
        status_html=status_html, note_html=note_html, facts_html=_facts_html(_pref_facts(a)),
        remove_html=_render_remove_confirm(
            name, escape(url), "/remove-pending", "Nothing has been downloaded for it yet."),
    )


def _render_edit_panel(i, entry, key, name, is_open=False):
    folder = entry.get("customPackage", entry.get("name", "Unknown"))
    rows = _render_download_rows(i, entry, key, name)
    rows += (
        '<form method="POST" action="/update-folder" class="wl-edit-row">{key}'
        '<label class="wl-edit-label" for="folder-{i}">Download folder</label>'
        '<div class="wl-inline">'
        '<input type="text" id="folder-{i}" name="folder" value="{folder}" class="folder-input">'
        '<button type="submit" class="btn btn-ghost btn-sm">Save folder{sr}</button>'
        '</div></form>').format(key=_key_input(key), i=i, folder=escape(folder),
                                sr=_sr(" for {}".format(name)))

    # Movies are filed without a season folder, so placement is series-only.
    if entry.get("media_type") != "movie":
        season = entry.get("tvdb_season")
        rows += (
            '<form method="POST" action="/entry-edit" class="wl-edit-row">{key}'
            '<input type="hidden" name="edit" value="library">'
            '<span class="wl-edit-label" id="lib-label-{i}">Library placement</span>'
            '<div class="wl-prefs" role="group" aria-labelledby="lib-label-{i}">'
            '<div class="wl-field wl-field-num"><label for="lib-season-{i}">Season</label>'
            '<input type="number" id="lib-season-{i}" name="tvdb_season" value="{season}" '
            'min="0" max="{max_season}" step="1" inputmode="numeric" placeholder="From file" '
            'class="wl-num" aria-describedby="lib-hint-{i}"></div>'
            '<div class="wl-field wl-field-num"><label for="lib-offset-{i}">Episode offset</label>'
            '<input type="number" id="lib-offset-{i}" name="episode_offset" value="{offset}" '
            'min="-{max_offset}" max="{max_offset}" step="1" class="wl-num" '
            'aria-describedby="lib-hint-{i}"></div>'
            '<button type="submit" class="btn btn-ghost btn-sm">Save placement{sr}</button></div>'
            '<p class="wl-edit-hint" id="lib-hint-{i}">Leave season blank to keep the one in each '
            'file name. The offset is added to release episode numbers: '
            '-12 files episode 13 as 1.</p>'
            '</form>').format(
                key=_key_input(key), i=i, sr=_sr(" for {}".format(name)),
                season="" if season is None else escape(str(season)),
                offset=escape(str(entry.get("episode_offset") or 0)),
                max_season=_MAX_LIBRARY_SEASON, max_offset=_MAX_EPISODE_OFFSET)

    if entry.get("tvdb_id"):
        linked = []
        if entry.get("tvdb_season"):
            linked.append("season {}".format(entry["tvdb_season"]))
        if entry.get("episode_offset", 0):
            linked.append("offset {:+d}".format(entry["episode_offset"]))
        rows += (
            '<div class="wl-edit-row"><span class="wl-edit-label">TVDB</span>'
            '<div class="wl-inline"><span class="wl-edit-note">Linked{detail}</span>'
            '{edit_form}'
            '<form method="POST" action="/tvdb-unlink">{key}'
            '<button type="submit" class="btn btn-ghost btn-sm" onclick="{confirm}">'
            'Unlink TVDB{sr}</button></form></div></div>').format(
                edit_form=(
                    '<form method="POST" action="/tvdb-link">{}'
                    '<button type="submit" class="btn btn-ghost btn-sm">Edit TVDB link{}</button>'
                    '</form>').format(_key_input(key), _sr(" for {}".format(name)))
                if tvdb.available else "",
                detail=escape(", " + ", ".join(linked)) if linked else "",
                key=_key_input(key),
                confirm=confirm_attr("Unlink {} from TVDB?".format(name)),
                sr=_sr(" for {}".format(name)))
    elif tvdb.available:
        rows += (
            '<div class="wl-edit-row"><span class="wl-edit-label">TVDB</span>'
            '<div class="wl-inline"><span class="wl-edit-note">Not linked</span>'
            '<form method="POST" action="/tvdb-link">{key}'
            '<button type="submit" class="btn btn-ghost btn-sm">Link TVDB{sr}</button>'
            '</form></div></div>').format(key=_key_input(key), sr=_sr(" for {}".format(name)))

    if entry.get("complete"):
        rows += (
            '<div class="wl-edit-row"><span class="wl-edit-label">Completion</span>'
            '<div class="wl-inline"><span class="wl-edit-note">Marked complete, so the bot skips it</span>'
            '<form method="POST" action="/mark-incomplete">{key}'
            '<button type="submit" class="btn btn-ghost btn-sm">Mark incomplete{sr}</button>'
            '</form></div></div>').format(key=_key_input(key), sr=_sr(" for {}".format(name)))

    rows += _render_remove_confirm(name, key, "/remove",
                                   "Episodes already downloaded stay on disk.")

    return ('<details class="wl-panel wl-edit"{open}><summary>Edit{sr}</summary>'
            '<div class="wl-panel-body">{rows}</div></details>').format(
                open=" open" if is_open else "", sr=_sr(" " + name), rows=rows)


def render_watchlist_card(i, a, outcome=None, open_panel=None, banner=""):
    name = a.get("name", "Unknown")
    url = a.get("url", "")
    # Mutations target this entry by its unique URL (see find_entry_by_url),
    # not by array index — the resolver shifts indices concurrently. ``i``
    # only makes element ids unique on the page.
    key = escape(url)

    tone, headline, details = watchlist_status(a)
    status_html = _status_line_html(tone, headline, details)

    # Badges carry facts only; every action lives in a panel below.
    facts = []
    site = translate_al_status(a.get("al_status"))
    if site and site != headline and not (site == "Finished" and headline == "Complete"):
        facts.append(("Site: {}".format(site), "Status on anime-loads.org"))
    if a.get("tvdb_id"):
        facts.append(("TVDB S{:02d}".format(a["tvdb_season"]) if a.get("tvdb_season") else "TVDB",
                      "Linked to TVDB"))
    elif isinstance(a.get("tvdb_season"), int):
        facts.append(("Season {}".format(a["tvdb_season"]),
                      "The mover files downloads into this library season"))
    if isinstance(a.get("episode_offset"), int) and a["episode_offset"] != 0:
        facts.append(("Offset {:+d}".format(a["episode_offset"]),
                      "Added to release episode numbers when filing downloads"))
    facts += _pref_facts(a)
    facts_html = _facts_html(facts)

    # While paused, Check now would do nothing: the header offers Resume.
    if a.get("paused"):
        head_action = (
            '<form method="POST" action="/entry-edit" class="wl-check">{}'
            '<input type="hidden" name="edit" value="paused">'
            '<input type="hidden" name="paused" value="0">'
            '<button type="submit" class="btn btn-ghost btn-sm">Resume{}</button></form>').format(
                _key_input(key), _sr(" downloads for {}".format(name)))
    else:
        head_action = (
            '<form method="POST" action="/check-now" class="wl-check">{}'
            '<button type="submit" class="btn btn-ghost btn-sm">Check now{}</button></form>').format(
                _key_input(key), _sr(" for {}".format(name)))

    return """
        <article class="card wl-card" id="{anchor}" aria-labelledby="wl-name-{i}" {filter_attrs}>
          {banner}
          <div class="wl-head">
            <div class="wl-title">
              <h3 class="anime-name" id="wl-name-{i}">{name}</h3>
              {url_html}
            </div>
            {head_action}
          </div>
          {status_html}{check_html}
          {facts_html}
          <div class="wl-panels">{ep_panel}{edit_panel}</div>
        </article>""".format(
        i=i, anchor=entry_anchor_id(url), banner=banner,
        name=escape(name), url_html=_watchlist_url_html(url),
        head_action=head_action, filter_attrs=watchlist_filter_attrs(i, a),
        status_html=status_html,
        check_html="" if a.get("paused") else render_entry_check(outcome), facts_html=facts_html,
        ep_panel=_render_episode_panel(i, a, key, name, is_open=open_panel == "episodes"),
        edit_panel=_render_edit_panel(i, a, key, name, is_open=open_panel == "edit"),
    )

_ADD_STEPS = ("Release", "TVDB", "Save")


def render_add_flow_head(current, anime_name, with_tvdb=True):
    """Step indicator (Release -> TVDB -> Save) plus a Cancel link for the
    add-anime flow. Nothing is written until the Save step, so Cancel is a
    plain link home that says so. ``with_tvdb`` drops the TVDB step when no
    TVDB client is configured, since that step then never appears."""
    steps = [s for s in _ADD_STEPS if with_tvdb or s != "TVDB"]
    current_idx = steps.index(current) if current in steps else 0
    items = ""
    for i, label in enumerate(steps):
        if i < current_idx:
            state, aria = "done", ""
        elif i == current_idx:
            state, aria = "current", ' aria-current="step"'
        else:
            state, aria = "todo", ""
        items += '<li class="step step-{}"{}><span class="step-num">{}</span>{}</li>'.format(
            state, aria, i + 1, label)
    cancel_href = status_url("Cancelled: {} was not added".format(anime_name),
                             level="ok", anchor=ANCHOR_ADD_FLOW)
    return (
        '<div class="flow-head">'
        '<ol class="steps" aria-label="Add anime progress">{}</ol>'
        '<a class="btn btn-ghost btn-sm" href="{}">Cancel</a>'
        '</div>'
    ).format(items, escape(cancel_href))


def render_search_results(results, ani_data=None):
    """Search hits, each tagged "In watchlist" / "Pending" when its URL
    (normalized, see normalize_anime_url) is already tracked. Those get no
    Add button, since /add-url would only refuse them."""
    if not results:
        return ""
    data = ani_data or {}
    pending_list = data.get("pending", [])
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

        existing = find_duplicate_entry(data, r.get("url", ""))
        if existing is None:
            fields["tag"] = ""
            fields["action"] = """
            <form method="POST" action="/add-url" style="margin:0;" onsubmit="return scrapeBusy(this, 'Fetching releases…');">
              <input type="hidden" name="url" value="{url}">
              <button type="submit" class="btn btn-primary btn-sm">Add to watchlist</button>
            </form>""".format(url=fields["url"])
        elif any(existing is p for p in pending_list):
            fields["tag"] = ' <span class="badge badge-accent">Pending</span>'
            fields["action"] = ""
        else:
            fields["tag"] = ' <span class="badge badge-ok">In watchlist</span>'
            fields["action"] = ""
        html += """
        <div class="card">
          <div style="display:flex;justify-content:space-between;align-items:start;gap:12px;">
            <div>
              <div class="anime-name">{name}{tag}</div>
              <div class="anime-meta">{type} &middot; {episodes} episodes &middot; {genre}</div>
              {lang_line}
              <div class="anime-url">{url}</div>
            </div>{action}
          </div>
        </div>""".format(**fields)
    html += "</div>"
    return html


def _render_have_field(have_episodes):
    """The add flow's "I already have episodes up to N" field. Every form on
    the step carries a hidden ``have_episodes`` the page script fills from
    this one visible input on submit."""
    return """
      <div class="flow-have">
        <label class="hint" for="flow-have">Already have episodes up to</label>
        <input type="number" id="flow-have" value="{have}" min="0" max="{cap}" inputmode="numeric"
               class="wl-num" aria-describedby="flow-have-hint" data-next-hint="flow-have-hint">
        <span class="hint" id="flow-have-hint">Next download: episode {next}</span>
      </div>""".format(have=have_episodes, cap=_MAX_SANE_EPISODE_COUNT, next=have_episodes + 1)


def render_releases(anime_info, best_id=None, with_tvdb=True, note="", have_episodes=0):
    """Release picker, step 1 of the add flow. ``note`` is an optional hint
    shown above the list (e.g. why auto-select didn't skip this step).
    ``have_episodes`` prefills "Already have episodes up to" (series only)."""
    if not anime_info:
        return ""
    html = '<div class="section">'
    html += render_add_flow_head("Release", anime_info["name"], with_tvdb=with_tvdb)
    html += '<h2>Select Release for: {}</h2>'.format(escape(anime_info["name"]))
    if note:
        html += '<p class="hint flow-note">{}</p>'.format(escape(note))
    media_type = anime_info.get("media_type", "series") or "series"
    have_episodes = parse_have_episodes(have_episodes) or 0
    html += """
    <div class="card" style="margin-bottom:16px;">
      <label class="hint" for="release-folder">Folder name in /anime library:</label>
      <input type="text" id="release-folder" value="{name}" style="margin-top:4px;margin-bottom:0;">{have}
    </div>""".format(name=escape(anime_info["name"]),
                     have="" if media_type == "movie" else _render_have_field(have_episodes))

    year = _parse_year(anime_info.get("year"))
    display_title = anime_info.get("display_title") or ""
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
              <input type="hidden" name="year" value="{year}">
              <input type="hidden" name="display_title" value="{display_title}">
              <input type="hidden" name="have_episodes" value="{have}">
              <button type="submit" class="btn btn-primary btn-sm">Add this release</button>
            </form>
          </div>
        </div>""".format(
            res=rel["resolution"], dubs=dubs, subs=subs, eps=rel["episodes"],
            size=rel["size_mb"], group=escape(rel["group"]), url=escape(anime_info["url"]),
            name=escape(anime_info["name"]), rid=rel["id"], highlight=highlight,
            best_label=best_label, mt=escape(media_type), valid_ids=escape(valid_ids),
            year=year or "", display_title=escape(display_title),
            have=0 if media_type == "movie" else have_episodes,
        )
    html += "</div>"
    return html


def render_entry_release_picker(entry, anime_info, best_id=None):
    """Release picker for an entry already on the watchlist (Edit > Change
    release). Picking one posts to /entry-edit; the entry keeps its episode
    count, so the page says where downloads continue from."""
    name = entry.get("name", "Unknown")
    key = entry.get("url", "")
    current = entry.get("releaseID")
    media_type = anime_info.get("media_type", "series") or "series"
    valid_ids = ",".join(str(rel["id"]) for rel in anime_info["releases"])
    cancel_href = status_url("Cancelled: {} unchanged".format(name), level="ok",
                             anchor=entry_anchor_id(key), panel="edit")

    html = '<div class="section">'
    html += '<h2>Change release: {}</h2>'.format(escape(name))
    if media_type == "movie":
        hint = "Switching release keeps everything else about this entry."
    else:
        eps = _episode_count(entry)
        hint = ("You have episodes up to {}, so after switching the bot downloads "
                "from episode {} of the new release.").format(eps, eps + 1)
    html += '<p class="hint">{}</p>'.format(escape(hint))
    html += ('<div class="flow-actions"><a class="btn btn-ghost" href="{}">Cancel</a></div>').format(
        escape(cancel_href))

    for rel in anime_info["releases"]:
        dubs = escape(", ".join(rel["dubs"])) if rel["dubs"] else "&mdash;"
        subs = escape(", ".join(rel["subs"])) if rel["subs"] else "&mdash;"
        is_current = str(rel["id"]) == str(current)
        labels = ""
        if is_current:
            labels += ' <span class="badge badge-ok">Current</span>'
        if rel["id"] == best_id:
            labels += ' <span class="badge badge-accent">Best match</span>'
        if is_current:
            action = '<span class="hint" style="margin-left:auto;">In use</span>'
        else:
            action = """
            <form method="POST" action="/entry-edit" style="margin:0;margin-left:auto;">
              <input type="hidden" name="key" value="{key}">
              <input type="hidden" name="edit" value="release">
              <input type="hidden" name="release_id" value="{rid}">
              <input type="hidden" name="release_ids" value="{valid_ids}">
              <input type="hidden" name="release_episodes" value="{eps}">
              <input type="hidden" name="media_type" value="{mt}">
              <button type="submit" class="btn btn-primary btn-sm">Use this release</button>
            </form>""".format(key=escape(key), rid=rel["id"], valid_ids=escape(valid_ids),
                              eps=rel["episodes"], mt=escape(media_type))
        html += """
        <div class="card {highlight}">
          <div class="release-row">
            <span class="badge badge-res">{res}p</span>
            <span class="badge badge-neutral">Dub: {dubs}</span>
            <span class="badge badge-neutral">Sub: {subs}</span>
            <span class="badge badge-ep">{eps} eps</span>
            <span class="hint">#{rid} &middot; {size}MB &middot; {group}</span>
            {labels}{action}
          </div>
        </div>""".format(
            highlight="card-selected" if is_current else "", res=rel["resolution"],
            dubs=dubs, subs=subs, eps=rel["episodes"], rid=rel["id"], size=rel["size_mb"],
            group=escape(rel["group"]), labels=labels, action=action)
    html += "</div>"
    return html


def _release_summary(rel):
    """One-line description of a release for the auto-select note."""
    bits = ["{}p".format(rel.get("resolution", "?"))]
    if rel.get("dubs"):
        bits.append("Dub: " + ", ".join(rel["dubs"]))
    if rel.get("subs"):
        bits.append("Sub: " + ", ".join(rel["subs"]))
    bits.append("{} eps".format(rel.get("episodes", 0)))
    if rel.get("group"):
        bits.append(str(rel["group"]))
    return " · ".join(bits)


def render_tvdb_step(anime_name, url, release_id, custom_folder,
                     search_results=None, seasons=None, selected_tvdb_id="",
                     selected_tvdb_name="", ep_count=0, edit_key=None,
                     media_type="series", release_ids="", episodes=0,
                     query=None, year="", display_title="", auto_release=None,
                     have_episodes=0, current_season=None, current_offset=0):
    """Render the TVDB correlation page shown between release selection and saving.

    When edit_key is set (the existing entry's URL), this is editing an existing
    entry — forms POST to /tvdb-save instead of /add-release, carrying the URL as
    the stable ``key`` so the save resolves the right entry even if the resolver
    shifted indices in the meantime.

    When media_type == "movie", the UI searches TVDB's movie catalogue and
    drops the season picker (movies have no seasons). Clicking "Link" on a
    result saves the tvdb_id and returns to the watchlist.

    ``release_ids``/``episodes``/``year``/``display_title`` are the same
    release-selection metadata render_releases first put on the page, carried
    forward through every form on this multi-step flow so /add-release and
    /tvdb-seasons never need to re-scrape to validate or re-derive them.

    ``query`` is the TVDB search text last used (defaults to the anime name).
    It stays in the search box and rides along to /tvdb-seasons, so picking a
    series doesn't silently re-run the search under a different query.
    ``auto_release`` is the release auto-select picked when it skipped the
    picker, so the page can say what was chosen.

    ``have_episodes`` is the add flow's "Already have episodes up to" value,
    shown as a field here too since auto-select can skip the picker.
    ``current_season``/``current_offset`` prefill the season picker when
    editing an existing TVDB link.
    """
    is_movie = (media_type == "movie")
    is_add = edit_key is None
    save_action = "/add-release" if is_add else "/tvdb-save"
    query = anime_name if query is None else query

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
    have_episodes = parse_have_episodes(have_episodes) or 0
    if is_add:
        hidden += (
            '<input type="hidden" name="year" value="{year}">'
            '<input type="hidden" name="display_title" value="{dt}">'
            '<input type="hidden" name="have_episodes" value="{have}">'
        ).format(year=_parse_year(year) or "", dt=escape(display_title or ""),
                 have=0 if is_movie else have_episodes)
    else:
        hidden += '<input type="hidden" name="key" value="{}">'.format(escape(edit_key))

    html = '<div class="section">'
    if is_add:
        html += render_add_flow_head("TVDB", anime_name)
    html += '<h2>TVDB Correlation: {}</h2>'.format(escape(anime_name))
    if is_movie:
        html += '<p class="hint">Detected as <strong>Anime Movie</strong>, searching TVDB movies. No season selection needed.</p>'
    else:
        html += '<p class="hint">Link this anime to a TVDB series and season so downloads are placed in the correct season folder.</p>'
    if auto_release:
        html += '<p class="hint flow-note">Auto-selected the release that best matches your preferences: <strong>{}</strong></p>'.format(
            escape(_release_summary(auto_release)))
    if not is_add and selected_tvdb_id:
        linked = "Currently linked to TVDB {}".format(selected_tvdb_id)
        if current_season is not None:
            linked += ", season {}".format(current_season)
        if current_offset:
            linked += ", offset {:+d}".format(current_offset)
        html += '<p class="hint flow-note">{}.</p>'.format(escape(linked))

    if is_add:
        # Leave the TVDB step without linking: save as-is, or go back to the
        # release picker (a fresh fetch, so it gets the scrape busy label).
        html += """
    <div class="flow-actions">
      <form method="POST" action="{action}">
        {hidden}
        <input type="hidden" name="tvdb_skip" value="1">
        <button type="submit" class="btn btn-ghost">Save without TVDB</button>
      </form>
      <form method="POST" action="/add-url" onsubmit="return scrapeBusy(this, 'Fetching releases…');">
        <input type="hidden" name="url" value="{url}">
        <input type="hidden" name="pick" value="1">
        <input type="hidden" name="have_episodes" value="{have}">
        <button type="submit" class="btn btn-ghost">Change release</button>
      </form>
    </div>""".format(hidden=hidden, action=save_action, url=escape(url), have=have_episodes)
        if not is_movie:
            html += '<div class="card" style="margin-bottom:16px;">{}</div>'.format(
                _render_have_field(have_episodes))
    else:
        html += """
    <div class="flow-actions">
      <form method="POST" action="{action}">
        {hidden}
        <input type="hidden" name="tvdb_skip" value="1">
        <button type="submit" class="btn btn-ghost">Cancel</button>
      </form>
    </div>""".format(hidden=hidden, action=save_action)

    # Search box
    search_placeholder = "Search TVDB movies..." if is_movie else "Search TVDB..."
    search_button = "Search TVDB Movies" if is_movie else "Search TVDB"
    html += """
    <div class="card" style="margin-bottom:16px;">
      <form method="POST" action="/tvdb-search" style="display:flex;gap:8px;align-items:center;margin:0;">
        {hidden}
        <input type="text" name="query" value="{query}" placeholder="{placeholder}" aria-label="TVDB search" style="flex:1;margin:0;">
        <button type="submit" class="btn btn-primary">{button}</button>
      </form>
    </div>""".format(hidden=hidden, query=escape(query),
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
            query_field = '<input type="hidden" name="tvdb_query" value="{}">'.format(escape(query))
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
                      {hidden}{query_field}
                      <input type="hidden" name="tvdb_id" value="{tid}">
                      <input type="hidden" name="tvdb_name" value="{name}">
                      <button type="submit" class="btn btn-primary btn-sm">{button}</button>
                    </form>
                  </div>
                </div>""".format(
                    name=escape(r["name"]), year=escape(year_str),
                    overview=escape(overview), tid=escape(str(r["tvdb_id"])),
                    hidden=hidden, query_field=query_field, border=border,
                    action=result_action, button=result_button)

    # Season picker (shown after selecting a series — never for movies)
    if seasons is not None and not is_movie:
        html += '<div class="card card-accent" style="margin-top:16px;">'
        html += '<h3 style="margin:0 0 8px;">Seasons for: {}</h3>'.format(escape(selected_tvdb_name))

        best_season = suggest_tvdb_season(seasons, ep_count)

        for s in seasons:
            is_suggested = best_season is not None and s["season_number"] == best_season
            suggest_label = ' <span class="badge badge-accent">Likely match ({} eps)</span>'.format(ep_count) if is_suggested else ""
            is_current = not is_add and current_season is not None and s["season_number"] == current_season
            if is_current:
                suggest_label = ' <span class="badge badge-ok">Current</span>' + suggest_label
            special_label = ' <span class="hint">Specials</span>' if s["season_number"] == 0 else ""
            highlight = "background:var(--accent-soft-bg);border-radius:6px;padding-left:8px;padding-right:8px;" if is_suggested else ""
            html += """
            <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;{highlight}">
              <span>
                <strong>Season {snum}</strong>{special}
                <span class="badge badge-ep">{eps} eps</span>
                {suggest}
              </span>
              <form method="POST" action="{save_action}" style="margin:0;">
                {hidden}
                <input type="hidden" name="tvdb_id" value="{tid}">
                <input type="hidden" name="tvdb_season" value="{snum}">
                <input type="hidden" name="episode_offset" value="{offset}">
                <button type="submit" class="btn btn-primary btn-sm">Use Season {snum}</button>
              </form>
            </div>""".format(
                snum=s["season_number"], eps=s["episode_count"],
                suggest=suggest_label, special=special_label, hidden=hidden,
                tid=escape(str(selected_tvdb_id)), highlight=highlight,
                save_action=save_action,
                # Re-picking the linked season keeps its offset; a different
                # season starts from none (set one under Advanced).
                offset=current_offset if is_current else 0)

        # Advanced: manual offset input. Defaults to the suggested season,
        # or with no suggestion to the first regular (non-specials) season.
        if not is_add and current_season is not None:
            default_season = current_season
        elif best_season is not None:
            default_season = best_season
        else:
            regular = [s["season_number"] for s in seasons if s["season_number"] != 0]
            default_season = min(regular) if regular else (seasons[0]["season_number"] if seasons else 1)
        html += """
        <details style="margin-top:12px;"{adv_open}>
          <summary class="hint" style="cursor:pointer;">Advanced: episode offset</summary>
          <div style="margin-top:8px;">
            <p class="hint" style="margin:0 0 8px;">
              Set this if anime-loads numbers episodes from 1 but TVDB continues from a previous season
              (e.g., offset 12 means ep 1 becomes E13).
            </p>
            <form method="POST" action="{save_action}" style="display:flex;gap:8px;align-items:center;margin:0;">
              {hidden}
              <input type="hidden" name="tvdb_id" value="{tid}">
              <label for="adv-season">Season:</label>
              <input type="number" id="adv-season" name="tvdb_season" min="0" value="{suggested}" style="width:60px;margin:0;" required>
              <label for="adv-offset">Offset:</label>
              <input type="number" id="adv-offset" name="episode_offset" value="{offset}" style="width:60px;margin:0;">
              <button type="submit" class="btn btn-primary btn-sm">Save season</button>
            </form>
          </div>
        </details>""".format(hidden=hidden, tid=escape(str(selected_tvdb_id)),
                             suggested=default_season,
                             save_action=save_action,
                             offset=current_offset if not is_add else 0,
                             adv_open=" open" if (not is_add and current_offset) else "")
        html += '</div>'

    html += "</div>"
    return html


def render_settings_card(settings, auth_enabled):
    """Render the collapsed Settings card's inner HTML.

    Non-secret fields (hoster, timedelay, jdhost, myjd_user, myjd_device) are
    editable and pre-filled. Secrets (myjd_pw, pushbullet_apikey) NEVER echo
    their value — only a "set"/"not set" badge — and their replace/clear
    inputs are rendered `disabled` (and hinted) unless `auth_enabled`, since
    the dashboard has no way to protect them from a drive-by POST otherwise.
    """
    settings = settings if isinstance(settings, dict) else {}
    filled, _ = config_defaults.fill_settings_defaults(settings)

    try:
        hoster_val = int(filled.get("hoster"))
    except (TypeError, ValueError):
        hoster_val = config_defaults.DEFAULT_SETTINGS["hoster"]
    hoster_options = "".join(
        '<option value="{}" {}>{}</option>'.format(
            val, "selected" if val == hoster_val else "", escape(label))
        for val, label in config_defaults.HOSTER_CHOICES
    )

    try:
        timedelay = int(filled.get("timedelay"))
        if timedelay <= 0:
            raise ValueError
    except (TypeError, ValueError):
        timedelay = config_defaults.DEFAULT_SETTINGS["timedelay"]
    timedelay_minutes = max(1, round(timedelay / 60))

    jdhost = escape(str(filled.get("jdhost") or ""))
    myjd_user = escape(str(filled.get("myjd_user") or ""))
    myjd_device = escape(str(filled.get("myjd_device") or ""))

    myjd_pw_set = bool(filled.get("myjd_pw"))
    pushbullet_set = bool(filled.get("pushbullet_apikey"))

    if auth_enabled:
        secret_disabled = ""
        secret_hint = ""
    else:
        secret_disabled = "disabled"
        secret_hint = ('<p class="hint">Enable dashboard login (DASHBOARD_USER + DASHBOARD_PASS) '
                        'to edit secrets here.</p>')

    def secret_badge(is_set):
        return ('<span class="badge badge-ok">set</span>' if is_set
                else '<span class="badge badge-neutral">not set</span>')

    return """
      <form method="POST" action="/save-settings">
        <div class="form-grid">
          <div class="form-group">
            <label for="settings-hoster">Hoster</label>
            <select name="hoster" id="settings-hoster">{hoster_options}</select>
          </div>
          <div class="form-group">
            <label for="settings-timedelay">Poll interval (minutes)</label>
            <input type="number" name="timedelay_minutes" id="settings-timedelay" min="1" max="1440" value="{timedelay_minutes}">
          </div>
          <div class="form-group">
            <label for="settings-jdhost">JDownloader host</label>
            <input type="text" name="jdhost" id="settings-jdhost" value="{jdhost}" placeholder="e.g. jdownloader or 127.0.0.1">
          </div>
          <div class="form-group">
            <label for="settings-myjd-user">MyJDownloader user</label>
            <input type="text" name="myjd_user" id="settings-myjd-user" value="{myjd_user}">
          </div>
          <div class="form-group">
            <label for="settings-myjd-device">MyJDownloader device</label>
            <input type="text" name="myjd_device" id="settings-myjd-device" value="{myjd_device}">
          </div>
        </div>
        <div class="form-grid">
          <div class="form-group">
            <label for="settings-myjd-pw">MyJDownloader password {myjd_pw_badge}</label>
            <input type="password" name="myjd_pw_new" id="settings-myjd-pw" placeholder="Replace…" autocomplete="new-password" {secret_disabled}>
            <div class="toggle">
              <input type="checkbox" name="myjd_pw_clear" id="myjd_pw_clear" {secret_disabled}>
              <label for="myjd_pw_clear" style="font-size:0.85rem;">Clear</label>
            </div>
          </div>
          <div class="form-group">
            <label for="settings-pushbullet-apikey">Pushbullet API key {pushbullet_badge}</label>
            <input type="password" name="pushbullet_apikey_new" id="settings-pushbullet-apikey" placeholder="Replace…" autocomplete="new-password" {secret_disabled}>
            <div class="toggle">
              <input type="checkbox" name="pushbullet_apikey_clear" id="pushbullet_apikey_clear" {secret_disabled}>
              <label for="pushbullet_apikey_clear" style="font-size:0.85rem;">Clear</label>
            </div>
          </div>
        </div>
        {secret_hint}
        <div style="margin-top:14px;">
          <button type="submit" class="btn btn-success">Save Settings</button>
        </div>
      </form>""".format(
        hoster_options=hoster_options,
        timedelay_minutes=timedelay_minutes,
        jdhost=jdhost,
        myjd_user=myjd_user,
        myjd_device=myjd_device,
        myjd_pw_badge=secret_badge(myjd_pw_set),
        pushbullet_badge=secret_badge(pushbullet_set),
        secret_disabled=secret_disabled,
        secret_hint=secret_hint,
    )


def validate_settings_form(params, auth_enabled):
    """Validate a raw /save-settings POST body.

    Returns (updates, errors): ``updates`` is a {settings-key: value} dict
    ready to merge into ani.json's "settings" block; ``errors`` is a list of
    user-facing validation messages (empty means the form was valid).
    Secret fields are only ever added to ``updates`` when ``auth_enabled`` —
    a POST that tries to set/clear one while auth is off is rejected
    outright rather than silently ignored, so a disabled-input bypass
    attempt surfaces as an error instead of a no-op that looks like success.
    Never puts a secret's VALUE into ``errors`` or anywhere else that gets
    logged or rendered back.
    """
    errors = []
    updates = {}

    try:
        hoster_val = int(params.get("hoster", ""))
    except ValueError:
        hoster_val = None
    if hoster_val not in {val for val, _ in config_defaults.HOSTER_CHOICES}:
        errors.append("Invalid hoster selection")
    else:
        updates["hoster"] = hoster_val

    try:
        minutes = int(params.get("timedelay_minutes", ""))
    except ValueError:
        minutes = None
    if minutes is None or not (1 <= minutes <= 1440):
        errors.append("Poll interval must be a whole number of minutes between 1 and 1440")
    else:
        updates["timedelay"] = minutes * 60

    updates["jdhost"] = params.get("jdhost", "").strip()
    updates["myjd_user"] = params.get("myjd_user", "").strip()
    updates["myjd_device"] = params.get("myjd_device", "").strip()

    wants_secret_change = (
        bool(params.get("myjd_pw_new")) or "myjd_pw_clear" in params
        or bool(params.get("pushbullet_apikey_new")) or "pushbullet_apikey_clear" in params
    )
    if wants_secret_change and not auth_enabled:
        errors.append("Enable dashboard login (DASHBOARD_USER + DASHBOARD_PASS) to edit secrets")
    elif auth_enabled:
        if "myjd_pw_clear" in params:
            updates["myjd_pw"] = ""
        elif params.get("myjd_pw_new"):
            updates["myjd_pw"] = params["myjd_pw_new"]
        if "pushbullet_apikey_clear" in params:
            updates["pushbullet_apikey"] = ""
        elif params.get("pushbullet_apikey_new"):
            updates["pushbullet_apikey"] = params["pushbullet_apikey_new"]

    return updates, errors


def render_page(status="", search_html="", prefs_open=False, ani_data=None, search_query="",
                max_runs=RUN_HISTORY_PAGE, status_at=None, open_panel=None):
    """``status_at`` is the anchor the action redirected to (see status_url):
    the ``status`` banner renders next to it rather than at the page top, and
    the section's disclosure (or the card's ``open_panel``) is open again. An
    anchor that no longer exists (e.g. a removed entry) falls back to the top."""
    data = ani_data if ani_data is not None else load_ani()
    anime_list = data.get("anime", [])
    pending_list = data.get("pending", [])

    entry_anchors = {entry_anchor_id(e.get("url", "")) for e in anime_list + pending_list}
    if status_at not in SECTION_ANCHORS and status_at not in entry_anchors:
        status_at = None
    prefs = load_prefs()

    activity = get_activity()
    bot_status_html, last_run_html, next_run_html = render_activity(activity)
    run_state = activity.get("run_state")
    run_state = run_state if isinstance(run_state, dict) else {}
    history_html = render_run_history(activity["runs"], run_state.get("runs"), max_runs=max_runs)

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
    page = page.replace("%%STATUS_MSG%%", "" if status_at else status)
    for anchor in SECTION_ANCHORS:
        page = page.replace("%%STATUS@{}%%".format(anchor), status if status_at == anchor else "")
    page = page.replace("%%SETTINGS_OPEN%%", "open" if status_at == ANCHOR_SETTINGS else "")
    prefs_open = prefs_open or status_at == ANCHOR_PREFS
    page = page.replace("%%BOT_STATUS%%", bot_status_html)
    page = page.replace("%%LAST_RUN%%", last_run_html)
    page = page.replace("%%NEXT_RUN%%", next_run_html)
    page = page.replace("%%RUN_HISTORY%%", history_html)
    page = page.replace("%%HEALTH%%", health_html)
    page = page.replace("%%MOVE_STATUS%%", move_status_html)
    page = page.replace("%%MOVE_LAST_RUN%%", move_last_html)
    page = page.replace("%%MOVE_NOW_BUTTON%%", render_move_now_button())
    page = page.replace("%%MOVE_HISTORY%%", move_history_html)
    page = page.replace("%%MOVE_STUCK%%", move_stuck_html)
    page = page.replace("%%SEARCH_RESULTS%%", search_html)
    focus = None
    if status_at in entry_anchors:
        focus = {"anchor": status_at, "panel": open_panel, "banner": status}
    page = page.replace("%%WATCHLIST%%", render_watchlist(
        anime_list, pending_list, run_state.get("entries"), focus=focus))
    page = page.replace("%%SETTINGS_CARD%%", render_settings_card(data.get("settings"), AUTH_ENABLED))
    page = page.replace("%%WATCHLIST_CONTROLS%%", render_watchlist_controls(anime_list, pending_list))
    page = page.replace("%%COUNT%%", watchlist_heading_count(anime_list, pending_list))
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
    # Last, so the user's query text can't be re-read as another %%TOKEN%%.
    page = page.replace("%%SEARCH_QUERY%%", escape(search_query))

    return page


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def parse_request(self):
        """The single gate for every route, present and future: this runs
        before any do_GET/do_POST/etc. is dispatched (see
        BaseHTTPRequestHandler.handle_one_request), so a handler added later
        can't accidentally bypass auth/CSRF by skipping a call other routes
        remember to make. Auth covers every method; CSRF only applies to
        state-changing POSTs."""
        if not BaseHTTPRequestHandler.parse_request(self):
            return False
        if not self._authorize():
            return False
        if self.command == "POST" and not self._check_csrf():
            return False
        return True

    def _authorize(self):
        """HTTP Basic auth, only enforced when both DASHBOARD_USER and
        DASHBOARD_PASS are configured. Both the username and password are
        compared with hmac.compare_digest, and both comparisons always run
        (even on a username mismatch) so a wrong username can't be timed
        apart from a wrong password. Never logs the Authorization header,
        the password, or the attempted username — at most the client IP."""
        if not AUTH_ENABLED:
            return True
        header = self.headers.get("Authorization", "")
        valid = False
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[len("Basic "):].strip(), validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                decoded = None
            if decoded is not None and ":" in decoded:
                user, _, password = decoded.partition(":")
                user_ok = hmac.compare_digest(user.encode("utf-8"), DASHBOARD_USER.encode("utf-8"))
                pass_ok = hmac.compare_digest(password.encode("utf-8"), DASHBOARD_PASS.encode("utf-8"))
                valid = user_ok and pass_ok
        if valid:
            return True
        _log.warning("failed dashboard login from %s", self.client_address[0])
        body = b"401 Unauthorized\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Aniloads", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _check_csrf(self):
        """CSRF check, independent of auth: a cross-site POST — e.g. from a
        malicious page a LAN browser visits — must not be able to trigger a
        state change. Origin (or, absent that, Referer) must match the
        dashboard's effective host, compared as host:port (inherently
        scheme-insensitive, since netloc excludes scheme).

        "Effective host" accounts for a reverse proxy in front of the
        dashboard: nginx's default proxy_pass forwards the UPSTREAM Host
        (e.g. "anime-web:8080") while the browser's Origin carries the
        PUBLIC name (e.g. "https://aniloads.home.lan") — an exact match
        against Host alone would then 403 every legitimate form POST after
        such a deploy, silently making the dashboard read-only. So a
        request also passes when Origin/Referer matches the first value of
        X-Forwarded-Host (set by a proxy that rewrites Host), or matches one
        of DASHBOARD_ALLOWED_ORIGINS (an explicit escape hatch for a proxy
        that forwards neither). X-Forwarded-Host is set by the proxy, not
        the browser — a cross-site attacker's own form POST has no way to
        set it — so honoring it doesn't weaken this check.

        Requests with neither Origin nor Referer (non-browser clients, old
        browsers) are allowed — there's nothing to check them against, and
        the dashboard's own forms are same-origin, so this only widens the
        door for clients that were never going to send a forgeable browser
        header anyway."""
        origin = self.headers.get("Origin")
        referer = self.headers.get("Referer")
        candidate = origin if origin is not None else referer
        if candidate is None:
            return True

        if origin is not None and origin.strip().lower() == "null":
            self._csrf_reject(origin)
            return False

        parsed = urlparse(candidate)
        candidate_netloc = parsed.netloc.lower()
        candidate_origin = "{}://{}".format(parsed.scheme.lower(), candidate_netloc)

        host = (self.headers.get("Host") or "").strip().lower()
        forwarded_host = (self.headers.get("X-Forwarded-Host") or "").split(",")[0].strip().lower()

        if candidate_netloc == host:
            return True
        if forwarded_host and candidate_netloc == forwarded_host:
            return True
        if candidate_origin in DASHBOARD_ALLOWED_ORIGINS:
            return True

        self._csrf_reject(candidate)
        return False

    def _csrf_reject(self, origin_value):
        host_value = self.headers.get("Host") or ""
        msg = (
            "Cross-site form post rejected (Origin {} does not match Host {}). "
            "If you use a reverse proxy, forward X-Forwarded-Host or set "
            "DASHBOARD_ALLOWED_ORIGINS.".format(origin_value, host_value)
        )
        _log.warning(msg)
        body = (msg + "\n").encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond(self, code, html):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _redirect(self, url):
        self.send_response(303)
        self.send_header("Location", url)
        self.end_headers()

    def _redirect_msg(self, msg, level=None, anchor=None, panel=None):
        """Redirect home with a status banner message, URL-encoded so a name
        containing ``& = # %`` survives intact — parse_qs decodes it on the GET
        side. A raw ``/?msg=...`` truncated everything after the first ``&``.

        ``level`` ("ok"/"err") lets a caller state the banner tone explicitly
        instead of the GET side guessing it from a leading "Error" — a
        failure message that doesn't start with that word (e.g. "Could not
        fetch releases: ...") otherwise renders as a green success. Omitted,
        do_GET falls back to the old prefix sniff, so an un-migrated caller
        keeps its previous behavior.

        ``anchor`` lands the user back where they acted instead of at the page
        top: a section anchor (ANCHOR_*) or entry_anchor_id(url) for a card,
        with ``panel`` ("episodes"/"edit") re-opened. See status_url."""
        self._redirect(status_url(msg, level=level, anchor=anchor, panel=panel,
                                  runs=self._referer_runs()))

    def _referer_runs(self):
        """The ``?runs=`` Run History depth of the page the form was posted
        from, so a redirect doesn't reset the reader's paging. None when the
        page had none (or there is no Referer)."""
        headers = getattr(self, "headers", None)
        referer = headers.get("Referer", "") if headers is not None else ""
        qs = parse_qs(urlparse(referer).query)
        return parse_runs_param(qs) if "runs" in qs else None

    def _show_flow(self, search_html, search_query=""):
        """Post/Redirect/Get for an add-flow step: stash the rendered step and
        redirect to it, so a reload re-renders instead of re-posting."""
        params = {"flow": stash_flow(search_html, search_query)}
        runs = self._referer_runs()
        if runs:
            params["runs"] = runs
        self._redirect("/?" + urlencode(params) + "#" + ANCHOR_ADD_FLOW)

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

        qs = parse_qs(parsed.query)
        if parsed.path == "/api/status":
            activity = get_activity()
            bot_status_html, last_run_html, next_run_html = render_activity(activity)
            history_html = render_run_history(
                activity["runs"], activity.get("run_state", {}).get("runs"),
                max_runs=parse_runs_param(qs))
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
        # Only ever compared against known ids by render_page, never echoed.
        status_at = qs.get("at", [None])[0]
        open_panel = qs.get("open", [None])[0]
        if "msg" in qs:
            msg = qs["msg"][0]
            level = qs.get("level", [None])[0]
            if level not in ("ok", "err"):
                level = "ok" if not msg.startswith("Error") else "err"
            status = render_status_banner(msg, level)

        search_html, search_query = "", ""
        if "flow" in qs:
            flow = load_flow(qs["flow"][0])
            if flow is not None:
                search_html, search_query = flow
            elif not status:
                status = render_status_banner(
                    "That step has expired. Search again or paste the URL again.", "err")
                status_at = ANCHOR_ADD_FLOW

        try:
            page = render_page(status=status, search_html=search_html, search_query=search_query,
                               max_runs=parse_runs_param(qs),
                               status_at=status_at, open_panel=open_panel)
        except anistore.CorruptStoreError as e:
            _log.error("[watchlist] ani.json is corrupt, refusing to render it: %s", e)
            status = render_status_banner(
                "Error: ani.json is corrupt — the watchlist can't be shown or edited until it is fixed or restored.",
                "err")
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

    def _add_release(self, params, auto_release=None):
        """/add-release: the TVDB step, then the save. Also entered straight
        from /add-url when auto-select picked ``auto_release`` for the user,
        with ``params`` shaped like the picker form's."""
        url = params.get("url", "").strip()
        name = params.get("name", "Unknown")
        custom_folder = params.get("custom_folder", "").strip()

        duplicate = find_duplicate_entry(load_ani(), url)
        if duplicate is not None:
            self._redirect_msg(_duplicate_msg(duplicate), level="err",
                               anchor=entry_anchor_id(duplicate.get("url", "")))
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
                "Error: invalid release selection — please fetch releases again",
                level="err", anchor=ANCHOR_ADD_FLOW)
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
                release_ids=params.get("release_ids", ""), episodes=episodes,
                year=params.get("year", ""),
                display_title=params.get("display_title", ""),
                auto_release=auto_release,
                have_episodes=params.get("have_episodes", 0))
            self._show_flow(search_html)
            return

        folder_name_raw = custom_folder if custom_folder else name
        have_episodes = parse_have_episodes(params.get("have_episodes"))
        if have_episodes is None:
            self._redirect_msg(
                "Error: 'Already have episodes up to' must be a whole number, 0 or more",
                level="err")
            return
        if media_type == "movie":
            have_episodes = 0
        folder_name = _safe_folder_segment(folder_name_raw)
        if not folder_name:
            self._redirect_msg("Error: invalid folder name", level="err", anchor=ANCHOR_ADD_FLOW)
            return

        # New entries start with the global prefs as their per-entry prefs,
        # the same badges an entry resolved earlier carries.
        prefs = load_prefs()
        entry = {
            "url": url,
            "name": name,
            "episodes": have_episodes,
            "missing": [],
            "customPackage": folder_name,
            "pref_audio_language": prefs.get("audio_language", "german"),
            "pref_sub_language": prefs.get("sub_language", "any"),
            "pref_resolution": prefs.get("min_resolution", 1080),
            # Bot-owned scalars, known from the release fetch already: set
            # now so e.g. a movie shows its Movie badge before the bot's
            # first cycle (the bot's delta save overwrites them only when
            # its own value differs).
            "media_type": media_type,
        }
        year = _parse_year(params.get("year"))
        if year:
            entry["year"] = year
        display_title = params.get("display_title", "").strip()[:200]
        if display_title:
            entry["display_title"] = display_title
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
        found = []

        def _append(data):
            dup = find_duplicate_entry(data, url)
            if dup is not None:
                found.append(dup)
                return
            data.setdefault("anime", []).append(entry)

        update_ani(_append)
        if found:
            self._redirect_msg(_duplicate_msg(found[0]), level="err",
                               anchor=entry_anchor_id(found[0].get("url", "")))
            return

        season_info = ""
        if entry.get("tvdb_season") is not None:
            season_info = ", season {}".format(entry["tvdb_season"])
        _log.info("[watchlist] Added: %s (folder=%s, tvdb_id=%s%s)",
                  name, folder_name, entry.get("tvdb_id", "-"), season_info)
        folder_display = (
            folder_name if folder_name == folder_name_raw
            else "{} (saved as '{}')".format(folder_name_raw, folder_name))
        auto_info = ""
        if auto_release:
            auto_info = "; auto-selected the {}p release".format(
                auto_release.get("resolution", "?"))
        if have_episodes:
            auto_info += "; downloads start at episode {}".format(have_episodes + 1)
        self._redirect_msg("Added: {} (folder: {}{}{})".format(
            name, folder_display, season_info, auto_info), level="ok",
            anchor=entry_anchor_id(url))

    def _entry_edit(self, params):
        """/entry-edit: one per-entry setting from a watchlist card's Edit
        panel (``edit`` = episodes / paused / prefs / release / library),
        keyed by URL.
        Input is validated first; a release pick may need one scrape to be
        confirmed, which happens before the single update_ani lock hold."""
        entry_url = params.get("key", "")
        edit = params.get("edit", "")
        if edit == "episodes":
            value = parse_have_episodes(params.get("episodes"))
            if value is None:
                self._redirect_msg("Error: episodes must be a whole number, 0 or more", level="err",
                                   anchor=entry_anchor_id(entry_url), panel="edit")
                return
        elif edit == "paused":
            if params.get("paused") not in ("0", "1"):
                self._redirect_msg("Error: invalid pause request", level="err",
                                   anchor=entry_anchor_id(entry_url), panel="edit")
                return
            value = params.get("paused") == "1"
        elif edit == "prefs":
            value, err = parse_entry_prefs(params)
            if err:
                self._redirect_msg("Error: {}".format(err), level="err",
                                   anchor=entry_anchor_id(entry_url), panel="edit")
                return
        elif edit == "library":
            value, err = parse_library_placement(params)
            if err:
                self._redirect_msg("Error: {}".format(err), level="err",
                                   anchor=entry_anchor_id(entry_url), panel="edit")
                return
        elif edit == "release":
            if not params.get("release_id"):
                self._redirect_msg("Error: no release selected", level="err",
                                   anchor=entry_anchor_id(entry_url), panel="edit")
                return
            value, _media_type, _eps = _resolve_release_selection(entry_url, {
                "release_id": params.get("release_id", ""),
                "release_ids": params.get("release_ids", ""),
                "media_type": params.get("media_type", ""),
                "episodes": params.get("release_episodes", ""),
            })
            if not value:
                self._redirect_msg(
                    "Error: invalid release selection. Fetch releases again", level="err",
                                   anchor=entry_anchor_id(entry_url), panel="edit")
                return
        else:
            self._redirect_msg("Error: unknown edit", level="err",
                               anchor=entry_anchor_id(entry_url), panel="edit")
            return

        outcome = {}

        def _apply(data):
            _, entry = find_entry_by_url(data.get("anime", []), entry_url)
            if entry is None:
                outcome["result"] = "not_found"
                return
            result, detail = apply_entry_edit(entry, edit, value)
            outcome.update(result=result, detail=detail, name=entry.get("name", "?"),
                           note=next_download_note(entry),
                           episodes=_episode_count(entry), release=entry.get("releaseID"))

        update_ani(_apply)
        result = outcome.get("result")
        if result == "not_found":
            self._redirect_msg("Error: entry not found", level="err", anchor=ANCHOR_WATCHLIST)
            return
        name = outcome["name"]
        if result == "invalid":
            self._redirect_msg("Error: {}: {}".format(name, outcome["detail"]), level="err",
                               anchor=entry_anchor_id(entry_url), panel="edit")
            return
        _log.info("[watchlist] Edit %s (%s): %s", name, edit, result)
        changed = result == "saved"
        note = outcome["note"]
        if edit == "episodes":
            msg = "{}: {} episodes up to {}. {}".format(
                name, "you have" if changed else "already set to have", outcome["episodes"], note)
        elif edit == "paused":
            if value:
                msg = "Paused {}. The bot skips it until you resume.".format(name) if changed                     else "{} is already paused.".format(name)
            else:
                msg = "Resumed {}. {}".format(name, note) if changed                     else "{} is not paused.".format(name)
        elif edit == "prefs":
            msg = ("Saved release preferences for {}. They apply when you change release."
                   if changed else "Release preferences for {} unchanged.").format(name)
        elif edit == "library":
            msg = "{}: {}.".format(
                name, library_placement_note(value) if changed
                else "library season and episode offset unchanged")
        else:
            msg = "{} {} release #{}. {}".format(
                name, "now uses" if changed else "already uses", outcome["release"], note)
        self._redirect_msg(msg.strip(), level="ok",
                           anchor=entry_anchor_id(entry_url), panel="edit")

    def _dispatch_post(self, parsed, params):
        if parsed.path == "/run-now":
            ok, msg = trigger_run_now()
            if ok:
                _log.info("[bot] Run now requested via dashboard")
                self._redirect_msg(msg, anchor=ANCHOR_BOT)
            else:
                _log.warning("[bot] Run now request rejected: %s", msg)
                self._redirect_msg("Error: {}".format(msg), level="err", anchor=ANCHOR_BOT)

        elif parsed.path == "/check-now":
            entry_url = params.get("key", "")
            ok, msg = trigger_run_now(entry_url=entry_url)
            if ok:
                _log.info("[bot] Check now requested via dashboard: %s", entry_url)
                self._redirect_msg(msg, anchor=entry_anchor_id(entry_url))
            else:
                _log.warning("[bot] Check now request rejected: %s", msg)
                self._redirect_msg("Error: {}".format(msg), level="err", anchor=entry_anchor_id(entry_url))

        elif parsed.path == "/move-now":
            if not os.path.isdir(DOWNLOAD_DIR):
                _log.warning("[mover] Move Now rejected: download directory not mounted")
                self._redirect_msg(
                    "Error: download directory not mounted — nothing to move", level="err",
                    anchor=ANCHOR_MOVER)
            else:
                _log.info("[mover] Move Now triggered via dashboard")
                _move_trigger.set()
                self._redirect_msg("Move cycle triggered", anchor=ANCHOR_MOVER)

        elif parsed.path == "/move-stuck-ignore":
            key = params.get("key", "")
            msg = stuck_ignore(key)
            if msg is not None:
                _log.info("[mover] Ignoring stuck item: %s", msg)
                self._redirect_msg("Ignoring: {}".format(msg), anchor=ANCHOR_MOVER)
            else:
                self._redirect_msg("Error: stuck item not found", level="err", anchor=ANCHOR_MOVER)

        elif parsed.path == "/move-stuck-delete":
            key = params.get("key", "")
            result = stuck_delete_download(key)
            if result is None:
                self._redirect_msg("Error: stuck item not found", level="err", anchor=ANCHOR_MOVER)
            elif result.startswith("error:"):
                self._redirect_msg("Error: {}".format(result[len("error:"):]), level="err",
                                   anchor=ANCHOR_MOVER)
            else:
                _log.info("[mover] Deleted downloaded copy: %s", result)
                self._redirect_msg("Deleted download copy: {}".format(result), anchor=ANCHOR_MOVER)

        elif parsed.path == "/move-stuck-anyway":
            key = params.get("key", "")
            msg = stuck_move_anyway(key)
            if msg is not None:
                _log.info("[mover] Will move anyway on next cycle: %s", msg)
                self._redirect_msg("Will move on next cycle: {}".format(msg), anchor=ANCHOR_MOVER)
            else:
                self._redirect_msg("Error: stuck item not found", level="err", anchor=ANCHOR_MOVER)

        elif parsed.path == "/save-prefs":
            try:
                min_resolution = int(params.get("min_resolution", "1080"))
            except ValueError:
                self._redirect_msg(
                    "Error: minimum resolution must be a number", level="err",
                    anchor=ANCHOR_PREFS)
                return
            prefs = {
                "audio_language": params.get("audio_language", "german"),
                "sub_language": params.get("sub_language", "any"),
                "min_resolution": min_resolution,
                "auto_select": "auto_select" in params,
            }
            save_prefs(prefs)
            self._redirect_msg("Preferences saved", anchor=ANCHOR_PREFS)

        elif parsed.path == "/save-settings":
            updates, errors = validate_settings_form(params, AUTH_ENABLED)
            if errors:
                self._redirect_msg("Error: {}".format("; ".join(errors)), level="err",
                                   anchor=ANCHOR_SETTINGS)
                return

            def _apply(data):
                settings = data.get("settings")
                filled, _ = config_defaults.fill_settings_defaults(
                    settings if isinstance(settings, dict) else {})
                filled.update(updates)
                data["settings"] = filled
                return data

            update_ani(_apply)
            self._redirect_msg("Saved — restart the bot container to apply", anchor=ANCHOR_SETTINGS)

        elif parsed.path == "/add-url":
            url = params.get("url", "").strip()
            if not url or "anime-loads.org" not in url.lower():
                self._redirect_msg("Error: Invalid URL", level="err", anchor=ANCHOR_ADD_FLOW)
                return
            # "Change release" from the TVDB step posts pick=1: show the
            # picker even when auto-select would otherwise skip it.
            force_pick = "pick" in params

            # Cheap pre-check so re-adding something already present skips
            # the scrape below entirely. Not the authoritative check — that
            # happens inside update_ani's single lock hold at save time, so
            # a concurrent add (or a bot/resolver write landing while this
            # request's scrape is in flight) is never missed or clobbered.
            duplicate = find_duplicate_entry(load_ani(), url)
            if duplicate is not None:
                self._redirect_msg(_duplicate_msg(duplicate), level="err",
                                   anchor=entry_anchor_id(duplicate.get("url", "")))
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
                # pre-scrape snapshot. Pending entries get no per-entry prefs
                # preset: the resolver applies the global prefs current at
                # resolve time, which is what its "adjust Preferences" hint
                # promises.
                slug = url.rstrip("/").split("/")[-1]
                name = slug.replace("-", " ").title()
                found = []

                def _add_pending(data):
                    dup = find_duplicate_entry(data, url)
                    if dup is not None:
                        found.append(dup)
                        return
                    data.setdefault("pending", []).append(
                        {"url": url, "name": name, "status": "pending"})

                update_ani(_add_pending)
                if found:
                    self._redirect_msg(_duplicate_msg(found[0]), level="err",
                                       anchor=entry_anchor_id(found[0].get("url", "")))
                    return
                msg = "Could not fetch releases{}, added to pending queue".format(
                    ": " + err if err else "")
                self._redirect_msg(msg, level="err", anchor=entry_anchor_id(url))
                return

            prefs = load_prefs()
            best = pick_best_release(anime_info["releases"], prefs)

            if prefs.get("auto_select") and best and not force_pick:
                # Honor auto-select: take the best match straight to the TVDB
                # step (or save, with no TVDB), exactly as if it had been
                # clicked on the picker below.
                self._add_release({
                    "url": anime_info["url"],
                    "name": anime_info["name"],
                    "release_id": str(best["id"]),
                    "release_ids": ",".join(str(rel["id"]) for rel in anime_info["releases"]),
                    "episodes": str(best.get("episodes", 0) or 0),
                    "custom_folder": "",
                    "media_type": anime_info.get("media_type", "series") or "series",
                    "year": str(anime_info.get("year") or ""),
                    "display_title": anime_info.get("display_title") or "",
                }, auto_release=best)
                return

            note = ""
            if prefs.get("auto_select") and not best:
                note = "No release matches your preferences, so auto-select was skipped. Pick one below."
            search_html = render_releases(
                anime_info, best["id"] if best else None,
                with_tvdb=tvdb.available, note=note,
                have_episodes=params.get("have_episodes", 0))
            self._show_flow(search_html)

        elif parsed.path == "/add-release":
            self._add_release(params)

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
                episodes=params.get("episodes", 0), query=query,
                year=params.get("year", ""),
                display_title=params.get("display_title", ""),
                have_episodes=params.get("have_episodes", 0))
            self._show_flow(search_html)

        elif parsed.path == "/tvdb-seasons":
            url = params.get("url", "").strip()
            name = params.get("name", "Unknown")
            custom_folder = params.get("custom_folder", "").strip()
            tvdb_id = params.get("tvdb_id", "")
            tvdb_name = params.get("tvdb_name", "")
            edit_key = params.get("key")
            # The query that produced the result the user just picked, so the
            # list stays the one they were looking at.
            query = params.get("tvdb_query", "").strip() or name

            # Fetch seasons for the selected series
            seasons = tvdb.get_seasons(tvdb_id) if tvdb.available and tvdb_id else []

            # Validate the release_id / media_type / episode-count carried
            # from the release step (for the season auto-suggestion) rather
            # than re-scraping to look them up — see
            # _resolve_release_selection's docstring for the
            # validate-or-rescrape rule this applies. An invalid/tampered
            # release_id resolves to "" here (no auto-suggestion); the
            # actual save at /add-release rejects it outright.
            release_id, media_type, ep_count = _resolve_release_selection(url, params)

            # Re-run the search so results stay visible
            content_type = "movie" if media_type == "movie" else "series"
            results = tvdb.search(query, content_type=content_type) if tvdb.available else []

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
                release_ids=params.get("release_ids", ""), episodes=ep_count,
                query=query, year=params.get("year", ""),
                display_title=params.get("display_title", ""),
                have_episodes=params.get("have_episodes", 0))
            self._show_flow(search_html)

        elif parsed.path == "/search":
            query = params.get("q", "").strip()
            if not query:
                self._redirect_msg("Error: Empty search", level="err", anchor=ANCHOR_ADD_FLOW)
                return

            results, err = search_anime(query)

            if err:
                search_html = '<div class="section"><div class="status-msg status-err">Search error: {}</div></div>'.format(escape(err))
            elif not results:
                search_html = '<div class="section"><div class="status-msg status-err">No results for &quot;{}&quot;</div></div>'.format(escape(query))
            else:
                search_html = render_search_results(results, load_ani())

            self._show_flow(search_html, query)

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
                self._redirect_msg("Removed: {}".format(removed.get("name", "?")),
                                   anchor=ANCHOR_WATCHLIST)
            else:
                self._redirect_msg("Error: entry not found", level="err", anchor=ANCHOR_WATCHLIST)

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
                self._redirect_msg("Removed: {}".format(removed.get("name", "?")),
                                   anchor=ANCHOR_WATCHLIST)
            else:
                self._redirect_msg("Error: entry not found", level="err", anchor=ANCHOR_WATCHLIST)

        elif parsed.path == "/ep-add":
            entry_url = params.get("key", "")
            try:
                ep = int(params.get("ep", -1))
            except ValueError:
                self._redirect_msg(
                    "Error: episode number must be numeric", level="err",
                    anchor=entry_anchor_id(entry_url), panel="episodes")
                return
            outcome = {}

            def _ep_add(data):
                anime_list = data.get("anime", [])
                _, entry = find_entry_by_url(anime_list, entry_url)
                if entry is None:
                    outcome["result"] = "invalid"
                    return
                ep_max = ep_add_max(entry)
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
                self._redirect_msg("Added episode {} to retry queue for {}".format(ep, outcome["name"]),
                                   anchor=entry_anchor_id(entry_url), panel="episodes")
            elif outcome["result"] == "already":
                self._redirect_msg("Episode {} already in retry queue".format(ep),
                                   anchor=entry_anchor_id(entry_url), panel="episodes")
            else:
                self._redirect_msg("Error: entry not found or invalid episode", level="err",
                                   anchor=entry_anchor_id(entry_url), panel="episodes")

        elif parsed.path == "/ep-remove":
            entry_url = params.get("key", "")
            try:
                ep = int(params.get("ep", -1))
            except ValueError:
                self._redirect_msg(
                    "Error: episode number must be numeric", level="err",
                    anchor=entry_anchor_id(entry_url), panel="episodes")
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
                self._redirect_msg("Removed episode {} from retry queue for {}".format(ep, outcome["name"]),
                                   anchor=entry_anchor_id(entry_url), panel="episodes")
            elif outcome["result"] == "not_queued":
                self._redirect_msg("Episode {} not in retry queue".format(ep),
                                   anchor=entry_anchor_id(entry_url), panel="episodes")
            else:
                self._redirect_msg("Error: entry not found or invalid episode", level="err",
                                   anchor=entry_anchor_id(entry_url), panel="episodes")

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
                if entry.get("tvdb_id"):
                    # Already linked: open the step on the current link so
                    # changing the season or offset is one click, not
                    # unlink + search + relink.
                    seasons = (tvdb.get_seasons(entry["tvdb_id"])
                               if media_type != "movie" else None)
                    search_html = render_tvdb_step(
                        name, url, "", "",
                        search_results=results, edit_key=url,
                        media_type=media_type, seasons=seasons,
                        selected_tvdb_id=entry["tvdb_id"], selected_tvdb_name=name,
                        ep_count=_episode_count(entry),
                        current_season=entry.get("tvdb_season"),
                        current_offset=entry.get("episode_offset", 0) or 0)
                else:
                    search_html = render_tvdb_step(
                        name, url, "", "",
                        search_results=results, edit_key=url,
                        media_type=media_type)
                self._show_flow(search_html)
            else:
                self._redirect_msg("Error: entry not found or TVDB unavailable", level="err",
                                   anchor=entry_anchor_id(entry_url), panel="edit")

        elif parsed.path == "/tvdb-save":
            entry_url = params.get("key", "")

            if "tvdb_skip" in params:
                # Cancelling a TVDB edit makes no changes — say so, otherwise the
                # bare redirect home looks like the click did nothing (UI-5).
                # Read-only lookup: no mutation, so no need for update_ani here.
                _, entry = find_entry_by_url(load_ani().get("anime", []), entry_url)
                if entry is not None:
                    self._redirect_msg("Cancelled — {} unchanged".format(
                        entry.get("name", "?")), anchor=entry_anchor_id(entry_url), panel="edit")
                else:
                    self._redirect_msg("Cancelled", anchor=ANCHOR_WATCHLIST)
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
                self._redirect_msg("TVDB linked: {}{}".format(outcome["name"], outcome["season_str"]),
                                   anchor=entry_anchor_id(entry_url), panel="edit")
            else:
                self._redirect_msg("Error: entry not found", level="err", anchor=ANCHOR_WATCHLIST)

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
                self._redirect_msg("TVDB unlinked: {}".format(outcome["name"]),
                                   anchor=entry_anchor_id(entry_url), panel="edit")
            else:
                self._redirect_msg("Error: entry not found", level="err", anchor=ANCHOR_WATCHLIST)

        elif parsed.path == "/entry-releases":
            entry_url = params.get("key", "")
            _, entry = find_entry_by_url(load_ani().get("anime", []), entry_url)
            if entry is None:
                self._redirect_msg("Error: entry not found", level="err", anchor=ANCHOR_WATCHLIST)
                return
            # Scrape with no lock held (see update_ani).
            anime_info, err = get_releases(entry_url)
            if err or not anime_info or not anime_info.get("releases"):
                self._redirect_msg("Error: could not fetch releases for {}{}".format(
                    entry.get("name", "?"), ": " + err if err else ""), level="err",
                    anchor=entry_anchor_id(entry_url), panel="edit")
                return
            best = pick_best_release(anime_info["releases"],
                                     entry_effective_prefs(entry, load_prefs()))
            search_html = render_entry_release_picker(
                entry, anime_info, best["id"] if best else None)
            self._show_flow(search_html)

        elif parsed.path == "/entry-edit":
            self._entry_edit(params)

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
                self._redirect_msg("Folder updated: {} -> {}".format(outcome["name"], shown),
                                   anchor=entry_anchor_id(entry_url), panel="edit")
            else:
                self._redirect_msg("Error: entry not found or empty folder", level="err",
                                   anchor=entry_anchor_id(entry_url), panel="edit")

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
                self._redirect_msg("Marked incomplete: {}".format(outcome["name"]),
                                   anchor=entry_anchor_id(entry_url), panel="edit")
            else:
                self._redirect_msg("Error: entry not found", level="err", anchor=ANCHOR_WATCHLIST)

        else:
            self._redirect("/")


def apply_resolved_pending(data, resolved_entries, no_match_urls=(), failures=None,
                           cleared_urls=()):
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

    `failures`: ``{url: {"reason": ..., "ts": ...}}`` for entries whose
    resolve attempt failed this pass, stored as the pending entry's
    `resolve_error` so its card can say why instead of "Resolving" forever.
    `cleared_urls`: entries whose scrape succeeded this pass; any stale
    `resolve_error` on them is dropped.

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
        if failures and url in failures:
            p["resolve_error"] = failures[url]
        elif url in cleared_urls or url in no_match_urls:
            p.pop("resolve_error", None)
        remaining.append(p)
    data["pending"] = remaining
    return data


def resolve_failure(reason):
    """A pending entry's ``resolve_error`` record: the reason (bounded, like
    the bot's run-state reasons) plus when the attempt failed, in UTC."""
    return {"reason": " ".join(str(reason).split())[:200],
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


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
            failures = {}
            cleared_urls = set()

            for entry in pending:
                url = entry.get("url", "")
                if not url:
                    continue

                _log.info("[resolver] Resolving: %s", entry.get("name", url))
                try:
                    info, err = get_releases(url)
                    if err or not info or not info.get("releases"):
                        _log.warning("[resolver] Failed for %s: %s", url, err)
                        failures[url] = resolve_failure(err or "no releases found on the site")
                        continue
                    if entry.get("resolve_error"):
                        cleared_urls.add(url)

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
                    failures[url] = resolve_failure("{}: {}".format(type(e).__name__, e))

                time.sleep(RESOLVE_PENDING_PER_ENTRY_DELAY)

            if resolved_entries or no_match_urls or failures or cleared_urls:
                update_ani(lambda d: apply_resolved_pending(
                    d, resolved_entries, no_match_urls, failures, cleared_urls))
                if resolved_entries:
                    _log.info("[resolver] Moved %d entries to anime list", len(resolved_entries))

        except Exception as e:
            _log.error("[resolver] Error: %s", e)

        time.sleep(RESOLVE_PENDING_BATCH_INTERVAL)


def _notify_mover_events(events):
    """Batch this cycle's NEW mover errors / stuck items into one
    notification, sent off the hot path on its own daemon thread so a slow
    or unreachable notify target never blocks the mover loop.

    `events` only carries an entry for a NEW stuck item — run_move_cycle()
    itself skips appending anything for an already-known (repeat) or ignored
    one, via _stuck_touch's is_new/ignored — so no further dedup is needed
    here."""
    if not NOTIFY_TARGETS:
        return
    noteworthy = [ev for ev in events if ev.get("type") in ("error", "skip")]
    if not noteworthy:
        return
    lines = "; ".join(ev.get("msg", "") for ev in noteworthy[:5])
    if len(noteworthy) > 5:
        lines += " (+{} more)".format(len(noteworthy) - 5)
    message = "Aniloads: {} mover issue{} — {}".format(
        len(noteworthy), "" if len(noteworthy) == 1 else "s", lines)
    threading.Thread(
        target=notify.send_all, args=(NOTIFY_TARGETS, "Aniloads", message), daemon=True
    ).start()


def _run_and_record_move_cycle():
    """Run one mover cycle and record its results. Extracted from the worker
    loop below so it's directly testable.

    Does NOT stamp ``_move_last_run`` when DOWNLOAD_DIR isn't mounted —
    ``run_move_cycle()`` returns immediately with no events in that case, so
    stamping anyway would claim a cycle ran when it never had anything to
    check."""
    global _move_last_run
    mounted = os.path.isdir(DOWNLOAD_DIR)
    events = run_move_cycle()
    with _move_lock:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        for ev in events:
            ev["time"] = ts
            _move_history.append(ev)
        if mounted:
            _move_last_run = datetime.now(timezone.utc)
    save_move_state()
    _notify_mover_events(events)

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


def move_completed_worker():
    """Background thread: move completed downloads to media library."""
    time.sleep(MOVE_STARTUP_DELAY)
    global _move_running

    while True:
        try:
            _move_running = True
            _run_and_record_move_cycle()
            _move_running = False
        except Exception as e:
            _move_running = False
            _log.error("[mover] Error: %s", e)

        _move_trigger.wait(timeout=MOVE_POLL_SECONDS)
        _move_trigger.clear()


if __name__ == "__main__":
    _log.info("Anime-Loads Dashboard starting on port %d", PORT)
    if AUTH_ENABLED:
        _log.info("Dashboard login enabled")
    else:
        _log.info("Dashboard login DISABLED — set DASHBOARD_USER and DASHBOARD_PASS to require one")

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
