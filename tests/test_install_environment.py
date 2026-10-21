"""R03: synthetic environment recovery; never execute an installer or real venv."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

try:
    from tools import ensure_venv as installer
except ImportError:
    installer = None


PROJECT = Path(__file__).resolve().parents[1]
TEST_TMP = PROJECT / "dist" / "test-tmp"


class InstallBootstrapTests(unittest.TestCase):
    def test_bootstrap_checks_external_python_before_handling_venv(self):
        script = (PROJECT / "安装依赖.bat").read_text(encoding="utf-8-sig")
        self.assertNotIn('if exist "%~dp0venv\\Scripts\\python.exe" goto install_packages', script)
        self.assertIn('py -3 "%~dp0tools\\ensure_venv.py"', script)
        self.assertIn('python "%~dp0tools\\ensure_venv.py"', script)
        self.assertLess(script.index("py -3 -c"), script.index("ensure_venv.py"))
        self.assertNotIn(" -m venv ", script)


class InstallEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(installer, "the installer needs executable, version and pip consistency checks")
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="安装 中文 path ", dir=TEST_TMP)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.venv = self.root / "venv"
        self.messages = []
        self.calls = []
        self.old_failure = None
        self.new_failure = None
        self.created = False

    def put_environment(self):
        executable = installer.venv_python(self.venv)
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"synthetic interpreter - never execute")
        (self.venv / "pyvenv.cfg").write_text("home = Z:/missing-build-host/python\n", encoding="utf-8")
        return executable

    def run_process(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        self.assertIsInstance(argv, list)
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["cwd"], self.root)
        if argv[1:3] == ["-m", "venv"]:
            self.created = True
            self.put_environment()
            return subprocess.CompletedProcess(argv, 0, "", "")
        failure = self.new_failure if self.created else self.old_failure
        if failure:
            result = failure(argv)
            if result is not None:
                return result
        return subprocess.CompletedProcess(argv, 0, "No broken requirements found.\n" if argv[1:3] == ["-m", "pip"] else "3.13.9\n", "")

    def ensure(self):
        return installer.ensure_venv(self.root, runner=self.run_process, emit=self.messages.append)

    def test_valid_environment_is_checked_and_kept(self):
        executable = self.put_environment()
        self.assertEqual(self.ensure(), executable)
        self.assertFalse(self.created)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][0][0], str(executable))
        self.assertEqual(self.calls[0][0][1], "-c")
        self.assertEqual(self.calls[1][0][1:], ["-m", "pip", "check"])
        self.assertEqual(list(self.root.glob("venv.invalid-*")), [])

    def test_stale_build_host_is_preserved_then_rebuilt_in_chinese_space_path(self):
        self.put_environment()
        self.old_failure = lambda argv: subprocess.CompletedProcess(argv, 103, "", "No Python at Z:/missing-build-host/python")
        self.assertEqual(self.ensure(), installer.venv_python(self.venv))
        backup = self.root / "venv.invalid-1"
        self.assertIn("missing-build-host", (backup / "pyvenv.cfg").read_text(encoding="utf-8"))
        self.assertTrue(self.created)
        self.assertTrue(any("No Python at" in message for message in self.messages))
        create_call = next(argv for argv, _ in self.calls if argv[1:3] == ["-m", "venv"])
        self.assertEqual(create_call[-1], str(self.venv))
        self.assertEqual(len(create_call), 4)

    def test_old_python_is_rebuilt(self):
        self.put_environment()
        self.old_failure = lambda argv: subprocess.CompletedProcess(argv, 3, "3.9.13\n", "")
        self.ensure()
        self.assertTrue(self.created)
        self.assertTrue(any("3.10" in message for message in self.messages))

    def test_broken_package_dependencies_are_rebuilt(self):
        self.put_environment()
        self.old_failure = lambda argv: subprocess.CompletedProcess(argv, 1, "synthetic-package has requirement missing-dependency", "") if argv[1:3] == ["-m", "pip"] else None
        self.ensure()
        self.assertTrue(self.created)
        self.assertTrue(any("missing-dependency" in message for message in self.messages))

    def test_missing_interpreter_preserves_existing_environment(self):
        self.venv.mkdir()
        (self.venv / "pyvenv.cfg").write_text("home = Z:/missing/python\n", encoding="utf-8")
        self.ensure()
        self.assertTrue((self.root / "venv.invalid-1" / "pyvenv.cfg").exists())
        self.assertTrue(self.created)

    def test_absent_environment_is_created_without_backup(self):
        self.ensure()
        self.assertTrue(self.created)
        self.assertEqual(list(self.root.glob("venv.invalid-*")), [])

    def test_existing_backup_is_never_overwritten(self):
        (self.root / "venv.invalid-1").mkdir()
        marker = self.root / "venv.invalid-1" / "keep.txt"
        marker.write_text("existing synthetic environment", encoding="utf-8")
        self.venv.mkdir()
        self.ensure()
        self.assertEqual(marker.read_text(encoding="utf-8"), "existing synthetic environment")
        self.assertTrue((self.root / "venv.invalid-2").is_dir())

    def test_interpreter_os_error_is_explained_and_rebuilt(self):
        self.put_environment()
        def fail(_argv):
            raise OSError("synthetic broken executable")
        self.old_failure = fail
        self.ensure()
        self.assertTrue(self.created)
        self.assertTrue(any("synthetic broken executable" in message for message in self.messages))

    def test_hung_check_has_timeout_and_rebuilds(self):
        self.put_environment()
        def fail(argv):
            raise subprocess.TimeoutExpired(argv, 30)
        self.old_failure = fail
        self.ensure()
        self.assertTrue(self.created)
        self.assertTrue(all(kwargs.get("timeout", 0) > 0 for _, kwargs in self.calls))
        self.assertTrue(any("超时" in message for message in self.messages))

    def test_creation_failure_preserves_old_environment_and_stops(self):
        self.venv.mkdir()
        def fail_create(argv, **kwargs):
            self.assertEqual(argv[1:3], ["-m", "venv"])
            return subprocess.CompletedProcess(argv, 1, "", "synthetic no disk space")
        with self.assertRaisesRegex(installer.EnvironmentSetupError, "synthetic no disk space"):
            installer.ensure_venv(self.root, runner=fail_create, emit=self.messages.append)
        self.assertTrue((self.root / "venv.invalid-1").is_dir())

    def test_failed_new_environment_validation_stops(self):
        self.new_failure = lambda argv: subprocess.CompletedProcess(argv, 1, "synthetic pip unavailable", "") if argv[1:3] == ["-m", "pip"] else None
        with self.assertRaisesRegex(installer.EnvironmentSetupError, "synthetic pip unavailable"):
            self.ensure()
        self.assertTrue(self.venv.is_dir())

    def test_rename_failure_does_not_start_creation_or_delete_environment(self):
        self.venv.mkdir()
        with mock.patch.object(Path, "rename", side_effect=PermissionError("synthetic access denied")):
            with self.assertRaisesRegex(installer.EnvironmentSetupError, "synthetic access denied"):
                self.ensure()
        self.assertTrue(self.venv.is_dir())
        self.assertFalse(self.created)
        self.assertEqual(self.calls, [])

    def test_subprocesses_ignore_inherited_python_path_configuration(self):
        with mock.patch.dict("os.environ", {"PYTHONHOME": "synthetic bad home", "PYTHONPATH": "synthetic bad path", "VIRTUAL_ENV": "synthetic old environment"}):
            self.ensure()
        for _, kwargs in self.calls:
            self.assertNotIn("PYTHONHOME", kwargs["env"])
            self.assertNotIn("PYTHONPATH", kwargs["env"])
            self.assertNotIn("VIRTUAL_ENV", kwargs["env"])

    def test_directory_link_is_rejected_without_running_or_renaming(self):
        self.venv.mkdir()
        with mock.patch.object(installer, "_is_link", return_value=True):
            with self.assertRaisesRegex(installer.EnvironmentSetupError, "目录链接"):
                self.ensure()
        self.assertTrue(self.venv.is_dir())
        self.assertEqual(self.calls, [])

    def test_active_install_interpreter_is_not_renamed_while_in_use(self):
        executable = self.put_environment()
        self.old_failure = lambda argv: subprocess.CompletedProcess(argv, 1, "synthetic broken dependency", "") if argv[1:3] == ["-m", "pip"] else None
        with mock.patch.object(installer.sys, "executable", str(executable)):
            with self.assertRaisesRegex(installer.EnvironmentSetupError, "待重建的 venv"):
                self.ensure()
        self.assertFalse(self.created)
        self.assertTrue(executable.is_file())
        self.assertEqual(list(self.root.glob("venv.invalid-*")), [])


if __name__ == "__main__":
    unittest.main()
