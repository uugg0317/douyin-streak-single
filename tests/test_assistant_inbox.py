"""小号收件：去重、白名单、限流、Webhook 载荷和回复校验。不启动浏览器。"""

from __future__ import annotations

import _isolation  # noqa: F401  先隔离数据目录，再导入项目模块

import io
import json
import logging
import os
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

import assistant_inbox
from assistant_inbox import (
    accept_incoming,
    allowlist,
    poll_seconds,
    profile_dir,
    pull_replies,
    run_cycle,
    state_path,
    status_snapshot,
    submit_reply,
)
from fastapi import HTTPException
from routers.assistant import api_assistant_reply, router
import webhook_service
from webhook_service import notify_event


def _incoming(message_id, text="你好", **extra):
    payload = {
        "id": message_id,
        "direction": "in",
        "text": text,
        "conversation_name": "主号",
        "sender_name": "主号",
        "sender_id": "owner-1",
        "message_time": "2026-11-07T18:00:00",
    }
    payload.update(extra)
    return payload


class AssistantInboxTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="assistant-inbox-")
        self._env = {
            name: os.environ.get(name)
            for name in (
                "DATA_DIR",
                "ASSISTANT_INBOX_ENABLED",
                "ASSISTANT_POLL_SECONDS",
                "ASSISTANT_ALLOWED_SENDERS",
                "ASSISTANT_REPLY_PULL_URL",
                "ASSISTANT_REPLY_PULL_KEY",
                "GROK_BOT_WEBHOOK_URL",
                "GROK_BOT_WEBHOOK_KEY",
                "GROK_BOT_WEBHOOK_DEBOUNCE_SECONDS",
            )
        }
        os.environ["DATA_DIR"] = self._tmp.name
        os.environ["ASSISTANT_INBOX_ENABLED"] = "1"
        os.environ["ASSISTANT_ALLOWED_SENDERS"] = "主号,owner-1"
        os.environ.pop("ASSISTANT_REPLY_PULL_URL", None)
        os.environ.pop("ASSISTANT_REPLY_PULL_KEY", None)
        os.environ["GROK_BOT_WEBHOOK_URL"] = "https://example.invalid/grok-bot/hook"
        os.environ["GROK_BOT_WEBHOOK_KEY"] = "test-sender-key"
        os.environ["GROK_BOT_WEBHOOK_DEBOUNCE_SECONDS"] = "600"
        webhook_service.reset_webhook_runtime()

    def tearDown(self):
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()
        webhook_service.reset_webhook_runtime()

    def test_paths_stay_off_the_main_account_files(self):
        self.assertEqual(state_path().name, "assistant_state.json")
        self.assertEqual(profile_dir().name, "assistant-browser-profile")
        self.assertNotEqual(state_path().name, "state.json")
        self.assertFalse(str(profile_dir()).endswith("qr-login-profile"))
        source = Path("assistant_browser.py").read_text(encoding="utf-8")
        login = Path("assistant_login.py").read_text(encoding="utf-8")
        self.assertNotIn("get_valid_state_path", source)
        self.assertNotIn("send_to_contact", source)
        self.assertNotIn("run_lock", source)
        self.assertNotIn("run_lock", login)
        self.assertNotIn("restart_browser", login)
        self.assertIn("conversationConversationItemtitle", source)
        self.assertIn("data-message-id", source)

    def test_poll_interval_defaults_and_enforces_the_minimum(self):
        os.environ.pop("ASSISTANT_POLL_SECONDS", None)
        self.assertEqual(poll_seconds(), 90)
        os.environ["ASSISTANT_POLL_SECONDS"] = "15"
        self.assertEqual(poll_seconds(), 60)
        os.environ["ASSISTANT_POLL_SECONDS"] = "nope"
        self.assertEqual(poll_seconds(), 90)
        os.environ["ASSISTANT_POLL_SECONDS"] = "120"
        self.assertEqual(poll_seconds(), 120)

    def test_duplicate_message_ids_are_forwarded_once(self):
        calls = []

        def notify(event, payload=None, **_kwargs):
            calls.append((event, payload))
            return True

        first = accept_incoming([_incoming("m-1", "第一句")], now=1_000, notify=notify)
        second = accept_incoming([_incoming("m-1", "第一句")], now=1_010, notify=notify)
        self.assertEqual(first["forwarded"], 1)
        self.assertEqual(second["forwarded"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "assistant_message")

    def test_allowlist_drops_other_senders_and_an_empty_list_forwards_nothing(self):
        calls = []

        def notify(event, payload=None, **_kwargs):
            calls.append(event)
            return True

        rejected = accept_incoming([
            _incoming("m-other", "不该转发", conversation_name="路人", sender_name="路人", sender_id="stranger"),
        ], now=2_000, notify=notify)
        self.assertEqual(rejected["forwarded"], 0)
        self.assertEqual(calls, [])

        os.environ["ASSISTANT_ALLOWED_SENDERS"] = ""
        self.assertEqual(allowlist(), [])
        cycle = mock.Mock()
        summary = run_cycle(cycle=cycle, pull=lambda: [], ack=lambda _ids: None, now=2_100, notify=notify)
        cycle.assert_not_called()
        self.assertEqual(summary["send"], "allowlist_empty")
        self.assertEqual(calls, [])

    def test_hourly_forward_limit_stops_at_twenty(self):
        calls = []

        def notify(event, payload=None, **_kwargs):
            calls.append(payload["details"]["message_id"])
            return True

        messages = [_incoming(f"m-{index}", f"第{index}条") for index in range(21)]
        result = accept_incoming(messages, now=5_000, notify=notify)
        self.assertEqual(result["forwarded"], 20)
        self.assertEqual(result["rate_limited"], 1)
        self.assertEqual(len(calls), 20)
        again = accept_incoming([_incoming("m-20", "第20条")], now=5_100, notify=notify)
        self.assertEqual(again["forwarded"], 0)
        self.assertEqual(again["rate_limited"], 1)

    def test_webhook_payload_includes_share_card_and_bypasses_debounce(self):
        response = mock.MagicMock()
        response.read.return_value = b"ok"
        message = _incoming(
            "share-1",
            "",
            share_title="周末的海",
            share_url="https://www.douyin.com/video/1234567890",
        )
        with mock.patch("webhook_service.urllib.request.urlopen", return_value=response) as urlopen:
            self.assertEqual(accept_incoming([message], now=8_000)["forwarded"], 1)
            self.assertTrue(notify_event("assistant_message", {"summary": "再来一条", "details": {"message_id": "manual"}}))
            self.assertTrue(notify_event("send_complete", {"summary": "主号完成", "details": {}}))
            self.assertFalse(notify_event("send_complete", {"summary": "主号完成重复", "details": {}}))
        self.assertEqual(urlopen.call_count, 3)
        body = json.loads(urlopen.call_args_list[0].args[0].data.decode("utf-8"))
        self.assertEqual(body["event"], "assistant_message")
        details = body["details"]
        self.assertEqual(details["conversation_name"], "主号")
        self.assertEqual(details["sender_id"], "owner-1")
        self.assertEqual(details["share_title"], "周末的海")
        self.assertEqual(details["share_url"], "https://www.douyin.com/video/1234567890")
        self.assertEqual(details["message_time"], "2026-11-07T18:00:00")
        self.assertEqual(details["message_id"], "share-1")
        self.assertNotIn("test-sender-key", json.dumps(body))

    def test_reply_validation_allowlist_length_and_rate_limit(self):
        os.environ["ASSISTANT_INBOX_ENABLED"] = "0"
        self.assertEqual(submit_reply("主号", "在吗", now=10_000)["error"], "disabled")
        os.environ["ASSISTANT_INBOX_ENABLED"] = "1"
        self.assertEqual(submit_reply("路人", "在吗", now=10_000)["error"], "not_allowed")
        self.assertEqual(submit_reply("主号", "   ", now=10_000)["error"], "empty_text")
        self.assertEqual(submit_reply("主号", "字" * 501, now=10_000)["error"], "too_long")
        self.assertEqual(submit_reply("owner-1", "收到", now=10_000)["ok"], True)
        self.assertEqual(submit_reply("主号", "第二条", now=10_005)["error"], "rate_limited")
        self.assertTrue(submit_reply("主号", "第二条", now=10_010)["ok"])
        self.assertTrue(submit_reply("主号", "刚刚", now=time.time())["ok"])

        with mock.patch("routers.assistant._check_auth"):
            with self.assertRaises(HTTPException) as rejected:
                api_assistant_reply({"to": "路人", "text": "不行", "extra": 1})
            self.assertEqual(rejected.exception.status_code, 400)
            with self.assertRaises(HTTPException) as limited:
                api_assistant_reply({"to": "主号", "text": "太快"})
            self.assertEqual(limited.exception.status_code, 429)
        with self.assertRaises(HTTPException) as gated:
            api_assistant_reply({"to": "主号", "text": "未登录"})
        self.assertEqual(gated.exception.status_code, 401)
        paths = {getattr(route, "path", "") for route in router.routes}
        self.assertIn("/api/assistant/reply", paths)
        self.assertIn("/api/assistant/status", paths)

    def test_queued_reply_outside_allowlist_is_dropped_and_not_sent(self):
        store = assistant_inbox._load()
        store["replies"] = [{"id": "bad-reply", "to": "陌生人", "text": "不要发出去"}]
        assistant_inbox._save(store)
        seen = {}

        def cycle(allowed, pending):
            seen["pending"] = pending
            seen["allowed"] = list(allowed)
            return {"session_ok": True, "reason": "", "messages": [], "send": None}

        run_cycle(cycle=cycle, pull=lambda: [], ack=lambda _ids: None, now=12_000, notify=lambda *args, **kwargs: True)
        self.assertIsNone(seen["pending"])
        self.assertNotIn("陌生人", seen["allowed"])
        self.assertEqual(status_snapshot()["pending_replies"], 0)

    def test_one_allowlisted_reply_is_sent_per_cycle_and_paced(self):
        queued = submit_reply("主号", "今晚继续", now=11_000)
        seen = []

        def cycle(allowed, pending):
            seen.append(pending)
            self.assertIn("主号", allowed)
            self.assertNotIn("state.json", " ".join(allowed))
            if pending is None:
                return {"session_ok": True, "messages": [], "send": None}
            self.assertEqual(pending["to"], "主号")
            self.assertEqual(pending["text"], "今晚继续")
            return {"session_ok": True, "messages": [], "send": {"ok": True}}

        store = assistant_inbox._load()
        store["last_send_at"] = 10_995
        assistant_inbox._save(store)
        paced = run_cycle(cycle=cycle, pull=lambda: [], ack=lambda _ids: None, now=11_000, notify=lambda *args, **kwargs: True)
        self.assertIsNone(seen[-1])
        self.assertEqual(paced["send"], "empty")
        self.assertEqual(status_snapshot()["pending_replies"], 1)

        sent = run_cycle(cycle=cycle, pull=lambda: [], ack=lambda _ids: None, now=11_020, notify=lambda *args, **kwargs: True)
        self.assertEqual(sent["send"], "sent")
        self.assertEqual(seen[-1]["id"], queued["id"])
        self.assertEqual(status_snapshot()["pending_replies"], 0)

    def test_credential_invalid_is_emitted_once_until_the_session_recovers(self):
        events = []

        def notify(event, payload=None, **kwargs):
            events.append((event, kwargs.get("bypass_debounce"), (payload or {}).get("details")))
            return True

        def cycle_factory(session_ok):
            def cycle(_allowed, _pending):
                return {"session_ok": session_ok, "reason": "页面出现登录提示", "messages": [], "send": None}
            return cycle

        run_cycle(cycle=cycle_factory(False), pull=lambda: [], ack=lambda _ids: None, now=13_000, notify=notify)
        run_cycle(cycle=cycle_factory(False), pull=lambda: [], ack=lambda _ids: None, now=13_100, notify=notify)
        self.assertEqual([event for event, _bypass, _details in events], ["credential_invalid"])
        self.assertTrue(events[0][1])
        self.assertEqual(events[0][2]["account"], "assistant")
        run_cycle(cycle=cycle_factory(True), pull=lambda: [], ack=lambda _ids: None, now=13_200, notify=notify)
        run_cycle(cycle=cycle_factory(False), pull=lambda: [], ack=lambda _ids: None, now=13_300, notify=notify)
        self.assertEqual([event for event, _bypass, _details in events], ["credential_invalid", "credential_invalid"])

    def test_reply_pull_is_off_until_configured_and_then_acks(self):
        with mock.patch("assistant_inbox.urllib.request.urlopen") as urlopen:
            self.assertEqual(pull_replies(), [])
        urlopen.assert_not_called()

        os.environ["ASSISTANT_REPLY_PULL_URL"] = "https://example.invalid/assistant-replies"
        os.environ["ASSISTANT_REPLY_PULL_KEY"] = "pull-secret-value"
        listed = json.dumps([
            {"id": "reply-1", "to": "主号", "text": "晚上见"},
            {"id": "reply-2", "to": "路人", "text": "不该发"},
        ]).encode("utf-8")
        ok = mock.MagicMock()
        ok.read.side_effect = [listed, b"{}"]
        ok.__enter__.return_value = ok
        ok.__exit__.return_value = False
        with mock.patch("assistant_inbox.urllib.request.urlopen", return_value=ok) as urlopen:
            summary = run_cycle(
                cycle=lambda _allowed, pending: {"session_ok": True, "messages": [], "send": None},
                now=14_000,
                notify=lambda *args, **kwargs: True,
            )
        self.assertEqual(summary["queued"], 2)
        self.assertEqual(status_snapshot()["pending_replies"], 1)
        posted = urlopen.call_args_list[1].args[0]
        self.assertEqual(posted.get_method(), "POST")
        self.assertEqual(posted.headers["Authorization"], "Bearer pull-secret-value")
        self.assertEqual(json.loads(posted.data.decode("utf-8"))["ack"], ["reply-1", "reply-2"])

    def test_status_and_logs_keep_the_full_message_text_out(self):
        secret = "UNIQUE-TAIL-MARKER"
        text = ("甲" * 90) + secret
        self.assertGreater(text.index(secret), 80)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("douyin-cloud-streak")
        logger.addHandler(handler)
        try:
            accept_incoming([_incoming("long-1", text)], now=15_000, notify=lambda *args, **kwargs: True)
            os.environ["ASSISTANT_REPLY_PULL_URL"] = "https://example.invalid/replies"
            os.environ["ASSISTANT_REPLY_PULL_KEY"] = "pull-secret-value"
            with mock.patch(
                "assistant_inbox.urllib.request.urlopen",
                side_effect=urllib.error.URLError("pull-secret-value blew up"),
            ):
                pull_replies()
        finally:
            logger.removeHandler(handler)
        logged = stream.getvalue()
        snapshot = status_snapshot()
        encoded = json.dumps(snapshot, ensure_ascii=False)
        stored = Path(self._tmp.name, "assistant_inbox.json").read_text(encoding="utf-8")
        self.assertNotIn(secret, encoded)
        self.assertNotIn(secret, stored)
        self.assertNotIn(secret, logged)
        self.assertNotIn("pull-secret-value", logged)
        self.assertTrue(snapshot["recent"][0]["preview"])
        self.assertNotIn(secret, snapshot["recent"][0]["preview"])

    def test_disabled_poller_does_not_register_a_job(self):
        os.environ["ASSISTANT_INBOX_ENABLED"] = "0"
        with mock.patch("core.scheduler.add_interval_job") as add_job:
            self.assertFalse(assistant_inbox.start_poller())
        add_job.assert_not_called()
        os.environ["ASSISTANT_INBOX_ENABLED"] = "1"
        os.environ["ASSISTANT_POLL_SECONDS"] = "75"
        with mock.patch("core.scheduler.add_interval_job") as add_job:
            self.assertTrue(assistant_inbox.start_poller())
        self.assertEqual(add_job.call_args.args[1], 75)
        self.assertEqual(add_job.call_args.args[2], "assistant_inbox_poll")


if __name__ == "__main__":
    unittest.main()
