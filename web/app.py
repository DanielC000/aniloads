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
import threading
import time
from datetime import datetime, timedelta
from html import escape
from http.server import HTTPServer, BaseHTTPRequestHandler
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

    def restart_container(self, container=BOT_CONTAINER):
        if not self.available:
            return False, "Docker socket not available"
        # Fire-and-forget: Docker restart blocks for seconds while the
        # container stops + starts.  Run it in a background thread so the
        # HTTP handler can redirect the browser immediately.
        t = threading.Thread(
            target=self._request,
            args=("POST", "/containers/{}/restart?t=5".format(container)),
            daemon=True,
        )
        t.start()
        return True, "Container restart triggered"


docker = DockerAPI()

# Move-completed state (thread-safe)
_move_lock = threading.Lock()
_move_history = collections.deque(maxlen=100)
_move_last_run = None
_move_running = False
_move_trigger = threading.Event()


# ---------------------------------------------------------------------------
# TVDB API v4 client (shared module in bot/tvdb.py)
# ---------------------------------------------------------------------------

from tvdb import TVDBClient, TVDB_API_KEY  # noqa: E402 — path set above

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
        elif content.startswith("[SKIP]"):
            # SKIP lines appear before a run starts (no Prüfe), create a standalone entry
            skip_msg = content[len("[SKIP]"):].strip()
            runs.append({
                "time": "",
                "docker_ts": docker_ts,
                "anime": "",
                "events": [{"type": "skip", "msg": skip_msg}],
            })
            continue
        elif content.startswith("[COMPLETE]"):
            # COMPLETE lines appear before a run starts, like SKIP
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
                        now = datetime.utcnow()
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

    return {
        "status": status,
        "runs": runs,
        "last_run": last_run,
        "next_run": next_run_estimate,
    }


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_ani():
    try:
        with open(ANI_JSON, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"settings": {}, "anime": []}


def save_ani(data):
    with open(ANI_JSON, "w") as f:
        json.dump(data, f, indent=4, sort_keys=True)


def load_prefs():
    defaults = {
        "language": "german",
        "min_resolution": 1080,
        "auto_select": True,
    }
    try:
        with open(PREFS_FILE, "r") as f:
            stored = json.load(f)
            defaults.update(stored)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return defaults


def save_prefs(prefs):
    with open(PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=2)


def match_language(dubs, pref_lang):
    dubs_lower = [d.lower() for d in dubs]
    if pref_lang == "german":
        return any("deutsch" in d or "german" in d or "ger" in d for d in dubs_lower)
    elif pref_lang == "japanese":
        return any("japan" in d or "jap" in d for d in dubs_lower)
    return True


def pick_best_release(releases, prefs):
    min_res = prefs.get("min_resolution", 1080)
    pref_lang = prefs.get("language", "german")

    scored = []
    for rel in releases:
        try:
            res = int(rel.get("resolution", 0) or 0)
        except (ValueError, TypeError):
            res = 0
        lang_match = match_language(rel.get("dubs", []), pref_lang)
        try:
            eps = int(rel.get("episodes", 0) or 0)
        except (ValueError, TypeError):
            eps = 0

        if res < min_res:
            continue

        score = 0
        if lang_match:
            score += 1000
        score += res
        score += eps
        scored.append((score, rel))

    if not scored:
        for rel in releases:
            try:
                res = int(rel.get("resolution", 0) or 0)
            except (ValueError, TypeError):
                res = 0
            lang_match = match_language(rel.get("dubs", []), pref_lang)
            score = (1000 if lang_match else 0) + res
            scored.append((score, rel))

    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]
    return releases[0] if releases else None


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
                    "url": r.getURL(),
                    "type": r.getTyp(),
                    "episodes": "{}/{}".format(r.getCurrentEpisodeCount(), r.getMaxEpisodeCount()),
                    "genre": r.getGenre(),
                })
            except Exception:
                pass
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


# ---------------------------------------------------------------------------
# Move-completed logic
# ---------------------------------------------------------------------------

_SEASON_EP_RE = re.compile(r'(.*?)[._][Ss](\d+)[Ee](\d+)')
_VIDEO_EXTS = {'.mkv', '.mp4', '.avi'}
_ARCHIVE_RE = re.compile(r'\.(rar|r\d\d)$')


def parse_season_episode(filename):
    """Extract (name_part, season, episode) from a filename like 'Anime.Name.S01E05.mkv'."""
    m = _SEASON_EP_RE.match(filename)
    if not m:
        return None
    name_part = m.group(1)
    season = int(m.group(2))
    episode = int(m.group(3))
    return name_part, season, episode


def _lookup_anime_entries():
    """Load the anime list once per cycle for move lookups."""
    return load_ani().get("anime", [])


def _entry_to_match(entry, folder_name):
    """Build a match dict from an ani.json entry."""
    return {
        "folder_name": folder_name,
        "tvdb_season": entry.get("tvdb_season"),
        "episode_offset": entry.get("episode_offset", 0) or 0,
        "media_type": entry.get("media_type", "series"),
        "year": entry.get("year"),
        "display_title": entry.get("display_title") or entry.get("name", ""),
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
    }


def _sanitize_folder(name):
    """Strip characters that are illegal in folder names on common filesystems."""
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


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


