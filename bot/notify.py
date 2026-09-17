"""Send short push notifications to ntfy, Discord or Gotify.

Stdlib only (`urllib.request`) so this module is importable from both the bot
and web containers without pulling in an extra dependency.

`NOTIFY_URL` holds a comma-separated list of Apprise-style target URLs:

- ``ntfy://[user:pass@]host[:port]/topic`` — plain HTTP (self-hosted, LAN).
- ``ntfys://[user:pass@]host[:port]/topic`` — HTTPS (the ``s`` suffix means
  "secure", same convention as ``http``/``https``). Use this for the public
  ntfy.sh instance or a self-hosted server behind TLS.
- ``https://ntfy.sh/<topic>`` — recognised as shorthand for
  ``ntfys://ntfy.sh/<topic>`` because ``ntfy.sh`` unambiguously identifies the
  public instance. A self-hosted server given as a plain ``https://host/topic``
  URL is NOT recognised (nothing distinguishes it from an arbitrary HTTPS URL)
  — use the explicit ``ntfy://``/``ntfys://`` scheme for anything self-hosted.
- ``discord://<webhook_id>/<webhook_token>`` or the plain webhook URL
  (``https://discord.com/api/webhooks/<id>/<token>``, ``discordapp.com`` too).
- ``gotify://host[:port]/token`` — plain HTTP; ``gotifys://`` — HTTPS.

Unknown or malformed entries are skipped with one warning (the offending URL
is redacted to its scheme+host — never logged with its topic/token/password).
Sending never raises into the caller; a target that times out or errors is
logged (redacted) and the rest of the list is still tried.
"""

import base64
import json
import logging
import urllib.request
from urllib.parse import quote, unquote, urlsplit, urlunsplit

_log = logging.getLogger("notify")

TIMEOUT_SECONDS = 10
DISCORD_LIMIT = 2000
# ntfy's server-side cap is ~4KB; Gotify has no hard limit but this keeps
# every non-Discord target's body comfortably bounded.
GENERIC_LIMIT = 3900
# Discord sits behind Cloudflare, which rejects urllib's default
# "Python-urllib/x.y" User-Agent (403 / "error code: 1010") — every request,
# to every service, carries this instead.
USER_AGENT = "Aniloads (+https://github.com/DanielC000/aniloads)"


def _redact(url):
    """Scheme+host only — safe to log. Never include path/query (topic,
    webhook id/token) or credentials."""
    try:
        parts = urlsplit(url)
        return "{}://{}/<redacted>".format(parts.scheme or "?", parts.hostname or "?")
    except Exception:
        return "<unparseable-url>"


