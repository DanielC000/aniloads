"""Tests for bot/anistore.py — the shared atomic load/save/lock primitives
for ani.json used by both the bot and the dashboard."""

import json
import os
import stat
import sys
import tempfile
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
    def test_lock_acquirable_when_lock_file_not_writable_by_caller(self):
        # flock only needs a valid fd, not write access — anistore.locked()
        # opens O_RDONLY specifically so a caller with only read permission
        # on the lock file (e.g. the PUID dashboard user against a lock file
        # the root bot created and left non-writable) can still acquire it.
        lock_path = self._path + ".lock"
        with anistore.locked(self._path):
            pass
        os.chmod(lock_path, 0o444)
        with anistore.locked(self._path):
            pass

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


if __name__ == "__main__":
    unittest.main()
