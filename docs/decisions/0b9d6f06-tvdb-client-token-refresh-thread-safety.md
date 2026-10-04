# TVDBClient's token lock is shared across dashboard request threads

`bot/tvdb.py` — `TVDBClient.__init__`'s token lock.

This client is a single module-level instance (see `web/app.py`'s `tvdb = ...`), shared
across the dashboard's request-handling threads. Without the lock, two threads racing
`_ensure_token` when the cached token is expired would both see it as invalid and both fire a
redundant login.

## Do not

Don't drop the lock around `_ensure_token`'s refresh check — without it, two request threads
racing an expired token both see it as invalid and both fire a redundant TVDB login.