def _truncate(text, limit):
    """Truncate to at most `limit` characters, appending an ellipsis marker.
    The marker itself is 3 bytes once UTF-8 encoded, so it reserves 3 chars
    of budget (not 1) — keeping the encoded body within `limit` bytes for
    ASCII text, which is what GENERIC_LIMIT/DISCORD_LIMIT are sized for."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "…"


def parse_targets(notify_url):
    """Parse a (possibly comma-separated) NOTIFY_URL value into a list of
    target dicts consumed by send_all(). Never raises — an unrecognised entry
    is skipped with one warning."""
    targets = []
    if not notify_url:
        return targets
    for raw in notify_url.split(","):
        raw = raw.strip()
        if not raw:
            continue
        target = _parse_one(raw)
        if target is None:
            _log.warning("notify: unrecognized NOTIFY_URL entry (%s) — ignoring", _redact(raw))
            continue
        targets.append(target)
    return targets


def _parse_one(raw):
    try:
        parts = urlsplit(raw)
    except Exception:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme in ("ntfy", "ntfys"):
        return _parse_ntfy(parts, secure=(scheme == "ntfys"))
    if scheme == "discord":
        return _parse_discord_scheme(parts)
    if scheme in ("gotify", "gotifys"):
        return _parse_gotify(parts, secure=(scheme == "gotifys"))
    if scheme in ("http", "https"):
        host = (parts.hostname or "").lower()
        if host == "ntfy.sh":
            return _parse_ntfy_plain(parts)
        if host in ("discord.com", "discordapp.com"):
            return _parse_discord_plain(parts)
        return None
    return None


def _parse_ntfy(parts, secure):
    topic = parts.path.lstrip("/")
    if not topic or not parts.hostname:
        return None
    host = parts.hostname
    if parts.port:
        host += ":" + str(parts.port)
    scheme = "https" if secure else "http"
    url = "{}://{}/{}".format(scheme, host, quote(topic))
    target = {"kind": "ntfy", "url": url}
    if parts.username:
        # urlsplit leaves userinfo percent-encoded — a password containing
        # '@', ':', '/' or '%' (which must be %-encoded in the URL) must be
        # decoded before use, or it authenticates with the wrong string.
        target["auth"] = (unquote(parts.username), unquote(parts.password) if parts.password else "")
    return target


def _parse_ntfy_plain(parts):
    topic = parts.path.lstrip("/")
    if not topic:
        return None
    return {"kind": "ntfy", "url": urlunsplit(("https", parts.netloc, "/" + quote(topic), "", ""))}


def _parse_discord_scheme(parts):
    webhook_id = parts.netloc
    token = parts.path.lstrip("/")
    if not webhook_id or not token:
        return None
    url = "https://discord.com/api/webhooks/{}/{}".format(quote(webhook_id), quote(token))
    return {"kind": "discord", "url": url}


def _parse_discord_plain(parts):
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 4 or segments[0] != "api" or segments[1] != "webhooks":
        return None
    webhook_id, token = segments[2], segments[3]
    url = "https://discord.com/api/webhooks/{}/{}".format(quote(webhook_id), quote(token))
    return {"kind": "discord", "url": url}


def _parse_gotify(parts, secure):
    token = parts.path.lstrip("/")
    if not token or not parts.hostname:
        return None
    host = parts.hostname
    if parts.port:
        host += ":" + str(parts.port)
    scheme = "https" if secure else "http"
    return {"kind": "gotify", "url": "{}://{}/message".format(scheme, host), "token": token}


def send_all(targets, title, message):
    """Send `message` (with `title`) to every target. Never raises — a
    per-target failure is logged (redacted) and the rest are still tried."""
    for target in targets:
        try:
            _send_one(target, title, message)
        except Exception as e:
            _log.warning("notify: send failed for %s: %s", _redact(target.get("url", "")), e)


def _send_one(target, title, message):
    kind = target["kind"]
    if kind == "ntfy":
        _send_ntfy(target, title, message)
    elif kind == "discord":
        _send_discord(target, message)
    elif kind == "gotify":
        _send_gotify(target, title, message)


def _build_request(url, data, method="POST"):
    """Build a POST Request with the shared User-Agent set on every outbound
    request, to every service — see USER_AGENT above."""
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    return req


def _urlopen(req):
    urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS).close()


def _send_ntfy(target, title, message):
    body = _truncate(message, GENERIC_LIMIT).encode("utf-8")
    req = _build_request(target["url"], body)
    req.add_header("Title", title)
    auth = target.get("auth")
    if auth:
        creds = base64.b64encode("{}:{}".format(auth[0], auth[1]).encode("utf-8")).decode("ascii")
        req.add_header("Authorization", "Basic " + creds)
    _urlopen(req)


def _send_discord(target, message):
    payload = json.dumps({"content": _truncate(message, DISCORD_LIMIT)}).encode("utf-8")
    req = _build_request(target["url"], payload)
    req.add_header("Content-Type", "application/json")
    _urlopen(req)


def _send_gotify(target, title, message):
    payload = json.dumps({
        "title": title,
        "message": _truncate(message, GENERIC_LIMIT),
        "priority": 5,
    }).encode("utf-8")
    url = target["url"] + "?token=" + quote(target["token"])
    req = _build_request(url, payload)
    req.add_header("Content-Type", "application/json")
    _urlopen(req)
