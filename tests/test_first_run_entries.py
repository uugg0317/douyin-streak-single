"""First-run entry checks use temporary config, fake UI and no platform connection."""
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import _isolation  # noqa: F401  先隔离数据目录，再导入项目模块
import password_setup
from desktop import build, launcher

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "synthetic-first-run-password-123"


class FirstRunPathTests(unittest.TestCase):
    def test_cli_resolves_current_custom_env_path_at_call_time(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"ENV_FILE_PATH": str(Path(directory) / "custom.env")}), \
                mock.patch.object(password_setup, "initialize_env") as initialize:
            self.assertEqual(password_setup.main(), 0)
            initialize.assert_called_once_with(Path(directory) / "custom.env")

    def test_frozen_default_uses_executable_directory(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", str(Path(directory) / "tool.exe")), \
                mock.patch.object(password_setup, "initialize_env") as initialize:
            self.assertEqual(password_setup.main(), 0)
            initialize.assert_called_once_with(Path(directory) / ".env")

    def test_cli_cancel_and_unwritable_config_fail_without_success(self):
        for error, code in ((KeyboardInterrupt(), 130), (password_setup.PasswordSetupError("unwritable"), 1)):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(password_setup, "initialize_env", side_effect=error), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(password_setup.main(), code)

    def test_config_link_is_rejected_before_resolution_or_initialization(self):
        with mock.patch.dict(os.environ, {"ENV_FILE_PATH": "synthetic-link.env"}), \
                mock.patch.object(Path, "is_symlink", return_value=True), \
                mock.patch.object(password_setup, "resolve_env_path") as resolve, \
                mock.patch.object(password_setup, "initialize_env") as initialize, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(password_setup.main(), 1)
        resolve.assert_not_called()
        initialize.assert_not_called()

    def test_version_output_does_not_claim_runtime_authentication_mode(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            runpy.run_path(str(ROOT / "tools/version_info.py"), run_name="__main__")
        self.assertIn("[版本]", output.getvalue())
        self.assertNotIn("密码门", output.getvalue())


class DesktopPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = self.directory / "custom.env"
        self.environment = mock.patch.dict(os.environ, {"ENV_FILE_PATH": str(self.config)}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for target, kwargs in (("setup_browsers_path", {"return_value": None}),
                               ("startup_check.check", {"return_value": []}),
                               ("importlib.import_module", {"return_value": object()})):
            patcher = mock.patch("desktop.launcher." + target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def seed(self):
        with contextlib.redirect_stdout(io.StringIO()):
            password_setup.initialize_env(self.config, password_reader=mock.Mock(side_effect=[PASSWORD, PASSWORD]))

    def prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):
            launcher.prepare_desktop_environment()

    def test_complete_existing_settings_are_not_changed_or_prompted(self):
        self.seed()
        raw = self.config.read_bytes().replace(b"LOGIN_GATE=on", b"LOGIN_GATE=off")
        raw += b"# custom preserved\nSMTP_SERVER=synthetic.invalid\n"
        self.config.write_bytes(raw)
        with mock.patch.object(launcher, "read_desktop_password") as reader:
            self.prepare()
        reader.assert_not_called()
        self.assertEqual(self.config.read_bytes(), raw)
        self.assertEqual(os.environ["LOGIN_GATE"], "off")
        self.assertEqual(os.environ["SMTP_SERVER"], "synthetic.invalid")

    def test_first_run_initializes_selected_file_before_loading_it(self):
        order = []
        with mock.patch.object(launcher, "read_desktop_password", side_effect=lambda prompt: order.append("password") or PASSWORD), \
                mock.patch.object(launcher, "load_desktop_env", wraps=launcher.load_desktop_env) as load:
            self.prepare()
        self.assertEqual(order, ["password", "password"])
        load.assert_called_once_with()
        self.assertTrue(self.config.is_file())
        self.assertEqual(set(p.name for p in self.directory.iterdir()), {"custom.env"})
        self.assertEqual(os.environ["LOGIN_GATE"], "on")
        self.assertTrue(os.environ["URL_PASSWORD_HASH"].startswith("scrypt$"))
        self.assertNotIn(PASSWORD, self.config.read_text(encoding="utf-8"))

    def test_stale_inherited_auth_is_replaced_but_other_environment_is_preserved(self):
        self.seed()
        os.environ.update(AUTH_TOKEN="synthetic-old-token", URL_ACCESS_TOKEN="synthetic-old-url", URL_PASSWORD_HASH="synthetic-old-hash",
                          URL_PASSWORD_SALT="synthetic-old-salt", SMTP_SERVER="inherited.invalid")
        self.prepare()
        values = password_setup._parse_env(self.config.read_text(encoding="utf-8"))
        for key in ("AUTH_TOKEN", "URL_ACCESS_TOKEN", "URL_PASSWORD_HASH", "URL_PASSWORD_SALT"):
            self.assertEqual(os.environ[key], values[key])
        self.assertEqual(os.environ["SMTP_SERVER"], "inherited.invalid")

    def test_component_failure_does_not_prompt_or_write_config(self):
        with mock.patch.object(launcher.startup_check, "check", return_value=["headless shell missing"]), \
                mock.patch.object(launcher, "read_desktop_password") as reader:
            with self.assertRaisesRegex(password_setup.PasswordSetupError, "headless shell"):
                self.prepare()
        reader.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_tray_dependency_failure_does_not_prompt_or_write_config(self):
        with mock.patch.object(launcher.importlib, "import_module", side_effect=ImportError("synthetic")), \
                mock.patch.object(launcher, "read_desktop_password") as reader:
            with self.assertRaisesRegex(password_setup.PasswordSetupError, "系统托盘"):
                self.prepare()
        reader.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_cancel_preserves_original_config_and_does_not_load_or_start(self):
        raw = b"# preserved\nSMTP_SERVER=synthetic.invalid\n"
        self.config.write_bytes(raw)
        with mock.patch.object(launcher, "read_desktop_password", side_effect=launcher.DesktopSetupCancelled("cancelled")), \
                mock.patch.object(launcher, "anchor_cwd"), \
                mock.patch.object(launcher, "show_startup_error") as show, \
                mock.patch.object(launcher, "TrayApp") as tray, \
                mock.patch.object(launcher, "load_desktop_env") as load, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(), 2)
        show.assert_called_once_with("cancelled")
        tray.assert_not_called()
        load.assert_not_called()
        self.assertEqual(self.config.read_bytes(), raw)

    def test_gui_failure_preserves_original_config(self):
        raw = b"SMTP_SERVER=synthetic.invalid\n"
        self.config.write_bytes(raw)
        with mock.patch.object(launcher, "read_desktop_password", side_effect=password_setup.PasswordSetupError("GUI unavailable")):
            with self.assertRaisesRegex(password_setup.PasswordSetupError, "GUI"):
                self.prepare()
        self.assertEqual(self.config.read_bytes(), raw)
        self.assertEqual([p.name for p in self.directory.iterdir()], ["custom.env"])

    def test_unwritable_config_stops_before_service(self):
        with mock.patch.object(launcher, "initialize_env", side_effect=password_setup.PasswordSetupError("unwritable")), \
                mock.patch.object(launcher, "anchor_cwd"), \
                mock.patch.object(launcher, "show_startup_error") as show, \
                mock.patch.object(launcher, "TrayApp") as tray:
            self.assertEqual(launcher.main(), 2)
        show.assert_called_once_with("unwritable")
        tray.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_link_path_failure_is_visible_before_anchor_or_service(self):
        with mock.patch.object(launcher, "resolve_setup_path", side_effect=password_setup.PasswordSetupError("unsafe link")), \
                mock.patch.object(launcher, "anchor_cwd") as anchor, \
                mock.patch.object(launcher, "show_startup_error") as show, \
                mock.patch.object(launcher, "TrayApp") as tray:
            self.assertEqual(launcher.main(), 2)
        show.assert_called_once_with("unsafe link")
        anchor.assert_not_called()
        tray.assert_not_called()


class DesktopDialogTests(unittest.TestCase):
    def fake_tk(self, value=PASSWORD):
        window = mock.Mock()
        tk = types.ModuleType("tkinter")
        tk.Tk = mock.Mock(return_value=window)
        tk.simpledialog = types.SimpleNamespace(askstring=mock.Mock(return_value=value))
        tk.messagebox = types.SimpleNamespace(showerror=mock.Mock())
        return tk, window

    def test_password_dialog_masks_characters_and_destroys_root(self):
        tk, window = self.fake_tk()
        with mock.patch.dict(sys.modules, {"tkinter": tk}):
            self.assertEqual(launcher.read_desktop_password("test prompt"), PASSWORD)
        self.assertEqual(tk.simpledialog.askstring.call_args.kwargs["show"], "*")
        window.withdraw.assert_called_once_with()
        window.destroy.assert_called_once_with()

    def test_cancel_survives_cleanup_error(self):
        tk, window = self.fake_tk(None)
        window.destroy.side_effect = RuntimeError("synthetic cleanup error")
        with mock.patch.dict(sys.modules, {"tkinter": tk}):
            with self.assertRaises(launcher.DesktopSetupCancelled):
                launcher.read_desktop_password("test prompt")

    def test_gui_errors_do_not_leak_input_and_never_call_getpass(self):
        tk, window = self.fake_tk()
        tk.simpledialog.askstring.side_effect = RuntimeError(PASSWORD)
        with mock.patch.dict(sys.modules, {"tkinter": tk}), \
                mock.patch.object(password_setup.getpass, "getpass") as getpass:
            with self.assertRaises(password_setup.PasswordSetupError) as raised:
                launcher.read_desktop_password("test prompt")
        self.assertNotIn(PASSWORD, str(raised.exception))
        getpass.assert_not_called()
        window.destroy.assert_called_once_with()

    def test_windows_error_is_visible_when_tk_is_missing(self):
        ctypes = types.ModuleType("ctypes")
        ctypes.windll = types.SimpleNamespace(user32=types.SimpleNamespace(MessageBoxW=mock.Mock()))
        with mock.patch.dict(sys.modules, {"tkinter": None, "ctypes": ctypes}), \
                mock.patch.object(sys, "platform", "win32"), contextlib.redirect_stderr(io.StringIO()):
            launcher.show_startup_error("synthetic missing component")
        self.assertEqual(ctypes.windll.user32.MessageBoxW.call_args.args[1], "synthetic missing component")

    def test_build_requires_tcl_and_keeps_private_config_out(self):
        with mock.patch.dict(sys.modules, {"tkinter": None}), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "Tcl/Tk"):
                build.check_python()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "source", root / "output"
            source.mkdir(); output.mkdir()
            (source / ".env").write_text("SYNTHETIC_PRIVATE=never-copy", encoding="utf-8")
            (source / ".env.example").write_text("# safe example", encoding="utf-8")
            with mock.patch.object(build, "ROOT", str(source)):
                build.prepare_first_run_files(str(output / "app.exe"))
            self.assertFalse((output / ".env").exists())
            self.assertEqual((output / ".env.example").read_text(encoding="utf-8"), "# safe example")
            self.assertIn("tkinter.simpledialog", build.HIDDEN_IMPORTS)


class SourceEntryProcessTests(unittest.TestCase):
    def test_relative_config_is_resolved_before_source_launcher_changes_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory) / "harness.py"
            harness.write_text('''import os, pathlib, runpy, sys
target = sys.argv[1]
expected = str(pathlib.Path('custom.env').resolve())
def stop_before_side_effects(frame, event, arg):
    if event == 'call' and frame.f_code.co_name == 'prepare_desktop_environment':
        assert os.environ['ENV_FILE_PATH'] == expected
        assert str(sys.modules['env_bootstrap'].ENV_PATH) == expected
        assert pathlib.Path.cwd() == pathlib.Path(target).parents[1]
        print('RELATIVE_CONFIG_MATCHES_SERVICE_PATH')
        raise SystemExit(0)
    return stop_before_side_effects
sys.path[0] = os.path.dirname(target)
sys.settrace(stop_before_side_effects)
runpy.run_path(target, run_name='__main__')
''', encoding="utf-8")
            environment = dict(os.environ, ENV_FILE_PATH="custom.env")
            environment.pop("PYTHONPATH", None)
            result = subprocess.run([sys.executable, str(harness), str(ROOT / "desktop/launcher.py")],
                                    cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("RELATIVE_CONFIG_MATCHES_SERVICE_PATH", result.stdout)
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["harness.py"])

    def test_source_launcher_anchors_imports_from_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory) / "harness.py"
            harness.write_text('''import os, runpy, sys
target = sys.argv[1]
def stop_before_side_effects(frame, event, arg):
    if event == 'call' and frame.f_code.co_name == 'main' and os.path.abspath(frame.f_code.co_filename) == target:
        assert 'password_setup' in sys.modules and 'startup_check' in sys.modules
        assert 'app' not in sys.modules
        print('SOURCE_ENTRY_IMPORTED_WITHOUT_APP')
        raise SystemExit(0)
    return stop_before_side_effects
sys.path[0] = os.path.dirname(target)
sys.settrace(stop_before_side_effects)
runpy.run_path(target, run_name='__main__')
''', encoding="utf-8")
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            result = subprocess.run([sys.executable, str(harness), str(ROOT / "desktop/launcher.py")],
                                    cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("SOURCE_ENTRY_IMPORTED_WITHOUT_APP", result.stdout)
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["harness.py"])


