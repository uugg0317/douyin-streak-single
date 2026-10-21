"""R13: confirmed clearing persists an authoritative empty selection.

Run through dist/run_checks.py; all state and network calls use synthetic fixtures.
"""
from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from core import automation, ledger
from fastapi import FastAPI
from models import SelectionBody
from routers import contacts_ledger, run
from test_r06_selection_snapshot import LocalClient


class ClearSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in {"DATA_DIR": root, "LEDGER_PATH": root / "ledger.json",
                            "LEDGER_BAK_PATH": root / "ledger.json.bak"}.items():
            patcher = mock.patch.object(ledger, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.first = ledger._default_entry("合成好友 A")
        self.second = ledger._default_entry("合成好友 B")
        self.first.update(selected=True, selected_order=0, has_conversation=True)
        self.second.update(selected=True, selected_order=1, has_conversation=True)
        ledger._save([self.first, self.second])
        app = FastAPI()
        app.include_router(contacts_ledger.router)
        app.include_router(run.router)
        self.client = LocalClient(app)
        for module in (contacts_ledger, run):
            patcher = mock.patch.object(module, "_check_auth")
            patcher.start()
            self.addCleanup(patcher.stop)

    def clear(self):
        return self.client.post("/api/ledger/selection", json={"selected_ids": [], "confirm_clear": True})

    def test_plain_empty_still_rejected_without_mutation(self):
        original = ledger.ledger_snapshot()
        for payload in ({}, {"selected_ids": []}, {"selected_names": [], "confirm_clear": False}):
            response = self.client.post("/api/ledger/selection", json=payload)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(ledger.ledger_snapshot(), original)

    def test_confirmed_clear_returns_same_empty_snapshot_and_reload_count(self):
        original = ledger.ledger_snapshot()
        response = self.clear()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["updated"], 2)
        snapshot = ledger.ledger_snapshot()
        self.assertEqual(response.json()["entries"], snapshot["entries"])
        self.assertEqual(response.json()["ledger_version"], snapshot["ledger_version"])
        self.assertNotEqual(snapshot["ledger_version"], original["ledger_version"])
        self.assertFalse(any(row["selected"] for row in snapshot["entries"]))
        self.assertTrue(all(row["selected_order"] is None for row in snapshot["entries"]))
        reloaded = self.client.get("/api/ledger")
        self.assertEqual(reloaded.status_code, 200)
        self.assertEqual(reloaded.json()["selected_count"], 0)

    def test_clear_confirmation_with_nonempty_selection_is_rejected(self):
        response = self.client.post("/api/ledger/selection", json={"selected_ids": [self.first["entry_id"]], "confirm_clear": True})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(ledger.get_selected()), 2)

    def test_string_confirmation_is_not_an_explicit_boolean(self):
        response = self.client.post("/api/ledger/selection", json={"selected_ids": [], "confirm_clear": "true"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(ledger.get_selected()), 2)

    def test_clear_write_failure_keeps_effective_selection(self):
        before = ledger.ledger_snapshot()
        body = SelectionBody(selected_ids=[], confirm_clear=True)
        with mock.patch.object(ledger, "atomic_write_text", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError):
                contacts_ledger.api_ledger_selection(body, token="synthetic")
        self.assertEqual(ledger.ledger_snapshot(), before)

    def test_legacy_cli_after_clear_is_rejected_even_with_old_config_friends(self):
        self.assertEqual(self.clear().status_code, 200)
        with mock.patch.object(run, "load_config", return_value={"friends": ["历史合成好友"]}), \
             mock.patch.object(run, "_start_run") as start:
            response = self.client.post("/api/run", json={"dry": True})
        self.assertEqual(response.status_code, 400)
        start.assert_not_called()
        self.assertEqual(ledger.get_selected(), [])

    def test_automatic_send_after_clear_never_reimports_historical_friends(self):
        self.assertEqual(self.clear().status_code, 200)
        with mock.patch.object(automation, "load_config", return_value={"friends": ["历史合成好友"]}), \
             mock.patch.object(automation, "get_valid_state_path", side_effect=AssertionError("must not inspect credentials")), \
             mock.patch.object(automation, "open_browser", side_effect=AssertionError("must not open browser")):
            result = automation._run_send_impl(dry_run=False)
        self.assertEqual(result["deferred"], [])
        self.assertEqual(result["failed"][0]["code"], "no_selected_contacts")
        self.assertEqual(ledger.get_selected(), [])

    def test_first_empty_confirmation_creates_authoritative_ledger(self):
        ledger.LEDGER_PATH.unlink()
        ledger.LEDGER_BAK_PATH.unlink(missing_ok=True)
        self.assertEqual(self.clear().status_code, 200)
        self.assertTrue(ledger.LEDGER_PATH.exists())
        self.assertFalse(ledger.legacy_import_allowed())
        self.assertEqual(ledger.get_selected(), [])

    def test_automatic_import_allows_first_run_and_explicit_import_remains_available(self):
        ledger.LEDGER_PATH.unlink()
        ledger.LEDGER_BAK_PATH.unlink(missing_ok=True)
        self.assertTrue(ledger.legacy_import_allowed())
        first = ledger.import_config_friends(["首次合成好友"], only_if_missing=True)
        self.assertEqual(first["selected"], 1)
        self.assertFalse(ledger.legacy_import_allowed())
        self.assertEqual(ledger.import_config_friends(["不应回填"], only_if_missing=True)["selected"], 0)
        explicit = ledger.import_config_friends(["显式导入好友"])
        self.assertEqual(explicit["selected"], 1)

    def test_backup_alone_prevents_automatic_legacy_import(self):
        ledger.LEDGER_PATH.replace(ledger.LEDGER_BAK_PATH)
        self.assertFalse(ledger.legacy_import_allowed())
        original = ledger.get_selected()
        ledger.import_config_friends(["不应回填"], only_if_missing=True)
        self.assertEqual(ledger.get_selected(), original)

    def test_clear_and_automatic_import_share_one_lock(self):
        entered, importing, finished = threading.Event(), threading.Event(), threading.Event()
        original = ledger.atomic_write_text
        def held_write(*args, **kwargs):
            entered.set()
            self.assertTrue(importing.wait(2))
            self.assertFalse(finished.is_set())
            return original(*args, **kwargs)
        def import_legacy():
            entered.wait(2)
            importing.set()
            ledger.import_config_friends(["不应回填"], only_if_missing=True)
            finished.set()
        worker = threading.Thread(target=import_legacy)
        with mock.patch.object(ledger, "atomic_write_text", side_effect=held_write):
            worker.start()
            try:
                ledger.clear_selection()
            finally:
                entered.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(finished.is_set())
        self.assertEqual(ledger.get_selected(), [])


if __name__ == "__main__":
    unittest.main()
