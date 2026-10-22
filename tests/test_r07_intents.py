"""R07 durable send intent failure points; all accounts and page actions are synthetic."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import app_common
from core import automation as A, ledger as L, runtime as R
from core.send_day import business_now, daily_skip_code
from fastapi import HTTPException
from routers import pages
import tasks_service as T


class Crash(BaseException):
    pass


class IntentTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for module, values in ((L, {"DATA_DIR": root, "LEDGER_PATH": root / "ledger.json", "LEDGER_BAK_PATH": root / "ledger.json.bak"}),
                               (R, {"DATA_DIR": root, "RUNTIME_PATH": root / "runtime.json", "_rt_cache": {"mtime": None, "data": None}}),
                               (app_common, {"run_lock": threading.Lock()})):
            for name, value in values.items():
                patcher = mock.patch.object(module, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.entry = L._default_entry("合成好友 A")
        self.entry.update(selected=True, has_conversation=True)
        L._save([self.entry])
        self.result = {"ok": [], "ok_entries": [], "failed": [], "skipped": [], "deferred": []}
        self.page = mock.Mock()
        self.scope = mock.Mock()
        self.presses = []
        self.page.keyboard.press.side_effect = self.presses.append
        for name, value in {"detect_rate_limit": False, "_bind_delivery_scope": self.scope,
                            "_delivery_scope_current": True, "_delivery_snapshot": {"messages": [], "failures": []},
                            "_wait_text_in_box": True, "_confirm_delivery": (True, "accepted")}.items():
            patcher = mock.patch.object(A, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(A.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(A, "send_to_contact", side_effect=lambda p, n, m, d: A._send_message(p, m, d, target_name=n))
        patcher.start()
        self.addCleanup(patcher.stop)

    def send(self):
        A._send_consumer(self.page, self.entry, "合成消息", False, self.result)

    def test_intent_is_persisted_once_before_enter_and_success_only_after_result_write(self):
        original = L.begin_send_attempt
        with mock.patch.object(L, "begin_send_attempt", wraps=original) as begin:
            def press(key):
                if key == "Enter":
                    row = L.load_ledger()[0]
                    self.assertEqual(row["send_attempt"]["status"], "sending")
                    self.assertEqual(row["send_attempt"]["entry_id"], self.entry["entry_id"])
                    self.assertEqual(row["send_attempt"]["business_date"], business_now().date().isoformat())
                    self.assertFalse(self.result["ok"])
                self.presses.append(key)
            self.page.keyboard.press.side_effect = press
            self.send()
        self.assertEqual(begin.call_count, 1)
        self.assertEqual(self.presses.count("Enter"), 1)
        self.assertEqual(L.load_ledger()[0]["send_attempt"]["status"], "accepted")
        self.assertEqual(self.result["ok"], [self.entry["display_name"]])

    def test_intent_write_failure_never_calls_enter(self):
        with mock.patch.object(L, "begin_send_attempt", side_effect=OSError("synthetic disk full")):
            self.send()
        self.assertNotIn("Enter", self.presses)
        self.assertFalse(self.result["ok"])
        self.assertFalse(L.load_ledger()[0].get("send_attempt"))

    def test_atomic_file_write_failure_never_calls_enter_or_erases_original_ledger(self):
        with mock.patch.object(L, "atomic_write_text", side_effect=OSError("synthetic fsync failure")):
            self.send()
        self.assertNotIn("Enter", self.presses)
        restored = L.load_ledger()
        self.assertEqual(restored[0]["entry_id"], self.entry["entry_id"])
        self.assertFalse(restored[0].get("send_attempt"))
        self.assertTrue(self.result["intent_write_failed"])

    def test_stop_received_during_intent_write_prevents_enter_and_keeps_review(self):
        stop = threading.Event()
        original = L.begin_send_attempt
        def persist_then_stop(*args, **kwargs):
            attempt = original(*args, **kwargs)
            stop.set()
            return attempt
        with mock.patch.object(A._send_context, "stop_event", stop, create=True), \
             mock.patch.object(L, "begin_send_attempt", side_effect=persist_then_stop):
            self.send()
        self.assertNotIn("Enter", self.presses)
        self.assertTrue(self.result["stopped"])
        self.assertTrue(self.result["delivery_unconfirmed"])
        self.assertEqual(L.load_ledger()[0]["send_attempt"]["status"], "unconfirmed")

    def test_stop_before_first_target_does_not_open_browser_and_preserves_safe_pending(self):
        stop = threading.Event()
        stop.set()
        with mock.patch.object(A._send_context, "stop_event", stop, create=True), \
             mock.patch.object(A, "get_valid_state_path", return_value=Path("synthetic-unused")), \
             mock.patch.object(A, "_get_prewarmed_page", side_effect=AssertionError("stop before browser")), \
             mock.patch.object(A, "load_config", return_value={"max_friends_per_run": 0}):
            result = A._run_send_impl()
        self.assertTrue(result["stopped"])
        self.assertEqual(result["deferred"][0]["entry_id"], self.entry["entry_id"])
        self.assertEqual(T._collect_retry_entries(result), [{"entry_id": self.entry["entry_id"], "name": self.entry["display_name"]}])

    def test_restart_in_fresh_process_recovers_intent_without_resending(self):
        attempt = L.begin_send_attempt(self.entry["entry_id"])
        env = os.environ.copy()
        env.update(DATA_DIR=str(L.DATA_DIR), ENV_FILE_PATH=str(L.DATA_DIR / "missing.env"),
                   INSTANCE_LOCK_PATH=str(L.DATA_DIR / "synthetic.pid"))
        code = """from core import ledger, automation
