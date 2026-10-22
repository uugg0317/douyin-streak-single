"""R16 evidence-only daily totals, explicit configuration locks and dormant stub jobs."""
import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fastapi import FastAPI
from core import config as C, ledger as L, scheduler as S
from routers import config as config_routes, pages


class StatusConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.now = datetime(2026, 10, 18, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        self.runtime = {}
        for module, fields in [(C, {"DATA_DIR": root, "CONFIG_PATH": root / "config.json"}),
                               (L, {"DATA_DIR": root, "LEDGER_PATH": root / "ledger.json", "LEDGER_BAK_PATH": root / "ledger.json.bak"})]:
            for key, value in fields.items():
                patcher = mock.patch.object(module, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        for module, key, kwargs in [(pages, "_check_auth", {}), (config_routes, "_check_auth", {}),
                                    (pages, "get_valid_state_path", {"return_value": None}),
                                    (pages, "retry_plan_view", {"side_effect": lambda: self.runtime}),
                                    (S, "background_tasks_enabled", {"return_value": False}),
                                    (S, "next_run_time", {"return_value": None}), (S, "next_harvest_time", {"return_value": None})]:
            patcher = mock.patch.object(module, key, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        C.CONFIG_PATH.write_text(json.dumps(dict(C.DEFAULT_CONFIG)), encoding="utf-8")
        L._save([])
        self.app = FastAPI()
        self.app.include_router(config_routes.router)
        self.app.include_router(pages.router)

    def request(self, method, path, body=None):
        async def invoke():
            messages = []
            async def receive():
                return {"type": "http.request", "body": json.dumps(body).encode() if body is not None else b"", "more_body": False}
            async def send(message):
                messages.append(message)
            await self.app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                            "query_string": b"", "root_path": "", "server": ("127.0.0.1", 8000),
                            "client": ("127.0.0.1", 1), "headers": [(b"content-type", b"application/json")]}, receive, send)
            status = next(m["status"] for m in messages if m["type"] == "http.response.start")
            data = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
            return status, data
        return asyncio.run(invoke())

    def entry(self, name="合成好友", **fields):
        e = L._default_entry(name)
        e.update(selected=True, has_conversation=True)
        e.update(fields)
        return e

    def summary(self, entries):
        L._save(entries)
        with mock.patch.object(pages, "business_now", return_value=self.now, create=True):
            status, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(payload["background_tasks_enabled"])
        self.assertIn("today_summary", payload)
        return payload["today_summary"]

    def test_no_records_full_failure_and_dry_runs_never_become_acceptance(self):
        for result in ({}, {"last_run": {"dry_run": False, "failed": [{"name": "合成好友"}]}},
                       {"last_run": {"dry_run": True, "ok": ["合成好友"]}},
                       {"last_run": {"dry_run": False, "ok": ["合成好友"]}}):
            self.runtime = result
            summary = self.summary([self.entry()])
            self.assertEqual(summary["accepted"], 0)
            self.assertEqual(summary["pending"], 1)
            self.assertEqual(summary["business_date"], "2026-10-18")

    def test_partial_success_unknown_and_pending_are_distinct(self):
        summary = self.summary([self.entry("已接受", last_sent_at=self.now.isoformat(), last_status="success"),
                                self.entry("未确认", last_sent_at=self.now.isoformat(), last_status="unconfirmed", last_unconfirmed_at=self.now.isoformat()),
                                self.entry("失败未提交", last_status="failed", last_attempt_at=self.now.isoformat())])
        self.assertEqual((summary["accepted"], summary["unconfirmed"], summary["pending"], summary["selected"]), (1, 1, 1, 3))
        self.assertFalse(summary["includes_historical_unconfirmed"])

    def test_historical_inflight_intents_remain_unknown_after_midnight(self):
        yesterday = (self.now - timedelta(days=1)).isoformat()
        summary = self.summary([self.entry("中断", send_attempt={"status": "sending", "business_date": "2026-10-17", "started_at": yesterday}),
                                self.entry("历史待核对", last_status="unconfirmed", last_unconfirmed_at=yesterday)])
        self.assertEqual(summary["unconfirmed"], 2)
        self.assertEqual(summary["pending"], 0)
        self.assertTrue(summary["includes_historical_unconfirmed"])

    def test_today_uses_business_timezone_and_counts_deselected_confirmed_evidence(self):
        summary = self.summary([self.entry("今日UTC", selected=False, last_sent_at="2026-10-17T16:01:00Z"),
                                self.entry("昨日UTC", last_sent_at="2026-10-17T15:59:00Z")])
        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(summary["selected"], 1)
        self.assertEqual(summary["pending"], 1)

    def test_distinct_ids_with_equal_names_are_not_merged(self):
        summary = self.summary([self.entry("同名", last_sent_at=self.now.isoformat()),
                                self.entry("同名", last_sent_at=self.now.isoformat())])
        self.assertEqual(summary["accepted"], 2)
        self.assertEqual(summary["selected"], 2)

    def test_ambiguous_or_unavailable_channel_is_not_safe_pending(self):
        summary = self.summary([self.entry("歧义", identity_conflict=True), self.entry("无会话", has_conversation=False)])
        self.assertEqual(summary["pending"], 0)
        self.assertEqual(summary["selected"], 2)

    def test_duplicate_observations_of_one_id_are_only_counted_once(self):
        e = self.entry(last_sent_at=self.now.isoformat())
        with mock.patch.object(L, "load_ledger", return_value=[e, dict(e)]):
            summary = self.summary([])
        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(summary["selected"], 1)

    def test_config_read_preserves_legacy_keys_and_returns_pure_effective_config(self):
        status, body = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertIn("locked_fields", body)
        self.assertIn("messages", body["locked_fields"])
        self.assertEqual(body["schedule_time"], body["effective_config"]["schedule_time"])
        self.assertNotIn("locked_fields", body["effective_config"])

    def test_changed_locked_field_rejects_whole_partial_update_with_metadata(self):
        before = C.CONFIG_PATH.read_bytes()
        with mock.patch.object(S, "apply_schedule") as apply:
            status, body = self.request("POST", "/api/config", {"config": {"schedule_time": "12:34", "auto_run_enabled": False}})
        self.assertEqual(status, 400)
        self.assertIsInstance(body["detail"], str)
        self.assertIn("schedule_time", body["locked_fields"])
        self.assertEqual(body["changed_locked_fields"], ["schedule_time"])
        self.assertTrue(body["effective_config"]["auto_run_enabled"])
        self.assertEqual(C.CONFIG_PATH.read_bytes(), before)
        apply.assert_not_called()

    def test_same_locked_values_and_unlocked_partial_update_succeed(self):
        cfg = C.load_config()
        with mock.patch.object(S, "apply_schedule"):
            status, body = self.request("PUT", "/api/config", {"messages": cfg["messages"], "schedule_time": cfg["schedule_time"], "auto_run_enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["effective_config"]["auto_run_enabled"])
        self.assertEqual(body["config"], body["effective_config"])
        self.assertTrue(body["locked_fields"])

    def test_message_and_interval_changes_are_rejected_without_creating_side_effects(self):
        for field, value in [("messages", ["不允许改变"]), ("send_gap_min", 9), ("send_gap_max", 15), ("jitter_minutes", 5)]:
            with self.subTest(field=field), mock.patch.object(S, "apply_schedule") as apply:
                before = C.CONFIG_PATH.read_bytes()
                status, body = self.request("POST", "/api/config", {field: value})
                self.assertEqual(status, 400)
                self.assertIn(field, body["changed_locked_fields"])
                self.assertEqual(C.CONFIG_PATH.read_bytes(), before)
                apply.assert_not_called()

    def test_equivalent_locked_time_and_number_are_not_changes(self):
        with mock.patch.object(S, "apply_schedule"):
            status, body = self.request("POST", "/api/config", {"schedule_time": "0:0", "send_gap_min": "1", "max_friends_per_run": 3})
        self.assertEqual(status, 200)
        self.assertEqual(body["effective_config"]["schedule_time"], "00:00")
        self.assertEqual(body["effective_config"]["send_gap_min"], 1)
        self.assertEqual(body["effective_config"]["max_friends_per_run"], 3)

    def test_stub_creator_collector_never_registers_weekly_job(self):
        jobs = {"weekly_harvest": object()}
        scheduler = mock.Mock()
        scheduler.get_job.side_effect = jobs.get
        scheduler.add_job.side_effect = lambda func, trigger, **kwargs: jobs.update({kwargs["id"]: func})
        scheduler.remove_job.side_effect = lambda key: jobs.pop(key, None)
        with mock.patch.object(S, "_scheduler", scheduler), mock.patch.object(S, "_harvest_func", mock.Mock()), \
             mock.patch.object(S, "_prewarm_func", None), mock.patch.object(S, "_session_check_func", None):
            S.apply_schedule()
        self.assertNotIn("weekly_harvest", jobs)


if __name__ == "__main__":
    unittest.main()
