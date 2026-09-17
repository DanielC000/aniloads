"""Tests for bot/anistore.py — the shared atomic load/save/lock primitives
for ani.json used by both the bot and the dashboard."""

import errno
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import support

anistore = support.load_anistore()

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class LoadTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="anistore-tests-")

    def tearDown(self):
        for name in os.listdir(self._dir):
            os.remove(os.path.join(self._dir, name))
        os.rmdir(self._dir)

    def _path(self, name="ani.json"):
        return os.path.join(self._dir, name)

    def test_missing_file_returns_default(self):
        result = anistore.load(self._path("does-not-exist.json"))
        self.assertEqual(result, {"settings": {}, "anime": []})

    def test_missing_file_returns_given_default(self):
        result = anistore.load(self._path("does-not-exist.json"), default={"x": 1})
        self.assertEqual(result, {"x": 1})

    def test_missing_file_default_not_shared_across_calls(self):
        # A shallow dict(default) would still share the nested "anime" list
        # across every missing-file call (or with the module-level
        # DEFAULT_DATA) — one caller appending to it would corrupt the next.
        first = anistore.load(self._path("missing1.json"))
        first["anime"].append("mutated")
        second = anistore.load(self._path("missing2.json"))
        self.assertEqual(second["anime"], [])

    def test_missing_file_given_default_not_shared_across_calls(self):
        default = {"anime": []}
        first = anistore.load(self._path("missing1.json"), default=default)
        first["anime"].append("mutated")
        second = anistore.load(self._path("missing2.json"), default=default)
        self.assertEqual(second["anime"], [])
        self.assertEqual(default["anime"], [])

    def test_corrupt_json_raises_corrupt_store_error(self):
        path = self._path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        with self.assertRaises(anistore.CorruptStoreError):
            anistore.load(path)

    def test_corrupt_encoding_raises_corrupt_store_error(self):
        path = self._path()
        with open(path, "wb") as f:
            f.write(b"\xff\xfe\x00\x01garbage")
        with self.assertRaises(anistore.CorruptStoreError):
            anistore.load(path)

    def test_valid_file_loads(self):
        path = self._path()
        fixture = {"settings": {"a": 1}, "anime": [{"name": "X"}]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(fixture, f)
        self.assertEqual(anistore.load(path), fixture)


class SaveTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="anistore-tests-")
        self._path = os.path.join(self._dir, "ani.json")

    def tearDown(self):
        for name in os.listdir(self._dir):
            os.remove(os.path.join(self._dir, name))
        os.rmdir(self._dir)

    def test_save_then_load_round_trips(self):
        data = {"settings": {"jdhost": "x"}, "anime": [{"name": "A"}]}
        anistore.save(self._path, data)
        self.assertEqual(anistore.load(self._path), data)

    def test_on_disk_format_matches_legacy_json_dump(self):
        # Byte-identical to the pre-anistore save path (plain text-mode
        # `open(...).write(json.dumps(...))`), including this platform's
        # newline translation — not a hardcoded "\n" assumption.
        data = {"b": 2, "a": 1, "anime": [{"name": "Ü-Anime"}]}
        legacy_path = self._path + ".legacy"
        with open(legacy_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=4, sort_keys=True))

        anistore.save(self._path, data)

        with open(self._path, "rb") as f:
            raw = f.read()
        with open(legacy_path, "rb") as f:
            expected = f.read()
        self.assertEqual(raw, expected)

    def test_no_leftover_temp_file_on_success(self):
        anistore.save(self._path, {"anime": []})
        leftovers = [n for n in os.listdir(self._dir) if n != "ani.json"]
        self.assertEqual(leftovers, [])

    def test_failure_mid_write_leaves_original_untouched_and_no_temp_file(self):
        original = {"settings": {}, "anime": [{"name": "Original"}]}
        anistore.save(self._path, original)

        with mock.patch.object(anistore.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                anistore.save(self._path, {"anime": [{"name": "Should not persist"}]})

        self.assertEqual(anistore.load(self._path), original)
        leftovers = [n for n in os.listdir(self._dir) if n != "ani.json"]
        self.assertEqual(leftovers, [])

    def test_failure_mid_write_when_target_never_existed_leaves_no_temp_file(self):
        with mock.patch.object(anistore.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                anistore.save(self._path, {"anime": []})

        self.assertFalse(os.path.exists(self._path))
        self.assertEqual(os.listdir(self._dir), [])

    @unittest.skipUnless(hasattr(os, "chmod") and sys.platform != "win32",
                          "POSIX file mode bits only")
    def test_existing_file_mode_is_preserved_across_save(self):
        # The bot (root, no `user:` in compose) and the dashboard (a
        # configured PUID/PGID) share this file across two containers —
        # tempfile.mkstemp's default 0600 replacing straight over it would
        # silently lock one of those two users out on the very next save.
        anistore.save(self._path, {"anime": []})
        os.chmod(self._path, 0o640)
        anistore.save(self._path, {"anime": [{"name": "A"}]})
        self.assertEqual(stat.S_IMODE(os.stat(self._path).st_mode), 0o640)

    @unittest.skipUnless(hasattr(os, "chmod") and sys.platform != "win32",
                          "POSIX file mode bits only")
    def test_new_file_is_not_left_at_mkstemp_default_mode(self):
        # mkstemp's own default (0600) must not leak through for a brand new
        # file either — that's tighter than a plain open(path, "w") would
        # produce, and just as capable of locking out the other container's
        # user.
        anistore.save(self._path, {"anime": []})
        mode = stat.S_IMODE(os.stat(self._path).st_mode)
        self.assertNotEqual(mode, 0o600)


class LockedTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="anistore-tests-")
        self._path = os.path.join(self._dir, "ani.json")

    def tearDown(self):
        for name in os.listdir(self._dir):
            os.remove(os.path.join(self._dir, name))
        os.rmdir(self._dir)

    def _try_acquire_nonblocking(self, lock_path):
        """True if a *second*, independent handle can grab the exclusive
        lock right now. Used instead of nesting anistore.locked() calls,
        which would deadlock a blocking lock in a single thread."""
        f = open(lock_path, "a+b")
        try:
            if sys.platform == "win32":
                f.seek(0, os.SEEK_END)
                if f.tell() == 0:
                    f.write(b"0")
                    f.flush()
                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return False
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                return True
            else:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return False
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return True
        finally:
            f.close()

    def test_creates_sibling_lock_file(self):
        with anistore.locked(self._path):
            pass
        self.assertTrue(os.path.exists(self._path + ".lock"))

    def test_lock_excludes_concurrent_acquire_and_releases_after(self):
        lock_path = self._path + ".lock"
        with anistore.locked(self._path):
            self.assertFalse(self._try_acquire_nonblocking(lock_path))
        self.assertTrue(self._try_acquire_nonblocking(lock_path))

    @unittest.skipUnless(hasattr(os, "chmod") and sys.platform != "win32",
                          "POSIX permission bits only")
    def test_lock_file_created_world_writable(self):
        # Whichever container's process (root bot, or PUID/PGID dashboard)
        # creates the lock file first must not leave it in a mode that
        # locks the other one out.
        with anistore.locked(self._path):
            pass
        mode = os.stat(self._path + ".lock").st_mode & 0o777
        self.assertEqual(mode, 0o666)

    @unittest.skipUnless(hasattr(os, "chmod") and sys.platform != "win32",
                          "POSIX permission bits only")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                      "root bypasses POSIX permission bits")
    def test_lock_unwritable_by_caller_raises_clear_error_not_silent_fallback(self):
        # anistore.locked() opens O_RDWR, not O_RDONLY: on NFS without
        # local_lock, flock() is emulated with POSIX byte-range locks, and
        # an exclusive lock on a read-only fd raises EBADF there (the
        # outage this module exists to prevent). A lock file the caller
        # can't write to must surface a clear, actionable error instead of
        # silently falling back to a read-only open that would reproduce
        # that same EBADF.
        lock_path = self._path + ".lock"
        with anistore.locked(self._path):
            pass
        os.chmod(lock_path, 0o444)
        try:
            with self.assertRaises(PermissionError) as cm:
                with anistore.locked(self._path):
                    pass
            self.assertIn(lock_path, str(cm.exception))
            self.assertIn("chmod 666", str(cm.exception))
        finally:
            os.chmod(lock_path, 0o666)

    def test_lock_released_even_on_exception(self):
        lock_path = self._path + ".lock"
        with self.assertRaises(ValueError):
            with anistore.locked(self._path):
                raise ValueError("boom")
        self.assertTrue(self._try_acquire_nonblocking(lock_path))


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="anistore-tests-")
        self._path = os.path.join(self._dir, "ani.json")

    def tearDown(self):
        for name in os.listdir(self._dir):
            os.remove(os.path.join(self._dir, name))
        os.rmdir(self._dir)

    def test_update_round_trip_mutates_in_place(self):
        anistore.save(self._path, {"anime": [{"name": "A"}]})

        def add_b(data):
            data["anime"].append({"name": "B"})

        result = anistore.update(self._path, add_b)
        names = [e["name"] for e in result["anime"]]
        self.assertEqual(names, ["A", "B"])
        self.assertEqual([e["name"] for e in anistore.load(self._path)["anime"]], ["A", "B"])

    def test_update_uses_return_value_as_replacement(self):
        anistore.save(self._path, {"anime": [{"name": "A"}]})

        def replace(data):
            return {"anime": [{"name": "REPLACED"}]}

        result = anistore.update(self._path, replace)
        self.assertEqual(result, {"anime": [{"name": "REPLACED"}]})
        self.assertEqual(anistore.load(self._path), {"anime": [{"name": "REPLACED"}]})

    def test_update_on_missing_file_uses_default(self):
        def add_entry(data):
            data["anime"].append({"name": "First"})

        result = anistore.update(self._path, add_entry)
        self.assertEqual(result["anime"], [{"name": "First"}])

    def test_update_propagates_corrupt_store_error_without_saving(self):
        with open(self._path, "w", encoding="utf-8") as f:
            f.write("not json")

        with self.assertRaises(anistore.CorruptStoreError):
            anistore.update(self._path, lambda data: data)

        # File must be untouched — still the same corrupt bytes.
        with open(self._path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "not json")


class SeedIfMissingTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="anistore-tests-")
        self._path = os.path.join(self._dir, "ani.json")

    def tearDown(self):
        for name in os.listdir(self._dir):
            os.remove(os.path.join(self._dir, name))
        os.rmdir(self._dir)

    def test_creates_file_from_default_factory_when_missing(self):
        created = anistore.seed_if_missing(self._path, lambda: {"settings": {"x": 1}, "anime": []})
        self.assertTrue(created)
        self.assertEqual(anistore.load(self._path), {"settings": {"x": 1}, "anime": []})

    def test_returns_false_and_never_overwrites_existing_file(self):
        anistore.save(self._path, {"settings": {"jdhost": "custom"}, "anime": [{"name": "A"}]})

        created = anistore.seed_if_missing(self._path, lambda: {"settings": {}, "anime": []})

        self.assertFalse(created)
        self.assertEqual(
            anistore.load(self._path),
            {"settings": {"jdhost": "custom"}, "anime": [{"name": "A"}]},
        )

    def test_default_factory_not_called_when_file_exists(self):
        anistore.save(self._path, {"settings": {}, "anime": []})
        factory = mock.Mock(side_effect=AssertionError("must not be called"))

        anistore.seed_if_missing(self._path, factory)

        factory.assert_not_called()

    def test_skips_the_lock_entirely_when_file_already_exists(self):
        # load_ani() calls this on every GET; once the file exists (the
        # overwhelming common case) it must not pay for the cross-container
        # flock at all -- only the "still missing" path needs it.
        anistore.save(self._path, {"settings": {}, "anime": []})

        with mock.patch.object(anistore, "locked", side_effect=AssertionError("must not lock")):
            created = anistore.seed_if_missing(self._path, lambda: {"settings": {}, "anime": []})

        self.assertFalse(created)

    def test_still_locks_and_creates_when_file_is_missing(self):
        created = anistore.seed_if_missing(self._path, lambda: {"settings": {"x": 1}, "anime": []})
        self.assertTrue(created)
        self.assertEqual(anistore.load(self._path), {"settings": {"x": 1}, "anime": []})