assert ledger.recover_interrupted_attempts() == 1
entry = ledger.load_ledger()[0]
assert entry['send_attempt']['status'] == 'unconfirmed'
assert not automation.compute_pending(cfg={})
print(entry['send_attempt']['attempt_id'])
"""
        process = subprocess.run([sys.executable, "-B", "-c", code], env=env,
                                 text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(process.stdout.strip(), attempt)

    def test_stopped_run_never_registers_automatic_retry(self):
        result = {"stopped": True, "deferred": [{"entry_id": self.entry["entry_id"], "name": self.entry["display_name"], "retryable": True}]}
        with mock.patch.object(T.scheduler, "background_tasks_enabled", return_value=True), \
             mock.patch.object(T.scheduler, "schedule_retry") as schedule, \
             mock.patch.object(T.scheduler, "cancel_retry"):
            T._schedule_retry(result)
        schedule.assert_not_called()
        self.assertEqual(R.load_runtime()["retry_status"], "manual")

    def test_lifespan_recovers_before_scheduling_and_clears_abandoned_stopping(self):
        import app
        L.begin_send_attempt(self.entry["entry_id"])
        R.update_runtime(running=True, stopping=True)
        def verify_configure(*args, **kwargs):
            self.assertEqual(L.load_ledger()[0]["send_attempt"]["status"], "unconfirmed")
            self.assertFalse(R.load_runtime()["running"])
            self.assertFalse(R.load_runtime()["stopping"])
        async def run_lifespan():
            async with app.lifespan(app.app):
                pass
        with mock.patch.object(app, "_check_auth_config"), mock.patch.object(app, "_acquire_instance_lock"), \
             mock.patch.object(app, "_clear_instance_metadata"), mock.patch.object(app, "migrate_legacy_email_config"), \
             mock.patch.object(app.scheduler, "background_tasks_enabled", return_value=True), \
             mock.patch.object(app.scheduler, "configure", side_effect=verify_configure) as configure, \
             mock.patch.object(app.scheduler, "shutdown"), mock.patch.object(app, "_start_watchdog"), \
             mock.patch.object(app, "_check_startup_catch_up", new=mock.AsyncMock()), \
             mock.patch.object(app.qr_credentials_router, "shutdown_qr_login"):
            asyncio.run(run_lifespan())
        configure.assert_called_once()

    def test_dry_run_never_creates_or_rewrites_intent(self):
        original = L.LEDGER_PATH.read_bytes()
        with mock.patch.object(L, "begin_send_attempt", side_effect=AssertionError("dry run has no intent")):
            A._send_consumer(self.page, self.entry, "合成演练消息", True, self.result)
        self.assertEqual(L.LEDGER_PATH.read_bytes(), original)
        self.assertNotIn("Enter", self.presses)

    def test_all_five_crash_points_preserve_safe_recovery(self):
        for stage in ("before_intent", "after_intent", "after_enter", "after_confirmation", "before_result"):
            with self.subTest(stage=stage):
                L._save([self.entry.copy()])
                self.presses.clear()
                self.result = {"ok": [], "ok_entries": [], "failed": [], "skipped": [], "deferred": []}
                if stage == "before_intent":
                    patcher = mock.patch.object(L, "begin_send_attempt", side_effect=Crash())
                elif stage == "after_intent":
                    original = A._persist_current_intent
                    def interrupted():
                        original()
                        raise Crash()
                    patcher = mock.patch.object(A, "_persist_current_intent", side_effect=interrupted)
                elif stage == "after_enter":
                    def press(key):
                        self.presses.append(key)
                        if key == "Enter":
                            raise Crash()
                    patcher = mock.patch.object(self.page.keyboard, "press", side_effect=press)
                elif stage == "after_confirmation":
                    patcher = mock.patch.object(A, "_confirm_delivery", side_effect=Crash())
                else:
                    patcher = mock.patch.object(L, "update_send_result", side_effect=Crash())
                with patcher, self.assertRaises(Crash):
                    self.send()
                self.assertFalse(self.result["ok"])
                recovered = L.recover_interrupted_attempts()
                row = L.load_ledger()[0]
                if stage == "before_intent":
                    self.assertEqual(recovered, 0)
                    self.assertIsNone(daily_skip_code(row))
                else:
                    self.assertEqual(recovered, 1)
                    self.assertEqual(row["send_attempt"]["status"], "unconfirmed")
                    self.assertEqual(daily_skip_code(row), "unconfirmed_today")
                    self.assertEqual(daily_skip_code(row, now=business_now() + timedelta(days=1)), "unconfirmed_today")
                    self.assertEqual(A.compute_pending(cfg={}), [])
                    self.assertEqual(L.recover_interrupted_attempts(), 0)

    def test_result_write_failure_cannot_report_success_or_queue_same_entry(self):
        with mock.patch.object(L, "update_send_result", side_effect=OSError("synthetic result write failed")):
            self.send()
        self.assertEqual(self.presses.count("Enter"), 1)
        self.assertFalse(self.result["ok"])
        self.assertTrue(self.result["delivery_unconfirmed"])
        self.assertEqual(self.result["skipped"][0]["entry_id"], self.entry["entry_id"])
        self.assertEqual(T._collect_retry_entries(self.result), [])
        self.assertEqual(L.load_ledger()[0]["send_attempt"]["status"], "sending")

    def test_stale_attempt_result_cannot_override_new_attempt(self):
        old = L.begin_send_attempt(self.entry["entry_id"])
        with self.assertRaises(L.IdentityConflict):
            L.begin_send_attempt(self.entry["entry_id"])
        L.recover_interrupted_attempts()
        new = L.begin_send_attempt(self.entry["entry_id"], force=True)
        with self.assertRaises(L.IdentityConflict):
            L.update_send_result(self.entry["display_name"], True, entry_id=self.entry["entry_id"], attempt_id=old)
        self.assertEqual(L.load_ledger()[0]["send_attempt"]["attempt_id"], new)
        self.assertIsNone(L.load_ledger()[0]["last_sent_at"])

    def test_final_deferred_filter_excludes_unresolved_attempt(self):
        with mock.patch.object(A, "load_config", return_value={"max_friends_per_run": 0}), \
             mock.patch.object(A, "get_valid_state_path", return_value=Path("synthetic-unused")), \
             mock.patch.object(A, "_get_prewarmed_page", return_value={"p": None, "browser": None, "context": None, "page": self.page}), \
             mock.patch.object(A, "check_login", return_value=(True, "synthetic")), \
             mock.patch.object(A, "_order_targets_by_list", side_effect=lambda page, targets: targets), \
             mock.patch.object(A, "_close_prewarmed"), mock.patch.object(A, "_keep_prewarmed_alive", return_value=True):
            def interrupted(*args):
                L.begin_send_attempt(self.entry["entry_id"])
                raise RuntimeError("synthetic unexpected interruption")
            with mock.patch.object(A, "_send_consumer", side_effect=interrupted):
                result = A._run_send_impl()
        self.assertEqual(result["deferred"], [])
        self.assertEqual(T._collect_retry_entries(result), [])
        self.assertTrue(result["delivery_unconfirmed"])

    def test_timeout_keeps_running_and_lock_until_real_worker_exit(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(5)
            finished.set()
            return {"ok": [], "failed": [], "skipped": [], "deferred": [], "dry_run": True}
        with mock.patch.object(A, "_pw_executor", executor), \
             mock.patch.object(A, "_run_send_impl", side_effect=blocked), \
             mock.patch.object(A, "SEND_HARD_TIMEOUT_SECONDS", 0.02), \
             mock.patch.object(T, "notify_after_run"), \
             mock.patch.object(pages, "_check_auth"), \
             mock.patch.object(pages, "get_valid_state_path", return_value=None):
            try:
                T._start_run(True)
                self.assertTrue(entered.wait(2))
                deadline = threading.Event()
                for _ in range(100):
                    if R.load_runtime().get("stopping"):
                        break
                    deadline.wait(0.01)
                status = pages.api_status(token="synthetic")
                self.assertTrue(status["stopping"])
                self.assertTrue(status["running"])
                self.assertTrue(app_common.run_lock.locked())
                with self.assertRaises(HTTPException) as blocked_run:
                    T._start_run(True)
                self.assertEqual(blocked_run.exception.status_code, 409)
                self.assertFalse(finished.is_set())
            finally:
                release.set()
                executor.shutdown(wait=True)
                for _ in range(100):
                    if not app_common.run_lock.locked():
                        break
                    threading.Event().wait(0.01)
        self.assertTrue(finished.is_set())
        self.assertFalse(app_common.run_lock.locked())
        self.assertFalse(R.load_runtime()["running"])
        self.assertFalse(R.load_runtime()["stopping"])


if __name__ == "__main__":
    unittest.main()
