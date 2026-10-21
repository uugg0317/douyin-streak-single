"""R09 configuration API cancellation and late automatic callbacks, no account activity."""
import asyncio
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
import app_common
from core import config as C, ledger as L, runtime as R, scheduler as S
from routers import config as config_routes
import tasks_service as T


class Jobs:
    def __init__(self):
        self.jobs = {}

    def add_executor(self, executor, *, alias):
        pass

    def add_job(self, func, trigger, **kwargs):
        job = SimpleNamespace(func=func, trigger=trigger, kwargs=kwargs.get("kwargs", {}),
                              next_run_time=getattr(trigger, "run_date", None))
        self.jobs[kwargs["id"]] = job
        return job

    def get_job(self, key):
        return self.jobs.get(key)

    def remove_job(self, key):
        self.jobs.pop(key, None)


class AutoCancelTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.jobs = Jobs()
        for module, fields in [(C, {"DATA_DIR": root, "CONFIG_PATH": root / "config.json"}),
                               (L, {"DATA_DIR": root, "LEDGER_PATH": root / "ledger.json", "LEDGER_BAK_PATH": root / "ledger.json.bak"}),
                               (R, {"DATA_DIR": root, "RUNTIME_PATH": root / "runtime.json", "_rt_cache": {"mtime": None, "data": None}}),
                               (S, {"_scheduler": self.jobs, "_run_func": lambda: T._start_run(False),
                                    "_prewarm_func": None, "_harvest_func": None, "_session_check_func": None}),
                               (app_common, {"run_lock": threading.Lock()})]:
            for name, value in fields.items():
                patcher = mock.patch.object(module, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        patcher = mock.patch.object(S, "background_tasks_enabled", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(config_routes, "_check_auth")
        patcher.start()
        self.addCleanup(patcher.stop)
        # Jitter is an existing persisted setting; the management API deliberately
        # does not unlock it. Seed that historical scenario with synthetic data.
        C.CONFIG_PATH.write_text(json.dumps({"auto_run_enabled": True, "jitter_minutes": 10}), encoding="utf-8")
        self.entry = L._default_entry("合成待发好友")
        self.entry.update(selected=True, has_conversation=True)
        L._save([self.entry])
        self.app = FastAPI()
        self.app.include_router(config_routes.router)

    def save_via_api(self, enabled):
        async def invoke():
            messages = []
            raw = json.dumps({"config": {"auto_run_enabled": enabled}}).encode()
            async def receive():
                return {"type": "http.request", "body": raw, "more_body": False}
            async def send(message):
                messages.append(message)
            await self.app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                            "method": "POST", "scheme": "http", "path": "/api/config", "raw_path": b"/api/config",
                            "query_string": b"", "root_path": "", "server": ("127.0.0.1", 8000),
                            "client": ("127.0.0.1", 1), "headers": [(b"content-type", b"application/json")]}, receive, send)
            return next(m["status"] for m in messages if m["type"] == "http.response.start")
        self.assertEqual(asyncio.run(invoke()), 200)

    def queue_both(self):
        S._daily_job()
        T._schedule_retry({"deferred": [{"entry_id": self.entry["entry_id"], "name": self.entry["display_name"], "retryable": True}]})
        return self.jobs.jobs["daily_send_jitter"], self.jobs.jobs["retry_send"]

    def test_config_api_cancels_queued_jitter_retry_and_preserves_manual_contacts(self):
        self.queue_both()
        before = R.load_runtime()
        self.save_via_api(False)
        self.assertIsNone(self.jobs.get_job("daily_send_jitter"))
        self.assertIsNone(self.jobs.get_job("retry_send"))
        saved = R.load_runtime()
        self.assertEqual(saved["retry_status"], "manual")
        self.assertEqual(saved["pending_retry_entries"], before["pending_retry_entries"])
        self.assertNotEqual(saved["retry_queue_version"], before["retry_queue_version"])

    def test_already_dispatched_old_callbacks_are_invalid_after_disable_and_reenable(self):
        jitter, retry = self.queue_both()
        self.save_via_api(False)
        self.save_via_api(True)
        with mock.patch.object(T, "_begin_run_locked", side_effect=lambda *a, **k: T._release_lock()) as begin:
            jitter.func(**jitter.kwargs)
            retry.func(**retry.kwargs)
        begin.assert_not_called()
        self.assertFalse(app_common.run_lock.locked())

    def test_daily_automatic_entry_rechecks_switch_after_run_lock_is_acquired(self):
        original = T._acquire_lock
        def acquire(**kwargs):
            result = original(**kwargs)
            C.save_config({"auto_run_enabled": False})
            return result
        with mock.patch.object(T, "_acquire_lock", side_effect=acquire), \
             mock.patch.object(T, "_begin_run_locked", side_effect=lambda *a, **k: T._release_lock()) as begin:
            S._fire_run()
        begin.assert_not_called()
        self.assertFalse(app_common.run_lock.locked())

    def test_retry_automatic_entry_rechecks_switch_even_without_api_cancellation(self):
        _, job = self.queue_both()
        C.save_config({"auto_run_enabled": False})
        with mock.patch.object(T, "_begin_run_locked", side_effect=lambda *a, **k: T._release_lock()) as begin:
            job.func(**job.kwargs)
        begin.assert_not_called()
        self.assertEqual(R.load_runtime()["retry_status"], "manual")

    def test_completed_running_job_cannot_rearm_retry_after_disable(self):
        self.save_via_api(False)
        result = {"deferred": [{"entry_id": self.entry["entry_id"], "name": self.entry["display_name"], "retryable": True}]}
        T._schedule_retry(result)
        self.assertIsNone(self.jobs.get_job("retry_send"))
        self.assertEqual(result["retry_status"], "manual")
        self.assertEqual(len(R.load_runtime()["pending_retry_entries"]), 1)

    def test_manual_run_and_manual_pending_remain_available_when_disabled(self):
        self.queue_both()
        self.save_via_api(False)
        with mock.patch.object(T, "_begin_run_locked", side_effect=lambda *a, **k: T._release_lock()) as begin:
            T._start_run(False)
            T._start_pending_retry()
        self.assertEqual(begin.call_count, 2)

    def test_disabling_while_running_does_not_release_lock_or_request_force_stop(self):
        self.queue_both()
        app_common.run_lock.acquire()
        try:
            with mock.patch.object(T.automation, "request_send_stop") as stop:
                self.save_via_api(False)
            self.assertTrue(app_common.run_lock.locked())
            stop.assert_not_called()
        finally:
            app_common.run_lock.release()

    def test_real_dispatched_retry_is_revoked_by_api_before_business_entry(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(timezone=S.TZ)
        scheduler.start()
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        original = T._scheduled_pending_retry
        def delayed(**kwargs):
            entered.set()
            release.wait(3)
            try:
                original(**kwargs)
            finally:
                done.set()
        now = datetime.now(timezone(timedelta(hours=8)))
        try:
            with mock.patch.object(S, "_scheduler", scheduler), \
                 mock.patch.object(T, "_scheduled_pending_retry", side_effect=delayed), \
                 mock.patch.object(T, "_begin_run_locked", side_effect=lambda *a, **k: T._release_lock()) as begin:
                R.update_runtime(pending_retry_entries=[{"entry_id": self.entry["entry_id"], "name": self.entry["display_name"]}])
                self.assertTrue(T._register_retry_locked(now + timedelta(milliseconds=100), "dispatched-before-disable", now.date().isoformat()))
                self.assertTrue(entered.wait(2))
                self.assertTrue(S.retry_is_dispatching("dispatched-before-disable"))
                self.save_via_api(False)
                self.assertFalse(S.retry_is_dispatching("dispatched-before-disable"))
                self.save_via_api(True)
                release.set()
                self.assertTrue(done.wait(2))
                begin.assert_not_called()
                self.assertEqual(R.load_runtime()["retry_status"], "manual")
                self.assertEqual(len(R.load_runtime()["pending_retry_entries"]), 1)
        finally:
            release.set()
            scheduler.shutdown(wait=True)

    def test_catchup_start_rechecks_disabled_switch_without_changing_manual_contract(self):
        C.save_config({"auto_run_enabled": False})
        with mock.patch.object(T, "_begin_run_locked", side_effect=lambda *a, **k: T._release_lock()) as begin:
            with self.assertRaises(Exception) as refused:
                T._start_run(False, require_qr_idle=True)
        self.assertEqual(refused.exception.status_code, 409)
        begin.assert_not_called()
        self.assertFalse(app_common.run_lock.locked())


if __name__ == "__main__":
    unittest.main()
