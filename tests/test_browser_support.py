import unittest
from unittest import mock
from types import SimpleNamespace


class BrowserSupportTests(unittest.TestCase):
    def support(self):
        import _browser
        _browser._probe.cache_clear()
        self.addCleanup(_browser._probe.cache_clear)
        return _browser

    def test_missing_executable_stops_started_playwright_and_caches_result(self):
        support=self.support()
        from playwright.sync_api import Error
        driver=mock.Mock();driver.chromium.launch.side_effect=Error("Executable doesn't exist at synthetic-cache/headless_shell")
        with mock.patch.object(support,'sync_playwright',return_value=SimpleNamespace(start=lambda:driver)):
            self.assertFalse(support.chromium_available())
            self.assertFalse(support.chromium_available())
        driver.stop.assert_called_once();self.assertEqual(driver.chromium.launch.call_count,1)

    def test_body_and_close_failure_still_stop_driver(self):
        support=self.support();driver=mock.Mock();browser=driver.chromium.launch.return_value
        with mock.patch.object(support,'sync_playwright',return_value=SimpleNamespace(start=lambda:driver)):
            with self.assertRaisesRegex(RuntimeError,'synthetic body'):
                with support.managed_chromium():raise RuntimeError('synthetic body')
        browser.close.assert_called_once();driver.stop.assert_called_once()
        browser.close.side_effect=RuntimeError('synthetic close');driver.stop.reset_mock()
        with mock.patch.object(support,'sync_playwright',return_value=SimpleNamespace(start=lambda:driver)):
            with self.assertRaisesRegex(RuntimeError,'synthetic close'):
                with support.managed_chromium():pass
        driver.stop.assert_called_once()

    def test_non_missing_launch_error_is_not_hidden_as_a_skip(self):
        support=self.support();driver=mock.Mock();driver.chromium.launch.side_effect=RuntimeError('synthetic crash')
        with mock.patch.object(support,'sync_playwright',return_value=SimpleNamespace(start=lambda:driver)):
            with self.assertRaisesRegex(RuntimeError,'synthetic crash'):support.chromium_available()
        driver.stop.assert_called_once()

    def test_skip_reason_is_explicit(self):
        support=self.support()
        with mock.patch.object(support,'chromium_available',return_value=False):
            @support.requires_chromium
            class Sample(unittest.TestCase):
                def test_body(self):raise AssertionError('must not run')
        result=unittest.TestResult();unittest.defaultTestLoader.loadTestsFromTestCase(Sample).run(result)
        self.assertEqual(len(result.skipped),1)
        self.assertEqual(result.skipped[0][1],'no_chromium_headless_shell')