class MergeEntryTest(unittest.TestCase):
    """Pure tests for anistore.merge_entry — the field-level merge that
    replaces a whole-document save (see bot/anibot.py's save_ani() and
    web/app.py's resolve_pending())."""

    def test_entry_not_found_returns_false(self):
        data = {"anime": [{"url": "https://x/a", "episodes": 1}]}
        found = anistore.merge_entry(data, "anime", "https://x/gone", fields={"episodes": 2})
        self.assertFalse(found)
        # Untouched — no phantom entry created, no other entry mutated.
        self.assertEqual(data["anime"], [{"url": "https://x/a", "episodes": 1}])

    def test_fields_overwrite_only_named_entry(self):
        data = {"anime": [
            {"url": "https://x/a", "episodes": 1, "customPackage": "A Folder"},
            {"url": "https://x/b", "episodes": 5},
        ]}
        found = anistore.merge_entry(data, "anime", "https://x/a",
                                      fields={"episodes": 2, "complete": True})
        self.assertTrue(found)
        self.assertEqual(data["anime"][0]["episodes"], 2)
        self.assertTrue(data["anime"][0]["complete"])
        # User-owned field on the SAME entry, untouched.
        self.assertEqual(data["anime"][0]["customPackage"], "A Folder")
        # Other entry untouched.
        self.assertEqual(data["anime"][1]["episodes"], 5)

    def test_unset_drops_field(self):
        data = {"anime": [{"url": "https://x/a", "al_available_max": 3}]}
        anistore.merge_entry(data, "anime", "https://x/a", unset=["al_available_max"])
        self.assertNotIn("al_available_max", data["anime"][0])

    def test_list_delta_applies_onto_fresh_list_not_replacement(self):
        # Fresh on-disk list already has a dashboard-added episode (4) that
        # the bot never saw — the delta must preserve it.
        data = {"anime": [{"url": "https://x/a", "missing": [2, 3, 4]}]}
        found = anistore.merge_entry(data, "anime", "https://x/a",
                                      list_deltas={"missing": ({6}, {2})})
        self.assertTrue(found)
        self.assertEqual(data["anime"][0]["missing"], [3, 4, 6])

    def test_list_delta_add_and_remove_together(self):
        data = {"anime": [{"url": "https://x/a", "missing": [1, 2]}]}
        anistore.merge_entry(data, "anime", "https://x/a",
                              list_deltas={"missing": ({5}, {1})})
        self.assertEqual(data["anime"][0]["missing"], [2, 5])

    def test_missing_collection_returns_false(self):
        data = {"settings": {}}
        found = anistore.merge_entry(data, "anime", "https://x/a", fields={"episodes": 1})
        self.assertFalse(found)


