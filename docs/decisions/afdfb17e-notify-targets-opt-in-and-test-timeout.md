# Notifications: opt-in reads as OK, and a test send can't hang the dashboard

`web/app.py` — the notification-targets Health row and the `/notify-test` POST handler.

## Opt-in absence is "ok", not "unknown"

Notifications are opt-in — leaving `NOTIFY_URL` unset is the common case, not a problem. The
row renders "ok" (rather than "unknown") so it never trips `render_health_card`'s "No problems
found · N unknown" wording, and a reader who never configured notifications still sees a plain
"All systems OK".

## A hung target can't hang the dashboard

`/notify-test`'s response-hold is bounded — a slow or unreachable notification target must not
be able to hang the dashboard (see `send_all`'s own per-target try/except; this bound is a
second, coarser backstop around the whole batch, since a hung `urlopen` still blocks its own
thread).

## Do not

Don't render an unconfigured `NOTIFY_URL` as "unknown" — notifications are opt-in, so absence
is "ok". Don't remove the bound around `/notify-test`'s batch send — a single unreachable
target's hung `urlopen` must not be able to hang the whole dashboard request.
