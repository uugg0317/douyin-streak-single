"""审查回归测试（2026-10-13 代码审查产出）。

约定：每个用例在【修复前应失败】（标注"不得回退"的除外），修复后必须通过。
放置位置：tests/test_review_regressions.py
运行方式（仓库根目录）：
    python -m unittest tests.test_review_regressions -v

说明：
- 每个用例都在【全新子进程】里执行真实源码（项目模块在 import 时读取环境变量，
  不能在同一进程内复用）。
- 不会连接抖音、不会启动浏览器、不会发送任何消息。
- 若重构后内部函数改名，请同步调整打桩点，但【断言的语义不得改变】。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SCRUB = {
    "DATA_DIR", "ENV_FILE_PATH", "INSTANCE_LOCK_PATH", "LOGIN_GATE", "AUTH_TOKEN",
    "URL_ACCESS_TOKEN", "URL_PASSWORD_SALT", "URL_PASSWORD_HASH", "STATE_FILE_PATH",
    "SPARKKEEPER_BACKGROUND_TASKS", "QR_PUBLIC_ORIGIN", "ALLOWED_HOSTS",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "HOST", "PORT",
}


def _env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra)
    return env


def run_py(code: str, timeout: int = 90, **env_extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=str(ROOT), env=_env(**env_extra), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def _out(r: subprocess.CompletedProcess) -> str:
    return f"\n--- stdout ---\n{r.stdout}\n--- stderr(尾部) ---\n{r.stderr[-1500:]}"


def _stop_fixture_process(proc: subprocess.Popen) -> None:
    """只清理本用例启动的进程树，兼容 Windows venv 的重定向父进程。"""
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15, check=False)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    if proc.stdout is not None:
        proc.stdout.close()


def _isolated(tmp: str, **extra: str) -> dict:
    """每个用例使用独立的数据目录与不存在的 .env，避免污染仓库与互相影响。"""
    base = Path(tmp)
    env = {"DATA_DIR": str(base / "data"), "ENV_FILE_PATH": str(base / "missing.env")}
    env.update(extra)
    return env


# ───────────────────────── T05 鉴权边界 ─────────────────────────


class SchedulerBreaker(unittest.TestCase):
    def test_sustained_failures_never_skip_a_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = run_py('''
                import logging
                logging.disable(logging.CRITICAL)
                from core import scheduler, runtime

                class Clock:
                    t = 1_800_000_000.0
                    def time(self):
                        return Clock.t

                scheduler.time = Clock()
                fired = []
                scheduler._run_func = lambda: fired.append(1)
                FAIL = {"dry_run": False, "ok": [], "failed": [{"name": "A", "reason": "x"}],
                        "skipped": [], "deferred": []}
                row = ""
                for _ in range(9):
                    n = len(fired)
                    scheduler._daily_job()
                    sent = len(fired) > n
                    row += "S" if sent else "X"
                    if sent:
                        runtime.record_run(dict(FAIL))
                    Clock.t += 86400
                print("TIMELINE", row)
            ''', **_isolated(tmp))
        self.assertIn("TIMELINE SSSSSSSSS", r.stdout, _out(r))


# ───────────────────────── T01 同一天不得重复发送 ─────────────────────────


class DuplicateSend(unittest.TestCase):
    def test_already_sent_today_is_skipped_and_second_run_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = run_py('''
                import logging
                from datetime import datetime, timedelta
                from pathlib import Path
                logging.disable(logging.CRITICAL)
                import core.automation as auto
                from core import ledger

                now = datetime.now().astimezone()
                iso = lambda d: d.isoformat(timespec="seconds")
                ledger._save([
                    {"display_name": "friend_today", "selected": True, "has_conversation": True,
                     "last_sent_at": iso(now), "last_status": "success", "last_ok": True},
                    {"display_name": "friend_yesterday", "selected": True, "has_conversation": True,
                     "last_sent_at": iso(now - timedelta(days=1)), "last_status": "success", "last_ok": True},
                ])
                calls = []
                auto.get_valid_state_path = lambda: Path("/tmp/x")
                auto._get_prewarmed_page = lambda: {"p": None, "browser": None, "context": None, "page": object()}
                auto.check_login = lambda page: (True, "ok")
                auto._order_targets_by_list = lambda page, targets: targets
                auto.send_to_contact = lambda page, name, msg, dry: (calls.append(name) or (True, "ok"))
                auto._keep_prewarmed_alive = lambda *a, **k: True
                auto._close_prewarmed = lambda: None
                auto.update_runtime = lambda **k: None
                auto.load_runtime = lambda: {}
                for _ in range(2):
                    auto._run_send_impl(dry_run=False)
                print("CALLS", calls)
            ''', **_isolated(tmp))
        self.assertIn("CALLS ['friend_yesterday']", r.stdout, _out(r))


# ───────────────────────── T10 单实例锁 ─────────────────────────


class InstanceLock(unittest.TestCase):
    CODE = '''
        import logging
        logging.disable(logging.CRITICAL)
        from instance_lock import _acquire_instance_lock
        try:
            _acquire_instance_lock()
            print("LOCK acquired")
        except SystemExit as e:
            print("LOCK refused", e)
    '''

    def _env_for(self, tmp: str, pid_text: str | None = None) -> dict:
        pid_file = Path(tmp) / "server.pid"
        if pid_text is not None:
            pid_file.write_text(pid_text)
        return _isolated(tmp, INSTANCE_LOCK_PATH=str(pid_file))

    def test_stale_pid_file_naming_an_unrelated_live_process_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # 残留 PID 文件指向一个与服务无关的活进程（这里用测试进程自己）
            r = run_py(self.CODE, **self._env_for(tmp, str(os.getpid())))
        self.assertIn("LOCK acquired", r.stdout, _out(r))

    def test_two_concurrent_instances_are_mutually_exclusive(self) -> None:
        """不得回退：修复前后都必须通过。"""
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env_for(tmp)
            holder_code = textwrap.dedent(self.CODE) + "\nimport time\nprint('HOLD', flush=True)\ntime.sleep(25)\n"
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_code], cwd=str(ROOT), env=_env(**env),
                stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            )
            try:
                seen, deadline = "", time.time() + 40
                while time.time() < deadline and "HOLD" not in seen:
                    line = holder.stdout.readline()
                    if not line:
                        break
                    seen += line
                self.assertIn("LOCK acquired", seen, "第一个实例应成功获得锁")
                r = run_py(self.CODE, **env)
                self.assertIn("LOCK refused", r.stdout, _out(r))
            finally:
                _stop_fixture_process(holder)


# ───────────────────────── T04 SMTP ─────────────────────────


class SmtpSecurity(unittest.TestCase):
    def test_smtp_connections_verify_certificates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = run_py('''
                import smtplib, ssl
                from unittest import mock
                import email_service

                seen = {}

                class FakeSMTP:
                    def __init__(self, *a, **k):
                        seen["init"] = k.get("context")
                    def ehlo(self): pass
                    def starttls(self, *a, **k):
                        seen["starttls"] = k.get("context")
                    def login(self, *a): pass
                    def send_message(self, m): pass
                    def quit(self): pass
                    def close(self): pass

                def verifying(ctx):
                    return (isinstance(ctx, ssl.SSLContext)
                            and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname)

                cfg = {"smtp_host": "smtp.example.com", "smtp_user": "u@example.com",
                       "smtp_pass": "p" * 16, "mail_to": "t@example.com"}
                with mock.patch.object(smtplib, "SMTP_SSL", FakeSMTP), \\
                        mock.patch.object(smtplib, "SMTP", FakeSMTP):
                    seen.clear()
                    email_service._send_email({**cfg, "smtp_port": 465}, "s", "b")
                    print("SSL465", verifying(seen.get("init")))
                    seen.clear()
                    email_service._send_email({**cfg, "smtp_port": 587}, "s", "b")
                    print("STARTTLS587", verifying(seen.get("starttls")))
            ''', **_isolated(tmp))
        self.assertIn("SSL465 True", r.stdout, _out(r))
        self.assertIn("STARTTLS587 True", r.stdout, _out(r))

    def test_changing_smtp_host_requires_password_reentry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "service.env"
            env_file.write_text(
                "SMTP_HOST=smtp.qq.com\nSMTP_PORT=465\nSMTP_USER=a@qq.com\nSMTP_PASS=abcdefghijklmnop\n",
                encoding="utf-8",
            )
            r = run_py('''
                import logging
                logging.disable(logging.CRITICAL)
                import email_service
                try:
                    email_service.save_email_config({"smtp_host": "evil.example", "smtp_pass": "******"})
                    print("SAVE accepted")
                except ValueError:
                    print("SAVE rejected")
            ''', DATA_DIR=str(Path(tmp) / "data"), ENV_FILE_PATH=str(env_file))
        self.assertIn("SAVE rejected", r.stdout, _out(r))


# ───────────────────────── T13 .env 加载顺序 ─────────────────────────


class EnvLoading(unittest.TestCase):
    def test_data_dir_from_dotenv_is_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = (Path(tmp) / "from_env_file").resolve()
            env_file = Path(tmp) / "x.env"
            env_file.write_text(f"DATA_DIR={target}\n", encoding="utf-8")
            r = run_py('''
                import app_common
                from core.config import DATA_DIR
                print("DATA_DIR", DATA_DIR)
            ''', ENV_FILE_PATH=str(env_file))
        self.assertIn(f"DATA_DIR {target}", r.stdout, _out(r))


# ───────────────────────── T06 全局请求守卫（登录门关闭时） ─────────────────────────


@unittest.skipUnless(
    importlib.util.find_spec("uvicorn") and importlib.util.find_spec("fastapi"),
    "需要先安装 requirements.lock / requirements.txt 的依赖",
)
class RequestGuardWhenGateOff(unittest.TestCase):
    """用真实服务进程验证：关闭登录门（桌面版默认）时，网页与外来请求不能驱动写接口。

    探针用 /api/ledger/selection：在临时 DATA_DIR 里是无害的状态写入。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.sandbox = tempfile.TemporaryDirectory(prefix="streak-guard-")
        root = Path(cls.sandbox.name)
        # 合法选择用例必须有明确的合成目标；未知好友依然应由业务接口拒绝。
        data = root / "data"
        data.mkdir()
        (data / "ledger.json").write_text(json.dumps([{
            "entry_id": "a" * 32, "display_name": "x", "selected": False,
            "identity_ambiguous": False,
        }]), encoding="utf-8")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        env = _env(
            DATA_DIR=str(root / "data"), ENV_FILE_PATH=str(root / "missing.env"),
            INSTANCE_LOCK_PATH=str(root / "server.pid"), LOGIN_GATE="off", HOST="127.0.0.1",
            PORT=str(cls.port), SPARKKEEPER_BACKGROUND_TASKS="0",
        )
        cls.proc = subprocess.Popen(
            [sys.executable, "app.py"], cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/api/health", timeout=1).read()
                return
            except Exception:
                time.sleep(0.3)
        cls.tearDownClass()
        raise RuntimeError("测试服务未能在 40 秒内启动")

    @classmethod
    def tearDownClass(cls) -> None:
        proc = getattr(cls, "proc", None)
        if proc is not None:
            _stop_fixture_process(proc)
        sandbox = getattr(cls, "sandbox", None)
        if sandbox is not None:
            sandbox.cleanup()

    def _status(self, method: str, path: str, headers: dict | None = None, body: dict | None = None) -> int:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        if data is not None and "Content-Type" not in (headers or {}):
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as err:
            return err.code

    def test_cross_origin_state_change_is_rejected(self) -> None:
        status = self._status(
            "POST", "/api/ledger/selection",
            {"Origin": "http://evil.example", "X-Requested-With": "sparkkeeper"},
            {"selected_names": ["x"]},
        )
        self.assertEqual(status, 403)

    def test_state_change_without_custom_header_is_rejected(self) -> None:
        status = self._status(
            "POST", "/api/ledger/selection",
            {"Origin": f"http://127.0.0.1:{self.port}"}, {"selected_names": ["x"]},
        )
        self.assertEqual(status, 403)

    def test_foreign_host_header_is_rejected(self) -> None:
        status = self._status("GET", "/api/status", {"Host": f"evil.example:{self.port}"})
        self.assertEqual(status, 403)

    def test_legit_same_origin_requests_still_work(self) -> None:
        """不得回退：修复前后都必须通过。"""
        self.assertEqual(self._status("GET", "/api/status"), 200)
        status = self._status(
            "POST", "/api/ledger/selection",
            {"Origin": f"http://127.0.0.1:{self.port}", "X-Requested-With": "sparkkeeper"},
            {"selected_names": ["x"]},
        )
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