class MergeEntryFieldsTest(unittest.TestCase):
    """anistore.merge_entry_fields — the write-side wrapper that re-reads
    ani.json fresh under the lock before merging (mirrors real bot/dashboard
    concurrency instead of just exercising the pure merge_entry logic)."""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="anistore-tests-")
        self._path = os.path.join(self._dir, "ani.json")

    def tearDown(self):
        for name in os.listdir(self._dir):
            os.remove(os.path.join(self._dir, name))
        os.rmdir(self._dir)

    def test_concurrent_removal_between_two_bot_saves_is_honored(self):
        anistore.save(self._path, {"anime": [{"url": "https://x/a", "episodes": 0}]})

        found1 = anistore.merge_entry_fields(self._path, "anime", "https://x/a",
                                              fields={"episodes": 1})
        self.assertTrue(found1)

        # Dashboard removes the entry mid-cycle (its own single-lock update).
        data = anistore.load(self._path)
        data["anime"] = [e for e in data["anime"] if e["url"] != "https://x/a"]
        anistore.save(self._path, data)

        # Bot's next save for the same (now-removed) entry must not resurrect it.
        found2 = anistore.merge_entry_fields(self._path, "anime", "https://x/a",
                                              fields={"episodes": 2})
        self.assertFalse(found2)
        self.assertEqual(anistore.load(self._path)["anime"], [])

    def test_concurrent_addition_between_two_bot_saves_survives(self):
        anistore.save(self._path, {"anime": [{"url": "https://x/a", "episodes": 0}]})
        anistore.merge_entry_fields(self._path, "anime", "https://x/a", fields={"episodes": 1})

        # Dashboard adds a brand new entry mid-cycle.
        data = anistore.load(self._path)
        data["anime"].append({"url": "https://x/new", "episodes": 0})
        anistore.save(self._path, data)

        anistore.merge_entry_fields(self._path, "anime", "https://x/a", fields={"episodes": 2})

        urls = {e["url"] for e in anistore.load(self._path)["anime"]}
        self.assertEqual(urls, {"https://x/a", "https://x/new"})

    def test_concurrent_missing_edit_and_bot_download_both_reflected(self):
        anistore.save(self._path, {"anime": [{"url": "https://x/a", "missing": [2, 3]}]})

        # Dashboard adds episode 9 to the retry queue mid-cycle.
        data = anistore.load(self._path)
        data["anime"][0]["missing"] = [2, 3, 9]
        anistore.save(self._path, data)

        # Bot's own save reflects it having just downloaded episode 2.
        anistore.merge_entry_fields(self._path, "anime", "https://x/a",
                                     list_deltas={"missing": (set(), {2})})

        self.assertEqual(anistore.load(self._path)["anime"][0]["missing"], [3, 9])

    def test_concurrent_user_owned_field_edit_not_reverted(self):
        anistore.save(self._path, {"anime": [
            {"url": "https://x/a", "episodes": 0, "customPackage": "Old", "tvdb_id": 1},
        ]})

        # Dashboard edits customPackage/tvdb_id mid-cycle.
        data = anistore.load(self._path)
        data["anime"][0]["customPackage"] = "New Folder"
        data["anime"][0]["tvdb_id"] = 99
        anistore.save(self._path, data)

        # Bot's save only ever names bot-owned fields — never customPackage/tvdb_id.
        anistore.merge_entry_fields(self._path, "anime", "https://x/a", fields={"episodes": 1})

        entry = anistore.load(self._path)["anime"][0]
        self.assertEqual(entry["customPackage"], "New Folder")
        self.assertEqual(entry["tvdb_id"], 99)
        self.assertEqual(entry["episodes"], 1)


