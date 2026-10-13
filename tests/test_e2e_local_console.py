"""Fresh-install HTTP checks with temporary data and no account/browser activity."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_ADMIN_VARIABLES = (
    "LOGIN_GATE", "AUTH_TOKEN", "URL_ACCESS_TOKEN", "URL_PASSWORD_SALT",
    "URL_PASSWORD_HASH", "WHITELIST_IPS",
)


class TemporaryConsole:
    PUBLIC_ORIGIN = ""

    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.TemporaryDirectory(prefix="noadmin-console-http-")
        base = Path(cls.sandbox.name)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        env = os.environ.copy()
        for name in LEGACY_ADMIN_VARIABLES:
            env.pop(name, None)
        env.update({
            "ENV_FILE_PATH": str(base / "does-not-exist.env"),
            "DATA_DIR": str(base / "data"),
            "STATE_FILE_PATH": "",
            "INSTANCE_LOCK_PATH": str(base / "server.pid"),
            "HOST": "127.0.0.1", "PORT": str(cls.port),
            "SPARKKEEPER_BACKGROUND_TASKS": "0",
            "SESSION_WARN_EMAIL_ENABLED": "off",
            "QR_PUBLIC_ORIGIN": cls.PUBLIC_ORIGIN,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        cls.process = subprocess.Popen(
            [sys.executable, "-B", "app.py"], cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if cls.process.poll() is not None:
                    raise RuntimeError("Fresh console exited before its health check")
                try:
                    with cls.request("GET", "/api/health") as response:
                        if json.load(response).get("ok"):
                            return
                except (OSError, urllib.error.URLError, json.JSONDecodeError):
                    time.sleep(0.1)
            raise RuntimeError("Fresh console did not become healthy")
        except BaseException:
            cls._cleanup()
            raise

    @classmethod
    def _cleanup(cls):
        process = getattr(cls, "process", None)
        if process is not None and process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=10, check=False,
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        sandbox = getattr(cls, "sandbox", None)
        if sandbox is not None:
            sandbox.cleanup()

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()

    @classmethod
    def url(cls, path=""):
        return f"http://127.0.0.1:{cls.port}{path}"

    @classmethod
    def request(cls, method, path, body=None, headers=None, same_origin=True):
        raw = json.dumps(body).encode() if body is not None else None
        supplied = {"Content-Type": "application/json"} if raw is not None else {}
        if method not in {"GET", "HEAD", "OPTIONS"} and same_origin:
            supplied["Origin"] = cls.url()
        supplied.update(headers or {})
        request = urllib.request.Request(cls.url(path), data=raw, method=method, headers=supplied)
        return cls.opener.open(request, timeout=3)

    def assert_http_error(self, code, *args, **kwargs):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(*args, **kwargs)
        try:
            self.assertEqual(caught.exception.code, code)
            return caught.exception.read().decode("utf-8")
        finally:
            caught.exception.close()


class FreshInstallConsoleTests(TemporaryConsole, unittest.TestCase):
    def test_no_configuration_or_management_credentials_are_needed(self):
        with self.request("GET", "/api/status") as response:
            status = json.load(response)
            self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(status["version"], "2.6")
        self.assertNotIn("auth_required", status)
        self.assertFalse(status["state_file_exists"])
        self.assertFalse(status["background_tasks_enabled"])

    def test_index_contains_console_without_a_management_login_form(self):
        with self.request("GET", "/") as response:
            html = response.read().decode("utf-8")
        self.assertIn("扫码登录", html)
        for removed in ("请输入访问密码", "验证并进入", "X-Auth-Token", "/api/auth/"):
            self.assertNotIn(removed, html)

    def test_all_former_management_endpoints_are_absent(self):
        self.assert_http_error(404, "GET", "/api/auth/challenge")
        for path in ("login", "url-token-login", "logout"):
            self.assert_http_error(404, "POST", "/api/auth/" + path, {})

    def test_local_same_origin_config_write_needs_no_management_cookie(self):
        with self.request("POST", "/api/config", {"auto_run_enabled": False}) as response:
            self.assertEqual(response.status, 200)
            self.assertNotIn("Set-Cookie", response.headers)

    def test_cross_origin_and_missing_origin_writes_are_rejected(self):
        self.assert_http_error(403, "POST", "/api/config", {}, headers={"Origin": "https://attacker.invalid"})
        self.assert_http_error(403, "POST", "/api/config", {}, same_origin=False)

    def test_wrong_host_cannot_rebind_local_console(self):
        self.assert_http_error(403, "GET", "/api/status", headers={"Host": "attacker.invalid"})

    def test_qr_route_keeps_its_own_request_validation_without_launching_browser(self):
        self.assert_http_error(403, "POST", "/api/credentials/qr/start", {"request_id": "invalid"})
        self.assert_http_error(
            400, "POST", "/api/credentials/qr/start", {"request_id": "invalid"},
            headers={"X-Spark-QR-Request": "1"},
        )


class ConfiguredProxyConsoleTests(TemporaryConsole, unittest.TestCase):
    PUBLIC_ORIGIN = "https://console.example.com"

    def proxy_headers(self, **overrides):
        result = {
            "Host": "console.example.com", "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "203.0.113.10", "Origin": self.PUBLIC_ORIGIN,
        }
        result.update(overrides)
        return result

    def test_explicit_public_origin_allows_loopback_reverse_proxy(self):
        with self.request("GET", "/api/status", headers=self.proxy_headers()) as response:
            self.assertEqual(response.status, 200)
            self.assertNotIn("auth_required", json.load(response))

    def test_proxy_config_write_needs_origin_but_not_management_credentials(self):
        with self.request("POST", "/api/config", {"auto_run_enabled": False}, headers=self.proxy_headers()) as response:
            self.assertEqual(response.status, 200)

    def test_proxy_qr_error_is_business_validation_instead_of_admin_login(self):
        body = self.assert_http_error(
            400, "POST", "/api/credentials/qr/start", {"request_id": "invalid"},
            headers=self.proxy_headers(**{"X-Spark-QR-Request": "1"}),
        )
        self.assertIn("扫码", body)

    def test_proxy_rejects_other_origins_hosts_and_forwarded_hosts(self):
        for extra in (
            {"Origin": "https://attacker.invalid"},
            {"Host": "attacker.invalid"},
            {"X-Forwarded-Host": "attacker.invalid"},
            {"X-Forwarded-Proto": "http"},
        ):
            self.assert_http_error(403, "GET", "/api/status", headers=self.proxy_headers(**extra))


if __name__ == "__main__":
    unittest.main()
