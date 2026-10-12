"""Offline local export regressions: fake browser/process, temporary files only."""
import contextlib
import http.client
import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "desktop" / "local_cookie_helper.py"
spec = importlib.util.spec_from_file_location("local_cookie_helper_test_module", MODULE_PATH)
H = importlib.util.module_from_spec(spec)
spec.loader.exec_module(H)
ORIGIN = "https://panel.example.invalid"


def fake_state(**changes):
    cookie = {"name": "sessionid", "value": "offline-only-cookie", "domain": ".douyin.com",
              "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}
    cookie.update(changes)
    return {"cookies": [cookie], "origins": []}


class FakeProcess:
    pid = 987654
    def __init__(self):
        self.returncode = None
        self.hanging = False
        self.on_wait = None
    def poll(self):
        return self.returncode
    def wait(self, timeout=None):
        if self.on_wait:
            self.on_wait()
        if self.hanging and self.returncode is None:
            raise subprocess.TimeoutExpired("fake-worker", timeout)
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


class ValidationTests(unittest.TestCase):
    def test_origin_https_and_loopback_only_http_normalization(self):
        self.assertEqual(H.normalize_origin("https://PANEL.example.invalid:443/panel?q=1"), ORIGIN)
        self.assertEqual(H.normalize_origin("http://127.0.0.1:8000/path"), "http://127.0.0.1:8000")
        self.assertEqual(H.normalize_origin("http://[::1]:8000/"), "http://[::1]:8000")
        for value in ("http://remote.example.invalid", "file:///C:/state.json", "javascript:1", "https://user:password@example.invalid", "https://example.invalid:99999"):
            with self.assertRaises(ValueError):
                H.normalize_origin(value)

    def test_session_exact_name_value_domain_expiration(self):
        self.assertTrue(H.has_session(fake_state()["cookies"], 1000))
        self.assertTrue(H.has_session(fake_state(name="sessionid_ss", domain="www.douyin.com", expires=1001)["cookies"], 1000))
        for change in ({"name": "sessionid_impostor"}, {"value": ""}, {"domain": "evildouyin.com"},
                       {"domain": "douyin.com.evil.invalid"}, {"expires": 1000}, {"expires": True}, {"expires": float("nan")}):
            self.assertFalse(H.has_session(fake_state(**change)["cookies"], 1000), change)

    def test_schema_and_size_errors_do_not_echo_values(self):
        for raw in (b"", b"bad-json", b"x" * (H.MAX_STATE + 1), json.dumps(fake_state(path="bad")).encode(),
                    json.dumps(fake_state(sameSite=[])).encode(), json.dumps({**fake_state(), "origins": [{}]}).encode()):
            with self.assertRaises(ValueError) as caught:
                H.validate_state(raw)
            self.assertEqual(str(caught.exception), "invalid_state")

    def test_request_origin_host_peer_and_nonce_boundary(self):
        self.assertTrue(H.request_allowed("127.0.0.1", "127.0.0.1:17653", H.HELPER_ORIGIN, write=True))
        for peer, host, origin in (("192.0.2.1", "127.0.0.1:17653", H.HELPER_ORIGIN),
                ("127.0.0.1", "rebind.example.invalid:17653", H.HELPER_ORIGIN),
                ("127.0.0.1", "127.0.0.1:17653", ORIGIN), ("127.0.0.1", "127.0.0.1:17653", None)):
            self.assertFalse(H.request_allowed(peer, host, origin, write=True))

    def test_product_has_no_remote_upload_or_ticket_capability(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("urllib.request", source)
        self.assertNotIn("X-Local-Cookie-Ticket", source)
        self.assertNotIn("/api/credentials/local/", source)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="helper-offline-")))
        self.launches = []
        self.processes = []
        def popen(argv, **kwargs):
            self.launches.append((argv, kwargs))
            proc = FakeProcess()
            self.processes.append(proc)
            return proc
        self.manager = H.HelperManager(ORIGIN, self.tmp, popen=popen)
        self.stack.enter_context(patch("threading.Thread.start"))
        self.addCleanup(self.manager.cleanup)

    def start(self, request_id="request-offline-0001"):
        return self.manager.start({"origin": ORIGIN, "request_id": request_id, "deadline": time.time() + 299})

    def success_candidate(self):
        task = self.manager.task
        H.atomic_json(task["folder"] / "candidate.json", fake_state())
        H.atomic_json(task["folder"] / "status.json", {"status": "success", "count": 1})

    def test_start_only_local_process_and_repeated_request_does_not_reopen(self):
        started = self.start()
        self.assertEqual(started["status"], "starting")
        self.assertTrue(H.JOB_RE.fullmatch(started["job_id"]))
        self.assertEqual(self.start()["job_id"], started["job_id"])
        self.assertEqual(len(self.launches), 1)
        argv, kwargs = self.launches[0]
        self.assertIn("--worker", argv)
        self.assertNotIn(ORIGIN, argv)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("STATE_FILE_PATH", kwargs["env"])
        if os.name == "nt":
            self.assertNotIn("DISPLAY", kwargs["env"])

    def test_browser_closed_before_export_and_only_local_safe_metadata(self):
        self.start()
        self.success_candidate()
        exports = self.tmp / "exports"
        self.processes[0].on_wait = lambda: self.assertFalse(exports.exists())
        result = self.manager.poll_once()
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["download_ready"])
        path = Path(result["saved_path"])
        self.assertEqual(path.parent, exports)
        self.assertEqual(json.loads(path.read_text()), fake_state())
        safe = self.manager.snapshot()
        self.assertNotIn("saved_path", safe)
        self.assertNotIn("offline-only-cookie", json.dumps(safe))
        self.assertNotIn(str(self.tmp), json.dumps(safe))
        self.assertFalse(self.manager.task["folder"].exists())
        self.assertEqual(json.loads(self.manager.download(result["job_id"])), fake_state())

    def test_success_preserves_prior_exports_and_duplicate_filename_never_overwrites(self):
        started = self.start()
        self.success_candidate()
        exports = self.tmp / "exports"
        exports.mkdir()
        existing = exports / ("state_" + started["job_id"] + ".json")
        existing.write_bytes(b"historical-state")
        result = self.manager.poll_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "save_failed")
        self.assertEqual(existing.read_bytes(), b"historical-state")

    def test_cancel_and_stale_messages_cannot_touch_new_task(self):
        started = self.start()
        with self.assertRaises(ValueError):
            self.manager.cancel({"request_id": started["request_id"], "job_id": "0" * 32})
        cancelled = self.manager.cancel(started)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse((self.tmp / "exports").exists())
        newer = self.start("request-offline-0002")
        with self.assertRaises(ValueError):
            self.manager.cancel(started)
        with self.assertRaises(ValueError):
            self.start(started["request_id"])
        self.assertEqual(self.manager.snapshot()["job_id"], newer["job_id"])

    def test_timeout_and_invalid_candidate_export_nothing(self):
        self.start()
        self.manager.task["end"] = 0
        self.assertEqual(self.manager.poll_once()["status"], "timeout")
        self.start("request-offline-0002")
        self.success_candidate()
        H.atomic_json(self.manager.task["folder"] / "candidate.json", fake_state(domain="example.invalid"))
        self.assertEqual(self.manager.poll_once()["error"], "invalid_state")
        self.assertFalse((self.tmp / "exports").exists())

    def test_untrusted_origin_extra_destination_fields_invalid_deadline_and_busy_rejected(self):
        body = {"origin": ORIGIN, "request_id": "request-offline-0001", "deadline": time.time() + 299}
        for changed in ({**body, "origin": "https://other.example.invalid"}, {**body, "upload_url": ORIGIN},
                        {**body, "deadline": True}, {**body, "deadline": float("nan")}, {**body, "deadline": time.time() + 999},
                        {**body, "request_id": "../../file"}):
            with self.assertRaises(ValueError):
                self.manager.start(changed)
        self.start()
        with self.assertRaises(ValueError):
            self.start("request-offline-0002")

    def test_hanging_process_tree_terminated_before_temp_cleanup(self):
        started = self.start()
        proc = self.processes[0]
        proc.hanging = True
        def kill_tree(child):
            self.assertTrue(self.manager.task["folder"].exists())
            child.returncode = -9
        with patch.object(H, "terminate_tree", side_effect=kill_tree) as killed:
            self.assertEqual(self.manager.cancel(started)["status"], "cancelled")
        killed.assert_called_once_with(proc)

    def test_cleanup_failure_blocks_new_job_and_missing_folder_does_not(self):
        started = self.start()
        with patch.object(H.shutil, "rmtree", side_effect=PermissionError("offline-denied")):
            self.assertEqual(self.manager.cancel(started)["status"], "stopping")
            with self.assertRaises(ValueError):
                self.start("request-offline-0002")
        H.shutil.rmtree(self.manager.task["folder"])
        self.assertEqual(self.manager.cancel(started)["status"], "cancelled")

    def test_shutdown_closes_task_and_rejects_further_launches(self):
        self.start()
        self.manager.cleanup()
        self.assertEqual(self.manager.snapshot()["status"], "cancelled")
        with self.assertRaises(ValueError):
            self.start("request-offline-0002")

    def test_process_launch_failure_does_not_expose_paths_or_leave_active_task(self):
        self.manager.popen = Mock(side_effect=OSError("offline-sensitive-path"))
        with self.assertRaises(ValueError) as caught:
            self.start()
        self.assertEqual(str(caught.exception), "helper_failed")
        self.assertFalse(self.manager.snapshot()["running"])
        self.assertFalse(self.manager.task["folder"].exists())


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="helper-fake-browser-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.browser, self.context, self.page = Mock(), Mock(), Mock()
        self.browser.is_connected.return_value = True
        self.page.is_closed.return_value = False
        self.context.cookies.return_value = fake_state()["cookies"]
        self.context.storage_state.return_value = fake_state()
        self.closed = False

    @contextlib.contextmanager
    def browser_factory(self):
        try:
            yield self.browser, self.context, self.page
        finally:
            self.assertNotEqual(json.loads((self.folder / "status.json").read_text())["status"], "success")
            self.closed = True

    def test_auto_export_and_success_only_after_browser_exit(self):
        result = H.extract_worker(self.folder, time.time() + 10, browser_factory=self.browser_factory)
        self.assertEqual(result, 0)
        self.assertTrue(self.closed)
        status = json.loads((self.folder / "status.json").read_text())
        self.assertEqual(status["status"], "success")
        self.assertNotIn("offline-only-cookie", json.dumps(status))
        self.assertEqual(json.loads((self.folder / "candidate.json").read_text()), fake_state())
        self.context.storage_state.assert_called_once_with()

    def test_cancel_timeout_and_user_closed_browser_leave_no_candidate(self):
        for mode in ("cancel", "timeout", "closed"):
            with self.subTest(mode=mode):
                (self.folder / "stop.json").unlink(missing_ok=True)
                self.page.is_closed.return_value = mode == "closed"
                if mode == "cancel":
                    (self.folder / "stop.json").write_text("{}")
                H.extract_worker(self.folder, time.time() - 1 if mode == "timeout" else time.time() + 10,
                                 browser_factory=self.browser_factory)
                status = json.loads((self.folder / "status.json").read_text())
                self.assertEqual(status["status"], "timeout" if mode == "timeout" else "cancelled" if mode == "cancel" else "failed")
                self.assertFalse((self.folder / "candidate.json").exists())

    def test_exception_message_is_not_exposed(self):
        self.context.cookies.side_effect = RuntimeError("offline-sensitive-cookie-value")
        H.extract_worker(self.folder, time.time() + 10, browser_factory=self.browser_factory)
        self.assertNotIn("offline-sensitive", (self.folder / "status.json").read_text())

    def test_dead_parent_never_opens_browser(self):
        folder = self.folder / ("a" * 32)
        folder.mkdir()
        args = Mock(task_dir=folder, deadline=time.time() + 300, parent_pid=12345)
        with patch.object(H, "parent_alive", return_value=False), patch.object(H, "extract_worker") as worker:
            self.assertEqual(H.worker_main(args), 2)
        worker.assert_not_called()

    def test_parent_exit_and_independent_deadline_request_stop_then_kill_only_own_tree(self):
        for reason in ("parent_exit", "deadline"):
            with self.subTest(reason=reason):
                folder = self.folder / ("a" * 32)
                folder.mkdir(exist_ok=True)
                args = Mock(task_dir=folder, deadline=time.time() + 300, parent_pid=12345)
                targets = []
                class FakeThread:
                    def __init__(self, target, **kwargs):
                        targets.append(target)
                    def start(self):
                        pass
                done = Mock()
                done.wait.side_effect = [False, False]
                monotonic = Mock(side_effect=[0., 999.]) if reason == "deadline" else Mock(return_value=0.)
                alive = Mock(side_effect=[True, True, False]) if reason == "parent_exit" else Mock(return_value=True)
                with patch.object(H.threading, "Thread", FakeThread), patch.object(H.threading, "Event", return_value=done), \
                        patch.object(H, "parent_alive", alive), patch.object(H.time, "monotonic", monotonic), \
                        patch.object(H, "extract_worker", return_value=0), patch.object(H, "terminate_tree") as kill_tree, \
                        patch.object(H.os, "_exit", side_effect=SystemExit(2)):
                    self.assertEqual(H.worker_main(args), 0)
                    (folder / "candidate.json").write_text("offline-only-private-state")
                    with self.assertRaises(SystemExit):
                        targets[0]()
                    self.assertEqual(kill_tree.call_args.args[0].pid, os.getpid())
                self.assertTrue((folder / "stop.json").exists())
                self.assertFalse((folder / "candidate.json").exists())


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="helper-http-offline-")
        self.addCleanup(self.temp.cleanup)
        self.manager = H.HelperManager(ORIGIN, Path(self.temp.name), popen=Mock(side_effect=AssertionError("must not launch")))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H.handler_for(self.manager))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Host": "127.0.0.1:17653", **(headers or {})}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_no_cors_remote_origin_and_rebinding_host_rejected(self):
        for headers in ({"Origin": ORIGIN}, {"Host": "evil.example.invalid:17653"}):
            status, response_headers, _ = self.request("GET", "/helper/config", headers=headers)
            self.assertEqual(status, 403)
            self.assertNotIn("Access-Control-Allow-Origin", response_headers)
        self.assertEqual(self.request("OPTIONS", "/helper/extract")[0], 403)

    def test_config_public_only_and_download_requires_nonce(self):
        code, headers, raw = self.request("GET", "/helper/config")
        self.assertEqual(code, 200)
        data = json.loads(raw)
        self.assertEqual(data["trusted_origin"], ORIGIN)
        self.assertIn("page_nonce", data)
        self.assertNotIn("cookies", data)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("GET", "/helper/download/" + "a" * 32)[0], 403)

    def test_write_needs_self_origin_and_nonce_even_with_valid_json(self):
        body = json.dumps({"origin": ORIGIN, "request_id": "request-offline-0001", "deadline": time.time() + 299})
        for headers in ({"Content-Type": "application/json", "X-Helper-Nonce": self.manager.nonce},
                {"Content-Type": "application/json", "Origin": H.HELPER_ORIGIN},
                {"Content-Type": "application/json", "Origin": ORIGIN, "X-Helper-Nonce": self.manager.nonce}):
            self.assertEqual(self.request("POST", "/helper/extract", body, headers)[0], 403)
        self.manager.popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
