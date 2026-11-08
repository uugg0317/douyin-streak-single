"""灰色火花、即将消失警告，以及发送诊断的追加与取代。不连接抖音。"""
from __future__ import annotations

import _isolation  # noqa: F401

from datetime import datetime, timezone
import unittest

from core import ledger, runtime
from core.send_day import business_now
from core.streaks import expiry_days, expiring_label, observe_streak
from routers import pages


class ExpiryLabelTests(unittest.TestCase):
    def test_expiry_days_only_accepts_disappear_labels_within_three_days(self):
        self.assertEqual(expiry_days("2 天后消失"), 2)
        self.assertEqual(expiry_days("0 天后消失"), 0)
        self.assertTrue(expiring_label("3 天后消失"))
        self.assertFalse(expiring_label("10 天后消失"))
        self.assertIsNone(expiry_days("重燃中 2/3"))
        self.assertIsNone(expiry_days("任意私信"))


class DiagnosticAndWarningTests(unittest.TestCase):
    def setUp(self):
        ledger._save([])
        runtime._save({"last_run": None, "history": []})

    def seed(self, **fields):
        entry = ledger._default_entry("合成好友")
        entry.update(selected=True, has_conversation=True, **fields)
        ledger._save([entry])
        return ledger.load_ledger()[0]

    def test_unconfirmed_delivery_records_pending_diagnostic_and_never_autosends(self):
        entry = self.seed()
        attempt = ledger.begin_send_attempt(entry["entry_id"])
        ledger.update_send_result(
            "合成好友", False, status="unconfirmed", entry_id=entry["entry_id"],
            attempt_id=attempt, diagnostic={"reason": "scope_changed", "phase": "delivery-after-send"},
        )
        row = ledger.load_ledger()[0]
        self.assertEqual(row["last_status"], "unconfirmed")
        self.assertEqual(row["send_verification"]["status"], "pending_review")
        self.assertEqual(row["send_verification"]["reason"], "scope_changed")
        active = ledger.active_send_diagnostics()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["reason"], "scope_changed")
        self.assertEqual(active[0]["display_name"], "合成好友")
        self.assertEqual(row["send_diagnostics"][0]["superseded"], False)

    def test_success_and_user_review_supersede_pending_diagnostics(self):
        entry = self.seed()
        attempt = ledger.begin_send_attempt(entry["entry_id"])
        ledger.update_send_result(
            "合成好友", False, status="unconfirmed", entry_id=entry["entry_id"],
            attempt_id=attempt, diagnostic={"reason": "delivery_not_observed", "phase": "delivery-after-send"},
        )
        self.assertEqual(ledger.active_send_diagnostics()[0]["reason"], "delivery_not_observed")
        version = ledger.ledger_snapshot()["ledger_version"]
        result = ledger.review_send(entry["entry_id"], "sent", version)
        self.assertTrue(result["ok"])
        row = ledger.load_ledger()[0]
        self.assertEqual(row["last_status"], "success")
        self.assertEqual(row["send_verification"]["by"], "user_confirmed_sent")
        self.assertTrue(row["send_diagnostics"][0]["superseded"])
        self.assertEqual(row["send_diagnostics"][0]["superseded_by"], "user_confirmed_sent")
        self.assertEqual(ledger.active_send_diagnostics(), [])

    def test_user_confirmed_not_sent_also_supersedes_diagnostics(self):
        entry = self.seed()
        attempt = ledger.begin_send_attempt(entry["entry_id"])
        ledger.update_send_result(
            "合成好友", False, status="unconfirmed", entry_id=entry["entry_id"],
            attempt_id=attempt, diagnostic="scope_changed",
        )
        version = ledger.ledger_snapshot()["ledger_version"]
        ledger.review_send(entry["entry_id"], "not_sent", version)
        row = ledger.load_ledger()[0]
        self.assertEqual(row["last_status"], "failed")
        self.assertEqual(row["send_verification"]["by"], "user_confirmed_not_sent")
        self.assertTrue(row["send_diagnostics"][0]["superseded"])
        self.assertEqual(row["send_diagnostics"][0]["superseded_by"], "user_confirmed_not_sent")
        self.assertEqual(ledger.active_send_diagnostics(), [])

    def test_accepted_send_supersedes_without_replacing_existing_confirmation_engine(self):
        entry = self.seed()
        attempt = ledger.begin_send_attempt(entry["entry_id"])
        ledger.update_send_result(
            "合成好友", False, status="unconfirmed", entry_id=entry["entry_id"],
            attempt_id=attempt, diagnostic={"reason": "no_new_message", "phase": "delivery-after-send"},
        )
        ledger.update_send_result(
            "合成好友", True, status="success", entry_id=entry["entry_id"],
            attempt_id=attempt, resolution="accepted_own_row",
        )
        row = ledger.load_ledger()[0]
        self.assertEqual(row["last_status"], "success")
        self.assertEqual(row["send_verification"]["by"], "accepted_own_row")
        self.assertEqual(row["send_verification"]["status"], "confirmed")
        self.assertTrue(row["send_diagnostics"][0]["superseded"])
        self.assertEqual(row["send_diagnostics"][0]["superseded_by"], "accepted_own_row")
        self.assertEqual(ledger.active_send_diagnostics(), [])

    def test_today_summary_warns_about_expiry_and_hides_superseded_diagnostics(self):
        entry = self.seed(
            last_status="unconfirmed",
            last_unconfirmed_at=business_now().isoformat(),
            send_diagnostics=[{
                "phase": "delivery-after-send",
                "reason": "scope_changed",
                "status": "pending_review",
                "business_date": business_now().date().isoformat(),
                "superseded": False,
            }],
            streak_label="1 天后消失",
            streak_status="unknown",
            streak_seen_at="2026-11-08T00:12:00+08:00",
        )
        other = ledger._default_entry("十天后")
        other.update(selected=True, has_conversation=True, streak_label="10 天后消失")
        ledger._save([entry, other])
        summary = pages._today_summary()
        self.assertEqual(summary["unconfirmed"], 1)
        self.assertEqual(len(summary["streak_warnings"]), 1)
        self.assertEqual(summary["streak_warnings"][0]["expires_in_days"], 1)
        self.assertEqual(summary["streak_warnings"][0]["display_name"], "合成好友")
        self.assertEqual(ledger.active_send_diagnostics()[0]["reason"], "scope_changed")
        ledger.review_send(entry["entry_id"], "sent", ledger.ledger_snapshot()["ledger_version"])
        summary = pages._today_summary()
        self.assertEqual(summary["unconfirmed"], 0)
        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(ledger.active_send_diagnostics(), [])
        self.assertEqual(summary["streak_warnings"][0]["streak_label"], "1 天后消失")

    def test_note_send_resolved_moves_unconfirmed_runtime_row(self):
        entry = self.seed()
        runtime._save({
            "last_run": {
                "ok": [],
                "failed": [],
                "skipped": [{"entry_id": entry["entry_id"], "name": "合成好友", "code": "delivery_unconfirmed"}],
                "delivery_unconfirmed": True,
            },
            "history": [],
        })
        runtime.note_send_resolved(entry["entry_id"], "合成好友", "confirmed")
        stored = runtime.load_runtime()["last_run"]
        self.assertEqual(stored["ok"], ["合成好友"])
        self.assertFalse(any(item.get("code") == "delivery_unconfirmed" for item in stored["skipped"]))
        self.assertFalse(stored["delivery_unconfirmed"])

    def test_observe_streak_records_seen_time_and_at_risk_without_changing_days_on_label(self):
        now = datetime(2026, 11, 8, 0, 12, tzinfo=timezone.utc)
        entry = {"streak_days": 81, "streak_status": "confirmed"}
        observe_streak(entry, {"streak_status": "unknown", "streak_label": "2 天后消失"}, now)
        self.assertEqual(entry["streak_days"], 81)
        self.assertEqual(entry["streak_label"], "2 天后消失")
        self.assertEqual(entry["streak_seen_at"], now.isoformat(timespec="seconds"))
        self.assertTrue(expiring_label(entry["streak_label"]))


if __name__ == "__main__":
    unittest.main()
