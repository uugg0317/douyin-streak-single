"""R18: all ordinary browser paths use the installed browser's native defaults.

Run through dist/run_checks.py so import-time state is synthetic. Browser and
page calls below are mocks; no browser is launched and no remote page is loaded.
"""
from __future__ import annotations

import ast
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from core import automation, browser
from desktop import build
import startup_check


ROOT = Path(__file__).resolve().parents[1]


class BrowserDefaultsTests(unittest.TestCase):
    def fake_driver(self, module):
        driver = Mock()
        launched = driver.chromium.launch.return_value
        context = launched.new_context.return_value
        page = context.new_page.return_value
        patcher = patch.object(module, "sync_playwright")
        manager = patcher.start()
        self.addCleanup(patcher.stop)
        manager.return_value.start.return_value = driver
        return driver, launched, context, page

    def assert_defaults(self, driver, launched, context, page):
        launch = driver.chromium.launch.call_args.kwargs
        self.assertTrue(launch["headless"])
        self.assertFalse(any("AutomationControlled" in arg for arg in launch["args"]))
        settings = launched.new_context.call_args.kwargs
        self.assertNotIn("user_agent", settings)
        self.assertEqual(settings["viewport"], {"width": 1366, "height": 768})
        self.assertEqual(settings["locale"], "zh-CN")
        self.assertEqual(settings["timezone_id"], "Asia/Shanghai")
        self.assertIs(settings["ignore_https_errors"], False)
        context.add_init_script.assert_not_called()
        page.add_init_script.assert_not_called()

    def test_normal_context_uses_native_user_agent_without_injected_scripts(self):
        driver, launched, context, page = self.fake_driver(browser)
        with browser.open_browser(use_state=False) as resources:
            self.assertEqual(resources, (driver, launched, context, page))
            self.assert_defaults(driver, launched, context, page)
            self.assertNotIn("storage_state", launched.new_context.call_args.kwargs)
        launched.close.assert_called_once()
        driver.stop.assert_called_once()

    def test_health_check_uses_same_native_defaults(self):
        driver, launched, context, page = self.fake_driver(automation)
        with TemporaryDirectory() as temporary:
            state = Path(temporary) / "synthetic-state.json"
            state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
            with patch.object(automation, "get_valid_state_path", return_value=state), \
                    patch.object(automation, "scan_state_cookies", return_value={}), \
                    patch.object(automation, "check_login", return_value=(True, "ok")):
                result = automation._check_session_health_impl()
            self.assertTrue(result["logged_in"], result)
            self.assert_defaults(driver, launched, context, page)
            self.assertEqual(launched.new_context.call_args.kwargs["storage_state"], str(state))
        launched.close.assert_called_once()
        driver.stop.assert_called_once()

    def test_prewarm_uses_same_native_defaults(self):
        driver, launched, context, page = self.fake_driver(automation)
        with patch.object(automation, "_prewarmed_browser", None), \
                patch.object(automation, "get_valid_state_path", return_value=None), \
                patch.object(automation, "check_login", return_value=(True, "ok")), \
                patch.object(automation.time, "sleep"):
            try:
                self.assertTrue(automation._prewarm_browser_impl())
                self.assert_defaults(driver, launched, context, page)
            finally:
                automation._close_prewarmed()
        launched.close.assert_called_once()
        driver.stop.assert_called_once()

    def test_runtime_and_build_do_not_import_stealth_or_hardcode_a_user_agent(self):
        for name in ("core/browser.py", "core/automation.py", "startup_check.py", "desktop/build.py"):
            with self.subTest(path=name):
                tree = ast.parse((ROOT / name).read_text(encoding="utf-8-sig"))
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        imports = [alias.name for alias in node.names]
                        if isinstance(node, ast.ImportFrom) and node.module:
                            imports.append(node.module)
                        self.assertFalse(any("stealth" in value for value in imports), imports)
                    if isinstance(node, ast.Name):
                        self.assertNotIn(node.id, {"_CHROME_UA", "_apply_stealth"})
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        self.assertNotIn("AutomationControlled", node.value)
                        self.assertNotEqual(node.value, "playwright_stealth")
        self.assertNotIn("playwright_stealth", startup_check.REQUIRED)
        self.assertNotIn("playwright_stealth", build.HIDDEN_IMPORTS)

    def test_install_inputs_exclude_stealth(self):
        for name in ("requirements.txt", "requirements.lock"):
            dependencies = [line.split("#", 1)[0].strip().lower().replace("_", "-")
                            for line in (ROOT / name).read_text(encoding="utf-8").splitlines()]
            with self.subTest(path=name):
                self.assertFalse(any(line.startswith("playwright-stealth") for line in dependencies))


if __name__ == "__main__":
    unittest.main()
