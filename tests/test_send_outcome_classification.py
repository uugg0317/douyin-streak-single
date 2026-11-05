"""Submission/result integration on synthetic page objects and temporary ledgers."""
from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import _isolation  # noqa: F401  先隔离数据目录，再导入项目模块
from core import automation as A, ledger as L
from core.send_day import business_now, daily_skip_code
import tasks_service as T
import test_r07_intents as intent_fixture


class SendOutcomeClassificationTests(unittest.TestCase):
    setUp = intent_fixture.IntentTests.setUp
    send = intent_fixture.IntentTests.send

    def reject_after_submission(self):
        def reject(*args, **kwargs):
            self.assertEqual(self.presses.count("Enter"), 1)
            self.assertEqual(L.load_ledger()[0]["send_attempt"]["status"], "sending")
            raise A.MessageDeliveryRejected()
        with mock.patch.object(A, "_confirm_delivery", side_effect=reject):
            self.send()

    def test_unattempted_scope_failure_does_not_create_unknown_history(self):
        A._bind_delivery_scope.return_value = None
        self.send()
        self.assertEqual(self.presses, [])
        row = L.load_ledger()[0]
        self.assertFalse(row.get("send_attempt"))
        self.assertEqual(row["last_status"], "not_attempted")
        self.assertFalse(row["last_ok"])
        self.assertIsNone(row["last_unconfirmed_at"])
        self.assertIsNone(daily_skip_code(row))
        item = self.result["failed"][0]
        self.assertEqual(item["code"], "send_not_attempted")
        self.assertFalse(item["enter_attempted"])
        self.assertTrue(item["retryable"])
        A._protect_unresolved_results(self.result)
        self.assertFalse(self.result["skipped"])
        self.assertEqual(T._collect_retry_entries(self.result),
                         [{"entry_id": row["entry_id"], "name": row["display_name"]}])
        # A later deliberate retry can actually reach Enter; the earlier zero
        # submission has not manufactured a same-day unknown-send block.
        A._bind_delivery_scope.return_value = self.scope
        self.result = {"ok": [], "failed": [], "skipped": [], "deferred": []}
        self.send()
        self.assertEqual(self.presses.count("Enter"), 1)
        self.assertEqual(L.load_ledger()[0]["last_status"], "success")

    def test_existing_intent_cannot_be_downgraded_by_an_unattempted_new_call(self):
        attempt = L.begin_send_attempt(self.entry["entry_id"])
        A._bind_delivery_scope.return_value = None
        self.send()
        row = L.load_ledger()[0]
        self.assertEqual(self.presses, [])
        self.assertEqual(row["send_attempt"]["attempt_id"], attempt)
        self.assertEqual(row["last_status"], "unconfirmed")
        self.assertEqual(daily_skip_code(row), "unconfirmed_today")
        # 防重复保护只管当天：次日不再阻止新一天的发送。
        self.assertIsNone(daily_skip_code(row, now=business_now() + timedelta(days=1)))
        self.assertEqual(self.result["skipped"][0]["code"], "delivery_unconfirmed")
        self.assertFalse(T._collect_retry_entries(self.result))

    def test_rejection_with_real_intent_keeps_failure_type_through_cleanup(self):
        self.reject_after_submission()
        row = L.load_ledger()[0]
        self.assertEqual(row["last_status"], "rejected")
        self.assertEqual(row["send_attempt"]["status"], "rejected")
        self.assertIsNone(row["last_unconfirmed_at"])
        A._protect_unresolved_results(self.result)
        self.assertEqual([item["code"] for item in self.result["failed"]], ["delivery_rejected"])
        self.assertFalse(self.result["failed"][0]["retryable"])
        self.assertFalse(self.result["skipped"])
        self.assertFalse(self.result.get("delivery_unconfirmed"))
        self.assertFalse(T._collect_retry_entries(self.result))

    def test_repeated_run_force_and_old_selection_do_not_touch_keyboard_or_browser(self):
        old_selection = [dict(self.entry)]
        self.reject_after_submission()
        self.presses.clear()
        with mock.patch.object(A, "load_config", return_value={"max_friends_per_run": 0}), \
             mock.patch.object(A, "get_valid_state_path", side_effect=AssertionError("must not read credentials")), \
             mock.patch.object(A, "_get_prewarmed_page", side_effect=AssertionError("must not open browser")):
            for force in (False, True):
                for snapshot in (None, old_selection):
                    result = A._run_send_impl(force=force, selected_snapshot=snapshot)
                    self.assertEqual(result["skipped"][0]["code"], "rejected_unreviewed")
                    self.assertFalse(result["ok"])
                    self.assertFalse(A.compute_pending(cfg={"max_friends_per_run": 0}, force=force,
                                                       selected_snapshot=snapshot))
        with mock.patch.object(A, "send_to_contact", side_effect=AssertionError("must not locate or type")):
            result = {"ok": [], "failed": [], "skipped": [], "deferred": []}
            A._send_consumer(self.page, self.entry, "synthetic message", False, result)
        self.assertEqual(result["skipped"][0]["code"], "rejected_unreviewed")
        self.assertEqual(self.presses, [])
        with self.assertRaises(L.IdentityConflict):
            L.begin_send_attempt(self.entry["entry_id"], force=True)

    def test_rejected_hold_survives_restart_same_day_and_expires_on_a_later_day(self):
        self.reject_after_submission()
        # 防重复保护只管当天：之后的日子不再阻止发送，避免一次拒绝让火花永久中断。
        self.assertIsNone(daily_skip_code(L.load_ledger()[0], now=business_now() + timedelta(days=2)))
        env = dict(os.environ)
        env.update(DATA_DIR=str(L.DATA_DIR), ENV_FILE_PATH=str(L.DATA_DIR / "missing.env"),
                   INSTANCE_LOCK_PATH=str(L.DATA_DIR / "instance.pid"), STATE_FILE_PATH="", LOGIN_GATE="off",
                   SPARKKEEPER_BACKGROUND_TASKS="0", SESSION_WARN_EMAIL_ENABLED="off",
                   PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        source = '''
from core import ledger as L, automation as A
from core.send_day import daily_skip_code
assert L.recover_interrupted_attempts()==0
row=L.load_ledger()[0]
assert row['last_status']==row['send_attempt']['status']=='rejected'
assert daily_skip_code(row)=='rejected_unreviewed'
assert not A.compute_pending(cfg={'max_friends_per_run':0},force=True)
try:L.begin_send_attempt(row['entry_id'],force=True)
except L.IdentityConflict:pass
else:raise AssertionError('force bypassed an unreviewed rejection')
'''
        result = subprocess.run([sys.executable, "-B", "-c", source], cwd=Path(__file__).resolve().parents[1],
                                env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_linked_new_record_cannot_bypass_old_rejection(self):
        self.reject_after_submission()
        L.merge_consumer_contacts([{"name": "different synthetic friend", "avatar": "fixture-new-avatar",
                                    "streak": 5, "streak_status": "confirmed", "streak_source": "class"}],
                                  capture={"complete": True})
        added = next(row for row in L.load_ledger() if row["entry_id"] != self.entry["entry_id"])
        self.assertEqual(L.identity_block_reason(added), "related_send_rejected")
        L.set_selected([{"entry_id": added["entry_id"], "selected": True}])
        self.assertFalse(A.compute_pending(cfg={"max_friends_per_run": 0}, force=True))
        with self.assertRaises(L.IdentityConflict):
            L.begin_send_attempt(added["entry_id"], force=True)

    def test_old_mixed_rejected_unknown_record_does_not_become_unknown_again(self):
        self.reject_after_submission()
        rows = L.load_ledger()
        rows[0]["send_attempt"]["status"] = "unconfirmed"
        L._save(rows)
        A._protect_unresolved_results(self.result)
        self.assertEqual(self.result["failed"][0]["code"], "delivery_rejected")
        self.assertFalse(self.result["skipped"])
        self.assertEqual(daily_skip_code(L.load_ledger()[0]), "rejected_unreviewed")
        for status in ("success", "unconfirmed", "not_attempted", "failed"):
            with self.assertRaises(L.IdentityConflict):
                L.update_send_result(self.entry["display_name"], status == "success",
                                     entry_id=self.entry["entry_id"], status=status)

    def test_old_deferred_rejected_item_is_nonretryable_without_unknown_relabeling(self):
        self.reject_after_submission()
        result = {"ok": [], "failed": [], "skipped": [], "deferred": [
            {"entry_id": self.entry["entry_id"], "name": self.entry["display_name"], "retryable": True}]}
        A._protect_unresolved_results(result)
        self.assertFalse(result["deferred"])
        self.assertEqual(result["skipped"][0]["code"], "rejected_unreviewed")
        self.assertFalse(result.get("delivery_unconfirmed"))
        self.assertFalse(T._collect_retry_entries(result))


if __name__ == "__main__":
    unittest.main()
