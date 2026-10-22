"""在随机本机端口启动临时服务，验证真实 HTTP Cookie 鉴权链路。"""

from __future__ import annotations

import hashlib
import http.cookiejar
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
TOKEN = "e2e-test-token-that-is-long-enough-456"
PUBLIC_PASSWORD = "e2e-public-password-123"
PUBLIC_SALT = "e2e-test-salt-value"
PUBLIC_HASH = hashlib.sha256(
    (PUBLIC_PASSWORD + PUBLIC_SALT).encode()
).hexdigest()


class EndToEndAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sandbox = tempfile.TemporaryDirectory(prefix="douyin-streak-e2e-")
        sandbox_path = Path(cls.sandbox.name)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]

        env = os.environ.copy()
        env.update({
            "DATA_DIR": str(sandbox_path / "data"),
            "ENV_FILE_PATH": str(sandbox_path / "missing.env"),
            "INSTANCE_LOCK_PATH": str(sandbox_path / "server.pid"),
            "LOGIN_GATE": "on",
            "AUTH_TOKEN": TOKEN,
            "URL_ACCESS_TOKEN": "",
            "URL_PASSWORD_SALT": "",
            "URL_PASSWORD_HASH": "",
            "HOST": "127.0.0.1",
            "PORT": str(cls.port),
            "SPARKKEEPER_BACKGROUND_TASKS": "0",
        })
        cls.process = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.cookies = http.cookiejar.CookieJar()
        cls.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cls.cookies)
        )
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                if cls.process.poll() is not None:
                    raise RuntimeError("临时服务启动失败")
                try:
                    with cls.opener.open(cls.url("/api/health"), timeout=0.5) as response:
                        health = json.load(response)
                        if health.get("ok") and health.get("app") == "sparkkeeper":
                            return
                except (OSError, urllib.error.URLError, json.JSONDecodeError):
                    time.sleep(0.1)
            raise RuntimeError("临时服务健康检查超时")
        except Exception:
            cls._cleanup()
            raise

    @classmethod
    def _cleanup(cls) -> None:
        process = getattr(cls, "process", None)
        if process is not None and process.poll() is None:
            if os.name == "nt":
                # venv python.exe can be a redirector; terminate only this
                # fixture's owned tree before waiting on its wrapper handle.
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, check=False)
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
    def tearDownClass(cls) -> None:
        cls._cleanup()

    @classmethod
    def url(cls, path: str) -> str:
        return f"http://127.0.0.1:{cls.port}{path}"

    @classmethod
    def request(cls, method: str, path: str, body: dict | None = None):
        raw = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            cls.url(path),
            data=raw,
            method=method,
            headers={"Origin": cls.url(""), **({"Content-Type": "application/json"} if raw else {}),
                     **({"X-Requested-With": "sparkkeeper"} if method.upper() not in {"GET", "HEAD", "OPTIONS"} else {})},
        )
        return cls.opener.open(request, timeout=3)

    def test_login_cookie_status_and_logout(self) -> None:
        with self.request("POST", "/api/auth/login", {"token": TOKEN}) as response:
            self.assertTrue(json.load(response)["ok"])
        session_cookie = next(
            cookie for cookie in self.cookies if cookie.name == "session_auth_token_" + str(self.port)
        )
        self.assertTrue(session_cookie.has_nonstandard_attr("HttpOnly"))

        with self.request("GET", "/api/status") as response:
            self.assertEqual(json.load(response)["version"], "2.9")
        with self.request("POST", "/api/auth/logout") as response:
            self.assertTrue(json.load(response)["ok"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("GET", "/api/status")
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()

    def test_url_token_login_rejected_when_public_mode_off(self) -> None:
        # 默认（未开启 PASSWORD_PUBLIC_LOGIN，且未配置 URL 令牌）：
        # 不带 access_token 的密码登录必须被拒绝。
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(
                "POST", "/api/auth/url-token-login",
                {"access_token": "", "password": PUBLIC_PASSWORD},
            )
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()

    def test_auth_mode_reports_public_disabled(self) -> None:
        with self.request("GET", "/api/auth/mode") as response:
            mode = json.load(response)
        self.assertFalse(mode["password_public"])
        self.assertFalse(mode["token_entry"])
        self.assertTrue(mode["login_gate"])


class PublicPasswordLoginTests(EndToEndAuthTests):
    """PASSWORD_PUBLIC_LOGIN=on：裸域名可仅凭密码登录。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sandbox = tempfile.TemporaryDirectory(prefix="douyin-streak-e2e-pub-")
        sandbox_path = Path(cls.sandbox.name)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]

        env = os.environ.copy()
        env.update({
            "DATA_DIR": str(sandbox_path / "data"),
            "ENV_FILE_PATH": str(sandbox_path / "missing.env"),
            "INSTANCE_LOCK_PATH": str(sandbox_path / "server.pid"),
            "LOGIN_GATE": "on",
            "AUTH_TOKEN": TOKEN,
            "URL_ACCESS_TOKEN": "e2e-url-access-token-32chars-xxxxxx",
            "URL_PASSWORD_SALT": PUBLIC_SALT,
            "URL_PASSWORD_HASH": PUBLIC_HASH,
            "PASSWORD_PUBLIC_LOGIN": "on",
            "HOST": "127.0.0.1",
            "PORT": str(cls.port),
            "SPARKKEEPER_BACKGROUND_TASKS": "0",
        })
        cls.process = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.cookies = http.cookiejar.CookieJar()
        cls.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cls.cookies)
        )
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                if cls.process.poll() is not None:
                    raise RuntimeError("临时服务启动失败")
                try:
                    with cls.opener.open(cls.url("/api/health"), timeout=0.5) as response:
                        health = json.load(response)
                        if health.get("ok") and health.get("app") == "sparkkeeper":
                            return
                except (OSError, urllib.error.URLError, json.JSONDecodeError):
                    time.sleep(0.1)
            raise RuntimeError("临时服务健康检查超时")
        except Exception:
            cls._cleanup()
            raise

    # 父类两个用例基于「开关关闭」环境，在本类环境不适用，覆盖为跳过。
    def test_url_token_login_rejected_when_public_mode_off(self) -> None:
        self.skipTest("PASSWORD_PUBLIC_LOGIN=on 场景不适用")

    def test_auth_mode_reports_public_disabled(self) -> None:
        self.skipTest("PASSWORD_PUBLIC_LOGIN=on 场景不适用")

    def test_auth_mode_reports_public_enabled(self) -> None:
        with self.request("GET", "/api/auth/mode") as response:
            mode = json.load(response)
        self.assertTrue(mode["password_public"])
        self.assertTrue(mode["token_entry"])

    def test_password_only_login_without_token(self) -> None:
        # 错误密码先拒绝
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(
                "POST", "/api/auth/url-token-login",
                {"access_token": "", "password": "wrong-password"},
            )
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()

        # 正确密码（不带任何访问令牌）→ 颁发 30 天设备 Cookie
        with self.request(
            "POST", "/api/auth/url-token-login",
            {"access_token": "", "password": PUBLIC_PASSWORD},
        ) as response:
            body = json.load(response)
        self.assertTrue(body["ok"])
        self.assertEqual(body["ttl_days"], 30)
        device_cookie = next(
            cookie for cookie in self.cookies
            if cookie.name == "device_auth_token_" + str(self.port)
        )
        self.assertTrue(device_cookie.has_nonstandard_attr("HttpOnly"))

        # 设备 Cookie 可直接访问受保护接口
        with self.request("GET", "/api/status") as response:
            self.assertEqual(json.load(response)["version"], "2.9")

    def test_wrong_url_token_still_rejected(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(
                "POST", "/api/auth/url-token-login",
                {"access_token": "forged-token", "password": PUBLIC_PASSWORD},
            )
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()


if __name__ == "__main__":
    unittest.main()
