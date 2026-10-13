"""Local-only export CUA fixture: fake website plus fake loopback helper.

No Cookie values, storage-state contents, tickets, remote upload or browser are
created here. The helper only simulates local export and reports safe status/count.
Run: python tests/local_cookie_fixture.py
Website: http://127.0.0.1:8765/ (opens directly without a management login)
Helper: http://127.0.0.1:17653/ (a clearly marked simulated helper popup)
GET /__test/state exposes safe counters, old endpoint calls and fake browser flags.
POST /__test/control: auto_complete_seconds (null for manual), expire, cancelled.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = (ROOT / "static").resolve()
ALLOWED_SUFFIXES = {".html", ".js", ".css", ".svg", ".png", ".jpg", ".webp", ".woff", ".woff2", ".ico"}


class LocalExportFixture:
    def __init__(self, cloud_port):
        self.lock = threading.RLock()
        self.website_origin = f"http://127.0.0.1:{cloud_port}"
        self.auto_complete_seconds = 2.0
        self.old_endpoint_calls = []
        self.calls = []
        self.task = None
        self.browser_open = False
        self.browser_closed = False
        self.exported_locally = False
        self.offset = 0

    def refresh(self):
        if self.task and self.task["running"] and time.time() + self.offset >= self.task["deadline"]:
            self.finish("timeout")

    def finish(self, status):
        if not self.task:
            return
        self.task.update(status=status, running=False)
        self.browser_open = False
        self.browser_closed = True
        if status == "success":
            self.task["count"] = 12
            self.exported_locally = True

    def snapshot(self):
        self.refresh()
        if not self.task:
            return {"status": "idle", "running": False, "count": 0, "request_id": None, "job_id": None}
        return {key: self.task[key] for key in ("request_id", "job_id", "status", "running", "count", "deadline")}


FIXTURE = None


class BaseHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, body, code=200, content_type="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 <= length <= 16384:
            raise ValueError("invalid fixture body size")
        return self.rfile.read(length)

    def data(self):
        value = json.loads(self.body() or b"{}")
        if not isinstance(value, dict):
            raise ValueError("object body required")
        return value

    def error(self, code, detail):
        self.respond({"detail": detail}, code)

    def deprecated(self, path):
        if path.startswith("/api/credentials/extract") or path.startswith("/api/credentials/local/"):
            with FIXTURE.lock:
                FIXTURE.old_endpoint_calls.append({"method": self.command, "path": path})
            self.error(410, "自动回传入口已停用；本机提取仅导出到用户电脑，网站不会接收登录态。")
            return True
        return False


class CloudHandler(BaseHandler):
    def serve_static(self, path):
        if path == "/":
            path = "/static/index.html"
        if path == "/favicon.ico":
            path = "/static/favicon.svg"
        if not path.startswith("/static/"):
            return self.error(404, "Only static and fake business endpoints are available")
        relative = path[len("/static/"):]
        if "\\" in relative or any(part in {".", ".."} or part.startswith(".") for part in relative.split("/")):
            return self.error(404, "Invalid fixture static path")
        target = (STATIC_ROOT / relative).resolve()
        try:
            target.relative_to(STATIC_ROOT)
        except ValueError:
            return self.error(404, "Invalid fixture static path")
        if not target.is_file() or target.suffix.lower() not in ALLOWED_SUFFIXES:
            return self.error(404, "Static fixture file not found")
        self.respond(target.read_bytes(), content_type=mimetypes.guess_type(str(target))[0] or "application/octet-stream")

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if self.deprecated(path):
            return
        if path == "/__test/state":
            with FIXTURE.lock:
                return self.respond({"fixture": True, "status": FIXTURE.snapshot(),
                                     "old_endpoint_calls": FIXTURE.old_endpoint_calls, "calls": FIXTURE.calls,
                                     "browser_open": FIXTURE.browser_open, "browser_closed": FIXTURE.browser_closed,
                                     "exported_locally": FIXTURE.exported_locally, "website_received_login_state": False})
        if path == "/api/health":
            return self.respond({"ok": True, "app": "sparkkeeper", "version": "2.6", "fixture": True})
        if not path.startswith("/api/"):
            return self.serve_static(path)
        if path == "/api/status":
            return self.respond({"version": "2.6", "running": False, "has_state": False,
                                 "session_status": "unknown", "next_run": None, "next_backup": None,
                                 "last_run": None, "manual_required": False, "auto_run_enabled": False,
                                 "schedule_enabled": False, "latest_result": None})
        if path == "/api/config":
            return self.respond({"config": {"schedule_time": "00:00", "jitter_minutes": 0, "send_gap_min": 6,
                                            "send_gap_max": 12, "max_friends_per_run": 20,
                                            "friends": [], "messages": ["[续火花吧]"], "auto_run_enabled": False}})
        if path in {"/api/ledger", "/api/contacts"}:
            return self.respond({"entries": [], "contacts": [], "selected_count": 0})
        if path == "/api/ledger/stats":
            return self.respond({"total": 0, "selected": 0, "ok": 0, "failed": 0})
        if path == "/api/logs":
            return self.respond({"logs": "隔离模拟：未连接真实抖音账号，网站未接收登录态。"})
        if path == "/api/browser/status":
            return self.respond({"running": False, "open": False})
        if path == "/api/email-config":
            return self.respond({})
        return self.error(404, "Unknown simulated website endpoint")

    def do_POST(self):
        path = unquote(urlsplit(self.path).path)
        try:
            if self.deprecated(path):
                self.body()
                return
            if path == "/__test/control":
                data = self.data()
                with FIXTURE.lock:
                    if "auto_complete_seconds" in data:
                        value = data["auto_complete_seconds"]
                        FIXTURE.auto_complete_seconds = None if value is None else max(0, min(120, float(value)))
                    if data.get("expire"):
                        FIXTURE.offset += 301
                    if data.get("cancelled"):
                        FIXTURE.finish("cancelled")
                    return self.respond({"ok": True, "status": FIXTURE.snapshot()})
            self.body()
            with FIXTURE.lock:
                FIXTURE.calls.append({"method": "POST", "path": path})
            return self.error(400, "此模拟网站不执行其他任务或接收登录态")
        except (ValueError, TypeError, json.JSONDecodeError):
            self.error(400, "Invalid fixture request")


HELPER_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>本机导出助手 · 隔离模拟</title>
<style>body{background:#12100c;color:#e9ddbe;font-family:system-ui,'Microsoft YaHei';margin:0;padding:34px}main{max-width:460px;margin:auto}h1{font-size:23px}p{line-height:1.8;color:#b7aa8e}button{background:#b68c3f;color:#151009;border:0;border-radius:12px;padding:14px 22px;font-size:16px;cursor:pointer}button:disabled{opacity:.4}small{display:block;color:#8e826b;margin-top:24px}</style>
<main><h1>本机导出助手 · 隔离模拟</h1><p id="status">等待网站请求。此页只模拟本机导出，不会打开真实抖音。</p><button id="complete" disabled>模拟本人登录成功并本机导出</button><small>网站只会收到状态和计数，不接收登录数据。完成后模拟浏览器标志自动关闭。</small></main>
<script>
const origin=new URLSearchParams(location.hash.slice(1)).get('origin');
let task=null, busy=false, pollTimer=null;
const statusEl=document.getElementById('status'), button=document.getElementById('complete');
const allowed=origin&&/^http:\\/\\/127\\.0\\.0\\.1:\\d+$/.test(origin);
function message(type,payload){if(allowed&&window.opener)window.opener.postMessage({type,payload},origin);}
function progress(data){message('sparkkeeper-local-cookie-progress',{request_id:data.request_id,job_id:data.job_id,status:data.status,count:data.count,error:''});}
async function poll(){if(!task)return;try{const r=await fetch('/__fixture/status?request_id='+encodeURIComponent(task.request_id)),d=await r.json();if(!r.ok)return;progress(d);if(d.running)pollTimer=setTimeout(poll,500);else{button.disabled=true;statusEl.textContent=d.status==='success'?'已模拟导出到本机并关闭登录窗口。网站未接收登录态。':d.status==='timeout'?'模拟任务超时，未导出文件。':'模拟任务已取消，未导出文件。';}}catch(e){statusEl.textContent='模拟状态暂时无法更新。';}}
async function finish(){if(!task||busy)return;busy=true;button.disabled=true;try{const r=await fetch('/__fixture/complete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:task.request_id})}),d=await r.json();if(!r.ok)throw Error(d.detail||'模拟本机导出未完成');progress(d);statusEl.textContent='已模拟导出到本机并关闭登录窗口。网站未接收登录态。';}catch(e){statusEl.textContent=e.message;busy=false;button.disabled=false;}}
button.addEventListener('click',finish);
window.addEventListener('message',async e=>{if(!allowed||e.origin!==origin||e.source!==window.opener||!e.data)return;const p=e.data.payload||{};if(e.data.type==='sparkkeeper-local-cookie-start'){if(p.origin!==origin||typeof p.request_id!=='string')return;try{const r=await fetch('/__fixture/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({origin:p.origin,request_id:p.request_id,deadline:p.deadline})}),d=await r.json();if(!r.ok)throw Error(d.detail||'模拟助手启动失败');task={request_id:p.request_id};message('sparkkeeper-local-cookie-ack',{request_id:p.request_id,job_id:d.job_id,ok:true});progress(d);button.disabled=false;statusEl.textContent='模拟登录窗口已打开，等待本人登录。';poll();if(d.auto_complete_seconds!==null)setTimeout(finish,d.auto_complete_seconds*1000);}catch(e){statusEl.textContent=e.message;message('sparkkeeper-local-cookie-error',{request_id:p.request_id});}}else if(e.data.type==='sparkkeeper-local-cookie-cancel'&&task&&p.request_id===task.request_id){await fetch('/__fixture/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:task.request_id})});poll();}});
if(allowed)message('sparkkeeper-local-cookie-ready',{origin});else statusEl.textContent='请从本机模拟网站点击提取后打开此页。';
</script></html>"""


