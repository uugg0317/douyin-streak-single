"""Webhook 投递：未配置不发请求，配置后带密钥头，失败不向外抛。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.error
from unittest import mock

import webhook_service
from webhook_service import WEBHOOK_KEY_HEADER, WEBHOOK_TIMEOUT_SECONDS, notify_event


class WebhookNotifyTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            name: os.environ.get(name)
            for name in ("GROK_BOT_WEBHOOK_URL", "GROK_BOT_WEBHOOK_KEY")
        }
        os.environ.pop("GROK_BOT_WEBHOOK_URL", None)
        os.environ.pop("GROK_BOT_WEBHOOK_KEY", None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_disabled_when_url_missing_does_not_call_http(self):
        os.environ["GROK_BOT_WEBHOOK_KEY"] = "unused-sender-key"
        with mock.patch("webhook_service.urllib.request.urlopen") as urlopen:
            delivered = notify_event("send_complete", {
                "summary": "本次发送完成",
                "details": {"ok_count": 1},
            })
        self.assertFalse(delivered)
        urlopen.assert_not_called()

    def test_blank_url_and_non_http_scheme_stay_disabled(self):
        with mock.patch("webhook_service.urllib.request.urlopen") as urlopen:
            os.environ["GROK_BOT_WEBHOOK_URL"] = "   "
            self.assertFalse(notify_event("send_complete", {"summary": "完成", "details": {}}))
            os.environ["GROK_BOT_WEBHOOK_URL"] = "file:///tmp/hook"
            self.assertFalse(notify_event("send_complete", {"summary": "完成", "details": {}}))
        urlopen.assert_not_called()

    def test_posts_json_with_sender_key_header(self):
        os.environ["GROK_BOT_WEBHOOK_URL"] = "https://example.invalid/grok-bot/hook"
        os.environ["GROK_BOT_WEBHOOK_KEY"] = "test-sender-key"
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b"ok"
        with mock.patch("webhook_service.urllib.request.urlopen", return_value=response) as urlopen:
            delivered = notify_event("send_complete", {
                "summary": "本次发送完成：通过 2，失败 1",
                "details": {
                    "ok_count": 2,
                    "failed_count": 1,
                    "ok_names": ["小明"],
                    "cookie": "must-not-leave",
                    "nested": {"sessionid": "abc123secret", "nickname": "小红"},
                    "note": "sessionid=abcdef123456 leftover",
                },
            })
        self.assertTrue(delivered)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], WEBHOOK_TIMEOUT_SECONDS)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.headers.get(WEBHOOK_KEY_HEADER), "test-sender-key")
        self.assertIn("application/json", request.headers.get("Content-Type", ""))
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["event"], "send_complete")
        self.assertEqual(body["host"], socket.gethostname())
        self.assertIn("T", body["time"])
        self.assertTrue(body["time"].endswith("+08:00"))
        self.assertEqual(body["summary"], "本次发送完成：通过 2，失败 1")
        self.assertEqual(body["details"]["ok_count"], 2)
        self.assertEqual(body["details"]["ok_names"], ["小明"])
        self.assertEqual(body["details"]["nested"]["nickname"], "小红")
        self.assertNotIn("cookie", body["details"])
        self.assertNotIn("sessionid", body["details"]["nested"])
        encoded = request.data.decode("utf-8")
        self.assertNotIn("must-not-leave", encoded)
        self.assertNotIn("abc123secret", encoded)
        self.assertNotIn("abcdef123456", encoded)
        self.assertNotIn("test-sender-key", encoded)
        self.assertIn("[redacted]", body["details"]["note"])

    def test_omits_key_header_when_sender_key_is_empty(self):
        os.environ["GROK_BOT_WEBHOOK_URL"] = "https://example.invalid/grok-bot/hook"
        response = mock.MagicMock()
        response.read.return_value = b""
        with mock.patch("webhook_service.urllib.request.urlopen", return_value=response) as urlopen:
            self.assertTrue(notify_event("watchdog_stuck", {"summary": "卡死", "details": {"age_seconds": 90}}))
        request = urlopen.call_args.args[0]
        self.assertNotIn(WEBHOOK_KEY_HEADER, request.headers)
        self.assertNotIn("X-webhook-key", request.headers)

    def test_http_and_network_failures_are_swallowed(self):
        os.environ["GROK_BOT_WEBHOOK_URL"] = "https://example.invalid/grok-bot/hook"
        os.environ["GROK_BOT_WEBHOOK_KEY"] = "test-sender-key"
        cases = [
            urllib.error.URLError("down"),
            urllib.error.HTTPError("https://example.invalid/grok-bot/hook", 500, "err", hdrs=None, fp=None),
            TimeoutError("timed out"),
            OSError("refused"),
        ]
        for error in cases:
            with self.subTest(error=type(error).__name__):
                with mock.patch("webhook_service.urllib.request.urlopen", side_effect=error):
                    delivered = notify_event("run_failed", {"summary": "本轮未成功", "details": {}})
                self.assertFalse(delivered)

    def test_unexpected_exception_is_swallowed(self):
        os.environ["GROK_BOT_WEBHOOK_URL"] = "https://example.invalid/grok-bot/hook"
        with mock.patch("webhook_service.urllib.request.urlopen", side_effect=RuntimeError("boom")):
            self.assertFalse(notify_event("session_warn", {"summary": "预警", "details": {}}))

    def test_enabled_flag_follows_url(self):
        self.assertFalse(webhook_service.webhook_enabled())
        os.environ["GROK_BOT_WEBHOOK_URL"] = "https://example.invalid/hook"
        self.assertTrue(webhook_service.webhook_enabled())


class WebhookHookTests(unittest.TestCase):
    def test_completion_session_and_watchdog_call_notify_event(self):
        root = Path(__file__).resolve().parents[1]
        script = textwrap.dedent('''
            import json
            from unittest import mock
            import tasks_service as tasks
            import session_service as session
            import watchdog_service as watchdog

            events = []

            def capture(event, payload=None):
                payload = payload or {}
                events.append((event, payload.get("summary"), payload.get("details") or {}))
                return True

            tasks.notify_event = capture
            session.notify_event = capture
            watchdog.notify_event = capture
            watchdog._send_email = mock.Mock(side_effect=RuntimeError("smtp disabled in test"))

            tasks._notify_run_webhooks({
                "dry_run": False,
                "ok": [],
                "failed": [{"name": "小明", "code": "not_found"}],
                "skipped": [],
                "deferred": [{"name": "小刚"}],
                "pending_retry_count": 1,
                "retry_status": "manual",
            }, dry=False, unconfirmed=[{"name": "小红", "code": "delivery_unconfirmed"}],
               consecutive_failures=3, failure_increased=True)
            kinds = [event for event, _, _ in events]
            assert kinds == ["send_complete", "pending_review", "session_warn", "run_failed", "consecutive_failure"], kinds
            assert events[0][2]["ok_count"] == 0 and events[0][2]["failed_count"] == 1
            assert events[0][2]["unconfirmed_count"] == 1
            assert "小红" in events[1][2]["unconfirmed_names"]

            events.clear()
            tasks._notify_run_webhooks({
                "ok": [],
                "failed": [{"name": "_system", "code": "credentials_missing"}],
                "credentials_missing": True,
            }, dry=False, unconfirmed=[], consecutive_failures=1, failure_increased=False)
            assert [event for event, _, _ in events] == ["send_complete", "credential_invalid", "run_failed"]

            events.clear()
            tasks._notify_run_webhooks({
                "ok": ["甲"], "failed": [], "dry_run": True,
            }, dry=True, unconfirmed=[{"name": "小红", "code": "unconfirmed_today"}],
               consecutive_failures=9, failure_increased=True)
            assert [event for event, _, _ in events] == ["send_complete"]

            events.clear()
            session._notify_session_webhook("关键 Cookie 临期", {
                "logged_in": True,
                "cookie": {"has_state": True, "has_session": True, "expired": False,
                           "expiring_soon": True, "min_days": 2, "details": "剩余 2 天"},
            })
            assert events[0][0] == "session_warn"
            events.clear()
            session._notify_session_webhook("页面登录检测未通过", {
                "logged_in": False,
                "cookie": {"has_state": False, "has_session": False, "expired": False,
                           "value": "secret-cookie-value"},
            })
            assert events[0][0] == "credential_invalid"
            assert "secret-cookie-value" not in json.dumps(events[0][2], ensure_ascii=False)

            events.clear()
            watchdog._watchdog_notify(120.2, "thread stack")
            assert events[0][0] == "watchdog_stuck"
            assert events[0][2]["age_seconds"] == 120
            assert events[0][2]["stack_captured"] is True
            watchdog._send_email.assert_called_once()
            print("hooks-ok")
        ''')
        with tempfile.TemporaryDirectory(prefix="webhook-hooks-") as folder:
            env = os.environ.copy()
            env.update({
                "DATA_DIR": str(Path(folder) / "data"),
                "ENV_FILE_PATH": str(Path(folder) / "missing.env"),
                "INSTANCE_LOCK_PATH": str(Path(folder) / "server.pid"),
                "HOST": "127.0.0.1",
                "SPARKKEEPER_BACKGROUND_TASKS": "0",
                "SESSION_WARN_EMAIL_ENABLED": "off",
                "FAILURE_BREAKER_THRESHOLD": "3",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUTF8": "1",
            })
            env.pop("GROK_BOT_WEBHOOK_URL", None)
            env.pop("GROK_BOT_WEBHOOK_KEY", None)
            for name in list(env):
                if name.startswith("SMTP_"):
                    env.pop(name)
            completed = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=root, env=env, text=True, encoding="utf-8",
                capture_output=True, timeout=30,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertIn("hooks-ok", completed.stdout)


if __name__ == "__main__":
    unittest.main()