class ShellEntryContractTests(unittest.TestCase):
    def test_linux_entry_orders_preflight_setup_and_service_and_stops_on_failure(self):
        bash = shutil.which("bash")
        if os.name == "nt":
            git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
            bash = str(git_bash) if git_bash.is_file() else None
        if not bash:
            self.skipTest("Bash unavailable; no Linux entry contract execution")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, commands = root / "project", root / "commands"
            project.mkdir(); commands.mkdir()
            (project / "start.sh").write_bytes((ROOT / "start.sh").read_bytes())
            (project / "venv/bin").mkdir(parents=True)
            # The shell entry runs real Bash; all Python/installer work is replaced by scripts.
            (commands / "python3").write_text('''#!/usr/bin/env bash
if [[ "$*" == *hashlib* ]]; then printf 'synthetic-hash\\n'; fi
exit 0
''', encoding="utf-8", newline="\n")
            (project / "venv/bin/python").write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$TEST_CALLS"
case "$1" in
  startup_check.py) exit "${TEST_COMPONENT_EXIT:-0}" ;;
  password_setup.py)
    [[ "$ENV_FILE_PATH" == "$TEST_EXPECTED_ENV" ]] || exit 71
    [[ -z "${AUTH_TOKEN:-}" ]] || exit 72
    [[ "$SMTP_SERVER" == 'synthetic.invalid' ]] || exit 73
    exit "$TEST_SETUP_EXIT" ;;
  app.py) exit 0 ;;