class HelperHandler(BaseHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            return self.respond(HELPER_HTML.encode(), content_type="text/html; charset=utf-8")
        if parsed.path == "/__fixture/status":
            request_id = parse_qs(parsed.query).get("request_id", [None])[0]
            with FIXTURE.lock:
                if not FIXTURE.task or request_id != FIXTURE.task["request_id"]:
                    return self.error(404, "No matching local simulated task")
                return self.respond(FIXTURE.snapshot())
        return self.error(404, "Unknown simulated helper endpoint")

    def do_POST(self):
        try:
            if self.headers.get("Origin") != "http://127.0.0.1:17653":
                self.body()
                return self.error(403, "Simulated helper accepts only its own page")
            data = self.data()
            with FIXTURE.lock:
                FIXTURE.refresh()
                if self.path == "/__fixture/start":
                    if data.get("origin") != FIXTURE.website_origin or not isinstance(data.get("request_id"), str):
                        return self.error(400, "Invalid simulated local request")
                    if set(data) - {"origin", "request_id", "deadline"}:
                        return self.error(400, "Only safe local request metadata is accepted")
                    if FIXTURE.task and FIXTURE.task["running"]:
                        return self.error(409, "已有本机模拟任务")
                    deadline = float(data.get("deadline") or (time.time() + 300))
                    if deadline > 1e12:
                        deadline /= 1000
                    FIXTURE.task = {"request_id": data["request_id"], "job_id": secrets.token_hex(8),
                                    "status": "waiting", "running": True, "count": 0,
                                    "deadline": min(deadline, time.time() + 300)}
                    FIXTURE.browser_open = True
                    FIXTURE.browser_closed = False
                    FIXTURE.exported_locally = False
                    FIXTURE.offset = 0
                    return self.respond({**FIXTURE.snapshot(), "auto_complete_seconds": FIXTURE.auto_complete_seconds})
                if not FIXTURE.task or data.get("request_id") != FIXTURE.task["request_id"]:
                    return self.error(404, "No matching local simulated task")
                if self.path == "/__fixture/complete":
                    if not FIXTURE.task["running"]:
                        return self.error(409, "模拟任务已结束，未新增本机导出")
                    FIXTURE.finish("success")
                    return self.respond(FIXTURE.snapshot())
                if self.path == "/__fixture/cancel":
                    if FIXTURE.task["running"]:
                        FIXTURE.finish("cancelled")
                    return self.respond(FIXTURE.snapshot())
                return self.error(404, "Unknown simulated helper endpoint")
        except (ValueError, TypeError, json.JSONDecodeError):
            self.error(400, "Invalid simulated local request")


def main():
    global FIXTURE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud-port", type=int, default=8765)
    args = parser.parse_args()
    FIXTURE = LocalExportFixture(args.cloud_port)
    cloud = ThreadingHTTPServer(("127.0.0.1", args.cloud_port), CloudHandler)
    helper = ThreadingHTTPServer(("127.0.0.1", 17653), HelperHandler)
    helper_thread = threading.Thread(target=helper.serve_forever, daemon=True)
    helper_thread.start()
    print(f"Local-only fixture website: {FIXTURE.website_origin}/", flush=True)
    print("Simulated helper: http://127.0.0.1:17653/", flush=True)
    try:
        cloud.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        helper.shutdown()
        helper.server_close()
        cloud.server_close()
        helper_thread.join(2)


if __name__ == "__main__":
    main()
