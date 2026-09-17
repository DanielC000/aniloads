"""Container healthcheck for the anime-loads bot.

Deliberately standalone -- it reads run_state.json directly instead of
importing anibot.py, so the compose HEALTHCHECK (`python3 healthcheck.py`)
never pays anibot's Selenium/opencv import cost and can't be broken by
whatever the bot loop happens to be doing.

Healthy:
- run_state.json is missing or unreadable (a fresh container with nothing
  written yet -- the compose healthcheck's own `start_period` is what keeps
  this window from flapping the container unhealthy, not this check).
- a `waiting_for_config` marker is present (bot/anibot.py's
  write_waiting_for_config, written while startbot()'s boot backoff loop
  waits on a download backend/port): the bot is up and waiting on the owner
  to fix Settings, not wedged.
- the last completed cycle (`last_run.finished_ts`) is younger than
  max(3 * last_run.timedelay, 30 minutes).

Unhealthy only when run_state.json exists, carries no waiting_for_config
marker, and its last completed cycle is older than that threshold -- the
bot loop is very likely stuck.
"""
import json
import os
import sys
from datetime import datetime, timezone

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
RUN_STATE_PATH = os.path.join(CONFIG_DIR, "run_state.json")

MIN_MAX_AGE_SECONDS = 30 * 60
STALE_MULTIPLIER = 3


def _parse_ts(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def check_health(run_state_path=None, now=None):
    """Return (healthy: bool, reason: str) for the bot container."""
    path = run_state_path or RUN_STATE_PATH
    now = now or datetime.now(timezone.utc)

    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return True, "run_state.json missing or unreadable (starting)"

    if not isinstance(state, dict):
        return True, "run_state.json malformed (starting)"

    if isinstance(state.get("waiting_for_config"), dict):
        return True, "waiting for configuration (boot backoff)"

    last = state.get("last_run")
    if not isinstance(last, dict):
        return True, "no completed cycle yet (starting)"

    finished = _parse_ts(last.get("finished_ts"))
    if finished is None:
        return True, "last_run.finished_ts missing or unparsable (starting)"

    timedelay = last.get("timedelay")
    if isinstance(timedelay, int) and not isinstance(timedelay, bool) and timedelay > 0:
        max_age = max(timedelay * STALE_MULTIPLIER, MIN_MAX_AGE_SECONDS)
    else:
        max_age = MIN_MAX_AGE_SECONDS

    age = (now - finished).total_seconds()
    if age <= max_age:
        return True, "last cycle finished {:.0f}s ago (within {:.0f}s)".format(age, max_age)
    return False, "last cycle finished {:.0f}s ago, exceeds {:.0f}s threshold".format(age, max_age)


if __name__ == "__main__":
    ok, reason = check_health()
    print(reason)
    sys.exit(0 if ok else 1)