esac
exit 0
''', encoding="utf-8", newline="\n")
            for path in (commands / "python3", project / "venv/bin/python"):
                path.chmod(0o700)
            (project / "venv/.requirements.sha256").write_text("synthetic-hash\n", encoding="utf-8")
            wrapper = root / "run.sh"
            wrapper.write_text('''#!/usr/bin/env bash
export PATH="$PWD/commands:$PATH"
export TEST_CALLS="$PWD/calls.txt"
export ENV_FILE_PATH='custom.env'
export TEST_EXPECTED_ENV="$PWD/custom.env"
export AUTH_TOKEN='synthetic-stale-synthetic-token'
export SMTP_SERVER='synthetic.invalid'
exec bash project/start.sh
''', encoding="utf-8", newline="\n")
            for setup_exit, component_exit in ((0, 0), (130, 0), (0, 2)):
                with self.subTest(setup_exit=setup_exit, component_exit=component_exit):
                    calls = root / "calls.txt"
                    calls.unlink(missing_ok=True)
                    environment = dict(os.environ, TEST_SETUP_EXIT=str(setup_exit), TEST_COMPONENT_EXIT=str(component_exit))
                    result = subprocess.run([bash, "run.sh"], cwd=root, env=environment,
                                            capture_output=True, text=True, encoding="utf-8", timeout=20)
                    self.assertEqual(result.returncode, component_exit or setup_exit, result.stderr)
                    lines = calls.read_text(encoding="utf-8").splitlines()
                    if component_exit:
                        self.assertNotIn("password_setup.py", lines)
                    else:
                        self.assertLess(lines.index("startup_check.py"), lines.index("password_setup.py"))
                    if setup_exit or component_exit:
                        self.assertNotIn("app.py", lines)
                    else:
                        self.assertLess(lines.index("password_setup.py"), lines.index("app.py"))
                    self.assertFalse((root / "custom.env").exists())
                    self.assertFalse((project / ".env").exists())

    def test_windows_entry_preserves_config_mode_and_custom_path(self):
        source = (ROOT / "启动单账号本机版.bat").read_text(encoding="utf-8-sig")
        self.assertIn('if not defined ENV_FILE_PATH set "ENV_FILE_PATH=%~dp0.env"', source)
        self.assertNotIn('set "LOGIN_GATE=on"', source)
        self.assertNotIn('set "PASSWORD_PUBLIC_LOGIN=on"', source)
        self.assertLess(source.index("startup_check.py"), source.index("password_setup.py"))


if __name__ == "__main__":
    unittest.main()