class _FakeFcntl:
    """Stands in for the real `fcntl` module so the POSIX branch of
    anistore.locked() can be exercised on any host, including this Windows
    one -- and so its flock() can be made to fail exactly like it does
    against NFS's POSIX-lock emulation.

    `writable_fds=None` (the default) means flock() always succeeds -- this
    is what real NFS actually does for LOCK_EX from two different fds/
    threads in the SAME process: it grants both, because those locks are
    owned per-process, not per-fd. Passing a `set()` instead restricts
    success to fds recorded in it (see `_track_rdwr_fds` below), so a
    read-only-opened fd raises OSError(EBADF) -- the exact failure 75f16d0
    shipped with.
    """

    LOCK_EX = 2
    LOCK_UN = 8

    def __init__(self, writable_fds=None):
        self._writable_fds = writable_fds

    def flock(self, fd, op):
        if (op == self.LOCK_EX and self._writable_fds is not None
                and fd not in self._writable_fds):
            raise OSError(errno.EBADF, "Bad file descriptor")


def _track_rdwr_fds(writable_fds):
    """A real os.open() wrapper that records which fds it opened O_RDWR,
    for `_FakeFcntl` above to consult. Uses the real os.open (and thus real
    fds) so the rest of anistore.locked() -- os.close, os.chmod, etc. --
    keeps working unmodified; only fcntl.flock's NFS-emulation is faked."""
    real_open = os.open

    def opener(path, flags, mode=0o777):
        fd = real_open(path, flags, mode)
        if flags & os.O_RDWR:
            writable_fds.add(fd)
        return fd

    return opener


