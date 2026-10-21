"""Local browser login and local-only storage-state export.

This helper has NO remote HTTP client and never uploads cookies. A trusted
website can launch it and receive status/count only. Exports stay on this PC.
"""
from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HELPER_ORIGIN = "http://127.0.0.1:17653"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
# The legacy BAT also launches this file directly, outside package import mode.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from state_validation import MAX_STATE, validate_storage_state

ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,80}$")
JOB_RE = re.compile(r"^[a-f0-9]{32}$")
ACTIVE = {"starting", "waiting", "saving", "stopping"}
ERROR_CODES = {"browser_failed", "browser_closed", "invalid_state", "save_failed", "cancelled",
               "timeout", "cleanup_failed", "helper_failed"}


def normalize_origin(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid_origin")
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid_origin")
    host = parsed.hostname.lower().encode("idna").decode("ascii")
    if parsed.scheme == "http":
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == "localhost"
        if not local:
            raise ValueError("https_required")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("invalid_origin") from None
    if ":" in host:
        host = "[" + host + "]"
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        host += ":" + str(port)
    return parsed.scheme + "://" + host


def has_session(cookies, now=None):
    now = time.time() if now is None else now
    if not isinstance(cookies, list):
        return False
    for cookie in cookies:
        if not isinstance(cookie, dict) or cookie.get("name") not in {"sessionid", "sessionid_ss"}:
            continue
        domain, value, expiry = cookie.get("domain"), cookie.get("value"), cookie.get("expires", -1)
        if not isinstance(domain, str) or not isinstance(value, str) or not value.strip():
            continue
        domain = domain.lower().lstrip(".")
        if domain != "douyin.com" and not domain.endswith(".douyin.com"):
            continue
        if (isinstance(expiry, bool) or not isinstance(expiry, (float, int)) or not math.isfinite(expiry)
                or (expiry != -1 and expiry <= now)):
            continue
        return True
    return False


def validate_state(raw, now=None):
    state = validate_storage_state(raw)
    # QR export retains its stricter session-cookie precondition. This remains
    # a local candidate check, not proof of acceptance by the remote platform.
    if not has_session(state["cookies"], now):
        raise ValueError("invalid_state")
    return state


def atomic_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def os_guard(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    locked = False
    try:
        if not stream.tell():
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def terminate_tree(proc):
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except Exception:
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()


class HelperManager:
    def __init__(self, trusted_origin, root, *, popen=None):
        self.trusted_origin = normalize_origin(trusted_origin)
        self.root = Path(root).resolve()
        self.nonce = secrets.token_urlsafe(32)
        self.popen = popen or subprocess.Popen
        self.lock = threading.RLock()
        self.task = None
        self.seen_requests = set()
        self.closed = False

    def snapshot(self, *, local=False):
        task = self.task
        result = {"request_id": task["request_id"] if task else None, "job_id": task["job_id"] if task else None,
                  "status": task["status"] if task else "idle", "count": task["count"] if task else 0,
                  "error": task["error"] if task else None,
                  "running": bool(task and task["status"] in ACTIVE)}
        if local:
            result["download_ready"] = bool(task and task.get("export") and task["export"].is_file())
            result["saved_path"] = str(task["export"]) if task and task.get("export") else None
        return result

    def start(self, body):
        if not isinstance(body, dict) or set(body) != {"origin", "request_id", "deadline"} or body.get("origin") != self.trusted_origin:
            raise ValueError("origin_mismatch")
        request_id, deadline = body.get("request_id"), body.get("deadline")
        if (not isinstance(request_id, str) or not ID_RE.fullmatch(request_id) or isinstance(deadline, bool)
                or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)
                or deadline <= time.time() or deadline > time.time() + 310):
            raise ValueError("invalid_request")
        with self.lock:
            if self.closed:
                raise ValueError("helper_closed")
            if self.task and self.task["request_id"] == request_id:
                return self.snapshot(local=True)
            if request_id in self.seen_requests:
                raise ValueError("stale_job")
            if self.task and self.task["status"] in ACTIVE:
                raise ValueError("busy")
            if len(self.seen_requests) >= 1000:
                raise ValueError("restart_required")
            temporary_root = self.root / "tasks"
            temporary_root.mkdir(parents=True, exist_ok=True)
            try:
                with os_guard(temporary_root / "worker.guard"):
                    for folder in temporary_root.iterdir():
                        if folder.is_dir() and JOB_RE.fullmatch(folder.name) and folder.resolve().parent == temporary_root:
                            shutil.rmtree(folder)
            except OSError:
                raise ValueError("busy") from None
            job_id = uuid.uuid4().hex
            task = {"request_id": request_id, "job_id": job_id, "deadline": min(deadline, time.time() + 300),
                    "end": time.monotonic() + min(300, deadline - time.time()), "status": "starting", "count": 0,
                    "error": None, "folder": temporary_root / job_id, "proc": None,
                    "stop": threading.Event(), "browser_closed": False, "export": None}
            self.task = task
            self.seen_requests.add(request_id)
            try:
                task["folder"].mkdir(mode=0o700)
                flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0
                env = dict(os.environ)
                env.pop("STATE_FILE_PATH", None)
                if os.name == "nt":
                    env.pop("DISPLAY", None)
                task["proc"] = self.popen([sys.executable, str(Path(__file__).resolve()), "--worker",
                    "--task-dir", str(task["folder"]), "--deadline", str(task["deadline"]),
                    "--parent-pid", str(os.getpid())], cwd=str(PROJECT_ROOT), env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=flags, start_new_session=os.name != "nt")
                threading.Thread(target=self._monitor, args=(task,), daemon=True, name="local-cookie-monitor").start()
            except Exception:
                self._finish(task, "failed", "helper_failed")
                raise ValueError("helper_failed") from None
            return self.snapshot(local=True)

    def _close_browser(self, task):
        if task["browser_closed"]:
            return True
        proc = task["proc"]
        if proc is None:
            task["browser_closed"] = True
            return True
        try:
            atomic_json(task["folder"] / "stop.json", {"stop": True})
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                terminate_tree(proc)
                proc.wait(timeout=4)
            except Exception:
                return False
        task["browser_closed"] = True
        return True

    def _finish(self, task, status, code=None):
        task.update(ending_status=status, ending_error=code)
        if not self._close_browser(task):
            task.update(status="stopping", error="cleanup_failed")
            return False
        folder = task["folder"].resolve()
        if folder.parent != self.root / "tasks" or not JOB_RE.fullmatch(folder.name):
            task.update(status="stopping", error="cleanup_failed")
            return False
        try:
            shutil.rmtree(folder)
        except FileNotFoundError:
            pass
        except OSError:
            task.update(status="stopping", error="cleanup_failed")
            return False
        task.update(status=status, error=code)
        return True

    def _export(self, task):
        path = task["folder"] / "candidate.json"
        if path.stat().st_size > MAX_STATE:
            raise ValueError("invalid_state")
        raw = path.read_bytes()
        data = validate_state(raw)
        export_root = self.root / "exports"
        export_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = export_root / ("state_" + task["job_id"] + ".json")
        created = False
        try:
            # Exclusive creation preserves every previous export, even on UUID
            # collision or if a user already created this exact filename.
            with target.open("xb") as stream:
                created = True
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            if created:
                target.unlink(missing_ok=True)
            raise ValueError("save_failed") from None
        task.update(export=target, count=len(data["cookies"]))

    def poll_once(self):
        with self.lock:
            task = self.task
            if not task or task["status"] not in ACTIVE:
                return self.snapshot(local=True)
            if task["status"] == "stopping":
                self._finish(task, task.get("ending_status", "failed"), task.get("ending_error", "cleanup_failed"))
                return self.snapshot(local=True)
            if task["stop"].is_set() or time.monotonic() >= task["end"]:
                code = "cancelled" if task["stop"].is_set() else "timeout"
                self._finish(task, code, code)
                return self.snapshot(local=True)
            state = {}
            try:
                path = task["folder"] / "status.json"
                if path.stat().st_size < 4096:
                    state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            if not isinstance(state, dict):
                state = {}
            worker_status = state.get("status")
            if worker_status == "waiting":
                task["status"] = "waiting"
            if worker_status == "success":
                try:
                    if not self._close_browser(task):
                        task.update(status="stopping", error="cleanup_failed", ending_status="failed", ending_error="cleanup_failed")
                        return self.snapshot(local=True)
                    if time.monotonic() >= task["end"]:
                        self._finish(task, "timeout", "timeout")
                    else:
                        task["status"] = "saving"
                        self._export(task)
                        self._finish(task, "success")
                except Exception as exc:
                    code = str(exc) if isinstance(exc, ValueError) and str(exc) in ERROR_CODES else "helper_failed"
                    self._finish(task, "failed", code)
            elif worker_status in {"failed", "timeout", "cancelled"}:
                code = state.get("error")
                self._finish(task, worker_status, code if code in ERROR_CODES else "browser_failed")
            elif task["proc"] is not None and task["proc"].poll() is not None:
                self._finish(task, "failed", "browser_failed")
            return self.snapshot(local=True)

    def _monitor(self, task):
        while True:
            with self.lock:
                if task is not self.task or task["status"] not in ACTIVE:
                    return
            self.poll_once()
            time.sleep(.2)

    def cancel(self, body):
        with self.lock:
            if (not isinstance(body, dict) or not self.task or body.get("job_id") != self.task["job_id"]
                    or body.get("request_id") != self.task["request_id"]):
                raise ValueError("stale_job")
            task = self.task
            if task["status"] == "stopping":
                self._finish(task, task["ending_status"], task["ending_error"])
            elif task["status"] in ACTIVE:
                task["stop"].set()
                self._finish(task, "cancelled", "cancelled")
            return self.snapshot(local=True)

    def download(self, job_id):
        with self.lock:
            task = self.task
            if not task or task["job_id"] != job_id or task["status"] != "success" or not task["export"]:
                raise ValueError("not_ready")
            return task["export"].read_bytes()

    def cleanup(self):
        with self.lock:
            self.closed = True
            if self.task and self.task["status"] == "stopping":
                self._finish(self.task, self.task["ending_status"], self.task["ending_error"])
            elif self.task and self.task["status"] in ACTIVE:
                self.task["stop"].set()
                self._finish(self.task, "cancelled", "cancelled")


def request_allowed(peer, host, origin=None, *, write=False):
    return peer == "127.0.0.1" and host == "127.0.0.1:17653" and (
        origin == HELPER_ORIGIN if write else origin in {None, HELPER_ORIGIN})


def handler_for(manager):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalCookieHelper"
        def log_message(self, *args):
            pass
        def reply(self, code, value, *, html=False, download=False):
            raw = value if html or download else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if download:
                self.send_header("Content-Disposition", 'attachment; filename="state.json"')
            if html:
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def allowed(self, write=False):
            if not request_allowed(self.client_address[0], self.headers.get("Host"), self.headers.get("Origin"), write=write):
                self.reply(403, {"error": "forbidden"})
                return False
            return True
        def nonce_allowed(self):
            if not secrets.compare_digest(self.headers.get("X-Helper-Nonce", ""), manager.nonce):
                self.reply(403, {"error": "forbidden"})
                return False
            return True
        def do_OPTIONS(self):
            self.reply(403, {"error": "forbidden"})
        def do_GET(self):
            if not self.allowed():
                return
            if self.path == "/":
                self.reply(200, (PROJECT_ROOT / "static" / "local-cookie-helper.html").read_bytes(), html=True)
            elif self.path == "/helper/config":
                self.reply(200, {"trusted_origin": manager.trusted_origin, "page_nonce": manager.nonce, "status": manager.snapshot(local=True)})
            elif self.path == "/helper/status":
                with manager.lock:
                    self.reply(200, manager.snapshot(local=True))
            elif self.path.startswith("/helper/download/"):
                if not self.nonce_allowed():
                    return
                try:
                    self.reply(200, manager.download(self.path[len("/helper/download/"):]), download=True)
                except (ValueError, OSError):
                    self.reply(404, {"error": "not_ready"})
            else:
                self.reply(404, {"error": "not_found"})
        def do_POST(self):
            # Drain a bounded body before rejecting authentication. On Windows
            # closing a socket with unread POST bytes can reset the connection
            # before the caller receives the intended 403 response.
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError()
                self.connection.settimeout(3)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError()
            except Exception:
                self.reply(400, {"error": "invalid_request"})
                return
            if not self.allowed(write=True) or not self.nonce_allowed():
                return
            try:
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    raise ValueError("invalid_request")
                body = json.loads(raw.decode("utf-8"))
                if self.path == "/helper/extract":
                    result = manager.start(body)
                elif self.path == "/helper/cancel":
                    result = manager.cancel(body)
                else:
                    self.reply(404, {"error": "not_found"})
                    return
                self.reply(200, result)
            except ValueError as exc:
                code = str(exc) if str(exc) in {"origin_mismatch", "invalid_request", "helper_closed", "busy", "stale_job", "helper_failed", "restart_required"} else "invalid_request"
                self.reply(409 if code in {"busy", "stale_job"} else 400, {"error": code})
            except Exception:
                self.reply(500, {"error": "helper_failed"})
    return Handler


@contextlib.contextmanager
def fresh_browser():
    from playwright.sync_api import sync_playwright
    driver = sync_playwright().start()
    browser = None
    try:
        browser = driver.chromium.launch(headless=False)
        context = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai", ignore_https_errors=False)
        page = context.new_page()
        yield browser, context, page
    finally:
        try:
            if browser:
                browser.close()
        finally:
            driver.stop()


def extract_worker(folder, deadline, *, browser_factory=fresh_browser, clock=time.time, sleep=time.sleep):
    status, code, count = "failed", "browser_failed", 0
    try:
        with browser_factory() as (browser, context, page):
            atomic_json(folder / "status.json", {"status": "waiting"})
            try:
                page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            while clock() < deadline:
                if (folder / "stop.json").exists():
                    status, code = "cancelled", "cancelled"
                    break
                if page.is_closed() or not browser.is_connected():
                    code = "browser_closed"
                    break
                if has_session(context.cookies(), clock()):
                    data = context.storage_state()
                    validate_state(json.dumps(data).encode("utf-8"), clock())
                    atomic_json(folder / "candidate.json", data)
                    status, code, count = "success", None, len(data["cookies"])
                    break
                sleep(.5)
            else:
                status, code = "timeout", "timeout"
        # Success is visible to the parent only after browser AND driver close.
    except Exception:
        status, code = "failed", "browser_failed"
    if status != "success":
        (folder / "candidate.json").unlink(missing_ok=True)
    atomic_json(folder / "status.json", {"status": status, "count": count, "error": code})
    return 0 if status == "success" else 1


def parent_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def worker_main(args):
    folder = args.task_dir.resolve()
    if not JOB_RE.fullmatch(folder.name) or not folder.is_dir() or not parent_alive(args.parent_pid):
        return 2
    seconds = max(0, min(300, args.deadline - time.time()))
    if not seconds:
        return 2
    done = threading.Event()
    end_monotonic = time.monotonic() + seconds
    def watchdog():
        while not done.wait(.5):
            if time.monotonic() < end_monotonic and parent_alive(args.parent_pid):
                continue
            try:
                atomic_json(folder / "stop.json", {"stop": True})
            except OSError:
                pass
            if done.wait(3):
                return
            try:
                (folder / "candidate.json").unlink(missing_ok=True)
                class OwnProcess:
                    pid = os.getpid()
                    def kill(self):
                        os._exit(2)
                terminate_tree(OwnProcess())
            finally:
                os._exit(2)
            return
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        with os_guard(folder.parent / "worker.guard"):
            if not parent_alive(args.parent_pid):
                return 2
            return extract_worker(folder, time.time() + seconds)
    finally:
        done.set()


def main(argv=None):
    parser = argparse.ArgumentParser(description="本机 Cookie 登录助手（只在本机导出，不回传网站）")
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--task-dir", type=Path)
    parser.add_argument("--deadline", type=float)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args(argv)
    if args.worker:
        if args.task_dir is None or args.deadline is None or args.parent_pid is None:
            return 2
        return worker_main(args)
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SparkkeeperCookieHelper"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path = base / "settings.json"
    origin = None
    if not args.configure:
        try:
            origin = normalize_origin(json.loads(config_path.read_text(encoding="utf-8"))["trusted_origin"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    while origin is None:
        try:
            origin = normalize_origin(input("请输入允许发起本机登录的网站网址（仅用于信任，不接收Cookie）：").strip())
        except ValueError:
            print("网址无效，请输入 HTTPS 网站，或本机 127.0.0.1/localhost 地址。")
    atomic_json(config_path, {"trusted_origin": origin})
    manager = HelperManager(origin, base)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 17653), handler_for(manager))
    except OSError:
        print("本机助手端口已被占用。请使用已启动的助手，或关闭后重试。")
        return 1
    server.daemon_threads = True
    print("本机 Cookie 助手已启动：" + HELPER_ORIGIN)
    print("允许发起登录的网站：" + origin)
    print("提取结果仅保存到本机，网站只收到任务状态。按 Ctrl+C 停止并关闭登录浏览器。")
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        manager.cleanup()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
