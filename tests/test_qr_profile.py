"""Dedicated profile cleanup tests; only synthetic temporary files are used."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from core import qr_profile as profiles


class QrProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qr-profile-test-")
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name) / "data"
        self.root = self.data / profiles.PROFILE_NAME

    def write(self, relative, content=b"synthetic"):
        path = self.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def link(self, target, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(target, destination, target_is_directory=Path(target).is_dir())
        except (OSError, NotImplementedError) as error:
            self.skipTest("Host does not permit creating test symlinks: " + type(error).__name__)

    def test_creates_only_dedicated_profile_and_is_idempotent(self):
        root = profiles.prepare_profile(self.data)
        self.assertEqual(root, self.root.absolute())
        self.assertTrue(root.is_dir())
        self.assertEqual(profiles.prepare_profile(self.data), root)
        self.assertEqual([p.name for p in self.data.iterdir()], [profiles.PROFILE_NAME])

    def test_removes_cookie_databases_and_companions_but_retains_cache(self):
        removed = [self.write(profiles.PROFILE_NAME + "/" + name) for name in (
            "Default/Cookies", "Default/Cookies-wal", "Default/Cookies-journal",
            "Default/Network/Cookies", "Default/Network/Cookies-shm", "Default/Network/Trust Tokens-journal",
            "Default/Local Storage/leveldb/000001.log", "Default/Session Storage/session",
            "Default/Service Worker/CacheStorage/private", "Default/IndexedDB/private",
            "Default/Login Data", "Default/Web Data-wal", "Default/Account Web Data",
        )]
        kept = [self.write(profiles.PROFILE_NAME + "/" + name, b"keep-cache") for name in (
            "Default/Cache/Cache_Data/data_0", "Default/Code Cache/js/cache",
            "Default/GPUCache/data_0", "ShaderCache/cache", "Local State",
        )]
        profiles.prepare_profile(self.data)
        self.assertTrue(all(not path.exists() for path in removed))
        self.assertTrue(all(path.read_bytes() == b"keep-cache" for path in kept))

    def test_clears_all_listed_personal_storage_directories(self):
        names = ("Sessions", "WebStorage", "SharedStorage", "File System", "Storage", "databases", "blob_storage")
        paths = [self.write(profiles.PROFILE_NAME + "/Default/" + name + "/nested/item") for name in names]
        profiles.clear_profile_login_data(self.root)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertTrue(self.root.is_dir())
        self.assertTrue((self.root / "Default").is_dir())

    def test_does_not_touch_state_or_other_data_directories(self):
        state = self.write("state.json", b"original-credential-sentinel")
        elsewhere = self.write("some-other-profile/Default/Cookies", b"other-profile")
        self.write(profiles.PROFILE_NAME + "/Default/Cookies")
        profiles.prepare_profile(self.data)
        self.assertEqual(state.read_bytes(), b"original-credential-sentinel")
        self.assertEqual(elsewhere.read_bytes(), b"other-profile")

    def test_rejects_other_directory_names_and_never_deletes_data_root(self):
        state = self.write("state.json", b"keep")
        with self.assertRaises(profiles.QrProfileError):
            profiles.clear_profile_login_data(self.data)
        with self.assertRaises(profiles.QrProfileError):
            profiles.clear_profile_login_data(self.data / "Default")
        self.assertEqual(state.read_bytes(), b"keep")

    def test_missing_profile_clear_is_safe(self):
        profiles.clear_profile_login_data(self.root)
        self.assertFalse(self.root.exists())

    def test_file_at_profile_path_is_rejected(self):
        target = self.write(profiles.PROFILE_NAME, b"keep-file")
        with self.assertRaises(profiles.QrProfileError):
            profiles.prepare_profile(self.data)
        self.assertEqual(target.read_bytes(), b"keep-file")

    def test_delete_failure_raises_instead_of_reusing_dirty_profile(self):
        cookie = self.write(profiles.PROFILE_NAME + "/Default/Cookies", b"keep-until-success")
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("simulated locked cookie")):
            with self.assertRaises(profiles.QrProfileError):
                profiles.prepare_profile(self.data)
        self.assertEqual(cookie.read_bytes(), b"keep-until-success")

    def test_recursive_directory_removal_failure_is_not_ignored(self):
        folder = self.root / "Default" / "Local Storage"
        folder.mkdir(parents=True)
        with mock.patch.object(Path, "rmdir", side_effect=PermissionError("simulated locked directory")):
            with self.assertRaises(profiles.QrProfileError):
                profiles.clear_profile_login_data(self.root)
        self.assertTrue(folder.exists())

    def test_symlink_profile_to_another_directory_is_rejected(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        marker = outside / "keep"; marker.write_bytes(b"outside")
        self.link(outside, self.root)
        with self.assertRaises(profiles.QrProfileError):
            profiles.prepare_profile(self.data)
        self.assertEqual(marker.read_bytes(), b"outside")

    def test_link_inside_login_storage_is_rejected_before_any_cleanup(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        marker = outside / "keep"; marker.write_bytes(b"outside")
        cookie = self.write(profiles.PROFILE_NAME + "/Default/Cookies", b"cookie")
        self.link(outside, self.root / "Default" / "Local Storage" / "linked")
        with self.assertRaises(profiles.QrProfileError):
            profiles.clear_profile_login_data(self.root)
        self.assertEqual(marker.read_bytes(), b"outside")
        self.assertEqual(cookie.read_bytes(), b"cookie")

    def test_reparse_junction_attribute_is_rejected_without_following_it(self):
        self.write(profiles.PROFILE_NAME + "/Default/Network/Cookies", b"keep")
        junction = self.root / "Default" / "Network"
        original = profiles._lstat
        def metadata(path):
            if Path(path) == junction:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original(path)
        with mock.patch.object(profiles, "_lstat", side_effect=metadata):
            with self.assertRaises(profiles.QrProfileError):
                profiles.clear_profile_login_data(self.root)
        self.assertEqual((junction / "Cookies").read_bytes(), b"keep")

    def test_target_escape_and_whole_profile_deletion_are_rejected(self):
        profiles.prepare_profile(self.data)
        outside = self.write("state.json", b"keep")
        with self.assertRaises(profiles.QrProfileError):
            profiles._remove_checked(self.root, outside)
        with self.assertRaises(profiles.QrProfileError):
            profiles._remove_checked(self.root, self.root)
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_target_is_rechecked_if_it_changes_after_preflight(self):
        cookie = self.write(profiles.PROFILE_NAME + "/Default/Cookies", b"keep")
        original_walk = profiles._walk_checked
        original_stat = profiles._lstat
        changed = False
        def walk(root, target):
            nonlocal changed
            original_walk(root, target)
            if target == cookie:
                changed = True
        def metadata(target):
            if changed and Path(target) == cookie:
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
            return original_stat(target)
        with mock.patch.object(profiles, "_walk_checked", side_effect=walk), mock.patch.object(profiles, "_lstat", side_effect=metadata):
            with self.assertRaises(profiles.QrProfileError):
                profiles.clear_profile_login_data(self.root)
        self.assertEqual(cookie.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
