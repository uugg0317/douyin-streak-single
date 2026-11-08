"""发送后只读核对、火花观察和诊断取代。页面都是合成或假对象，不连接抖音。"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from core import automation, chat_probe, ledger, runtime
from core.browser_errors import MessageDeliveryUnconfirmed
from core.send_day import business_now, daily_skip_code
from routers import pages
from _browser import managed_chromium, requires_chromium


TODAY = date(2026, 11, 8)
ROOT = Path(__file__).resolve().parents[1]


def evidence(text="[续火花吧]", *, failed=False, explicit=True, right=False, time_iso="", time_text="00:00",
             title="合成好友", title_matches=True, opened=True, badge=None):
    return {
        "opened": opened,
        "title": title,
        "title_matches": title_matches,
        "messages": [{
            "explicit": explicit,
            "right_side": right,
            "text": text,
            "texts": [text],
            "failed": failed,
            "time_iso": time_iso,
            "time_text": time_text,
        }],
        "badge": badge,
    }


class ClassifyTests(TestCase):
    def test_live_selector_inventory_is_explicit_and_send_free(self):
        source = (ROOT / "core" / "chat_probe.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(len(chat_probe.LIVE_DOM_SELECTORS), 8)
        self.assertTrue(all(item["needs_live_verification"] is True for item in chat_probe.LIVE_DOM_SELECTORS))
        for marker in ("isFromMe", "data-is-self", "commonStreakstreakContainer", "发送失败", "data-message-time"):
            self.assertIn(marker, source)
        self.assertNotIn("keyboard", source)
        self.assertNotIn("_type_and_send", source)
        self.assertNotIn("_send_message", source)

    def test_today_own_message_confirms_and_never_asks_for_resend(self):
        result = chat_probe.classify_chat_evidence(
            evidence(time_iso="2026-11-08T00:00:00+08:00"), "[续火花吧]", TODAY, "合成好友")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["reason"], "matched_today")
        self.assertNotIn("resend", result)

    def test_bare_clock_counts_as_today_but_conflicting_or_old_dates_do_not(self):
        bare = chat_probe.classify_chat_evidence(evidence(time_text="00:00"), "[续火花吧]", TODAY, "合成好友")
        self.assertEqual(bare["status"], "confirmed")
        yesterday = chat_probe.classify_chat_evidence(
            evidence(time_iso="2026-11-07T00:00:00+08:00", time_text="昨天 00:00"), "[续火花吧]", TODAY, "合成好友")
        self.assertEqual(yesterday["status"], "pending_review")
        self.assertEqual(yesterday["reason"], "time_not_today")
        conflict = chat_probe.classify_chat_evidence(
            evidence(time_iso="2026-11-07T00:00:00+08:00", time_text="今天 00:00"), "[续火花吧]", TODAY, "合成好友")
        self.assertEqual(conflict["reason"], "time_unknown")

    def test_failure_mark_on_matching_message_is_failed_without_resend_signal(self):
        failed = chat_probe.classify_chat_evidence(
            evidence(failed=True, time_text="刚刚"), "[续火花吧]", TODAY, "合成好友")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["reason"], "failure_mark")
        other = chat_probe.classify_chat_evidence(
            evidence(text="别的内容", failed=True, time_text="刚刚"), "[续火花吧]", TODAY, "合成好友")
        self.assertEqual(other["status"], "pending_review")
        self.assertEqual(other["reason"], "failure_mark_unmatched")

    def test_wrong_chat_or_other_side_stays_pending(self):
        wrong = chat_probe.classify_chat_evidence(
            evidence(title="另一个人", title_matches=False), "[续火花吧]", TODAY, "合成好友")
        self.assertEqual(wrong["reason"], "scope_mismatch")
        incoming = chat_probe.classify_chat_evidence(
            evidence(explicit=False, right=False), "[续火花吧]", TODAY, "合成好友")
        self.assertEqual(incoming["reason"], "no_own_message")
        closed = chat_probe.classify_chat_evidence(evidence(opened=False), "[续火花吧]", TODAY, "合成好友")
        self.assertEqual(closed["reason"], "not_opened")

    def test_right_side_fallback_is_used_only_without_explicit_markers(self):
        right = chat_probe.classify_chat_evidence(
            evidence(explicit=False, right=True, time_text="今天 00:00"), "[续火花吧]", TODAY, "合成好友")
        self.assertEqual(right["status"], "confirmed")

    def test_expiry_label_and_normal_icon_are_distinct_from_gray_icons(self):
        label = chat_probe.interpret_streak_badge({"text": "2 天后消失", "icon": "", "label": True})
        self.assertEqual(label["streak_label"], "2 天后消失")
        self.assertTrue(chat_probe.expiring_label(label["streak_label"]))
        self.assertFalse(chat_probe.expiring_label("10 天后消失"))
        numeric = chat_probe.interpret_streak_badge({
            "text": "128", "icon": "https://fixture.invalid/flame_icon/normal/normal_normal.png", "label": False})
        self.assertEqual(numeric["streak"], 128)
        self.assertEqual(numeric["streak_status"], "confirmed")
        self.assertIsNone(chat_probe.interpret_streak_badge({
            "text": "128", "icon": "https://fixture.invalid/flame_icon/mate/gray_mate.png", "label": False}))


class ProbeFlowTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for module, fields in (
            (ledger, {"DATA_DIR": root, "LEDGER_PATH": root / "ledger.json", "LEDGER_BAK_PATH": root / "ledger.json.bak"}),
            (runtime, {"RUNTIME_PATH": root / "runtime.json", "DATA_DIR": root}),
        ):
            for key, value in fields.items():
                patch = mock.patch.object(module, key, value)
                patch.start()
                self.addCleanup(patch.stop)
        runtime._rt_cache.update(mtime=None, data=None)
        self.delay = mock.patch.object(chat_probe, "POST_SEND_VERIFY_SECONDS", 0)
        self.delay.start()
        self.addCleanup(self.delay.stop)

    def seed(self, name="合成好友"):
        entry = ledger._default_entry(name)
        entry.update(selected=True, has_conversation=True, streak_days=40, streak_status="confirmed",
                     streak_confirmed_at="2026-10-31T00:00:00+08:00", streak_seen_at="2026-10-31T00:00:00+08:00")
        ledger._save([entry])
        return ledger.load_ledger()[0]

    def result(self):
        return {"ok": [], "ok_entries": [], "failed": [], "skipped": [], "deferred": [], "delivery_unconfirmed": False}

    def payload(self, **changes):
        today = business_now().date().isoformat()
        value = evidence(time_iso=today + "T00:00:00+08:00", time_text="00:00",
                         badge={"text": "2 天后消失", "icon": "", "label": True})
        value.update(changes)
        return value

    def page(self, payload):
        class ProbePage:
            def __init__(self):
                self.presses = 0
                self.keyboard = self
                self.evaluated = 0
            def press(self, *args, **kwargs):
                self.presses += 1
                raise AssertionError("核对不能按键")
            def type(self, *args, **kwargs):
                raise AssertionError("核对不能输入")
            def locator(self, selector):
                raise AssertionError("重新打开会话应走打桩，而不是真实定位")
            def evaluate(self, script, arg=None):
                self.evaluated += 1
                self.script = script
                self.arg = arg
                return payload
        return ProbePage()

    def test_scope_changed_is_confirmed_by_reopen_without_another_send(self):
        entry = self.seed()
        page = self.page(self.payload())
        located = []
        with mock.patch.object(automation, "send_to_contact", side_effect=MessageDeliveryUnconfirmed(reason="scope_changed")) as send, \
             mock.patch.object(automation, "_locate_contact", side_effect=lambda page, name: located.append(name) or True):
            outcome = self.result()
            automation._send_consumer(page, entry, "[续火花吧]", False, outcome)
        self.assertEqual(send.call_args.args[1], "合成好友")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(located, ["合成好友"])
        self.assertEqual(page.presses, 0)
        self.assertEqual(page.evaluated, 1)
        self.assertEqual(outcome["ok"], ["合成好友"])
        self.assertFalse(outcome["delivery_unconfirmed"])
        self.assertFalse(outcome["skipped"])
        row = ledger.load_ledger()[0]
        self.assertEqual(row["last_status"], "success")
        self.assertIsNone(row.get("last_unconfirmed_at"))
        self.assertEqual(row["send_verification"]["by"], "post_send_verification")
        self.assertEqual(row["send_verification"]["status"], "confirmed")
        self.assertEqual(row["streak_label"], "2 天后消失")
        self.assertNotEqual(row["streak_seen_at"][:10], "2026-10-31")
        diagnostic = row["send_diagnostics"][0]
        self.assertEqual(diagnostic["phase"], "delivery-after-send")
        self.assertEqual(diagnostic["reason"], "scope_changed")
        self.assertTrue(diagnostic["superseded"])
        self.assertEqual(diagnostic["superseded_by"], "post_send_verification")
        self.assertEqual(ledger.active_send_diagnostics(), [])
        self.assertTrue(any(item["display_name"] == "合成好友" and item["expires_in_days"] == 2
                            for item in ledger.streak_expiry_warnings()))

    def test_failure_mark_does_not_resend_and_blocks_the_rest_of_the_day(self):
        entry = self.seed()
        page = self.page(self.payload(messages=[{
            "explicit": True, "right_side": False, "text": "[续火花吧]", "texts": ["[续火花吧]"],
            "failed": True, "time_iso": "", "time_text": "刚刚",
        }]))
        with mock.patch.object(automation, "send_to_contact", side_effect=MessageDeliveryUnconfirmed(reason="scope_changed")) as send, \
             mock.patch.object(automation, "_locate_contact", return_value=True):
            outcome = self.result()
            automation._send_consumer(page, entry, "[续火花吧]", False, outcome)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(page.presses, 0)
        self.assertEqual(outcome["ok"], [])
        self.assertEqual(outcome["failed"][0]["code"], "delivery_verified_failed")
        self.assertFalse(outcome["failed"][0]["retryable"])
        self.assertFalse(outcome["delivery_unconfirmed"])
        row = ledger.load_ledger()[0]
        self.assertEqual(row["last_status"], "failed")
        self.assertEqual(row["send_attempt"]["status"], "failed_observed")
        self.assertEqual(daily_skip_code(row), "verified_send_failed")
        self.assertEqual(ledger.active_send_diagnostics(), [])

    def test_unreadable_fake_page_keeps_pending_review_diagnostic(self):
        entry = self.seed()
        with mock.patch.object(automation, "send_to_contact", side_effect=MessageDeliveryUnconfirmed(reason="scope_changed")):
            outcome = self.result()
            automation._send_consumer(object(), entry, "[续火花吧]", False, outcome)
        self.assertTrue(outcome["delivery_unconfirmed"])
        self.assertEqual(outcome["skipped"][0]["diagnostic_reason"], "scope_changed")
        self.assertEqual(outcome["ok"], [])
        row = ledger.load_ledger()[0]
        self.assertEqual(row["last_status"], "unconfirmed")
        self.assertEqual(ledger.active_send_diagnostics()[0]["reason"], "scope_changed")
        version = ledger.ledger_snapshot()["ledger_version"]
        runtime._save({"last_run": outcome, "history": []})
        confirmed = ledger.confirm_sent_by_user(entry["entry_id"], version)
        self.assertEqual(confirmed["action"], "user_confirmed_sent")
        row = ledger.load_ledger()[0]
        self.assertEqual(row["last_status"], "success")
        self.assertEqual(row["send_verification"]["by"], "user_confirmed_sent")
        self.assertEqual(row["identity_history"][-1]["action"], "user_confirmed_sent")
        self.assertTrue(row["send_diagnostics"][0]["superseded"])
        self.assertEqual(row["send_diagnostics"][0]["superseded_by"], "user_confirmed_sent")
        self.assertEqual(ledger.active_send_diagnostics(), [])
        runtime.note_send_resolved(entry["entry_id"], entry["display_name"], "confirmed")
        stored = runtime.load_runtime()["last_run"]
        self.assertEqual(stored["ok"], ["合成好友"])
        self.assertFalse(any(item.get("code") == "delivery_unconfirmed" for item in stored["skipped"]))
        self.assertFalse(stored["delivery_unconfirmed"])
        with self.assertRaises(ledger.IdentityConflict):
            ledger.confirm_sent_by_user(entry["entry_id"], ledger.ledger_snapshot()["ledger_version"])

    def test_today_summary_hides_superseded_review_and_warns_about_expiry(self):
        entry = self.seed()
        entry.update(last_status="unconfirmed", last_unconfirmed_at=business_now().isoformat(),
                     send_diagnostics=[{"phase": "delivery-after-send", "reason": "scope_changed",
                                        "status": "pending_review", "business_date": business_now().date().isoformat(),
                                        "superseded": False}],
                     streak_label="1 天后消失", streak_status="unknown", streak_seen_at="2026-10-31T00:12:00+08:00")
        ledger._save([entry])
        summary = pages._today_summary()
        self.assertEqual(summary["unconfirmed"], 1)
        self.assertEqual(summary["streak_warnings"][0]["expires_in_days"], 1)
        active = ledger.active_send_diagnostics()
        self.assertEqual(active[0]["reason"], "scope_changed")
        ledger.confirm_sent_by_user(entry["entry_id"], ledger.ledger_snapshot()["ledger_version"])
        summary = pages._today_summary()
        self.assertEqual(summary["unconfirmed"], 0)
        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(ledger.active_send_diagnostics(), [])
        self.assertEqual(summary["streak_warnings"][0]["streak_label"], "1 天后消失")


@requires_chromium
class SyntheticChatDomTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner = managed_chromium()
        cls.playwright, cls.browser = cls.owner.__enter__()
        cls.addClassCleanup(cls.owner.__exit__, None, None, None)

    def setUp(self):
        self.context = self.browser.new_context(service_workers="block")
        self.context.route("**/*", lambda route: route.abort())
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()

    def read(self, body, name="合成好友"):
        self.page.set_content(body)
        return chat_probe.verify_recent_outgoing(
            self.page, name, "[续火花吧]", TODAY, locate=lambda page, friend: True, sleep=lambda seconds: None, delay=0)

    def test_is_from_me_sticker_time_and_header_streak(self):
        result = self.read("""
        <section data-conversation-active="true" data-conversation-id="fixture" style="width:640px">
          <h2 data-conversation-title>合成好友</h2>
          <div class="commonStreakstreakContainer">
            <img class="commonStreakicon" alt="" src="https://fixture.invalid/flame_icon/normal/normal_normal.png">
            <div class="commonStreaknormalText">128</div>
          </div>
          <div data-message-list style="width:640px">
            <div class="isFromMe" data-message-id="own" style="width:120px">
              <span data-message-text>[续火花吧]</span>
              <time datetime="2026-11-08T00:00:00+08:00">00:00</time>
            </div>
          </div>
        </section>""")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["streak"]["streak"], 128)
        self.assertEqual(result["streak"]["streak_status"], "confirmed")
        self.assertEqual(self.page.locator(".isFromMe").count(), 1)

    def test_right_side_bubble_expiry_and_failure_mark(self):
        expiry = self.read("""
        <section data-conversation-active="true" style="width:640px">
          <h2 data-conversation-title>合成好友</h2>
          <div class="commonStreakstreakContainer">
            <img class="commonStreakicon" alt="" src="https://fixture.invalid/flame_icon/normal/gray_normal.png">
            <div class="commonStreaknormalText commonStreaklabelText">2 天后消失</div>
          </div>
          <div data-message-list id="list" style="position:relative;width:640px;height:80px">
            <div data-message-id="mine" style="position:absolute;left:420px;width:160px">
              <span data-message-text>[续火花吧]</span>
              <time>00:00</time>
            </div>
          </div>
        </section>""")
        self.assertEqual(expiry["status"], "confirmed")
        self.assertEqual(expiry["streak"]["streak_label"], "2 天后消失")
        self.assertIsNone(expiry["streak"]["streak"])
        failed = self.read("""
        <section data-conversation-active="true" style="width:640px">
          <h2 data-conversation-title>合成好友</h2>
          <div data-message-list>
            <div class="isFromMe" aria-label="发送失败">
              <span data-message-text>[续火花吧]</span>
              <span data-message-time-text>刚刚</span>
            </div>
          </div>
        </section>""")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["reason"], "failure_mark")

    def test_other_conversation_message_cannot_confirm(self):
        result = self.read("""
        <section data-conversation-active="true" style="width:640px">
          <h2 data-conversation-title>合成好友</h2>
          <div data-message-list></div>
        </section>
        <section data-conversation-active="false">
          <h2 data-conversation-title>别人</h2>
          <div data-message-list><div class="isFromMe"><span data-message-text>[续火花吧]</span><time>00:00</time></div></div>
        </section>""")
        self.assertEqual(result["status"], "pending_review")
        self.assertNotEqual(result["status"], "confirmed")
