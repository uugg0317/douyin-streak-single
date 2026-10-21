"""R08 persisted retry plans and real job agreement, using synthetic local state."""
from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import app_common
from core import ledger as L, runtime as R, scheduler as S
import tasks_service as T
from routers import pages


class FakeScheduler:
    def __init__(self):
        self.jobs = {}
        self.fail_registration = False

    def add_job(self, func, trigger, **kwargs):
        if self.fail_registration:
            raise RuntimeError("synthetic registration failure")
        job = SimpleNamespace(func=func, trigger=trigger, next_run_time=trigger.run_date,
                              kwargs=kwargs.get("kwargs", {}))
        self.jobs[kwargs["id"]] = job
        return job

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def add_executor(self, executor, *, alias):
        pass

    def remove_job(self, job_id):
        self.jobs.pop(job_id, None)


class RetryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        self.scheduler = FakeScheduler()
        patches = [(L, {"DATA_DIR": root, "LEDGER_PATH": root / "ledger.json", "LEDGER_BAK_PATH": root / "ledger.json.bak"}),
                   (R, {"DATA_DIR": root, "RUNTIME_PATH": root / "runtime.json", "_rt_cache": {"mtime": None, "data": None}}),
                   (app_common, {"run_lock": threading.Lock()}), (S, {"_scheduler": self.scheduler})]
        for module, values in patches:
            for name, value in values.items():
                patcher = mock.patch.object(module, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        for module, name, value in [(T, "business_now", lambda: self.now),
                                    (S, "background_tasks_enabled", lambda: True),
                                    (T, "load_config", lambda: {"auto_run_enabled": True})]:
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.a = L._default_entry("合成好友 A")
        self.a.update(selected=True, has_conversation=True)
        L._save([self.a])

    def outcome(self):
        return {"deferred": [{"entry_id": self.a["entry_id"], "name": self.a["display_name"], "retryable": True}]}

    def plan(self):
        result = self.outcome()
        T._schedule_retry(result)
        return R.load_runtime()

    def test_schedule_persists_aware_due_identity_and_matching_job(self):
        rt = self.plan()
        self.assertEqual(rt["retry_status"], "scheduled")
        due = datetime.fromisoformat(rt["retry_due_at"])
        self.assertIsNotNone(due.utcoffset())
        self.assertEqual((due - self.now).total_seconds(), 2700)
        self.assertEqual(rt["retry_business_date"], self.now.date().isoformat())
        self.assertTrue(rt["retry_queue_version"])
        job = self.scheduler.get_job("retry_send")
        self.assertEqual(job.next_run_time, due)
        self.assertEqual(job.kwargs["queue_version"], rt["retry_queue_version"])

    def test_restart_restores_original_due_and_current_queue(self):
        rt = self.plan()
        self.scheduler.jobs.clear()
        self.now += timedelta(minutes=5)
        T.restore_retry_plan()
        saved = R.load_runtime()
        self.assertEqual(saved["retry_status"], "scheduled")
        self.assertEqual(saved["retry_due_at"], rt["retry_due_at"])
        self.assertEqual(self.scheduler.get_job("retry_send").next_run_time.isoformat(), rt["retry_due_at"])

    def test_expired_restart_preserves_manual_queue_without_job(self):
        rt = self.plan()
        self.scheduler.jobs.clear()
        self.now += timedelta(hours=1)
        T.restore_retry_plan()
        saved = R.load_runtime()
        self.assertEqual(saved["retry_status"], "manual")
        self.assertEqual(saved["pending_retry_entries"], rt["pending_retry_entries"])
        self.assertIsNone(self.scheduler.get_job("retry_send"))
        self.assertTrue(saved["retry_reason"])

    def test_restore_filters_sent_unconfirmed_deselected_and_ambiguous_identities(self):
        rt = self.plan()
        entries = [self.a]
        for name, fields in [("已发", {"last_sent_at": self.now.isoformat()}),
                             ("待核对", {"send_attempt": {"status": "unconfirmed"}}),
                             ("发送中", {"send_attempt": {"status": "sending"}}),
                             ("已取消", {"selected": False}),
                             ("身份冲突", {"identity_conflict": True})]:
            e = L._default_entry(name)
            e.update(selected=True, has_conversation=True)
            e.update(fields)
            entries.append(e)
        L._save(entries)
        R.update_runtime(pending_retry_entries=[{"entry_id": e["entry_id"], "name": e["display_name"]} for e in entries])
        self.scheduler.jobs.clear()
        T.restore_retry_plan()
        self.assertEqual(R.load_runtime()["pending_retry_entries"], rt["pending_retry_entries"])
        self.assertEqual(R.load_runtime()["retry_status"], "scheduled")

    def test_registration_failure_keeps_manual_plan(self):
        self.scheduler.fail_registration = True
        rt = self.plan()
        self.assertEqual(rt["retry_status"], "manual")
        self.assertEqual(len(rt["pending_retry_entries"]), 1)
        self.assertIsNone(self.scheduler.get_job("retry_send"))

    def test_lock_conflict_keeps_queue_and_cannot_claim_scheduled(self):
        before = self.plan()
        job = self.scheduler.jobs.pop("retry_send")
        app_common.run_lock.acquire()
        try:
            job.func(**job.kwargs)
        finally:
            app_common.run_lock.release()
        rt = R.load_runtime()
        self.assertEqual(rt["retry_status"], "manual")
        self.assertEqual(rt["pending_retry_entries"], before["pending_retry_entries"])

    def test_missing_job_is_corrected_by_status_endpoint(self):
        before = self.plan()
        self.scheduler.jobs.clear()
        with mock.patch.object(pages, "_check_auth"), mock.patch.object(pages, "get_valid_state_path", return_value=None):
            status = pages.api_status(token="synthetic")
        self.assertEqual(status["retry_status"], "manual")
        self.assertEqual(status["pending_retry_entries"], before["pending_retry_entries"])
        self.assertEqual(R.load_runtime()["retry_status"], "manual")

    def test_stale_callback_never_consumes_new_queue(self):
        before = self.plan()
        old_job = self.scheduler.get_job("retry_send")
        R.update_runtime(retry_date=None)
        new = self.plan()
        self.assertNotEqual(new["retry_queue_version"], before["retry_queue_version"])
        with mock.patch.object(T, "_begin_run_locked") as begin:
            old_job.func(**old_job.kwargs)
        begin.assert_not_called()
        self.assertEqual(R.load_runtime()["pending_retry_entries"], new["pending_retry_entries"])
        self.assertEqual(R.load_runtime()["retry_queue_version"], new["retry_queue_version"])
        self.assertEqual(R.load_runtime()["retry_status"], "scheduled")

    def test_registration_success_then_metadata_write_failure_cancels_orphan_job(self):
        original = T.update_runtime
        def failing(**fields):
            if fields.get("retry_status") == "scheduled":
                raise OSError("synthetic final metadata failure")
            return original(**fields)
        with mock.patch.object(T, "update_runtime", side_effect=failing):
            rt = self.plan()
        self.assertIsNone(self.scheduler.get_job("retry_send"))
        self.assertEqual(len(rt["pending_retry_entries"]), 1)
        self.assertEqual(rt["retry_status"], "manual")

    def test_legacy_plan_without_due_is_manual_and_stopped_run_is_not_restored(self):
        R.update_runtime(pending_retry_entries=self.outcome()["deferred"], retry_status="scheduled")
        T.restore_retry_plan()
        self.assertEqual(R.load_runtime()["retry_status"], "manual")
        result = self.outcome()
        result["stopped"] = True
        T._schedule_retry(result)
        T.restore_retry_plan()
        self.assertEqual(R.load_runtime()["retry_status"], "manual")
        self.assertIsNone(self.scheduler.get_job("retry_send"))

    def test_prepared_plan_recovers_after_crash_before_registration(self):
        rt = self.plan()
        self.scheduler.jobs.clear()
        R.update_runtime(retry_status="scheduling")
        T.restore_retry_plan()
        self.assertEqual(R.load_runtime()["retry_status"], "scheduled")
        self.assertTrue(S.retry_job_matches(rt["retry_queue_version"], datetime.fromisoformat(rt["retry_due_at"])))

    def test_prepare_write_failure_leaves_saved_manual_queue_and_no_job(self):
        original = T.update_runtime
        def fail_prepare(**fields):
            if fields.get("retry_status") == "scheduling":
                raise OSError("synthetic prepare failure")
            return original(**fields)
        with mock.patch.object(T, "update_runtime", side_effect=fail_prepare), self.assertRaises(OSError):
            self.plan()
        self.assertEqual(R.load_runtime()["retry_status"], "manual")
        self.assertEqual(len(R.load_runtime()["pending_retry_entries"]), 1)
        self.assertIsNone(self.scheduler.get_job("retry_send"))

    def test_restore_while_run_lock_is_held_preserves_manual_queue(self):
        before = self.plan()
        self.scheduler.jobs.clear()
        app_common.run_lock.acquire()
        try:
            T.restore_retry_plan()
        finally:
            app_common.run_lock.release()
        self.assertEqual(R.load_runtime()["retry_status"], "manual")
        self.assertEqual(R.load_runtime()["pending_retry_entries"], before["pending_retry_entries"])
        self.assertIsNone(self.scheduler.get_job("retry_send"))

    def test_boolean_registration_success_without_real_job_stays_manual(self):
        with mock.patch.object(S, "schedule_retry", return_value=True):
            rt = self.plan()
        self.assertEqual(rt["retry_status"], "manual")
        self.assertEqual(len(rt["pending_retry_entries"]), 1)

    def test_real_paused_scheduler_uses_exact_persisted_due_and_version(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(timezone=S.TZ)
        scheduler.start(paused=True)
        try:
            with mock.patch.object(S, "_scheduler", scheduler):
                rt = self.plan()
                job = scheduler.get_job("retry_send")
                self.assertEqual(rt["retry_status"], "scheduled")
                self.assertEqual(job.next_run_time.isoformat(), rt["retry_due_at"])
                self.assertEqual(job.kwargs, {"queue_version": rt["retry_queue_version"]})
                self.assertTrue(S.retry_job_matches(rt["retry_queue_version"], job.next_run_time))
                self.assertFalse(S.retry_job_matches("obsolete", job.next_run_time))
        finally:
            scheduler.shutdown(wait=True)

    def test_automatic_callback_filters_current_identity_before_starting(self):
        rt = self.plan()
        job = self.scheduler.jobs.pop("retry_send")
        seen = []
        def begin(*args, **kwargs):
            seen.append(kwargs["only_ids"])
            T._release_lock()
        with mock.patch.object(T, "_begin_run_locked", side_effect=begin):
            job.func(**job.kwargs)
        self.assertEqual(seen, [[self.a["entry_id"]]])
        self.assertFalse(app_common.run_lock.locked())

    def test_real_scheduler_dispatch_window_cannot_be_cancelled_by_status_poll(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(timezone=S.TZ)
        scheduler.start()
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        original = T._scheduled_pending_retry
        started = []
        def blocked_callback(**kwargs):
            entered.set()
            release.wait(3)
            try:
                original(**kwargs)
            finally:
                completed.set()
        def begin(*args, **kwargs):
            started.append(kwargs["only_ids"])
            T._release_lock()
        self.now = datetime.now(timezone(timedelta(hours=8)))
        try:
            with mock.patch.object(S, "_scheduler", scheduler), \
                 mock.patch.object(T, "_scheduled_pending_retry", side_effect=blocked_callback), \
                 mock.patch.object(T, "_begin_run_locked", side_effect=begin):
                R.update_runtime(pending_retry_entries=self.outcome()["deferred"])
                self.assertTrue(T._register_retry_locked(self.now + timedelta(milliseconds=100), "real-dispatch", self.now.date().isoformat()))
                self.assertTrue(entered.wait(2))
                for _ in range(100):
                    if scheduler.get_job("retry_send") is None:
                        break
                    threading.Event().wait(0.005)
                self.assertIsNone(scheduler.get_job("retry_send"))
                view = T.retry_plan_view()
                self.assertEqual(view["retry_status"], "dispatching")
                self.assertEqual(R.load_runtime()["retry_status"], "scheduled")
                release.set()
                self.assertTrue(completed.wait(2))
                self.assertEqual(started, [[self.a["entry_id"]]])
        finally:
            release.set()
            scheduler.shutdown(wait=True)

    def test_cancelled_old_completion_cannot_clear_new_version_with_same_due(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(timezone=S.TZ)
        scheduler.start()
        old_entered, release_old, old_done, new_done = [threading.Event() for _ in range(4)]
        original = T._scheduled_pending_retry
        started = []
        def callback(**kwargs):
            old = kwargs["queue_version"] == "old-same-due"
            if old:
                old_entered.set()
                release_old.wait(3)
            try:
                original(**kwargs)
            finally:
                (old_done if old else new_done).set()
        def begin(*args, **kwargs):
            started.append(kwargs["only_ids"])
            T._release_lock()
        self.now = datetime.now(timezone(timedelta(hours=8)))
        due = self.now + timedelta(milliseconds=100)
        try:
            with mock.patch.object(S, "_scheduler", scheduler), \
                 mock.patch.object(T, "_scheduled_pending_retry", side_effect=callback), \
                 mock.patch.object(T, "_begin_run_locked", side_effect=begin):
                R.update_runtime(pending_retry_entries=self.outcome()["deferred"])
                self.assertTrue(T._register_retry_locked(due, "old-same-due", self.now.date().isoformat()))
                self.assertTrue(old_entered.wait(2))
                scheduler.pause()
                S.cancel_retry()
                self.assertFalse(S.retry_is_dispatching("old-same-due", due))
                self.assertTrue(T._register_retry_locked(due, "new-same-due", self.now.date().isoformat()))
                release_old.set()
                self.assertTrue(old_done.wait(2))
                # Wait for the executor completion callback, not just the user
                # callback's final line, before submitting the replacement job.
                executor = scheduler._lookup_executor("retry")
                for _ in range(100):
                    with executor._lock:
                        active = executor._instances.get("retry_send", 0)
                    if not active:
                        break
                    threading.Event().wait(0.005)
                self.assertEqual(started, [])
                self.assertTrue(S.retry_job_matches("new-same-due", due))
                scheduler.resume()
                self.assertTrue(new_done.wait(2))
                self.assertEqual(started, [[self.a["entry_id"]]])
        finally:
            release_old.set()
            scheduler.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
