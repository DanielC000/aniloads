# "Waiting for configuration" must show even when the Docker socket is unreachable

`web/app.py` — `render_activity()`'s container-status rendering.

`waiting_for_config` comes from `run_state.json`, not Docker, so it's still the most useful
fact even when the socket is unreachable and container status is otherwise unknown. It's
presented the same way as Health's Download Backend row (`check_download_backend_health`):
reason text plus a Settings link.

## Do not

Don't gate the "Waiting for configuration" message on Docker being reachable — it's read from
`run_state.json`, independent of Docker, and is the single most useful fact to show when a
fresh install's download backend isn't configured yet, regardless of socket availability.
