"""R01 regression tests; all generated inputs are synthetic and stay in dist."""
from __future__ import annotations

import builtins
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

from tools import make_release as release

PROJECT = Path(__file__).resolve().parents[1]
TEST_TMP = PROJECT / "dist" / "test-tmp"


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="release-", dir=TEST_TMP)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.put("app.py", "print('synthetic source')\n")
        self.put("app_common.py", "VERSION = '2.8'\n")
        self.put(".env.example", "LOGIN_GATE=on\n")
        self.put("README.md", "synthetic instructions\n")
        self.put("static/index.html", "<!doctype html><title>Fixture</title>")
        self.put("core/example.py", "VALUE = 1\n")
        self.put("tests/test_example.py", "# synthetic test\n")
        self.put("tools/helper.py", "# synthetic helper\n")

    def put(self, relative, content):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def build(self):
        return release.build_release(self.source, name="clean-fixture")

    def test_private_worktree_files_excluded_before_open_or_descent(self):
        forbidden = [".env", ".env.local", "data/x", "data/state.json",
                     "venv/pyvenv.cfg", "venv.invalid-1/pyvenv.cfg", ".git/config",
                     ".review-work/result.txt", "__pycache__/app.pyc",
                     "server.out.log", "old.zip", "dist/old/app.py",
                     "unlisted-browser-profile/Cookies", "_test_private.py"]
        for relative in forbidden:
            self.put(relative, "PRIVATE SYNTHETIC CONTENT MUST NOT BE OPENED")
        real_path_open, real_open, real_scandir = Path.open, builtins.open, os.scandir

        def ensure_allowed(path):
            if isinstance(path, (str, os.PathLike)):
                candidate = Path(path)
                for relative in forbidden:
                    if candidate == self.source / relative:
                        raise AssertionError("excluded path opened: " + relative)

        def path_open(path, *args, **kwargs):
            ensure_allowed(path)
            return real_path_open(path, *args, **kwargs)

        def builtin_open(path, *args, **kwargs):
            ensure_allowed(path)
            return real_open(path, *args, **kwargs)

        def scandir(path):
            if isinstance(path, (str, os.PathLike)) and Path(path) in {
                self.source / p for p in ("data", "venv", "venv.invalid-1", ".git",
                                          ".review-work", "__pycache__", "unlisted-browser-profile")
            }:
                raise AssertionError("excluded directory traversed")
            return real_scandir(path)

        with mock.patch.object(Path, "open", path_open), mock.patch("builtins.open", builtin_open), mock.patch("os.scandir", scandir):
            archive = self.build()
        self.assertTrue(archive.is_file())
        with zipfile.ZipFile(archive) as package:
            names = package.namelist()
        self.assertIn("app.py", names)
        self.assertIn(".env.example", names)
        self.assertFalse(set(forbidden) & set(names))
        release.validate_archive(archive)

    def test_clean_package_and_outer_zip_are_valid(self):
        archive = self.build()
        self.assertEqual(archive.parent, self.source / "dist")
        self.assertEqual(archive.suffix, ".zip")
        release.validate_archive(archive)
        release.validate_staging(self.source / "dist" / "clean-fixture-staging")

    def test_username_paths_are_rejected_with_only_relative_file_and_line(self):
        paths=[r'C:\Users\someone\Desktop', '/home/claude/work', '/Users/someone/work']  # SYNTHETIC_USER_PATH
        for index,value in enumerate(paths):
            self.put('README.md','safe line\n'+value+'\n')
            with self.assertRaises(release.ReleaseError) as raised:
                release.build_release(self.source,name='privacy-'+str(index))
            self.assertIn('README.md:2',str(raised.exception))
            self.assertNotIn('someone',str(raised.exception))

    def test_only_explicitly_marked_synthetic_test_path_is_exempt(self):
        value=r'C:\Users\someone\Desktop'  # SYNTHETIC_USER_PATH
        self.put('tests/test_synthetic.py','# SYNTHETIC_USER_PATH '+value+'\n')
        release.validate_archive(self.build())
        self.put('tests/test_unmarked.py','# '+value+'\n')
        with self.assertRaises(release.ReleaseError):
            release.build_release(self.source,name='unmarked')

    def test_generated_metadata_is_scanned_in_staging_and_zip(self):
        stage=self.base/'metadata-stage';stage.mkdir()
        value=r'{"path":"C:\\Users\\someone\\Desktop"}'  # SYNTHETIC_USER_PATH
        (stage/'SOURCE_VERSION.json').write_text(value,encoding='utf-8')
        with self.assertRaises(release.ReleaseError):release.validate_staging(stage)
        archive=self.base/'metadata.zip'
        with zipfile.ZipFile(archive,'w') as package:package.writestr('test-report.json',value)
        with self.assertRaises(release.ReleaseError):release.validate_archive(archive)

    def test_every_forbidden_path_is_rejected_in_staging_and_archive(self):
        forbidden = [".env", "core/.env", "data/x", "data/deeper/.gitkeep", "venv/x.py",
                     "venv.invalid-2/x.py", ".git/config", "core/__pycache__/x.pyc",
                     "server.pid", "server.LOG", ".review-work/x.py", "state.json",
                     "private.pem", "private.KEY", "old.zip", "old.tar",
                     "old.tar.gz", "old.tgz", "dist/report.txt", "core/data/x"]
        for index, relative in enumerate(forbidden):
            with self.subTest(path=relative):
                stage = self.base / ("stage-" + str(index))
                target = stage / relative
                target.parent.mkdir(parents=True)
                target.write_text("synthetic", encoding="utf-8")
                with self.assertRaises(release.ReleaseError):
                    release.validate_staging(stage)
                archive = self.base / ("tainted-" + str(index) + ".zip")
                with zipfile.ZipFile(archive, "w") as package:
                    package.writestr(relative, "synthetic")
                with self.assertRaises(release.ReleaseError):
                    release.validate_archive(archive)

    def test_only_empty_data_directory_or_gitkeep_is_allowed(self):
        stage = self.base / "empty-stage"
        (stage / "data").mkdir(parents=True)
        (stage / "data" / ".gitkeep").write_bytes(b"")
        release.validate_staging(stage)
        archive = self.base / "empty.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("data/", "")
            package.writestr("data/.gitkeep", "")
        release.validate_archive(archive)

    def test_staging_and_archive_unknown_files_are_rejected(self):
        stage = self.base / "unknown-stage"
        stage.mkdir()
        (stage / "Cookies").write_text("synthetic", encoding="utf-8")
        with self.assertRaises(release.ReleaseError):
            release.validate_staging(stage)
        archive = self.base / "unknown.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("Cookies", "synthetic")
        with self.assertRaises(release.ReleaseError):
            release.validate_archive(archive)

    def test_archive_rejects_unsafe_paths_duplicate_names_and_links(self):
        for index, relative in enumerate(("../app.py", "/app.py", "C:/app.py", "core\\app.py")):
            with self.subTest(path=relative):
                archive = self.base / ("unsafe-" + str(index) + ".zip")
                with zipfile.ZipFile(archive, "w") as package:
                    info = zipfile.ZipInfo("placeholder.py")
                    info.filename = relative  # Preserve an actually malformed backslash name on Windows.
                    package.writestr(info, "synthetic")
                with self.assertRaises(release.ReleaseError):
                    release.validate_archive(archive)
        archive = self.base / "link.zip"
        with zipfile.ZipFile(archive, "w") as package:
            info = zipfile.ZipInfo("core/link.py")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            package.writestr(info, "../../outside")
        with self.assertRaises(release.ReleaseError):
            release.validate_archive(archive)
        archive = self.base / "duplicates.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("app.py", "one")
            package.writestr("APP.py", "two")
        with self.assertRaises(release.ReleaseError):
            release.validate_archive(archive)

    def test_outside_directory_link_is_rejected_without_reading_target(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "private.py").write_text("synthetic secret", encoding="utf-8")
        link = self.source / "core" / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            if result.returncode:
                self.skipTest("host cannot create synthetic symlink or junction")
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("target must not be read")):
            with self.assertRaises(release.ReleaseError):
                release.collect_source_files(self.source)

    def test_reparse_metadata_is_rejected_on_all_platforms(self):
        fake = mock.Mock(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        self.assertTrue(release.is_link_or_reparse(fake))

    def test_output_and_package_name_cannot_escape_dist(self):
        for name in ("../outside", "C:/outside", "nested/name", "nested\\name", ".", ".."):
            with self.subTest(name=name), self.assertRaises(release.ReleaseError):
                release.build_release(self.source, name=name)

    def test_existing_staging_is_not_reused_or_deleted(self):
        archive = self.build()
        before = archive.read_bytes()
        with self.assertRaises(release.ReleaseError):
            self.build()
        self.assertEqual(before, archive.read_bytes())

    def test_source_manifest_is_stable_and_tracks_source_content(self):
        first = release.source_manifest(self.source)
        self.put(".env", "synthetic private version one")
        self.assertEqual(first, release.source_manifest(self.source))
        self.put("app.py", "print('changed synthetic source')\n")
        self.assertNotEqual(first, release.source_manifest(self.source))
        self.assertEqual(set(first), {p.as_posix() for p in release.collect_source_files(self.source)})


if __name__ == "__main__":
    unittest.main()

