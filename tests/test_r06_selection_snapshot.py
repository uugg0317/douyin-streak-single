"""R06: compare the saved ledger and send its stable snapshot, using synthetic data."""
from __future__ import annotations

import asyncio
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import app_common
from core import automation, ledger
from fastapi import FastAPI
from routers import contacts_ledger, run
import tasks_service


class LocalClient:
    """Invoke the real ASGI routes without network or optional httpx dependency."""
    def __init__(self, app):
        self.app = app

    def request(self, method, path, payload=None):
        raw = json.dumps(payload).encode() if payload is not None else b""
        messages = []
        async def invoke():
            async def receive():
                return {"type": "http.request", "body": raw, "more_body": False}
            async def send(message):
                messages.append(message)
            await self.app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                            "query_string": b"", "root_path": "", "server": ("127.0.0.1", 8000),
                            "client": ("127.0.0.1", 1234),
                            "headers": [(b"content-type", b"application/json")]}, receive, send)
        asyncio.run(invoke())
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        text = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body").decode()
        return type("Response", (), {"status_code": status, "text": text, "json": lambda self: json.loads(text)})()

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, *, json):
        return self.request("POST", path, json)


class SelectionSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        for name, value in {"DATA_DIR": directory, "LEDGER_PATH": directory / "ledger.json",
                            "LEDGER_BAK_PATH": directory / "ledger.json.bak"}.items():
            patcher = mock.patch.object(ledger, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(app_common, "run_lock", threading.Lock())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.a, self.b = ledger._default_entry("合成好友 A"), ledger._default_entry("合成好友 B")
        self.a.update(selected=True, has_conversation=True)
        self.b.update(selected=False, has_conversation=True)
        ledger._save([self.a, self.b])
        app = FastAPI()
        app.include_router(run.router)
        app.include_router(contacts_ledger.router)
        self.client = LocalClient(app)
        for module in (run, contacts_ledger):
            patcher = mock.patch.object(module, "_check_auth")
            patcher.start()
            self.addCleanup(patcher.stop)

    def read(self):
        response = self.client.get("/api/ledger")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def select_b(self):
        ledger.set_selected([{"entry_id": self.a["entry_id"], "selected": False},
                             {"entry_id": self.b["entry_id"], "selected": True}])

    def test_version_is_stable_and_changes_with_saved_selection(self):
        original = self.read()
        self.assertTrue(original.get("ledger_version"), "GET must identify its ledger snapshot")
        self.assertEqual(original["ledger_version"], self.read()["ledger_version"])
        self.select_b()
        updated = self.read()
        self.assertNotEqual(original["ledger_version"], updated["ledger_version"])
        self.assertEqual([row["entry_id"] for row in updated["entries"] if row["selected"]], [self.b["entry_id"]])

    def test_stale_version_is_409_before_worker_and_releases_run_lock(self):
        version = self.read().get("ledger_version", "old-ledger")
        self.select_b()
        with mock.patch.object(tasks_service, "_begin_run_locked") as begin:
            response = self.client.post("/api/run", json={"dry": True, "expected_ledger_version": version})
        self.assertEqual(response.status_code, 409, response.text)
        begin.assert_not_called()
        self.assertFalse(app_common.run_lock.locked())

    def test_version_is_checked_after_acquiring_run_lock(self):
        version = self.read().get("ledger_version", "old-ledger")
        original = tasks_service._acquire_lock
        def acquire(**kwargs):
            acquired = original(**kwargs)
            if acquired:
                self.select_b()
            return acquired
        with mock.patch.object(tasks_service, "_acquire_lock", side_effect=acquire), \
             mock.patch.object(tasks_service, "_begin_run_locked") as begin:
            response = self.client.post("/api/run", json={"dry": True, "expected_ledger_version": version})
        self.assertEqual(response.status_code, 409, response.text)
        begin.assert_not_called()
        self.assertFalse(app_common.run_lock.locked())

    def test_verified_snapshot_survives_edit_before_worker_runs(self):
        version = self.read().get("ledger_version", "old-ledger")
        workers = []
        class HeldThread:
            def __init__(self, *, target, daemon):
                workers.append(target)
            def start(self):
                pass
        with mock.patch.object(tasks_service.threading, "Thread", HeldThread), \
             mock.patch.object(tasks_service, "set_running"), \
             mock.patch.object(tasks_service, "record_run"), \
             mock.patch.object(automation, "get_valid_state_path", return_value=None), \
             mock.patch.object(automation, "_run_in_pw_thread", side_effect=lambda fn, *a, **kw: fn(*a, **kw)), \
             mock.patch.object(automation, "open_browser", side_effect=AssertionError("no browser")), \
             mock.patch.object(automation, "load_config", return_value={"messages": ["合成消息"], "max_friends_per_run": 0}):
            response = self.client.post("/api/run", json={"dry": True, "expected_ledger_version": version})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(app_common.run_lock.locked())
            self.select_b()
            with mock.patch.object(ledger, "get_selected", side_effect=AssertionError("must use verified snapshot")):
                workers.pop()()
            result = tasks_service.record_run.call_args.args[0]
        self.assertEqual([row["entry_id"] for row in result["deferred"]], [self.a["entry_id"]])
        self.assertFalse(app_common.run_lock.locked())

    def test_explicit_empty_snapshot_never_imports_legacy_friends(self):
        ledger.set_selected([{"entry_id": self.a["entry_id"], "selected": False}])
        version = self.read().get("ledger_version", "empty-ledger")
        with mock.patch.object(run, "load_config", return_value={"friends": ["旧配置好友"]}), \
             mock.patch.object(ledger, "import_config_friends", side_effect=AssertionError("no hidden import")), \
             mock.patch.object(tasks_service, "_begin_run_locked") as begin:
            response = self.client.post("/api/run", json={"dry": True, "expected_ledger_version": version})
        self.assertEqual(response.status_code, 400, response.text)
        begin.assert_not_called()
        self.assertFalse(app_common.run_lock.locked())

    def test_old_cli_without_version_retains_call_contract(self):
        with mock.patch.object(run, "_start_run") as start:
            response = self.client.post("/api/run", json={"dry": True})
        self.assertEqual(response.status_code, 200, response.text)
        start.assert_called_once_with(True, force=False)

    def test_direct_automation_legacy_config_friends_still_imports(self):
        # R13: only a first-run ledger imports automatically; saved emptiness
        # is an authoritative user decision and cannot restore old friends.
        ledger.LEDGER_PATH.unlink()
        ledger.LEDGER_BAK_PATH.unlink(missing_ok=True)
        with mock.patch.object(automation, "load_config", return_value={"friends": ["旧配置好友"]}), \
             mock.patch.object(automation, "get_valid_state_path", return_value=None):
            result = automation._run_send_impl(dry_run=True)
        self.assertEqual([row["name"] for row in result["deferred"]], ["旧配置好友"])

    def test_pending_preview_uses_same_entries_as_version(self):
        original = automation.compute_pending
        def racing_preview(*args, **kwargs):
            self.select_b()
            return original(*args, **kwargs)
        with mock.patch.object(automation, "compute_pending", side_effect=racing_preview):
            data = self.read()
        self.assertEqual([row["entry_id"] for row in data["pending_send"]], [self.a["entry_id"]])

    def test_save_response_contains_version_and_the_saved_selection(self):
        old = self.read()["ledger_version"]
        response = self.client.post("/api/ledger/selection", json={"selected_ids": [self.b["entry_id"]]})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertNotEqual(old, data["ledger_version"])
        self.assertEqual(data["ledger_version"], self.read()["ledger_version"])
        self.assertEqual([row["entry_id"] for row in data["entries"] if row["selected"]], [self.b["entry_id"]])

    def test_ledger_version_comparison_and_read_hold_one_lock(self):
        version = self.read()["ledger_version"]
        read_entered, writer_started, writer_finished = threading.Event(), threading.Event(), threading.Event()
        original = ledger._load_ledger_unlocked
        observed = []
        def held_read():
            entries = original()
            if threading.current_thread() is threading.main_thread():
                read_entered.set()
                self.assertTrue(writer_started.wait(2))
                observed.append(writer_finished.is_set())
            return entries
        def change():
            read_entered.wait(2)
            writer_started.set()
            self.select_b()
            writer_finished.set()
        writer = threading.Thread(target=change)
        with mock.patch.object(ledger, "_load_ledger_unlocked", side_effect=held_read):
            writer.start()
            try:
                snapshot = ledger.ledger_snapshot(version)
            finally:
                writer.join(2)
        self.assertFalse(writer.is_alive())
        self.assertEqual(observed, [False])
        self.assertTrue(writer_finished.is_set())
        self.assertEqual(snapshot["ledger_version"], version)
        self.assertEqual([row["entry_id"] for row in snapshot["entries"] if row["selected"]], [self.a["entry_id"]])
        self.assertNotEqual(ledger.ledger_snapshot()["ledger_version"], version)


if __name__ == "__main__":
    unittest.main()
