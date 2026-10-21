"""T17 prewarm defaults and stale-page checks with fake clocks/pages only."""
import os
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import automation as A, scheduler as S


class PrewarmIdleTests(unittest.TestCase):
    def setUp(self):
        self.page, self.browser, self.driver = mock.Mock(), mock.Mock(), mock.Mock()
        self.cache = {"page": self.page, "browser": self.browser, "p": self.driver,
                      "context": object(), "ready": True, "created_at": 1000.0}
        for name, value in {"_prewarmed_browser": self.cache, "BROWSER_MAX_IDLE_SECONDS": 600}.items():
            patcher = mock.patch.object(A, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = mock.patch.object(A.time, "time", return_value=1100.0).start()
        self.addCleanup(mock.patch.stopall)
        self.login = mock.patch.object(A, "check_login", return_value=(True, "synthetic login")).start()

    def test_fresh_page_reuses_without_reload_but_rechecks_login(self):
        self.assertIs(A._get_prewarmed_page(), self.cache)
        self.page.reload.assert_not_called()
        self.login.assert_called_once_with(self.page)

    def test_stale_page_reloads_waits_for_contacts_then_rechecks_login(self):
        self.clock.return_value = 1601.0
        calls = []
        self.page.reload.side_effect = lambda **kw: calls.append("reload")
        self.page.wait_for_selector.side_effect = lambda selector, **kw: calls.append(selector)
        self.login.side_effect = lambda page: (calls.append("login") or (True, "synthetic"))
        self.assertIs(A._get_prewarmed_page(), self.cache)
        self.assertEqual(calls[0], "reload")
        self.assertIn(".conversationConversationItemtitle", calls)
        self.assertEqual(calls[-1], "login")
        self.assertEqual(self.cache["created_at"], 1601.0)
        self.clock.return_value = 1602.0
        A._get_prewarmed_page()
        self.assertEqual(self.page.reload.call_count, 1)

    def test_reload_or_list_failure_closes_old_cache_instead_of_reusing_it(self):
        for failure in ("reload", "list"):
            with self.subTest(failure=failure):
                A._prewarmed_browser = self.cache
                self.page.reset_mock(side_effect=True)
                self.browser.reset_mock()
                self.driver.reset_mock()
                self.clock.return_value = 1601.0
                getattr(self.page, "reload" if failure == "reload" else "wait_for_selector").side_effect = RuntimeError("synthetic stale page failure")
                self.assertIsNone(A._get_prewarmed_page())
                self.assertIsNone(A._prewarmed_browser)
                self.browser.close.assert_called_once()
                self.driver.stop.assert_called_once()

    def test_stale_logged_out_page_closes_and_declines_reuse(self):
        self.clock.return_value = 1601.0
        self.login.return_value = (False, "synthetic expired login")
        self.assertIsNone(A._get_prewarmed_page())
        self.page.reload.assert_called_once()
        self.browser.close.assert_called_once()

    def test_explicit_zero_disables_idle_reload_but_keeps_login_check(self):
        self.clock.return_value = 100000.0
        with mock.patch.object(A, "BROWSER_MAX_IDLE_SECONDS", 0):
            self.assertIs(A._get_prewarmed_page(), self.cache)
        self.page.reload.assert_not_called()
        self.login.assert_called_once()

    def test_stop_during_refresh_closes_on_worker_and_does_not_continue(self):
        self.clock.return_value = 1601.0
        stop = threading.Event()
        self.page.reload.side_effect = lambda **kwargs: stop.set()
        with mock.patch.object(A._send_context, "stop_event", stop, create=True):
            with self.assertRaises(A.SendStopped):
                A._get_prewarmed_page()
        self.page.wait_for_selector.assert_not_called()
        self.browser.close.assert_called_once()
        self.assertIsNone(A._prewarmed_browser)

    def test_stale_prewarm_refresh_reuses_existing_driver_when_healthy(self):
        self.clock.return_value = 1601.0
        with mock.patch.object(A, "sync_playwright", side_effect=AssertionError("no new browser needed")):
            self.assertTrue(A._prewarm_browser_impl())
        self.page.reload.assert_called_once()
        self.browser.close.assert_not_called()

    def test_failed_refresh_falls_back_to_normal_cold_start(self):
        self.clock.return_value = 1601.0
        self.page.reload.side_effect = RuntimeError("synthetic reload failure")
        new_page = mock.Mock()
        owner = mock.Mock()
        owner.__enter__ = mock.Mock(return_value=(mock.Mock(), mock.Mock(), mock.Mock(), new_page))
        owner.__exit__ = mock.Mock()
        with mock.patch.object(A, "load_config", return_value={"max_friends_per_run": 0}), \
             mock.patch.object(A.ledger, "get_selected", return_value=[{"entry_id": "synthetic", "display_name": "合成好友", "has_conversation": True}]), \
             mock.patch.object(A, "get_valid_state_path", return_value=Path("synthetic-unused")), \
             mock.patch.object(A, "open_browser", return_value=owner) as open_browser, \
             mock.patch.object(A, "_open_chat_page", return_value=True), \
             mock.patch.object(A, "_order_targets_by_list", side_effect=lambda page, rows: rows), \
             mock.patch.object(A, "_send_consumer"), mock.patch.object(A, "update_runtime"), \
             mock.patch.object(A, "KEEP_BROWSER_ALWAYS", False), mock.patch.object(A.time, "sleep"):
            A._run_send_impl(dry_run=True)
        open_browser.assert_called_once()
        self.browser.close.assert_called_once()
        owner.__exit__.assert_called_once()

    def test_refresh_and_close_run_on_the_playwright_worker_thread(self):
        self.clock.return_value = 1601.0
        observed = []
        self.page.reload.side_effect = lambda **kw: observed.append(threading.get_ident())
        self.page.wait_for_selector.side_effect = RuntimeError("synthetic list gone")
        self.browser.close.side_effect = lambda: observed.append(threading.get_ident())
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            with mock.patch.object(A, "_pw_executor", executor):
                self.assertIsNone(A._run_in_pw_thread(A._get_prewarmed_page))
        finally:
            executor.shutdown(wait=True)
        self.assertEqual(len(set(observed)), 1)
        self.assertNotEqual(observed[0], threading.get_ident())

    def test_defaults_and_explicit_idle_values_in_fresh_process(self):
        for always, explicit, expected in [(False, None, 600), (True, None, 600), (True, "0", 0), (False, "1200", 1200)]:
            env = os.environ.copy()
            env["KEEP_BROWSER_ALWAYS"] = "true" if always else "false"
            env.pop("BROWSER_MAX_IDLE_SECONDS", None)
            if explicit is not None:
                env["BROWSER_MAX_IDLE_SECONDS"] = explicit
            child = subprocess.run([sys.executable, "-B", "-c", "from core.automation import BROWSER_MAX_IDLE_SECONDS; print(BROWSER_MAX_IDLE_SECONDS)"],
                                   env=env, capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(int(child.stdout.strip()), expected)

    def test_default_prewarm_is_two_minutes_and_explicit_override_wins(self):
        for explicit, minute in [(None, "58"), ("7", "53")]:
            jobs = {}
            scheduler = mock.Mock()
            scheduler.get_job.return_value = None
            scheduler.add_job.side_effect = lambda func, trigger, **kwargs: jobs.update({kwargs["id"]: trigger})
            env = dict(os.environ)
            env.pop("PREWARM_LEAD_MINUTES", None)
            if explicit is not None:
                env["PREWARM_LEAD_MINUTES"] = explicit
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(S, "_scheduler", scheduler), \
                 mock.patch.object(S, "load_config", return_value={"auto_run_enabled": True, "schedule_time": "00:00", "schedule_harvest_day": "off"}), \
                 mock.patch.object(S, "_prewarm_func", mock.Mock()), mock.patch.object(S, "_session_check_func", None):
                S.apply_schedule()
            fields = {field.name: str(field) for field in jobs["daily_prewarm"].fields}
            self.assertEqual(fields["hour"], "23")
            self.assertEqual(fields["minute"], minute)


if __name__ == "__main__":
    unittest.main()
