# `log()`'s unused `pushbullet` parameter

`bot/anibot.py` — `log()`.

Pushbullet no longer gets a push per call (that pushed every attempt, not just outcomes) —
it now receives the same one-per-cycle summary as the other notify targets, sent from
`_notify_cycle()`. `pushbullet` is kept as a parameter for call-site compatibility but is
unused here.

## Do not

Don't reintroduce a push-per-call from inside `log()` — every call site already expects one
cycle-summary notification from `_notify_cycle()`, and pushing again here would duplicate
it. The unused `pushbullet` parameter stays only so existing call sites don't need updating.
