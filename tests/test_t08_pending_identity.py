"""T08: preserve homonymous rows while excluding ambiguous send predictions."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from fastapi import FastAPI
from core import automation, ledger
from routers import contacts_ledger
from test_r06_selection_snapshot import LocalClient


class PendingIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        for name, value in {"DATA_DIR": root, "LEDGER_PATH": root / "ledger.json",
                            "LEDGER_BAK_PATH": root / "ledger.json.bak"}.items():
            patcher = mock.patch.object(ledger, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for module, name, value in [(contacts_ledger, "_check_auth", None),
                                    (contacts_ledger, "load_runtime", {}),
                                    (contacts_ledger, "load_harvest_last", {}),
                                    (automation, "load_config", {"allow_first_message": False}),
                                    (automation, "load_runtime", {})]:
            patcher = mock.patch.object(module, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.first = ledger._default_entry("同名合成好友")
        self.second = ledger._default_entry("同名合成好友")
        self.safe = ledger._default_entry("唯一合成好友")
        for row in (self.first, self.second, self.safe):
            row.update(selected=True, has_conversation=True, channel="consumer")
        ledger._save([self.first, self.second, self.safe])
        app = FastAPI()
        app.include_router(contacts_ledger.router)
        self.client = LocalClient(app)

    def test_homonymous_ids_and_ambiguity_remain_but_pending_excludes_them(self):
        entries = ledger.load_ledger()
        same = [row for row in entries if row["display_name"] == "同名合成好友"]
        self.assertEqual(len(same), 2)
        self.assertEqual(len({row["entry_id"] for row in same}), 2)
        self.assertTrue(all(row["identity_ambiguous"] and row["selected"] for row in same))
        pending = automation.compute_pending(selected_snapshot=entries)
        self.assertEqual([row["entry_id"] for row in pending], [self.safe["entry_id"]])
        self.assertEqual(ledger.load_ledger(), entries)

    def test_dry_run_and_force_never_treat_unknown_identity_as_sendable(self):
        entries = ledger.load_ledger()
        for dry_run in (False, True):
            for force in (False, True):
                with self.subTest(dry_run=dry_run, force=force):
                    pending = automation.compute_pending(dry_run=dry_run, force=force,
                                                         selected_snapshot=entries)
                    self.assertEqual([row["entry_id"] for row in pending], [self.safe["entry_id"]])

    def test_creator_identity_conflict_is_not_a_pending_first_message(self):
        creator = ledger._default_entry("合成创作者")
        creator.update(selected=True, identity_ambiguous=True, has_conversation=False)
        pending = automation.compute_pending({"allow_first_message": True, "first_message_daily_limit": 1},
                                             selected_snapshot=[creator], force=True, dry_run=True)
        self.assertEqual(pending, [])

    def test_ledger_api_retains_identity_fields_and_excludes_ambiguous_pending(self):
        response = self.client.get("/api/ledger")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        for field in ("entries", "contacts"):
            same = [row for row in payload[field] if row["display_name"] == "同名合成好友"]
            self.assertEqual(len(same), 2)
            self.assertEqual(len({row["entry_id"] for row in same}), 2)
            self.assertTrue(all(row["identity_ambiguous"] is True for row in same))
        self.assertEqual([row["entry_id"] for row in payload["pending_send"]], [self.safe["entry_id"]])
        self.assertEqual(payload["selected_count"], 3)


if __name__ == "__main__":
    unittest.main()
