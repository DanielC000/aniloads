# Docker-unavailable is not the same signal as "container stopped"

`web/app.py` — `render_activity()`'s container-status rendering.

The container status comes only from Docker — when the socket isn't reachable, the dashboard
genuinely doesn't know whether the bot is running, so a red "Stopped" here would be a false
signal. The run_state-derived Last/Next Run display stays authoritative regardless of Docker
availability.

## Do not

Don't render a red "Stopped" status purely because the Docker socket is unreachable — that's
"unknown", not "stopped". Keep Last/Next Run sourced from `run_state.json` so it stays correct
even when Docker itself can't be asked.
