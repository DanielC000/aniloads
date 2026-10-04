# Explicit CSRF allowlist for a reverse-proxy mismatch

`web/app.py` — `DASHBOARD_ALLOWED_ORIGINS` / `_normalize_origin()`, consulted by
`Handler._check_csrf`.

A full `"scheme://host:port"` origins allowlist, comma-separated via
`DASHBOARD_ALLOWED_ORIGINS`, exists for a reverse-proxy setup where neither the request's own
`Host` header nor `X-Forwarded-Host` lines up with the browser's `Origin`/`Referer` — the
normal same-origin check (comparing the request's own apparent host against the posted
Origin) can't be trusted in that topology, so an explicit allowlist is the escape hatch.

## Do not

Don't drop `DASHBOARD_ALLOWED_ORIGINS` in favor of inferring the dashboard's origin from the
request alone — behind a reverse proxy, neither `Host` nor `X-Forwarded-Host` is guaranteed to
match the browser's real Origin/Referer, so an explicit allowlist is the only reliable check.
