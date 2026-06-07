"""
TVDB API v4 client — shared between bot (anibot.py) and web dashboard (app.py).
"""

import logging
import os
import time

_log = logging.getLogger("tvdb")

TVDB_API_KEY = os.environ.get("TVDB_API_KEY", "")
TVDB_BASE = "https://api4.thetvdb.com/v4"
TVDB_HTTP_TIMEOUT = int(os.environ.get("TVDB_HTTP_TIMEOUT", "10"))


def _strip_tvdb_prefix(raw_id):
    """TVDB /search returns ids like 'series-12345' or 'movie-678' — strip the
    prefix so downstream code can store a plain int. Returns the raw value if
    it doesn't match the expected pattern."""
    if isinstance(raw_id, str) and "-" in raw_id:
        tail = raw_id.rsplit("-", 1)[-1]
        if tail.isdigit():
            return int(tail)
    if isinstance(raw_id, int):
        return raw_id
    try:
        return int(raw_id)
    except (ValueError, TypeError):
        return raw_id


class TVDBClient:
    def __init__(self, api_key=TVDB_API_KEY):
        self.api_key = api_key
        self.available = bool(api_key)
        self._token = None
        self._token_expiry = 0

    def _login(self):
        if not self.api_key:
            return False
        try:
            import requests
            resp = requests.post(
                TVDB_BASE + "/login",
                json={"apikey": self.api_key},
                timeout=TVDB_HTTP_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                self._token = data.get("token")
                # Tokens last ~30 days; refresh after 29
                self._token_expiry = time.time() + 29 * 86400
                return True
        except Exception as e:
            _log.warning("TVDB login error: %s", e)
        return False

    def _ensure_token(self):
        if not self.available:
            return False
        if self._token and time.time() < self._token_expiry:
            return True
        return self._login()

    def _get(self, path):
        if not self._ensure_token():
            return None
        import requests
        try:
            resp = requests.get(
                TVDB_BASE + path,
                headers={"Authorization": "Bearer " + self._token},
                timeout=TVDB_HTTP_TIMEOUT,
            )
            if resp.status_code == 401:
                # Token expired, retry once
                if self._login():
                    resp = requests.get(
                        TVDB_BASE + path,
                        headers={"Authorization": "Bearer " + self._token},
                        timeout=TVDB_HTTP_TIMEOUT,
                    )
            if resp.status_code == 200:
                return resp.json().get("data")
        except Exception as e:
            _log.warning("TVDB request error: %s", e)
        return None

    def search(self, query, content_type="series"):
        """Search for series or movies by name. Returns list of dicts.

        content_type is either "series" (default) or "movie" — maps to TVDB v4's
        /search?type= filter. Movie results use a different id prefix on TVDB
        ("movie-<n>") — we strip that so the stored id is a plain int where
        possible and fall back to the raw value otherwise.
        """
        from urllib.parse import quote
        data = self._get("/search?query={}&type={}".format(quote(query), content_type))
        if not data:
            return []
        results = []
        for item in data:
            raw_id = item.get("tvdb_id") or item.get("id", "")
            results.append({
                "tvdb_id": _strip_tvdb_prefix(raw_id),
                "name": item.get("name", ""),
                "year": item.get("year", ""),
                "overview": (item.get("overview") or "")[:200],
                "image_url": item.get("image_url") or item.get("thumbnail", ""),
            })
        return results

    def get_seasons(self, tvdb_id):
        """Get aired-order seasons for a series. Returns list of dicts."""
        data = self._get("/series/{}/extended".format(tvdb_id))
        if not data:
            return []
        seasons = []
        for s in data.get("seasons", []):
            stype = s.get("type", {})
            # Aired Order = type id 1; skip specials (season 0)
            if stype.get("id") != 1:
                continue
            num = s.get("number", 0)
            if num == 0:
                continue
            ep_count = 0
            last_ep = s.get("lastEpisodeNumber")
            if last_ep is not None:
                ep_count = last_ep
            seasons.append({
                "season_number": num,
                "episode_count": ep_count,
                "tvdb_season_id": s.get("id"),
            })
        seasons.sort(key=lambda x: x["season_number"])
        return seasons

    def get_series_status(self, tvdb_id):
        """Returns status string like 'Ended', 'Continuing', or ''."""
        data = self._get("/series/{}".format(tvdb_id))
        if data:
            status = data.get("status", {})
            return status.get("name", "") if isinstance(status, dict) else str(status)
        return ""

    def get_season_episode_count(self, tvdb_id, season_number):
        """Returns total episode count for a specific season, or None."""
        seasons = self.get_seasons(tvdb_id)
        for s in seasons:
            if s["season_number"] == season_number:
                return s["episode_count"]
        return None

    def get_next_episode_airdate(self, tvdb_id, season_number, after_episode):
        """Returns ISO date string of next episode after after_episode, or None."""
        data = self._get("/series/{}/episodes/default?season={}&page=0".format(
            tvdb_id, season_number))
        if not data:
            return None
        episodes = data.get("episodes", [])
        for ep in sorted(episodes, key=lambda e: e.get("number", 0)):
            if ep.get("number", 0) > after_episode and ep.get("aired"):
                return ep["aired"]
        return None
