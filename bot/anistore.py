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

if sys.platform == "win32":
    import msvcrt
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


def _umask():
    """The process umask, read without side effects (there's no getter —
    os.umask() only ever sets-and-returns-previous, so this sets it back
    immediately)."""
    mask = os.umask(0)
    os.umask(mask)
    return mask


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
            os.chmod(tmp_path, 0o666 & ~_umask())
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
    """
    lock_path = str(path) + ".lock"
    d = os.path.dirname(lock_path) or "."
    os.makedirs(d, exist_ok=True)

    if sys.platform == "win32":
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
        return

    # POSIX: open O_RDONLY (flock only needs a valid fd, not write access —
    # a non-owner with just read permission on the lock file can still
    # acquire it). If we're the one creating it, open it world-writable so
    # neither container's user is later locked out, and hand ownership to
    # whoever owns the shared directory when we're root.
    created = not os.path.exists(lock_path)
    fd = os.open(lock_path, os.O_RDONLY | os.O_CREAT, 0o666)
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
