"""
Shared load/save/lock primitives for ani.json — used by both the bot
(bot/anibot.py) and the dashboard (web/app.py, which runs the same image and
already imports bot modules the same way, e.g. ``from tvdb import ...``).

Both processes read and write ani.json in place on the same host-mounted
``/config`` directory, so a torn read/write from one can be observed by the
other. This module makes that safe: atomic saves (never a half-written
file), a lock shared across both containers, and a load that distinguishes a
missing file (fine, use defaults) from a corrupt one (never silently treated
as "no watchlist yet" — that's what let a torn read wipe a real watchlist).

Stdlib only.
"""

import contextlib
import copy
import json
import os
import stat
import sys
import tempfile
import threading

if sys.platform == "win32":
    import msvcrt
    fcntl = None
else:
    import fcntl

DEFAULT_DATA = {"settings": {}, "anime": []}


class CorruptStoreError(Exception):
    """The file exists but could not be parsed as JSON/UTF-8.

    Callers must not treat this like a missing file — doing so is the exact
    bug this module fixes: a reader that swallows this and returns the empty
    default, followed by a save, wipes the real data on disk.
    """

    def __init__(self, path, cause):
        super().__init__("{} is corrupt: {}".format(path, cause))
        self.path = path
        self.cause = cause


def load(path, default=None):
    """Read and parse the JSON file at `path`.

    Missing file -> a fresh deep copy of `default` (or of DEFAULT_DATA when
    no default is given) — never a shared reference, so one caller mutating
    the returned dict (e.g. appending to its "anime" list) can't leak into
    another call's result. Corrupt file (bad JSON or bad encoding — a
    UnicodeDecodeError is a ValueError subclass, so this covers both) ->
    raises CorruptStoreError.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return copy.deepcopy(default) if default is not None else copy.deepcopy(DEFAULT_DATA)
    except ValueError as e:
        raise CorruptStoreError(path, e) from e


def _read_umask():
    """The process umask, read without lasting side effects (there's no
    getter — os.umask() only ever sets-and-returns-previous, so this sets it
    back immediately).

    Not safe to call once the process is multi-threaded: the brief window
    between the set and the reset is a real (if momentary) process-wide
    umask of 0, and a concurrent save on another thread creating a new file
    in that window would get world-writable permissions. Call this once at
    import time instead (see _PROCESS_UMASK below) and read the cached value
    from then on.
    """
    mask = os.umask(0)
    os.umask(mask)
    return mask


# Read once at import time, while the process is still single-threaded — see
# _read_umask's docstring for why this can't be done safely per-call once the
# dashboard's threaded server is up.
_PROCESS_UMASK = _read_umask()


def _match_permissions(tmp_path, target_path):
    """Make `tmp_path` end up with the owner/mode `target_path` should have,
    before it gets `os.replace`d over it.

    The bot container runs as root; the dashboard runs as a configured
    PUID/PGID, and both bind-mount the same host `/config` directory.
    `tempfile.mkstemp` creates its file mode 0600, owned by whoever is
    writing — replacing straight over the target with that would silently
    flip ani.json's permissions/ownership on every save, locking the OTHER
    container's user out with PermissionError (not CorruptStoreError) on its
    very next read or write.
    """
    try:
        st = os.stat(target_path)
    except OSError:
        # No existing file yet — give it what a plain open(path, "w") would
        # have produced (0666 minus umask), not mkstemp's restrictive 0600.
        try:
            os.chmod(tmp_path, 0o666 & ~_PROCESS_UMASK)
        except OSError:
            pass
        return
    try:
        os.chmod(tmp_path, stat.S_IMODE(st.st_mode))
    except OSError:
        pass
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            os.chown(tmp_path, st.st_uid, st.st_gid)
        except OSError:
            pass


def save(path, data):
    """Write `data` to `path` atomically.

    Serializes to a temp file in the same directory, flushes + fsyncs it,
    matches the target's existing owner/mode (see `_match_permissions`),
    then `os.replace`s it over the target — a reader can never observe a
    partially-written file, and a failure mid-write leaves the original file
    untouched and no temp file behind. Same on-disk format this project has
    always used: ``indent=4, sort_keys=True``, UTF-8.
    """
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".ani-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=4, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        _match_permissions(tmp_path, path)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


# Module-level per-lock-path threading.RLock registry, guarded by its own
# lock. The homelab's /config is NFS without local_lock, so flock()/fcntl
# locks are emulated with POSIX byte-range locks there -- and those are
# owned per PROCESS, not per fd: two threads in this same process can each
# "acquire" LOCK_EX at once, and closing ANY fd for the file drops every one
# of this process's locks on it. The dashboard is multithreaded
# (ThreadingHTTPServer request threads, the resolver thread, the mover
# thread), so the OS-level lock alone does not serialize concurrent
# `update_ani` calls within this process on NFS -- this RLock is what does.
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS = {}

# Per-thread reentrancy depth, keyed by lock_path. A thread already holding
# `locked(path)` that calls it again (directly or via a helper) must not
# open a second fd or flock/close again -- an inner close would drop the
# outer POSIX lock on NFS. Only the outermost call for a given thread+path
# actually opens/locks/closes; a nested call just extends the RLock hold.
_THREAD_LOCAL = threading.local()


def _process_lock_for(lock_path):
    with _PATH_LOCKS_GUARD:
        rlock = _PATH_LOCKS.get(lock_path)
        if rlock is None:
            rlock = threading.RLock()
            _PATH_LOCKS[lock_path] = rlock
        return rlock


def _thread_lock_depths():
    depths = getattr(_THREAD_LOCAL, "depths", None)
    if depths is None:
        depths = {}
        _THREAD_LOCAL.depths = depths
    return depths


def _describe_lock_file(lock_path):
    try:
        st = os.stat(lock_path)
    except OSError:
        return "unknown owner/mode (could not stat it)"
    return "owned by uid {}, mode {:o}".format(st.st_uid, stat.S_IMODE(st.st_mode))


@contextlib.contextmanager
def _open_and_flock_win32(lock_path):
    f = open(lock_path, "a+b")
    try:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        f.close()


@contextlib.contextmanager
def _open_and_flock_posix(lock_path, d):
    """Open the lock file read-write and hold an exclusive flock on it.

    Opened O_RDWR, never O_RDONLY: on NFS without local_lock, flock() is
    emulated with POSIX byte-range locks, and taking an exclusive lock on a
    read-only fd raises EBADF there -- every caller then fails the same way.
    If we're the one creating the file, open it world-writable so neither
    container's user is later locked out, and hand ownership to whoever
    owns the shared directory when we're root.

    If the write-open itself fails with EACCES/EPERM (PermissionError), we
    do NOT fall back to O_RDONLY -- that would just reproduce the same EBADF
    on NFS. Instead this raises a clear, actionable error naming the lock
    path and how to fix it.
    """
    created = not os.path.exists(lock_path)
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
    except PermissionError as e:
        raise PermissionError(
            "cannot open {} read-write ({}); currently {} -- make {} "
            "writable by both containers: chmod 666 {}".format(
                lock_path, e.strerror or e, _describe_lock_file(lock_path),
                lock_path, lock_path,
            )
        ) from e
    try:
        if created:
            try:
                os.chmod(lock_path, 0o666)
            except OSError:
                pass
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                try:
                    dir_st = os.stat(d)
                    os.chown(lock_path, dir_st.st_uid, dir_st.st_gid)
                except OSError:
                    pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def locked(path):
    """Advisory exclusive lock on a sibling ``<path>.lock`` file, held for
    the duration of the ``with`` block.

    The bot and dashboard containers bind-mount the same host directory, so
    this lock (held on a real file in that directory) is visible across both
    processes, not just within one of them — and one of those processes runs
    as root, the other as a configured PUID/PGID, so whichever one creates
    the lock file first must not leave it in a mode the other can't even
    open. ``fcntl.flock`` on POSIX; ``msvcrt.locking`` on Windows (the
    platform this test suite runs on), locking a single byte of the lock
    file.

    Also holds a module-level, per-lock-path ``threading.RLock`` for the
    duration of the block, on both POSIX and Windows -- see
    ``_PATH_LOCKS``'s comment above for why the OS-level lock alone isn't
    enough once a single process (the multithreaded dashboard) can have more
    than one thread in here at once. Reentrant-safe: a thread already
    holding the lock for this path that calls ``locked(path)`` again just
    extends the hold instead of opening a second fd.
    """
    lock_path = str(path) + ".lock"
    d = os.path.dirname(lock_path) or "."
    os.makedirs(d, exist_ok=True)

    process_lock = _process_lock_for(lock_path)
    process_lock.acquire()
    depths = _thread_lock_depths()
    depth = depths.get(lock_path, 0)
    depths[lock_path] = depth + 1
    try:
        if depth == 0:
            if sys.platform == "win32":
                with _open_and_flock_win32(lock_path):
                    yield
            else:
                with _open_and_flock_posix(lock_path, d):
                    yield
        else:
            yield
    finally:
        remaining = depths[lock_path] - 1
        if remaining:
            depths[lock_path] = remaining
        else:
            del depths[lock_path]
        process_lock.release()


def seed_if_missing(path, default_factory):
    """Create ``path`` with ``default_factory()`` if and only if it doesn't
    already exist -- never overwrites a real config.

    Callers (e.g. the dashboard's load_ani(), called on every GET) hit this
    on every read once the file exists, which is nearly always -- so check
    for existence WITHOUT the cross-container lock first and return early;
    only the rare "still missing" path pays for taking it. The check is
    re-done inside the lock (see below) so a concurrent creator racing this
    same fast path can't still cause two writers to step on each other.

    Runs under the same lock as every other read/write here, so a race
    between the bot and the dashboard both seeding at startup can't produce
    two writers stepping on each other. Returns True if it created the file,
    False if one was already there.
    """
    if os.path.exists(path):
        return False
    with locked(path):
        if os.path.exists(path):
            return False
        save(path, default_factory())
        return True


def merge_entry(data, collection, url, fields=None, unset=None, list_deltas=None):
    """Apply a field-level merge to ONE entry inside ``data[collection]``,
    matched by ``url``, in place.

    This is the pure half of the fix for the bug where the bot (and the
    dashboard's ``resolve_pending``) loaded ani.json once at the start of a
    multi-minute cycle and then saved that whole stale snapshot back several
    times, silently reverting any edit made through the other side in the
    meantime. Callers must load ``data`` FRESH under the lock right before
    calling this (see ``merge_entry_fields`` below, which does exactly that),
    then pass only the handful of fields *this step* actually changed.

    Ownership contract (enforced by the caller, not this function):
      - ``fields``: {field: value} — plain overwrites. Only ever pass fields
        the caller exclusively owns (e.g. the bot's own scrape/download
        results); a field the OTHER side can also edit (e.g. the dashboard's
        user-editable fields) must never appear here, or a stale value would
        clobber a concurrent edit.
      - ``unset``: field names to drop entirely (``dict.pop``) — for a field
        the caller is retracting (e.g. clearing a stale cache value).
      - ``list_deltas``: {field: (added, removed)} — for a list field BOTH
        sides can mutate (e.g. ``missing``: the dashboard adds/skips
        episodes, the bot removes downloaded ones and adds failed ones).
        The caller's added/removed sets are applied onto the FRESH on-disk
        list, never used to replace it wholesale, so a concurrent edit to
        the same list from the other side survives.

    Returns True if an entry with this ``url`` was found (and merged), False
    if it was not (e.g. removed by the other side mid-cycle) — callers must
    treat False as "stop processing this entry", never re-create it here.
    """
    items = data.get(collection)
    if not isinstance(items, list):
        return False
    for entry in items:
        if entry.get("url") != url:
            continue
        if fields:
            entry.update(fields)
        if unset:
            for f in unset:
                entry.pop(f, None)
        if list_deltas:
            for f, (added, removed) in list_deltas.items():
                merged = [v for v in entry.get(f, []) if v not in removed]
                for v in added:
                    if v not in merged:
                        merged.append(v)
                merged.sort()
                entry[f] = merged
        return True
    return False


def merge_entry_fields(path, collection, url, fields=None, unset=None, list_deltas=None, default=None):
    """Persist a field-level merge for one entry, under the lock shared with
    the other process — the write-side counterpart to ``merge_entry``.

    Re-reads ani.json fresh under the lock (via ``update``), applies the
    merge to that fresh copy, and saves it — so a concurrent edit from the
    other process (made after the caller's own stale in-memory copy was
    loaded) is preserved instead of being overwritten by it. See
    ``merge_entry``'s docstring for the field-ownership contract callers
    must follow.

    Returns True/False exactly as ``merge_entry`` does.
    """
    result = {}

    def _apply(data):
        result["found"] = merge_entry(data, collection, url, fields=fields,
                                       unset=unset, list_deltas=list_deltas)

    update(path, _apply, default=default)
    return result["found"]


def update(path, fn, default=None):
    """Load-modify-save under the lock.

    Acquires the lock, loads a fresh copy of the data, calls ``fn(data)`` to
    mutate it (in place, or by returning a replacement value), saves the
    result, and returns it. This is the primitive a real read-modify-write
    (e.g. the bot's per-cycle field-level merge with the dashboard's edits)
    should build on, instead of composing bare load()/save() calls — those
    two calls alone leave a window where another process's write in between
    gets silently clobbered by the stale in-memory copy.
    """
    with locked(path):
        data = load(path, default=default)
        result = fn(data)
        data = data if result is None else result
        save(path, data)
        return data