def run_move_cycle():
    """Scan download directory and move completed anime to media library.

    Returns a list of event dicts for the history log.
    """
    events = []

    if not os.path.isdir(DOWNLOAD_DIR):
        return events

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
                if os.path.exists(target_path):
                    events.append({"type": "skip", "msg": "{} \u2014 already exists".format(dest_name)})
                    continue
                try:
                    os.makedirs(target_dir, exist_ok=True)
                    shutil.move(filepath, target_path)
                    events.append({"type": "moved", "msg": "{} \u2192 {}/{}".format(src_name, movie_folder, dest_name)})
                except Exception as e:
                    events.append({"type": "error", "msg": "Failed to move {}: {}".format(src_name, e)})

        else:
            # --- Series path: parse SxxExx per video and route into AnimeName/SXX/ ---
            for filepath in video_files:
                filename = os.path.basename(filepath)
                parsed = parse_season_episode(filename)
                if not parsed:
                    events.append({"type": "error", "msg": "Cannot parse season/episode: {}".format(filename)})
                    continue

                name_part, season, episode = parsed

                # Convert dots/underscores to spaces for matching
                parsed_name = name_part.replace('.', ' ').replace('_', ' ')

                # Match to ani.json entry — gets folder name, TVDB season, and episode offset.
                # Pass parsed season so multiple entries sharing one generic dir prefix
                # (e.g. S01/S02/S03 of the same show) are tiebroken by tvdb_season.
                match = match_anime_entry(parsed_name, entry_name, anime_list, parsed_season=season)
                anime_name = match["folder_name"]

                # Check for existing folder in media library (case-insensitive)
                existing = find_existing_media_folder(anime_name)
                if existing:
                    anime_name = existing

                # TVDB season override
                if match["tvdb_season"] is not None:
                    season = match["tvdb_season"]

                # TVDB episode offset — adjust episode number in filename
                ep_offset = match["episode_offset"]
                if ep_offset:
                    new_ep = episode + ep_offset
                    old_ep_str = 'E{:02d}'.format(episode)
                    new_ep_str = 'E{:02d}'.format(new_ep)
                    new_filename = filename.replace(old_ep_str, new_ep_str)
                    if new_filename != filename:
                        filename = new_filename

                season_dir = 'S{:02d}'.format(season)
                target_dir = os.path.join(MEDIA_DIR, anime_name, season_dir)
                target_path = os.path.join(target_dir, filename)

                if os.path.exists(target_path):
                    events.append({"type": "skip", "msg": "{} \u2014 already exists".format(filename)})
                    continue

                try:
                    os.makedirs(target_dir, exist_ok=True)
                    shutil.move(filepath, target_path)
                    dest_short = "{}/{}".format(anime_name, season_dir)
                    events.append({"type": "moved", "msg": "{} \u2192 {}".format(filename, dest_short)})
                except Exception as e:
                    events.append({"type": "error", "msg": "Failed to move {}: {}".format(filename, e)})

        # --- Cleanup download directory ---
        # Remove leftover non-video files
        for root, _dirs, files in os.walk(dir_path):
            for f in files:
                if os.path.splitext(f)[1].lower() not in _VIDEO_EXTS:
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
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f0f; color: #e0e0e0; padding: 20px; max-width: 900px; margin: 0 auto; }
  h1 { color: #7c4dff; margin-bottom: 20px; font-size: 1.5rem; }
  h2 { color: #b388ff; margin: 20px 0 10px; font-size: 1.2rem; }
  .card { background: #1a1a1a; border-radius: 8px; padding: 16px; margin-bottom: 12px; border: 1px solid #2a2a2a; }
  .card:hover { border-color: #7c4dff; }
  .anime-name { font-weight: 600; font-size: 1.1rem; color: #fff; }
  .anime-meta { color: #888; font-size: 0.85rem; margin-top: 4px; }
  .anime-url { color: #7c4dff; font-size: 0.8rem; word-break: break-all; }
  .btn { display: inline-block; padding: 8px 16px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.9rem; font-weight: 500; text-decoration: none; }
  .btn-primary { background: #7c4dff; color: #fff; }
  .btn-primary:hover { background: #651fff; }
  .btn-danger { background: #d32f2f; color: #fff; }
  .btn-danger:hover { background: #b71c1c; }
  .btn-success { background: #2e7d32; color: #fff; }
  .btn-success:hover { background: #1b5e20; }
  .btn-sm { padding: 4px 10px; font-size: 0.8rem; }
  .btn-warning { background: #e65100; color: #fff; }
  .btn-warning:hover { background: #bf360c; }
  input[type=text], input[type=url], select { width: 100%; padding: 10px 14px; border-radius: 6px; border: 1px solid #333; background: #222; color: #e0e0e0; font-size: 0.95rem; margin-bottom: 10px; }
  select { appearance: none; -webkit-appearance: none; }
  input:focus, select:focus { outline: none; border-color: #7c4dff; }
  .form-row { display: flex; gap: 10px; align-items: end; }
  .form-row input, .form-row select { flex: 1; margin-bottom: 0; }
  .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .form-group label { display: block; color: #888; font-size: 0.85rem; margin-bottom: 4px; }
  .empty { color: #666; font-style: italic; padding: 20px; text-align: center; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 500; }
  .badge-lang { background: #1b5e20; color: #a5d6a7; }
  .badge-res { background: #0d47a1; color: #90caf9; }
  .badge-ep { background: #4a148c; color: #ce93d8; }
  .badge-auto { background: #e65100; color: #ffcc80; }
  .release-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding: 8px 0; border-bottom: 1px solid #2a2a2a; }
  .release-row:last-child { border-bottom: none; }
  .status-msg { padding: 10px; border-radius: 6px; margin-bottom: 15px; }
  .status-ok { background: #1b5e20; color: #a5d6a7; }
  .status-err { background: #b71c1c; color: #ef9a9a; }
  .section { margin-bottom: 30px; }
  .toggle { display: flex; align-items: center; gap: 8px; }
  .toggle input[type=checkbox] { width: 18px; height: 18px; accent-color: #7c4dff; }
  .prefs-current { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
  details summary { cursor: pointer; color: #b388ff; font-size: 1rem; font-weight: 500; }
  details summary:hover { color: #7c4dff; }
  details[open] summary { margin-bottom: 12px; }

  /* Activity section */
  .activity-grid { display: grid; grid-template-columns: 1fr 1fr 1fr auto; gap: 16px; align-items: center; }
  .activity-label { color: #888; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.5px; }
  .activity-value { font-size: 1rem; font-weight: 500; margin-top: 2px; }
  .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .status-dot.running { background: #4caf50; }
  .status-dot.stopped { background: #f44336; }
  .status-dot.unknown { background: #666; }

  /* Run history */
  .run-entry { padding: 10px 0; border-bottom: 1px solid #222; }
  .run-entry:last-child { border-bottom: none; }
  .run-header { display: flex; justify-content: space-between; align-items: center; }
  .run-time { color: #888; font-size: 0.8rem; font-family: monospace; }
  .run-anime { color: #fff; font-weight: 500; font-size: 0.95rem; }
  .run-events { margin-top: 6px; padding-left: 12px; }
  .run-event { font-size: 0.85rem; padding: 2px 0; }
  .ev-download { color: #66bb6a; }
  .ev-error { color: #ef5350; }
  .ev-info { color: #888; }
  .ev-sleep { color: #555; font-size: 0.8rem; }
  .ev-moved { color: #66bb6a; }
  .ev-wait { color: #ffb74d; }
  .ev-skip { color: #888; }
  .ev-cleanup { color: #78909c; }

  /* Episode management panel */
  .ep-panel { margin-top: 12px; border-top: 1px solid #2a2a2a; padding-top: 8px; }
  .ep-panel summary { font-size: 0.85rem; color: #888; }
  .ep-panel summary:hover { color: #b388ff; }
  .ep-panel[open] summary { margin-bottom: 8px; }
  .ep-row { display: flex; align-items: center; gap: 8px; padding: 4px 0; border-bottom: 1px solid #1a1a1a; }
  .ep-row:last-child { border-bottom: none; }
  .ep-num { font-family: monospace; font-size: 0.85rem; min-width: 50px; color: #ccc; }
  .badge-ok { background: #1b5e20; color: #a5d6a7; }
  .badge-retry { background: #b71c1c; color: #ef9a9a; }
  .btn-ghost { background: #333; color: #aaa; border: none; }
  .btn-ghost:hover { background: #444; color: #fff; }
  .ep-add-row { display: flex; gap: 8px; margin-top: 8px; padding-top: 8px; border-top: 1px solid #2a2a2a; align-items: center; }
  .ep-add-row input[type=number] { width: 80px; padding: 4px 8px; border-radius: 4px; border: 1px solid #333; background: #222; color: #e0e0e0; font-size: 0.85rem; margin: 0; }

  @media (max-width: 600px) {
    .activity-grid { grid-template-columns: 1fr 1fr; }
    .form-row { flex-wrap: wrap; }
    .form-row select { flex: 1 1 100%; }
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
          <button type="submit" class="btn btn-warning" onclick="return confirm('Restart bot and trigger a new run?')">Run Now</button>
        </form>
      </div>
    </div>
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
            <label>Preferred Language</label>
            <select name="language">
              <option value="german" %%LANG_GER%%>German (Deutsch)</option>
              <option value="japanese" %%LANG_JAP%%>Japanese</option>
              <option value="any" %%LANG_ANY%%>Any</option>
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
          <label for="auto_select" style="color:#ccc;font-size:0.9rem;">Auto-select best matching release when adding</label>
        </div>
        <div style="margin-top:14px;">
          <button type="submit" class="btn btn-success">Save Preferences</button>
        </div>
      </form>
      <div class="prefs-current">
        <span class="badge badge-lang">%%PREF_LANG_DISPLAY%%</span>
        <span class="badge badge-res">&ge; %%PREF_RES%%p</span>
        %%AUTO_BADGE%%
      </div>
    </div>
  </details>
</div>

<div class="section">
  <h2>Add Anime</h2>
  <div class="card">
    <form method="POST" action="/add-url">
      <label style="color:#888;font-size:0.85rem;">Paste anime-loads.org URL to see available releases:</label>
      <div class="form-row" style="margin-top:6px;">
        <input type="url" name="url" placeholder="https://www.anime-loads.org/media/..." required>
        <button type="submit" class="btn btn-primary">Fetch Releases</button>
      </div>
    </form>
  </div>
  <div class="card">
    <form method="POST" action="/search">
      <label style="color:#888;font-size:0.85rem;">Or search by name:</label>
      <div class="form-row" style="margin-top:6px;">
        <input type="text" name="q" placeholder="Search anime..." required>
        <button type="submit" class="btn btn-primary">Search</button>
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
(function() {
  var ids = ['bot-status','last-run','next-run','run-history',
             'move-status','move-last-run','move-history'];
  function refresh() {
    fetch('/api/status')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        ids.forEach(function(id) {
          var key = id.replace(/-/g, '_');
          var el = document.getElementById(id);
          if (el && d[key] !== undefined) el.innerHTML = d[key];
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
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def render_activity(activity):
    """Render the activity status bar values."""
    status = activity["status"]
    bot_running = status.get("running", False)
    dot_class = "running" if bot_running else "stopped"
    status_text = '<span class="status-dot {}"></span>{}'.format(
        dot_class, "Running" if bot_running else "Stopped"
    )

    last_run = activity.get("last_run")
    if last_run and last_run.get("time"):
        last_text = last_run["time"]
        if last_run.get("anime"):
            last_text += " &mdash; {}".format(escape(last_run["anime"]))
    elif not docker.available:
        last_text = '<span style="color:#666">Docker socket unavailable</span>'
    else:
        last_text = '<span style="color:#666">No runs yet</span>'

    next_text = activity.get("next_run") or '<span style="color:#666">&mdash;</span>'

    return status_text, last_text, next_text


def render_run_history(runs, max_runs=20):
    """Render the run history feed."""
    if not runs:
        if not docker.available:
            return '<div class="empty">Docker socket not mounted — cannot read bot logs</div>'
        return '<div class="empty">No bot runs recorded yet</div>'

    # Show most recent runs first, limit count
    display_runs = list(reversed(runs))[:max_runs]

    html = ""
    for run in display_runs:
        time_str = run.get("time", "")
        anime = run.get("anime", "")
        # [COMPLETE] entries are run-history noise the owner doesn't want surfaced.
        # Drop them from the rendered feed only — the bot still logs [COMPLETE] and
        # parse_bot_logs() still parses it; this is a presentation-layer suppression.
        events = [ev for ev in run.get("events", []) if ev.get("type") != "complete"]

        if not anime and not events:
            continue

        html += '<div class="run-entry">'
        html += '<div class="run-header">'
        if anime:
            html += '<span class="run-anime">{}</span>'.format(escape(anime))
        else:
            html += '<span class="run-anime" style="color:#888">System</span>'
        if time_str:
            html += '<span class="run-time">{}</span>'.format(time_str)
        html += "</div>"

        if events:
            html += '<div class="run-events">'
            for ev in events:
                etype = ev.get("type", "info")
                msg = escape(ev.get("msg", ""))
                css = {
                    "download": "ev-download",
                    "error": "ev-error",
                    "batch": "ev-download",
                    "complete": "ev-moved",
                    "throttle": "ev-wait",
                    "skip": "ev-skip",
                    "info": "ev-info",
                    "sleep": "ev-sleep",
                }.get(etype, "ev-info")
                prefix = {
                    "download": "+",
                    "error": "!",
                    "batch": "\u25a0",
                    "complete": "\u2713",
                    "throttle": "\u23f3",
                    "skip": "\u2013",
                    "info": "-",
                    "sleep": "~",
                }.get(etype, "-")
                html += '<div class="run-event {}"><span style="opacity:0.5">{}</span> {}</div>'.format(
                    css, prefix, msg
                )
            html += "</div>"
        html += "</div>"

    return html


def render_move_status():
    """Render move status and last run time."""
    if _move_running:
        status_html = '<span class="status-dot running"></span>Running'
    else:
        status_html = '<span class="status-dot unknown"></span>Idle'

    with _move_lock:
        if _move_last_run:
            last_html = _move_last_run.strftime("%H:%M:%S")
        elif not os.path.isdir(DOWNLOAD_DIR):
            last_html = '<span style="color:#666">Download dir not mounted</span>'
        else:
            last_html = '<span style="color:#666">Not yet</span>'

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
    css_map = {
        "moved": "ev-moved",
        "error": "ev-error",
        "wait": "ev-wait",
        "skip": "ev-skip",
        "cleanup": "ev-cleanup",
    }
    prefix_map = {
        "moved": "\u2192",
        "error": "!",
        "wait": "\u23f3",
        "skip": "\u2013",
        "cleanup": "\u2717",
    }

    html = ""
    for ev in display:
        etype = ev.get("type", "info")
        msg = escape(ev.get("msg", ""))
        css = css_map.get(etype, "ev-info")
        prefix = prefix_map.get(etype, "-")
        html += '<div class="run-event {}"><span style="opacity:0.5">{}</span> {}</div>'.format(
            css, prefix, msg)
    return html


def render_watchlist(anime_list, pending_list=None):
    if not anime_list and not pending_list:
        return '<div class="empty">No anime in watchlist. Add some above!</div>'
    html = ""

    if pending_list:
        for i, a in enumerate(pending_list):
            name = a.get("name", "Unknown")
            url = a.get("url", "")
            pref_lang = a.get("pref_language", "")
            pref_res = a.get("pref_resolution", "")
            pref_badges = ""
            if pref_lang:
                pref_badges += '<span class="badge badge-lang">{}</span> '.format(pref_lang.title())
            if pref_res:
                pref_badges += '<span class="badge badge-res">{}p</span> '.format(pref_res)

            html += """
            <div class="card" style="border-color:#e65100;">
              <div style="display:flex;justify-content:space-between;align-items:start;">
                <div>
                  <div class="anime-name">{name} <span class="badge badge-auto">Resolving...</span></div>
                  <div class="anime-url">{url}</div>
                  <div class="anime-meta">{pref_badges}</div>
                </div>
                <form method="POST" action="/remove-pending" style="margin:0;">
                  <input type="hidden" name="index" value="{idx}">
                  <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('Remove {name}?')">Remove</button>
                </form>
              </div>
            </div>""".format(name=escape(name), url=escape(url), pref_badges=pref_badges, idx=i)

    for i, a in enumerate(anime_list):
        name = a.get("name", "Unknown")
        url = a.get("url", "")
        eps = a.get("episodes", a.get("episodes_downloaded", 0))
        missing = a.get("missing", [])

        url_html = '<div class="anime-url">{}</div>'.format(escape(url)) if url else ""

        pref_lang = a.get("pref_language", "")
        pref_res = a.get("pref_resolution", "")
        pref_badges = ""
        if pref_lang:
            pref_badges += '<span class="badge badge-lang">{}</span> '.format(pref_lang.title())
        if pref_res:
            pref_badges += '<span class="badge badge-res">{}p</span> '.format(pref_res)

        missing_badge = ""
        if missing:
            missing_badge = ' <span class="badge" style="background:#b71c1c;color:#ef9a9a;">{} retry</span>'.format(len(missing))

        # TVDB badges
        tvdb_badges = ""
        if a.get("tvdb_id"):
            if a.get("tvdb_season"):
                tvdb_badges += ' <span class="badge" style="background:#0d47a1;color:#90caf9;">S{:02d}</span>'.format(
                    a["tvdb_season"])
            else:
                tvdb_badges += ' <span class="badge" style="background:#0d47a1;color:#90caf9;">TVDB</span>'
            if a.get("episode_offset", 0) != 0:
                tvdb_badges += ' <span class="badge" style="background:#4a148c;color:#ce93d8;">Offset +{}</span>'.format(
                    a["episode_offset"])
            tvdb_badges += (' <form method="POST" action="/tvdb-unlink" style="margin:0;display:inline;">'
                            '<input type="hidden" name="index" value="{}">'
                            '<button type="submit" class="btn btn-ghost btn-sm" style="font-size:0.7rem;" '
                            'onclick="return confirm(\'Remove TVDB link?\')">Unlink TVDB</button>'
                            '</form>').format(i)
        elif tvdb.available:
            tvdb_badges += (' <form method="POST" action="/tvdb-link" style="margin:0;display:inline;">'
                            '<input type="hidden" name="index" value="{}">'
                            '<button type="submit" class="btn btn-ghost btn-sm" style="font-size:0.7rem;">Link TVDB</button>'
                            '</form>').format(i)

        # Completion / skip badges
        status_badges = ""
        if a.get("complete"):
            status_badges += ' <span class="badge" style="background:#2e7d32;color:#a5d6a7;">Complete</span>'
            status_badges += (' <form method="POST" action="/mark-incomplete" style="margin:0;display:inline;">'
                              '<input type="hidden" name="index" value="{}">'
                              '<button type="submit" class="btn btn-ghost btn-sm" style="font-size:0.7rem;">Mark Incomplete</button>'
                              '</form>').format(i)
        elif a.get("skip_until"):
            status_badges += ' <span class="badge" style="background:#e65100;color:#ffcc80;">Next: {}</span>'.format(
                escape(str(a["skip_until"])))
        if a.get("al_status"):
            status_badges += ' <span class="badge" style="background:#37474f;color:#b0bec5;">{}</span>'.format(
                escape(a["al_status"]))

        # Folder name (editable)
        folder = a.get("customPackage", name)
        folder_html = (
            '<div class="folder-row" style="margin-top:4px;font-size:0.85rem;color:#999;">'
            'Folder: <form method="POST" action="/update-folder" style="display:inline;margin:0;">'
            '<input type="hidden" name="index" value="{idx}">'
            '<input type="text" name="folder" value="{folder}" '
            'style="background:#1e1e1e;border:1px solid #333;color:#ccc;padding:2px 6px;'
            'border-radius:4px;font-size:0.85rem;width:220px;">'
            ' <button type="submit" class="btn btn-ghost btn-sm" style="font-size:0.7rem;">Save</button>'
            '</form></div>').format(idx=i, folder=escape(folder))

        # Build episode detail panel
        eps_count = int(eps) if isinstance(eps, (int, float)) else 0
        missing_set = set(missing)
        retry_label = ", {} retrying".format(len(missing)) if missing else ""

        ep_rows = ""
        for ep_num in range(1, eps_count + 1):
            if ep_num in missing_set:
                badge = '<span class="badge badge-retry">Retrying</span>'
                action = ('<form method="POST" action="/ep-remove" style="margin:0;">'
                          '<input type="hidden" name="index" value="{}"><input type="hidden" name="ep" value="{}">'
                          '<button type="submit" class="btn btn-ghost btn-sm">Skip</button>'
                          '</form>').format(i, ep_num)
            else:
                badge = '<span class="badge badge-ok">OK</span>'
                action = ('<form method="POST" action="/ep-add" style="margin:0;">'
                          '<input type="hidden" name="index" value="{}"><input type="hidden" name="ep" value="{}">'
                          '<button type="submit" class="btn btn-ghost btn-sm">Retry</button>'
                          '</form>').format(i, ep_num)
            ep_rows += '<div class="ep-row"><span class="ep-num">Ep {}</span> {} {}</div>'.format(ep_num, badge, action)

        # Show missing episodes beyond the current count (manually added)
        for ep_num in sorted(missing):
            if ep_num > eps_count:
                ep_rows += ('<div class="ep-row"><span class="ep-num">Ep {}</span>'
                            ' <span class="badge badge-retry">Retrying</span>'
                            ' <span class="badge badge-auto">Queued</span>'
                            ' <form method="POST" action="/ep-remove" style="margin:0;">'
                            '<input type="hidden" name="index" value="{}"><input type="hidden" name="ep" value="{}">'
                            '<button type="submit" class="btn btn-ghost btn-sm">Skip</button>'
                            '</form></div>').format(ep_num, i, ep_num)

        ep_add_form = ('<div class="ep-add-row">'
                       '<form method="POST" action="/ep-add" style="margin:0;display:flex;gap:8px;align-items:center;">'
                       '<input type="hidden" name="index" value="{}">'
                       '<input type="number" name="ep" min="1" placeholder="Ep #" required>'
                       '<button type="submit" class="btn btn-primary btn-sm">Add to retry</button>'
                       '</form></div>').format(i)

        ep_panel = ('<details class="ep-panel"><summary>Episodes ({} total{})</summary>'
                    '{}{}</details>').format(eps_count, retry_label, ep_rows, ep_add_form)

        html += """
        <div class="card">
          <div style="display:flex;justify-content:space-between;align-items:start;">
            <div>
              <div class="anime-name">{name}</div>
              {url_html}
              <div class="anime-meta">
                <span class="badge badge-ep">{eps} eps</span>{missing_badge}
                {pref_badges}{tvdb_badges}{status_badges}
              </div>
              {folder_html}
            </div>
            <form method="POST" action="/remove" style="margin:0;">
              <input type="hidden" name="index" value="{idx}">
              <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('Remove {name}?')">Remove</button>
            </form>
          </div>
          {ep_panel}
        </div>""".format(
            name=escape(name), url_html=url_html, eps=eps,
            missing_badge=missing_badge, pref_badges=pref_badges,
            tvdb_badges=tvdb_badges, status_badges=status_badges,
            folder_html=folder_html, idx=i, ep_panel=ep_panel,
        )
    return html


def render_search_results(results):
    if not results:
        return ""
    html = '<div class="section"><h2>Search Results</h2>'
    for r in results:
        html += """
        <div class="card">
          <div style="display:flex;justify-content:space-between;align-items:start;">
            <div>
              <div class="anime-name">{name}</div>
              <div class="anime-meta">{type} &middot; {episodes} episodes &middot; {genre}</div>
              <div class="anime-url">{url}</div>
            </div>
            <form method="POST" action="/add-url" style="margin:0;">
              <input type="hidden" name="url" value="{url}">
              <button type="submit" class="btn btn-primary btn-sm">Add</button>
            </form>
          </div>
        </div>""".format(**{k: escape(str(v)) for k, v in r.items()})
    html += "</div>"
    return html


def render_releases(anime_info, best_id=None):
    if not anime_info:
        return ""
    html = '<div class="section"><h2>Select Release for: {}</h2>'.format(escape(anime_info["name"]))
    html += """
    <div class="card" style="margin-bottom:16px;">
      <label style="color:#888;font-size:0.85rem;">Folder name in /anime library:</label>
      <input type="text" id="release-folder" value="{name}" style="margin-top:4px;margin-bottom:0;">
    </div>""".format(name=escape(anime_info["name"]))

    media_type = anime_info.get("media_type", "series") or "series"
    for rel in anime_info["releases"]:
        dubs = ", ".join(rel["dubs"]) if rel["dubs"] else "&mdash;"
        subs = ", ".join(rel["subs"]) if rel["subs"] else "&mdash;"
        is_best = rel["id"] == best_id
        highlight = "border-color:#7c4dff;border-width:2px;" if is_best else ""
        best_label = ' <span class="badge badge-auto">Best Match</span>' if is_best else ""

        html += """
        <div class="card" style="{highlight}">
          <div class="release-row">
            <span class="badge badge-res">{res}p</span>
            <span class="badge badge-lang">Dub: {dubs}</span>
            <span class="badge" style="background:#4a148c;color:#ce93d8;">Sub: {subs}</span>
            <span class="badge badge-ep">{eps} eps</span>
            <span style="color:#888;font-size:0.8rem;">{size}MB &middot; {group}</span>
            {best_label}
            <form method="POST" action="/add-release" style="margin:0;margin-left:auto;"
                  onsubmit="this.querySelector('[name=custom_folder]').value=document.getElementById('release-folder').value;">
              <input type="hidden" name="url" value="{url}">
              <input type="hidden" name="name" value="{name}">
              <input type="hidden" name="release_id" value="{rid}">
              <input type="hidden" name="custom_folder" value="">
              <input type="hidden" name="media_type" value="{mt}">
              <button type="submit" class="btn btn-primary btn-sm">Add this release</button>
            </form>
          </div>
        </div>""".format(
            res=rel["resolution"], dubs=dubs, subs=escape(subs), eps=rel["episodes"],
            size=rel["size_mb"], group=escape(rel["group"]), url=escape(anime_info["url"]),
            name=escape(anime_info["name"]), rid=rel["id"], highlight=highlight,
            best_label=best_label, mt=escape(media_type),
        )
    html += "</div>"
    return html


def render_tvdb_step(anime_name, url, release_id, custom_folder,
                     search_results=None, seasons=None, selected_tvdb_id="",
                     selected_tvdb_name="", ep_count=0, edit_index=None,
                     media_type="series"):
    """Render the TVDB correlation page shown between release selection and saving.

    When edit_index is set, this is editing an existing entry — forms POST to
    /tvdb-save instead of /add-release.

    When media_type == "movie", the UI searches TVDB's movie catalogue and
    drops the season picker (movies have no seasons). Clicking "Link" on a
    result saves the tvdb_id and returns to the watchlist.
    """
    is_movie = (media_type == "movie")
    save_action = "/tvdb-save" if edit_index is not None else "/add-release"

    # Hidden fields carried through every form on this page
    hidden = (
        '<input type="hidden" name="url" value="{url}">'
        '<input type="hidden" name="name" value="{name}">'
        '<input type="hidden" name="release_id" value="{rid}">'
        '<input type="hidden" name="custom_folder" value="{folder}">'
        '<input type="hidden" name="media_type" value="{mt}">'
    ).format(url=escape(url), name=escape(anime_name),
             rid=escape(str(release_id)), folder=escape(custom_folder),
             mt=escape(media_type))
    if edit_index is not None:
        hidden += '<input type="hidden" name="index" value="{}">'.format(edit_index)

    html = '<div class="section"><h2>TVDB Correlation: {}</h2>'.format(escape(anime_name))
    if is_movie:
        html += '<p style="color:#888;font-size:0.85rem;">Detected as <strong>Anime Movie</strong> — searching TVDB movies. No season selection needed.</p>'
    else:
        html += '<p style="color:#888;font-size:0.85rem;">Link this anime to a TVDB series and season so downloads are placed in the correct season folder.</p>'

    # Skip button — save without TVDB
    skip_label = (
        "save without TVDB link" if is_movie
        else ("save without season mapping" if edit_index is None else "cancel")
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
                border = "border-color:#7c4dff;border-width:2px;" if is_selected else ""
                html += """
                <div class="card" style="{border}">
                  <div style="display:flex;justify-content:space-between;align-items:start;">
                    <div>
                      <div class="anime-name">{name}{year}</div>
                      <div style="color:#888;font-size:0.8rem;margin-top:2px;">{overview}</div>
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
        html += '<div class="card" style="border-color:#7c4dff;border-width:2px;margin-top:16px;">'
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
            suggest_label = ' <span class="badge badge-auto">Likely match ({} eps)</span>'.format(ep_count) if is_suggested else ""
            highlight = "background:#1a1a2e;" if is_suggested else ""
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
          <summary style="color:#888;cursor:pointer;font-size:0.85rem;">Advanced: episode offset</summary>
          <div style="margin-top:8px;">
            <p style="color:#888;font-size:0.8rem;margin:0 0 8px;">
              Set this if anime-loads numbers episodes from 1 but TVDB continues from a previous season
              (e.g., offset 12 means ep 1 becomes E13).
            </p>
            <form method="POST" action="{save_action}" style="display:flex;gap:8px;align-items:center;margin:0;">
              {hidden}
              <input type="hidden" name="tvdb_id" value="{tid}">
              <label style="color:#ccc;">Season:</label>
              <input type="number" name="tvdb_season" min="1" value="{suggested}" style="width:60px;margin:0;" required>
              <label style="color:#ccc;">Offset:</label>
              <input type="number" name="episode_offset" value="0" style="width:60px;margin:0;">
              <button type="submit" class="btn btn-primary btn-sm">Confirm</button>
            </form>
          </div>
        </details>""".format(hidden=hidden, tid=selected_tvdb_id,
                             suggested=best_season or 1,
                             save_action=save_action)
        html += '</div>'

    html += "</div>"
    return html


def render_page(status="", search_html="", prefs_open=False):
    data = load_ani()
    anime_list = data.get("anime", [])
    pending_list = data.get("pending", [])
    prefs = load_prefs()
    total = len(anime_list) + len(pending_list)

    activity = get_activity()
    bot_status_html, last_run_html, next_run_html = render_activity(activity)
    history_html = render_run_history(activity["runs"])

    move_status_html, move_last_html = render_move_status()
    move_history_html = render_move_history()

    lang_display = {"german": "German", "japanese": "Japanese", "any": "Any"}.get(prefs["language"], "Any")

    page = HTML_TEMPLATE
    page = page.replace("%%STATUS_MSG%%", status)
    page = page.replace("%%BOT_STATUS%%", bot_status_html)
    page = page.replace("%%LAST_RUN%%", last_run_html)
    page = page.replace("%%NEXT_RUN%%", next_run_html)
    page = page.replace("%%RUN_HISTORY%%", history_html)
    page = page.replace("%%MOVE_STATUS%%", move_status_html)
    page = page.replace("%%MOVE_LAST_RUN%%", move_last_html)
    page = page.replace("%%MOVE_HISTORY%%", move_history_html)
    page = page.replace("%%SEARCH_RESULTS%%", search_html)
    page = page.replace("%%WATCHLIST%%", render_watchlist(anime_list, pending_list))
    page = page.replace("%%COUNT%%", str(total))
    page = page.replace("%%PREFS_OPEN%%", "open" if prefs_open else "")
    page = page.replace("%%LANG_GER%%", 'selected' if prefs["language"] == "german" else "")
    page = page.replace("%%LANG_JAP%%", 'selected' if prefs["language"] == "japanese" else "")
    page = page.replace("%%LANG_ANY%%", 'selected' if prefs["language"] == "any" else "")
    page = page.replace("%%RES_480%%", 'selected' if prefs["min_resolution"] == 480 else "")
    page = page.replace("%%RES_720%%", 'selected' if prefs["min_resolution"] == 720 else "")
    page = page.replace("%%RES_1080%%", 'selected' if prefs["min_resolution"] == 1080 else "")
    page = page.replace("%%AUTO_CHECKED%%", 'checked' if prefs.get("auto_select") else "")
    page = page.replace("%%PREF_LANG_DISPLAY%%", lang_display)
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
            history_html = render_run_history(activity["runs"])
            move_status_html, move_last_html = render_move_status()
            move_history_html = render_move_history()
            payload = json.dumps({
                "bot_status": bot_status_html,
                "last_run": last_run_html,
                "next_run": next_run_html,
                "run_history": history_html,
                "move_status": move_status_html,
                "move_last_run": move_last_html,
                "move_history": move_history_html,
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
            cls = "status-ok" if not msg.startswith("Error") else "status-err"
            status = '<div class="status-msg {}" id="status-msg">{}</div>'.format(cls, escape(msg))

        self._respond(200, render_page(status=status))

    def do_POST(self):
        parsed = urlparse(self.path)
        params = self._read_post()

        if parsed.path == "/run-now":
            ok, msg = docker.restart_container()
            if ok:
                _log.info("[bot] Restart requested via dashboard")
                self._redirect("/?msg=Bot restarting, new run will begin shortly")
            else:
                _log.warning("[bot] Restart failed: %s", msg)
                self._redirect("/?msg=Error: {}".format(msg))

        elif parsed.path == "/move-now":
            _log.info("[mover] Move Now triggered via dashboard")
            _move_trigger.set()
            self._redirect("/?msg=Move cycle triggered")

        elif parsed.path == "/save-prefs":
            prefs = {
                "language": params.get("language", "german"),
                "min_resolution": int(params.get("min_resolution", "1080")),
                "auto_select": "auto_select" in params,
            }
            save_prefs(prefs)
            self._redirect("/?msg=Preferences saved")

        elif parsed.path == "/add-url":
            url = params.get("url", "").strip()
            if not url or "anime-loads.org" not in url:
                self._redirect("/?msg=Error: Invalid URL")
                return

            data = load_ani()
            all_entries = data.get("anime", []) + data.get("pending", [])
            for a in all_entries:
                if a.get("url") == url:
                    self._redirect("/?msg=Already in watchlist")
                    return

            # Fetch releases from site so user can see what's available
            anime_info, err = get_releases(url)
            if err or not anime_info or not anime_info.get("releases"):
                # Fallback: queue to pending if fetch fails
                slug = url.rstrip("/").split("/")[-1]
                name = slug.replace("-", " ").title()
                entry = {"url": url, "name": name, "status": "pending"}
                data.setdefault("pending", []).append(entry)
                save_ani(data)
                msg = "Could not fetch releases{}, added to pending queue".format(
                    ": " + err if err else "")
                self._redirect("/?msg={}".format(msg))
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
            release_id = params.get("release_id", "")
            custom_folder = params.get("custom_folder", "").strip()

            data = load_ani()
            for a in data.get("anime", []):
                if a.get("url") == url:
                    self._redirect("/?msg=Already in watchlist")
                    return

            # If TVDB is available and user hasn't been through the TVDB step yet,
            # show the correlation page instead of saving immediately.
            # For movies the "through the TVDB step" signal is either tvdb_skip
            # or a posted tvdb_id — there's no tvdb_season field to look for.
            media_type = params.get("media_type", "")
            has_tvdb_data = (
                "tvdb_season" in params
                or "tvdb_skip" in params
                or (media_type == "movie" and "tvdb_id" in params)
            )
            if tvdb.available and not has_tvdb_data:
                # First time through — scrape to determine type, then search.
                if not media_type:
                    info, _err = get_releases(url)
                    if info:
                        media_type = info.get("media_type", "series")
                    else:
                        media_type = "series"
                results = tvdb.search(
                    name,
                    content_type="movie" if media_type == "movie" else "series")
                search_html = render_tvdb_step(
                    name, url, release_id, custom_folder,
                    search_results=results, media_type=media_type)
                self._respond(200, render_page(search_html=search_html))
                return

            folder_name = custom_folder if custom_folder else name
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

            data.setdefault("anime", []).append(entry)
            save_ani(data)
            season_info = ""
            if entry.get("tvdb_season"):
                season_info = ", season {}".format(entry["tvdb_season"])
            _log.info("[watchlist] Added: %s (folder=%s, tvdb_id=%s%s)",
                      name, folder_name, entry.get("tvdb_id", "-"), season_info)
            self._redirect("/?msg=Added: {} (folder: {}{})".format(
                name, folder_name, season_info))

        elif parsed.path == "/tvdb-search":
            url = params.get("url", "").strip()
            name = params.get("name", "Unknown")
            release_id = params.get("release_id", "")
            custom_folder = params.get("custom_folder", "").strip()
            query = params.get("query", "").strip() or name
            media_type = params.get("media_type", "series")
            edit_idx = params.get("index")
            edit_idx = int(edit_idx) if edit_idx is not None else None

            content_type = "movie" if media_type == "movie" else "series"
            results = tvdb.search(query, content_type=content_type) if tvdb.available else []
            search_html = render_tvdb_step(
                name, url, release_id, custom_folder,
                search_results=results, edit_index=edit_idx,
                media_type=media_type)
            self._respond(200, render_page(search_html=search_html))

        elif parsed.path == "/tvdb-seasons":
            url = params.get("url", "").strip()
            name = params.get("name", "Unknown")
            release_id = params.get("release_id", "")
            custom_folder = params.get("custom_folder", "").strip()
            tvdb_id = params.get("tvdb_id", "")
            tvdb_name = params.get("tvdb_name", "")
            media_type = params.get("media_type", "series")
            edit_idx = params.get("index")
            edit_idx = int(edit_idx) if edit_idx is not None else None

            # Fetch seasons for the selected series
            seasons = tvdb.get_seasons(tvdb_id) if tvdb.available and tvdb_id else []

            # Re-run the search so results stay visible
            results = tvdb.search(name) if tvdb.available else []

            # Determine ep count from the release (for auto-suggestion)
            ep_count = 0
            if release_id:
                try:
                    anime_info, _ = get_releases(url)
                    if anime_info:
                        for rel in anime_info.get("releases", []):
                            if str(rel.get("id")) == str(release_id):
                                ep_count = rel.get("episodes", 0)
                                break
                except Exception:
                    pass

            search_html = render_tvdb_step(
                name, url, release_id, custom_folder,
                search_results=results, seasons=seasons,
                selected_tvdb_id=tvdb_id, selected_tvdb_name=tvdb_name or name,
                ep_count=ep_count, edit_index=edit_idx,
                media_type=media_type)
            self._respond(200, render_page(search_html=search_html))

        elif parsed.path == "/search":
            query = params.get("q", "").strip()
            if not query:
                self._redirect("/?msg=Error: Empty search")
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
            idx = int(params.get("index", -1))
            data = load_ani()
            pending = data.get("pending", [])
            if 0 <= idx < len(pending):
                removed = pending.pop(idx)
                data["pending"] = pending
                save_ani(data)
                _log.info("[watchlist] Removed pending: %s", removed.get("name") or removed.get("url", "?"))
                self._redirect("/?msg=Removed: {}".format(removed.get("name", "?")))
            else:
                self._redirect("/?msg=Error: Invalid index")

        elif parsed.path == "/remove":
            idx = int(params.get("index", -1))
            data = load_ani()
            anime_list = data.get("anime", [])
            if 0 <= idx < len(anime_list):
                removed = anime_list.pop(idx)
                save_ani(data)
                _log.info("[watchlist] Removed anime: %s", removed.get("name", "?"))
                self._redirect("/?msg=Removed: {}".format(removed.get("name", "?")))
            else:
                self._redirect("/?msg=Error: Invalid index")

        elif parsed.path == "/ep-add":
            idx = int(params.get("index", -1))
            ep = int(params.get("ep", -1))
            data = load_ani()
            anime_list = data.get("anime", [])
            if 0 <= idx < len(anime_list) and ep > 0:
                entry = anime_list[idx]
                missing = entry.get("missing", [])
                if ep not in missing:
                    missing.append(ep)
                    missing.sort()
                    entry["missing"] = missing
                    save_ani(data)
                    self._redirect("/?msg=Added episode {} to retry queue for {}".format(ep, entry.get("name", "?")))
                else:
                    self._redirect("/?msg=Episode {} already in retry queue".format(ep))
            else:
                self._redirect("/?msg=Error: Invalid index or episode")

        elif parsed.path == "/ep-remove":
            idx = int(params.get("index", -1))
            ep = int(params.get("ep", -1))
            data = load_ani()
            anime_list = data.get("anime", [])
            if 0 <= idx < len(anime_list) and ep > 0:
                entry = anime_list[idx]
                missing = entry.get("missing", [])
                if ep in missing:
                    missing.remove(ep)
                    entry["missing"] = missing
                    save_ani(data)
                    self._redirect("/?msg=Removed episode {} from retry queue for {}".format(ep, entry.get("name", "?")))
                else:
                    self._redirect("/?msg=Episode {} not in retry queue".format(ep))
            else:
                self._redirect("/?msg=Error: Invalid index or episode")

        elif parsed.path == "/tvdb-link":
            idx = int(params.get("index", -1))
            data = load_ani()
            anime_list = data.get("anime", [])
            if 0 <= idx < len(anime_list) and tvdb.available:
                entry = anime_list[idx]
                name = entry.get("name", "Unknown")
                url = entry.get("url", "")
                media_type = entry.get("media_type", "series")
                content_type = "movie" if media_type == "movie" else "series"
                results = tvdb.search(name, content_type=content_type)
                search_html = render_tvdb_step(
                    name, url, "", "",
                    search_results=results, edit_index=idx,
                    media_type=media_type)
                self._respond(200, render_page(search_html=search_html))
            else:
                self._redirect("/?msg=Error: Invalid index or TVDB unavailable")

        elif parsed.path == "/tvdb-save":
            idx = int(params.get("index", -1))
            data = load_ani()
            anime_list = data.get("anime", [])

            if "tvdb_skip" in params:
                self._redirect("/")
                return

            if 0 <= idx < len(anime_list):
                entry = anime_list[idx]
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
                        elif "episode_offset" in entry:
                            del entry["episode_offset"]
                    except ValueError:
                        pass

                save_ani(data)
                season_str = " S{:02d}".format(entry.get("tvdb_season", 0)) if entry.get("tvdb_season") else ""
                self._redirect("/?msg=TVDB linked: {}{}".format(
                    entry.get("name", "?"), season_str))
            else:
                self._redirect("/?msg=Error: Invalid index")

        elif parsed.path == "/tvdb-unlink":
            idx = int(params.get("index", -1))
            data = load_ani()
            anime_list = data.get("anime", [])
            if 0 <= idx < len(anime_list):
                entry = anime_list[idx]
                for key in ("tvdb_id", "tvdb_season", "episode_offset"):
                    entry.pop(key, None)
                save_ani(data)
                self._redirect("/?msg=TVDB unlinked: {}".format(entry.get("name", "?")))
            else:
                self._redirect("/?msg=Error: Invalid index")

        elif parsed.path == "/update-folder":
            idx = int(params.get("index", -1))
            folder = params.get("folder", "").strip()
            data = load_ani()
            anime_list = data.get("anime", [])
            if 0 <= idx < len(anime_list) and folder:
                entry = anime_list[idx]
                entry["customPackage"] = folder
                save_ani(data)
                self._redirect("/?msg=Folder updated: {} -> {}".format(
                    entry.get("name", "?"), folder))
            else:
                self._redirect("/?msg=Error: Invalid index or empty folder")

        elif parsed.path == "/mark-incomplete":
            idx = int(params.get("index", -1))
            data = load_ani()
            anime_list = data.get("anime", [])
            if 0 <= idx < len(anime_list):
                entry = anime_list[idx]
                for key in ("complete", "skip_until"):
                    entry.pop(key, None)
                save_ani(data)
                self._redirect("/?msg=Marked incomplete: {}".format(entry.get("name", "?")))
            else:
                self._redirect("/?msg=Error: Invalid index")

        else:
            self._redirect("/")


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
            resolved = []

            for i, entry in enumerate(pending):
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
                    if entry.get("pref_language"):
                        entry_prefs["language"] = entry["pref_language"]
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
                        data.setdefault("anime", []).append(anime_entry)
                        resolved.append(i)
                        _log.info("[resolver] Resolved %s -> release %d (%sp, %s)",
                            info["name"], best["id"], best["resolution"],
                            ", ".join(best.get("dubs", [])))
                except Exception as e:
                    _log.error("[resolver] Error resolving %s: %s", url, e)

                time.sleep(RESOLVE_PENDING_PER_ENTRY_DELAY)

            if resolved:
                for i in sorted(resolved, reverse=True):
                    pending.pop(i)
                data["pending"] = pending
                save_ani(data)
                _log.info("[resolver] Moved %d entries to anime list", len(resolved))

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
                ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
                for ev in events:
                    ev["time"] = ts
                    _move_history.append(ev)
                _move_last_run = datetime.utcnow()
            _move_running = False

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

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
