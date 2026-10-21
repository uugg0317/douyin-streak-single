"""R20: minimum interpreter, install input and documented dependency intent."""
import contextlib
import io
from pathlib import Path
import re
import unittest
from unittest import mock

import startup_check


ROOT = Path(__file__).resolve().parents[1]
DIRECT = {"fastapi", "uvicorn", "playwright", "apscheduler", "python-multipart", "pydantic"}


def dependency_lines(filename):
    return [line.split("#", 1)[0].strip() for line in (ROOT / filename).read_text(encoding="utf-8-sig").splitlines()
            if line.split("#", 1)[0].strip()]


class InterpreterMinimumTests(unittest.TestCase):
    def test_python_39_stops_before_importing_any_dependency_or_querying_components(self):
        with mock.patch.object(startup_check.sys, "version_info", (3, 9, 13)), \
                mock.patch.object(startup_check.importlib, "import_module") as imports, \
                mock.patch.object(startup_check, "_browser_component_paths") as components:
            problems = startup_check.check()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("Python 3.10 及以上", problems[0])
        self.assertIn("3.9", problems[0])
        imports.assert_not_called()
        components.assert_not_called()

    def test_python_310_is_accepted(self):
        with mock.patch.object(startup_check.sys, "version_info", (3, 10, 0)), \
                mock.patch.object(startup_check.importlib, "import_module") as imports:
            self.assertEqual(startup_check.check(require_browser=False), [])
        self.assertEqual(imports.call_count, len(startup_check.REQUIRED))

    def test_python_39_command_returns_failure_with_explicit_instruction(self):
        output = io.StringIO()
        with mock.patch.object(startup_check.sys, "version_info", (3, 9, 13)), \
                mock.patch.object(startup_check.sys, "argv", ["startup_check.py", "--dependencies-only"]), \
                contextlib.redirect_stdout(output):
            self.assertEqual(startup_check.main(), 2)
        self.assertIn("Python 3.10 及以上", output.getvalue())


class RequirementsDocumentationTests(unittest.TestCase):
    def test_windows_transitive_dependencies_have_explicit_existing_pins(self):
        lines = dependency_lines("requirements.lock")
        self.assertIn('tzdata==2026.5; sys_platform == "win32"', lines)
        self.assertIn('colorama==0.4.6; sys_platform == "win32"', lines)

    def test_lock_is_exact_and_distinguishes_direct_from_transitive(self):
        lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        self.assertIn("直接依赖", lock)
        self.assertIn("传递依赖", lock)
        for line in dependency_lines("requirements.lock"):
            with self.subTest(requirement=line):
                self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[0-9][^\s;]*(?:\s*;.*)?$")

    def test_requirements_text_contains_only_direct_dependencies_and_lock_guidance(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        names = {re.split(r"[<>=!~;\[\s]", line, 1)[0].lower().replace("_", "-")
                 for line in dependency_lines("requirements.txt")}
        self.assertEqual(names, DIRECT)
        self.assertIn("requirements.lock", text)
        self.assertNotIn("requests", text.lower())
        self.assertNotIn("paramiko", text.lower())

    def test_readme_installer_and_user_guide_state_same_python_minimum(self):
        for name in ("README.md", "安装依赖.bat", "使用说明.txt"):
            with self.subTest(path=name):
                text = (ROOT / name).read_text(encoding="utf-8-sig")
                self.assertRegex(text, r"Python\s+3\.10\s*及以上")

    def test_installer_uses_lock_as_only_requirements_input(self):
        installer = (ROOT / "安装依赖.bat").read_text(encoding="utf-8-sig")
        self.assertIn('requirements.lock', installer)
        self.assertNotRegex(installer, r"(?im)^\s*[^r\r\n].*pip\s+install.*requirements\.txt")


if __name__ == "__main__":
    unittest.main()
