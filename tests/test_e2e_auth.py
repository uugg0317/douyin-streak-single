"""在随机本机端口启动临时服务，验证真实 HTTP Cookie 鉴权链路。"""

from __future__ import annotations

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
            headers={"Content-Type": "application/json"} if raw else {},
        )
        return cls.opener.open(request, timeout=3)

    def test_login_cookie_status_and_logout(self) -> None:
        with self.request("POST", "/api/auth/login", {"token": TOKEN}) as response:
            self.assertTrue(json.load(response)["ok"])
        session_cookie = next(
            cookie for cookie in self.cookies if cookie.name == "session_auth_token"
        )
        self.assertTrue(session_cookie.has_nonstandard_attr("HttpOnly"))

        with self.request("GET", "/api/status") as response:
            self.assertEqual(json.load(response)["version"], "2.5")
        with self.request("POST", "/api/auth/logout") as response:
            self.assertTrue(json.load(response)["ok"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("GET", "/api/status")
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()


if __name__ == "__main__":
    unittest.main()
