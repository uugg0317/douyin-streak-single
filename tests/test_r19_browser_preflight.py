"""R19 component preflight: inspect installed executables without launching pages."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from playwright import sync_api
import startup_check


class BrowserPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.components = {"chromium": str(self.root / "chrome.exe"),
                           "chromium-headless-shell": str(self.root / "headless.exe")}
        for value in self.components.values():
            Path(value).write_bytes(b"synthetic component, never execute")
        imports = mock.patch.object(startup_check.importlib, "import_module")
        self.imports = imports.start()
        self.addCleanup(imports.stop)
        components = mock.patch.object(startup_check, "_browser_component_paths",
                                       return_value=self.components, create=True)
        self.probe = components.start()
        self.addCleanup(components.stop)
        # Keep the old implementation offline while demonstrating its missing
        # headless-shell regression; revised code does not start Sync Playwright.
        sync = mock.patch.object(sync_api, "sync_playwright")
        self.sync = sync.start()
        self.addCleanup(sync.stop)
        self.sync.return_value.__enter__.return_value.chromium.executable_path = self.components["chromium"]

    def test_complete_installation_passes_without_browser_launch(self):
        self.assertEqual(startup_check.check(), [])
        self.probe.assert_called_once_with()
        self.sync.assert_not_called()

    def test_missing_headless_shell_is_precisely_reported(self):
        Path(self.components["chromium-headless-shell"]).unlink()
        problems = startup_check.check()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("headless shell", problems[0].lower())

    def test_missing_full_chromium_is_precisely_reported(self):
        Path(self.components["chromium"]).unlink()
        problems = startup_check.check()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("Chromium 完整浏览器", problems[0])

    def test_missing_both_components_reports_both(self):
        for value in self.components.values():
            Path(value).unlink()
        problems = startup_check.check()
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("Chromium 完整浏览器", " ".join(problems))
        self.assertIn("headless shell", " ".join(problems).lower())

    def test_dependencies_only_never_queries_browser_registry(self):
        self.assertEqual(startup_check.check(require_browser=False), [])
        self.probe.assert_not_called()
        self.sync.assert_not_called()

    def test_dependency_failure_is_separate_from_browser_components(self):
        def import_module(name):
            if name == "greenlet":
                raise ImportError("synthetic missing dependency")
        self.imports.side_effect = import_module
        problems = startup_check.check()
        self.assertEqual(len(problems), 1)
        self.assertIn("greenlet", problems[0])
        self.probe.assert_not_called()

    def test_registry_failure_has_own_diagnostic_without_raw_output(self):
        self.probe.side_effect = subprocess.TimeoutExpired("synthetic private details", 15)
        problems = startup_check.check()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("组件清单", problems[0])
        self.assertIn("TimeoutExpired", problems[0])
        self.assertNotIn("private details", problems[0])


class RegistryQueryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cli = self.root / "package" / "cli.js"
        self.node = self.root / "node.exe"
        self.paths = {"chromium": str(self.root / "chrome.exe"),
                      "chromium-headless-shell": str(self.root / "headless.exe")}
        driver = mock.patch("playwright._impl._driver.compute_driver_executable",
                            return_value=(str(self.node), str(self.cli)))
        driver.start()
        self.addCleanup(driver.stop)
        runner = mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(self.paths), ""))
        self.runner = runner.start()
        self.addCleanup(runner.stop)

    def put_module(self, relative):
        module = self.cli.parent / relative
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text("// synthetic module, never execute", encoding="utf-8")
        return module

    def test_modern_bundle_uses_bundled_node_and_one_bounded_query(self):
        module = self.put_module("lib/coreBundle.js")
        with mock.patch.dict("os.environ", {"NODE_OPTIONS": "synthetic override", "NODE_PATH": "synthetic override"}):
            self.assertEqual(startup_check._browser_component_paths(), self.paths)
        self.runner.assert_called_once()
        command = self.runner.call_args.args[0]
        options = self.runner.call_args.kwargs
        self.assertEqual(command[:2], [str(self.node), "-e"])
        self.assertEqual(command[-1], str(module))
        self.assertGreater(options["timeout"], 0)
        self.assertLessEqual(options["timeout"], 30)
        self.assertTrue(options["check"])
        self.assertFalse(options.get("shell", False))
        self.assertNotIn("NODE_OPTIONS", options["env"])
        self.assertNotIn("NODE_PATH", options["env"])

    def test_legacy_registry_module_is_supported_if_bundle_absent(self):
        module = self.put_module("lib/server/registry/index.js")
        self.assertEqual(startup_check._browser_component_paths(), self.paths)
        self.assertEqual(self.runner.call_args.args[0][-1], str(module))

    def test_missing_registry_module_fails_without_subprocess(self):
        with self.assertRaisesRegex(RuntimeError, "registry"):
            startup_check._browser_component_paths()
        self.runner.assert_not_called()

    def test_incomplete_or_invalid_registry_response_fails_closed(self):
        self.put_module("lib/coreBundle.js")
        for payload in ({"chromium": self.paths["chromium"]}, {**self.paths, "chromium-headless-shell": ""},
                        {**self.paths, "chromium": 42}, ["not a mapping"]):
            with self.subTest(payload=payload):
                self.runner.return_value.stdout = json.dumps(payload)
                with self.assertRaises((RuntimeError, ValueError)):
                    startup_check._browser_component_paths()


class RegistryNodeProtocolTests(unittest.TestCase):
    def test_actual_bundled_node_accepts_modern_and_legacy_export_shapes(self):
        from playwright._impl._driver import compute_driver_executable

        node, _ = compute_driver_executable()
        for layout, nested in (("lib/coreBundle.js", True), ("lib/server/registry/index.js", False)):
            with self.subTest(layout=layout), TemporaryDirectory() as temporary:
                root = Path(temporary)
                paths = {"chromium": str(root / "chrome.exe"),
                         "chromium-headless-shell": str(root / "headless.exe")}
                module = root / layout
                module.parent.mkdir(parents=True)
                exports = "{registry:{registry}}" if nested else "{registry}"
                module.write_text(
                    "const paths=" + json.dumps(paths) + ";"
                    "const registry={findExecutable:(name)=>({executablePath:()=>paths[name]})};"
                    "module.exports=" + exports + ";", encoding="utf-8")
                with mock.patch("playwright._impl._driver.compute_driver_executable",
                                return_value=(node, str(root / "cli.js"))):
                    self.assertEqual(startup_check._browser_component_paths(), paths)


if __name__ == "__main__":
    unittest.main()