class NfsLockRegressionTest(unittest.TestCase):
    """Regression coverage for the NFS /config outage fixed by 27485ae3:
    anistore.locked()'s POSIX branch opened O_RDONLY, and an exclusive
    flock() on a read-only fd raises EBADF once flock is emulated with
    POSIX byte-range locks (as it is for NFS without local_lock) -- every
    dashboard request and bot call site then 500s. Every test in this class
    would be RED against 75f16d0 (before the fix)."""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="anistore-nfs-tests-")
        self._path = os.path.join(self._dir, "ani.json")

    def tearDown(self):
        for name in os.listdir(self._dir):
            os.remove(os.path.join(self._dir, name))
        os.rmdir(self._dir)

    def test_posix_branch_opens_with_read_write_flags(self):
        # (a) Would be RED on 75f16d0: the old code opened
        # `os.O_RDONLY | os.O_CREAT`, so this flags check fails against it.
        open_calls = []
        real_open = os.open

        def capturing_open(path, flags, mode=0o777):
            open_calls.append(flags)
            return real_open(path, flags, mode)

        with mock.patch.object(anistore.sys, "platform", "linux"), \
                mock.patch.object(anistore, "fcntl", _FakeFcntl()), \
                mock.patch.object(anistore.os, "open", side_effect=capturing_open):
            with anistore.locked(self._path):
                pass

        self.assertTrue(open_calls)
        for flags in open_calls:
            self.assertTrue(flags & os.O_RDWR, "expected O_RDWR in {!r}".format(flags))

    def test_flock_ebadf_on_readonly_fd_is_avoided_by_rw_open(self):
        # (b) Would be RED on 75f16d0: opening O_RDONLY there means the fd
        # is never in `writable_fds`, so the fake NFS flock() below would
        # raise EBADF for it instead of succeeding.
        writable_fds = set()
        fake_fcntl = _FakeFcntl(writable_fds)

        with mock.patch.object(anistore.sys, "platform", "linux"), \
                mock.patch.object(anistore, "fcntl", fake_fcntl), \
                mock.patch.object(anistore.os, "open",
                                   side_effect=_track_rdwr_fds(writable_fds)):
            with anistore.locked(self._path):
                pass  # must not raise

        # Demonstrate the failure mode this avoids: flock() against a fd
        # that was never opened O_RDWR (as the pre-fix O_RDONLY open would
        # produce) raises EBADF under this same fake NFS emulation. Use a
        # sentinel fd number guaranteed absent from `writable_fds` rather
        # than a fresh real fd -- real fd numbers get reused after close,
        # so a newly-opened fd could collide with one already recorded.
        never_writable_fd = max(writable_fds, default=0) + 1000
        with self.assertRaises(OSError) as cm:
            fake_fcntl.flock(never_writable_fd, fake_fcntl.LOCK_EX)
        self.assertEqual(cm.exception.errno, errno.EBADF)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                      "root bypasses POSIX permission bits")
    def test_write_open_eacces_raises_clear_error_never_silent_fallback(self):
        # (c) Would be RED on 75f16d0: the old code opened O_RDONLY, which
        # would have succeeded here instead of raising -- this test only
        # makes sense once the open is O_RDWR.
        lock_path = self._path + ".lock"
        open(lock_path, "a").close()

        open_calls = []

        def denying_open(path, flags, mode=0o777):
            open_calls.append(flags)
            raise PermissionError(errno.EACCES, "Permission denied")

        with mock.patch.object(anistore.sys, "platform", "linux"), \
                mock.patch.object(anistore, "fcntl", _FakeFcntl()), \
                mock.patch.object(anistore.os, "open", side_effect=denying_open):
            with self.assertRaises(PermissionError) as cm:
                with anistore.locked(self._path):
                    pass

        self.assertEqual(len(open_calls), 1, "must not retry with O_RDONLY")
        self.assertTrue(open_calls[0] & os.O_RDWR)
        self.assertIn(lock_path, str(cm.exception))
        self.assertIn("chmod 666", str(cm.exception))

    def test_concurrent_threads_never_overlap_in_critical_section(self):
        # (d) Would be RED on 75f16d0's design (no process-local
        # serialization at all): with a fake flock that grants LOCK_EX to
        # every fd unconditionally -- emulating NFS's per-PROCESS lock
        # ownership, where two threads in this process can each "acquire"
        # successfully -- only anistore.locked()'s own threading.RLock can
        # still keep two threads out of the critical section at once.
        active = 0
        max_active = 0
        counter_lock = threading.Lock()
        errors = []

        def worker():
            nonlocal active, max_active
            try:
                with anistore.locked(self._path):
                    with counter_lock:
                        active += 1
                        max_active = max(max_active, active)
                    try:
                        time.sleep(0.05)
                    finally:
                        with counter_lock:
                            active -= 1
            except Exception as e:  # pragma: no cover - surfaced via errors
                errors.append(e)

        with mock.patch.object(anistore.sys, "platform", "linux"), \
                mock.patch.object(anistore, "fcntl", _FakeFcntl()):
            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(max_active, 1)

    def test_nested_same_thread_lock_does_not_deadlock_and_opens_one_fd(self):
        # (e) Would hang (deadlock) on a naive fix that reuses a plain
        # (non-reentrant) lock per path without depth tracking; would open
        # a second fd (and, on the old code, drop the outer POSIX lock on
        # close) without the depth guard.
        open_calls = []
        real_open = os.open

        def counting_open(path, flags, mode=0o777):
            open_calls.append(flags)
            return real_open(path, flags, mode)

        with mock.patch.object(anistore.sys, "platform", "linux"), \
                mock.patch.object(anistore, "fcntl", _FakeFcntl()), \
                mock.patch.object(anistore.os, "open", side_effect=counting_open):
            with anistore.locked(self._path):
                with anistore.locked(self._path):
                    pass

        self.assertEqual(len(open_calls), 1)


if __name__ == "__main__":
    unittest.main()
