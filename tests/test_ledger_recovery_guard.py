"""Old backups must not turn an uncertain submission into a fresh send target."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from core import automation as A, ledger as L

ROOT = Path(__file__).resolve().parents[1]


class LedgerRecoveryGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ledger-recovery-guard-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = mock.patch.multiple(L, DATA_DIR=self.root, LEDGER_PATH=self.root / "ledger.json",
                                     LEDGER_BAK_PATH=self.root / "ledger.json.bak")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.entry = L._default_entry("synthetic friend")
        self.entry.update(selected=True, has_conversation=True)
        L._save([self.entry])

    def assert_send_blocked(self):
        self.assertTrue(L.recovery_required())
        for force in (False, True):
            with self.assertRaises(L.IdentityConflict):
                L.begin_send_attempt(self.entry["entry_id"], force=force)
            with mock.patch.object(A, "load_config", return_value={"friends": ["synthetic fallback"]}), \
                 mock.patch.object(A, "get_valid_state_path", side_effect=AssertionError("must stop before credentials")), \
                 mock.patch.object(A, "_get_prewarmed_page", side_effect=AssertionError("must stop before browser")):
                result = A._run_send_impl(force=force)
            self.assertTrue(result["ledger_recovery_required"])
            self.assertEqual(result["failed"][0]["code"], "ledger_recovery_required")
            self.assertFalse(result["failed"][0]["retryable"])
            self.assertFalse(result["ok"])
        self.assertEqual(A.compute_pending(cfg={}), [])

    def damaged_after_intent(self, *, missing=False):
        L.begin_send_attempt(self.entry["entry_id"])
        self.assertIsNone(json.loads(L.LEDGER_BAK_PATH.read_text(encoding="utf-8"))[0].get("send_attempt"))
        if missing:
            L.LEDGER_PATH.unlink()
        else:
            L.LEDGER_PATH.write_bytes(b"{synthetic broken primary")
        self.assertEqual(L.recover_interrupted_attempts(), 0)
        rows = L.load_ledger()
        self.assertTrue(rows[0]["selected"])
        self.assertFalse(rows[0].get("send_attempt"))
        self.assertEqual(L.identity_block_reason(rows[0]), "ledger_recovery_required")
        self.assert_send_blocked()

    def test_corrupt_primary_fallback_never_treats_pre_intent_backup_as_unsent(self):
        self.damaged_after_intent()

    def test_missing_primary_fallback_never_treats_pre_intent_backup_as_unsent(self):
        self.damaged_after_intent(missing=True)

    def test_both_corrupt_blocks_even_empty_targets_and_legacy_import(self):
        L.LEDGER_PATH.write_bytes(b"{broken primary")
        L.LEDGER_BAK_PATH.write_bytes(b"{broken backup")
        self.assertEqual(L.load_ledger(), [])
        self.assert_send_blocked()
        self.assertFalse(L.legacy_import_allowed())
        L.LEDGER_PATH.unlink()
        L.LEDGER_BAK_PATH.unlink()
        self.assertEqual(L.import_config_friends(["must not automatically import"], only_if_missing=True)["added"], 0)
        self.assert_send_blocked()

    def test_missing_primary_with_unreadable_backup_also_sets_guard(self):
        L.LEDGER_PATH.unlink()
        L.LEDGER_BAK_PATH.write_bytes(b"\xff\xfe synthetic invalid backup")
        self.assertEqual(L.load_ledger(), [])
        self.assert_send_blocked()

    def test_marker_write_failure_never_reaches_submission_or_rewrites_bad_primary(self):
        L.begin_send_attempt(self.entry["entry_id"])
        raw = b"{synthetic corruption"
        L.LEDGER_PATH.write_bytes(raw)
        actual_write = L.atomic_write_text
        submitted = []

        def write(path, text):
            if Path(path) == L.recovery_marker_path():
                raise OSError("synthetic marker disk full")
            actual_write(path, text)

        def send(*args):
            A._persist_current_intent()
            submitted.append("Enter")
            return True, "accepted"

        result = {"ok": [], "failed": [], "skipped": [], "deferred": []}
        with mock.patch.object(L, "atomic_write_text", side_effect=write), \
             mock.patch.object(A, "send_to_contact", side_effect=send):
            with self.assertRaises(OSError):
                L.load_ledger()
            A._send_consumer(object(), self.entry, "synthetic message", False, result)
        self.assertEqual(submitted, [])
        self.assertFalse(result["ok"])
        self.assertTrue(result["intent_write_failed"])
        self.assertEqual(L.LEDGER_PATH.read_bytes(), raw)
        self.assertFalse(L.recovery_marker_path().exists())
        L.load_ledger()
        self.assert_send_blocked()

    def test_sync_selection_and_repaired_primary_cannot_clear_guard(self):
        self.damaged_after_intent()
        marker = L.recovery_marker_path().read_bytes()
        L.merge_consumer_contacts([{"name": "synthetic friend", "avatar": "fixture-avatar", "streak": 7},
                                   {"name": "synthetic new friend", "avatar": "fixture-new", "streak": 3}],
                                  capture={"complete": True})
        rows = L.load_ledger()
        L.set_selected([{"entry_id": row["entry_id"], "selected": True} for row in rows])
        self.assertTrue(all(row["ledger_recovery_required"] for row in L.load_ledger()))
        self.assertEqual(L.recovery_marker_path().read_bytes(), marker)
        self.assert_send_blocked()
        L.clear_selection()
        self.assertEqual(L.recovery_marker_path().read_bytes(), marker)
        self.assert_send_blocked()

    def test_malformed_marker_is_still_protection_with_healthy_primary(self):
        L.recovery_marker_path().write_text("{invalid marker", encoding="utf-8")
        self.assert_send_blocked()

    def test_marker_is_checked_even_for_a_previously_captured_selection(self):
        snapshot = L.get_selected()
        L.recovery_marker_path().write_text("{}", encoding="utf-8")
        self.assertEqual(A.compute_pending(cfg={"allow_first_message": False}, selected_snapshot=snapshot, force=True), [])
        with mock.patch.object(A, "load_config", return_value={}), \
             mock.patch.object(A, "get_valid_state_path", side_effect=AssertionError("no credentials")):
            result = A._run_send_impl(selected_snapshot=snapshot, force=True)
        self.assertTrue(result["ledger_recovery_required"])
        self.assertFalse(result["ok"])

    def test_direct_creator_path_cannot_bypass_marker_with_an_old_entry(self):
        from types import SimpleNamespace
        send = mock.Mock(side_effect=AssertionError("creator must not submit"))
        L.recovery_marker_path().write_text("{}", encoding="utf-8")
        result = {"ok": [], "failed": [], "skipped": [], "deferred": []}
        with mock.patch.object(A, "creator_channel", SimpleNamespace(send_first_message=send)):
            A._send_creator(self.entry, "synthetic message", False, result, None)
        send.assert_not_called()
        self.assertTrue(result["ledger_recovery_required"])
        self.assertEqual(result["skipped"][0]["code"], "ledger_recovery_required")

    def child(self, folder, source, *, exit_code=0):
        env = dict(os.environ)
        env.update(DATA_DIR=str(folder), ENV_FILE_PATH=str(folder / "missing.env"),
                   INSTANCE_LOCK_PATH=str(folder / "instance.pid"), STATE_FILE_PATH="", LOGIN_GATE="off",
                   SPARKKEEPER_BACKGROUND_TASKS="0", SESSION_WARN_EMAIL_ENABLED="off",
                   PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-B", "-c", textwrap.dedent(source)], cwd=ROOT, env=env,
                                text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
        return result

    def test_real_process_exit_boundaries_preserve_intents_and_accepted_results(self):
        for stage in ("before_intent", "after_intent", "after_submit", "after_result"):
            with self.subTest(stage=stage):
                folder = self.root / stage
                self.child(folder, f'''
                    import os
                    from core import ledger as L
                    entry=L._default_entry('synthetic crash friend')
                    entry.update(selected=True,has_conversation=True)
                    L._save([entry])
                    stage={stage!r}
                    if stage=='before_intent':os._exit(73)
                    attempt=L.begin_send_attempt(entry['entry_id'])
                    if stage=='after_intent':os._exit(73)
                    L.atomic_write_text(L.DATA_DIR/'synthetic-submit.txt','one synthetic submission')
                    if stage=='after_submit':os._exit(73)
                    L.update_send_result(entry['display_name'],True,entry_id=entry['entry_id'],attempt_id=attempt)
                    os._exit(73)
                ''', exit_code=73)
                self.child(folder, f'''
                    from core import ledger as L, automation as A
                    stage={stage!r}
                    recovered=L.recover_interrupted_attempts()
                    assert recovered==(1 if stage in ('after_intent','after_submit') else 0)
                    assert len(A.compute_pending(cfg={{}}))==(1 if stage=='before_intent' else 0)
                    assert not L.recovery_required()
                    assert (L.DATA_DIR/'synthetic-submit.txt').exists()==(stage in ('after_submit','after_result'))
                    if stage=='after_result':assert L.load_ledger()[0]['send_attempt']['status']=='accepted'
                ''')

    def test_real_crash_backup_fallback_remains_blocked_after_another_restart(self):
        folder = self.root / "recovery-restart"
        self.child(folder, '''
            import os
            from core import ledger as L
            entry=L._default_entry('synthetic restart friend')
            entry.update(selected=True,has_conversation=True)
            L._save([entry])
            L.begin_send_attempt(entry['entry_id'])
            L.atomic_write_text(L.DATA_DIR/'synthetic-submit.txt','one synthetic submission')
            os._exit(74)
        ''', exit_code=74)
        (folder / "ledger.json").write_bytes(b"{synthetic corrupted after crash")
        self.child(folder, '''
            import os
            from core import ledger as L
            L.recover_interrupted_attempts()
            assert L.recovery_required()
            rows=L.load_ledger()
            L.set_selected([{'entry_id':rows[0]['entry_id'],'selected':False}])
            assert L.LEDGER_PATH.read_text(encoding='utf-8').startswith('[')
            os._exit(75)
        ''', exit_code=75)
        self.child(folder, '''
            from core import ledger as L, automation as A
            L.recover_interrupted_attempts()
            rows=L.load_ledger()
            assert L.recovery_required() and rows[0]['ledger_recovery_required']
            L.set_selected([{'entry_id':rows[0]['entry_id'],'selected':True}])
            assert not A.compute_pending(cfg={},force=True)
            try:L.begin_send_attempt(rows[0]['entry_id'],force=True)
            except L.IdentityConflict:pass
            else:raise AssertionError('restart or force bypassed recovery protection')
            assert (L.DATA_DIR/'synthetic-submit.txt').read_text()=='one synthetic submission'
        ''')


if __name__ == "__main__":
    unittest.main()
