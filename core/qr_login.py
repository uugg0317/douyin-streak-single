"""Single-account, QR-only login with one thread owning all Playwright objects.

This module does not import app configuration or load existing account state.
An optional owned profile clears login data and retains only resource caches.
A caller owns HTTP authentication, its run lock, and the atomic state-file write.
The persistence callback must be short and must not call this manager recursively.
Return False when persistence is busy; None/True mean the write completed.
"""
from __future__ import annotations

import hmac
import json
import logging
import math
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Protocol

from desktop.local_cookie_helper import has_session, validate_state

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_QR_BYTES = 2 * 1024 * 1024
TERMINAL_STATES = frozenset({"success", "cancelled", "timeout", "error"})
_MESSAGES = {
    "starting": "正在启动后台扫码登录，不会弹出浏览器窗口。",
    "waiting": "请用抖音 App 扫码并在手机确认，成功后自动保存。",
    "qr_not_ready": "正在等待抖音生成二维码，可稍后刷新。",
    "component_not_ready": "抖音登录组件尚未加载，正在继续检测。",
    "opening_login": "正在打开抖音官方扫码登录面板，请稍候。",
    "login_component_failed": "创作者中心暂未生成二维码，本次等待已结束。请稍后重新开始，或使用已有 state.json。",
    "login_entry_failed": "抖音备用登录入口加载失败，本次等待已结束。请稍后重新开始。",
    "login_page_timeout": "抖音官方登录页连接超时，本次未生成二维码。请稍后重新开始。",
    "verification_required": "请在框内点击或拖动完成抖音官方验证，通过后会自动继续获取二维码。",
    "qr_expired": "二维码已过期，请刷新二维码。",
    "saving": "已确认登录，正在保存登录态。",
    "waiting_for_runtime": "已确认登录，正在等待后台任务结束后保存。",
    "success": "登录成功，登录态已自动保存。",
    "cancelled": "本次扫码已取消。",
    "timeout": "本次扫码已超过五分钟，请重新开始。",
    "browser_failed": "后台登录浏览器暂时不可用，请重试。",
    "browser_closed": "后台登录浏览器已关闭，请重新开始。",
    "invalid_state": "未取得可用登录态，请重新扫码。",
    "save_failed": "登录态保存失败，请重试。",
    "cleanup_failed": "后台浏览器清理异常；请检查服务后重新开始。",
    "cleanup_pending": "后台浏览器尚未完成关闭，请重试清理；完成前无法开始新扫码。",
}


class QrLoginError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class QrLoginBusy(QrLoginError):
    def __init__(self): super().__init__("qr_login_busy")


class QrLoginInvalid(QrLoginError):
    def __init__(self, code="invalid_request"): super().__init__(code)


class QrLoginNotFound(QrLoginError):
    def __init__(self): super().__init__("job_not_found")


class QrLoginForbidden(QrLoginError):
    def __init__(self): super().__init__("invalid_job_token")


@dataclass(frozen=True)
class LoginObservation:
    """Safe browser observation; no account identifiers or credentials."""
    code: str = "qr_not_ready"
    qr_png: bytes | None = field(default=None, repr=False)
    authenticated: bool = False
    login_signal: str = ""
    verification_png: bytes | None = field(default=None, repr=False)
    verification_width: int = 0
    verification_height: int = 0
    verification_generation: int = 0


class LoginBrowser(Protocol):
    """Factory and every method are called on the dedicated worker thread."""
    def open(self) -> None: ...
    def observe(self) -> LoginObservation: ...
    def refresh(self) -> None: ...
    def storage_state(self) -> dict | bytes: ...
    def close(self) -> None: ...


@dataclass
class _Job:
    job_id: str
    token: str = field(repr=False)
    request_id: str = field(repr=False)
    created: float
    deadline: float
    expires_at: float
    state: str = "starting"
    code: str = "starting"
    revision: int = 0
    qr_revision: int = 0
    image: bytes | None = field(default=None, repr=False)
    cleanup_complete: bool = False
    saved: bool = False
    verification_image: bytes | None = field(default=None, repr=False)
    verification_width: int = 0
    verification_height: int = 0
    verification_revision: int = 0
    verification_generation: int = -1
    verification_busy: bool = False
    verification_error: str = ""
    actions: list = field(default_factory=list, repr=False)
    action_ids: set = field(default_factory=set, repr=False)
    cleanup_attempts: int = 0
    cleanup_retrying: bool = False
    cleanup_retry_requested: bool = field(default=False, repr=False)


class QrLoginManager:
    """Thread-safe facade; never returns storage_state or Cookie values.

    start returns job_token; subsequent operations require it but never echo it.
    is_active stays true during browser cleanup, even after cancel or timeout.
    A cancellation that acquires the commit lock first prevents persistence.
    Once persistence began, cancellation waits and reports the completed result.
    """
    def __init__(self, persist_state: Callable, browser_factory: Callable | None = None,
                 *, deadline_seconds=300, poll_interval=1.5, browser_channel=None,
                 login_url="https://creator.douyin.com/", clock=time.monotonic,
                 wall_clock=time.time, profile_data_dir=None):
        if not callable(persist_state) or not 0 < deadline_seconds <= 300 or poll_interval <= 0:
            raise QrLoginInvalid()
        self._persist = persist_state
        def source_browser():
            from core.sparkflow_login.adapter import SparkFlowLoginBrowser
            return SparkFlowLoginBrowser(profile_data_dir=profile_data_dir, channel=browser_channel)
        self._factory = browser_factory or source_browser
        self._deadline_seconds = float(deadline_seconds)
        self._poll_interval = float(poll_interval)
        self._clock, self._wall_clock = clock, wall_clock
        self._condition = threading.Condition(threading.RLock())
        self._jobs: dict[str, _Job] = {}
        self._requests: dict[str, str] = {}
        self._active_id: str | None = None
        self._pending_id: str | None = None
        self._thread: threading.Thread | None = None
        self._shutdown = threading.Event()

    def _expire_locked(self, job):
        if job.state == "cleanup_pending":
            return
        if job.state not in TERMINAL_STATES and not job.saved and self._clock() >= job.deadline:
            self._terminal_locked(job, "timeout", "timeout")

    def _public_locked(self, job):
        self._expire_locked(job)
        return {
            "job_id": job.job_id, "state": job.state, "code": job.code,
            "message": ("登录态已保存，后台浏览器尚未完成关闭；可重试清理，凭据无需再次保存。"
                        if job.code == "cleanup_pending" and job.saved
                        else _MESSAGES.get(job.code, _MESSAGES["waiting"])),
            "expires_at": job.expires_at,
            "remaining_seconds": max(0, int(job.deadline - self._clock())),
            "qr_revision": job.qr_revision, "image_ready": bool(job.image),
            "cleanup_complete": job.cleanup_complete, "saved": job.saved,
            "cleanup_attempts": job.cleanup_attempts, "cleanup_retrying": job.cleanup_retrying,
            "verification_ready": bool(job.verification_image),
            "verification_width": job.verification_width,
            "verification_height": job.verification_height,
            "verification_revision": job.verification_revision,
            "verification_busy": job.verification_busy,
            "verification_error": job.verification_error,
        }

    def _authorized_locked(self, job_id, job_token):
        job = self._jobs.get(job_id) if isinstance(job_id, str) else None
        if job is None:
            raise QrLoginNotFound()
        if not isinstance(job_token, str) or len(job_token) > 256 or not hmac.compare_digest(job.token.encode("utf-8"), job_token.encode("utf-8")):
            raise QrLoginForbidden()
        self._expire_locked(job)
        return job

    def _terminal_locked(self, job, state, code):
        job.state, job.code = state, code
        job.revision += 1
        if job.image:
            job.qr_revision += 1
        job.image = None
        self._clear_verification_locked(job)
        self._condition.notify_all()

    def _clear_verification_locked(self, job):
        if job.verification_image or job.actions:
            job.verification_revision += 1
        job.verification_image = None
        job.verification_width = job.verification_height = 0
        job.verification_busy = False
        job.verification_error = ""
        job.actions.clear()

    def _prune_locked(self):
        # Never evict a running or not-yet-cleaned job. Terminal idempotency keys
        # are bounded, and retained for the latest 64 completed requests.
        finished = [j for j in self._jobs.values() if j.cleanup_complete]
        for job in sorted(finished, key=lambda j: j.created)[:-63]:
            self._jobs.pop(job.job_id, None)
            self._requests.pop(job.request_id, None)

    def start(self, client_request_id):
        if not isinstance(client_request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", client_request_id):
            raise QrLoginInvalid("invalid_client_request_id")
        with self._condition:
            if self._shutdown.is_set():
                raise QrLoginInvalid("manager_closed")
            existing = self._jobs.get(self._requests.get(client_request_id, ""))
            if existing:
                return {**self._public_locked(existing), "job_token": existing.token}
            if self._active_id:
                raise QrLoginBusy()
            self._prune_locked()
            now = self._clock()
            job = _Job(uuid.uuid4().hex, secrets.token_urlsafe(32), client_request_id,
                       now, now + self._deadline_seconds, self._wall_clock() + self._deadline_seconds)
            self._jobs[job.job_id] = job
            self._requests[client_request_id] = job.job_id
            self._active_id = self._pending_id = job.job_id
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="qr-login-browser", daemon=True)
                try:
                    self._thread.start()
                except Exception:
                    # A failed start owns no browser. Remove the reservation so
                    # a retry cannot remain permanently busy on a dead worker.
                    if not self._thread.is_alive():
                        self._jobs.pop(job.job_id, None)
                        self._requests.pop(client_request_id, None)
                        self._active_id = self._pending_id = None
                        self._thread = None
                    raise QrLoginInvalid("browser_start_failed") from None
            self._condition.notify_all()
            return {**self._public_locked(job), "job_token": job.token}

    def status(self, job_id, job_token):
        with self._condition:
            return self._public_locked(self._authorized_locked(job_id, job_token))

    def qr(self, job_id, job_token):
        with self._condition:
            job = self._authorized_locked(job_id, job_token)
            public = self._public_locked(job)
            return job.image, public

    def verification(self, job_id, job_token):
        with self._condition:
            job = self._authorized_locked(job_id, job_token)
            public = self._public_locked(job)
            return job.verification_image, public

    def verification_action(self, job_id, job_token, action_id, screenshot_revision, action, points):
        """Queue a human gesture. Request threads never access Playwright."""
        try:
            if not isinstance(action_id, str) or str(uuid.UUID(action_id)) != action_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise QrLoginInvalid("invalid_action_id") from None
        if type(screenshot_revision) is not int or screenshot_revision < 1 or action not in {"click", "drag"}:
            raise QrLoginInvalid("invalid_verification_action")
        if not isinstance(points, list) or not (len(points) == 1 if action == "click" else 2 <= len(points) <= 64):
            raise QrLoginInvalid("invalid_verification_points")
        clean = []
        last_time = -1.0
        for point in points:
            if not isinstance(point, dict) or set(point) != {"x", "y", "t"}:
                raise QrLoginInvalid("invalid_verification_points")
            values = [point[key] for key in ("x", "y", "t")]
            if any(type(value) not in {int, float} or not math.isfinite(value) for value in values):
                raise QrLoginInvalid("invalid_verification_points")
            x, y, stamp = map(float, values)
            if not (0 <= x <= 1 and 0 <= y <= 1 and 0 <= stamp <= 3000 and stamp >= last_time):
                raise QrLoginInvalid("invalid_verification_points")
            clean.append({"x": x, "y": y, "t": stamp})
            last_time = stamp
        with self._condition:
            job = self._authorized_locked(job_id, job_token)
            if action_id in job.action_ids:
                return self._public_locked(job)
            if (not self._valid_locked(job) or job.state != "verification_required"
                    or not job.verification_image or screenshot_revision != job.verification_revision):
                raise QrLoginInvalid("verification_changed")
            if job.verification_busy:
                raise QrLoginBusy()
            if len(job.action_ids) >= 128:
                raise QrLoginInvalid("too_many_verification_actions")
            job.action_ids.add(action_id)
            job.verification_busy = True
            job.verification_error = ""
            job.actions.append((job.revision, screenshot_revision, job.verification_generation, action, clean))
            self._condition.notify_all()
            return self._public_locked(job)

    def refresh(self, job_id, job_token):
        with self._condition:
            job = self._authorized_locked(job_id, job_token)
            if job.state not in TERMINAL_STATES and job.state != "cleanup_pending" and not job.saved:
                job.revision += 1
                job.qr_revision += 1
                job.image = None
                self._clear_verification_locked(job)
                job.state, job.code = "waiting", "qr_not_ready"
                self._condition.notify_all()
            return self._public_locked(job)

    def cancel(self, job_id, job_token):
        with self._condition:
            job = self._authorized_locked(job_id, job_token)
            if job.state not in TERMINAL_STATES and job.state != "cleanup_pending" and not job.saved:
                self._terminal_locked(job, "cancelled", "cancelled")
            return self._public_locked(job)

    def retry_cleanup(self, job_id, job_token):
        """Ask the original browser worker for another bounded close attempt."""
        with self._condition:
            job = self._authorized_locked(job_id, job_token)
            if job.cleanup_complete:
                return self._public_locked(job)
            if job.state != "cleanup_pending":
                raise QrLoginInvalid("cleanup_not_pending")
            if not job.cleanup_retrying:
                job.cleanup_retry_requested = True
                job.cleanup_retrying = True
                self._condition.notify_all()
            return self._public_locked(job)

    def recover_cleanup(self):
        """Return only a pending cleanup capability to an authenticated route.

        Reloaded administrators may recover closing authority without accessing
        account state. This does not rotate the token or request a close attempt.
        """
        with self._condition:
            job = self._jobs.get(self._active_id or "")
            if job is None or job.state != "cleanup_pending" or job.cleanup_complete:
                raise QrLoginInvalid("cleanup_not_pending")
            return {**self._public_locked(job), "job_token": job.token}

    def is_active(self):
        with self._condition:
            return self._active_id is not None

    def shutdown(self, timeout=6):
        """Bounded shutdown; only the worker ever closes its Playwright objects."""
        self._shutdown.set()
        end = time.monotonic() + max(0, float(timeout))
        if self._condition.acquire(timeout=max(0, min(0.2, end-time.monotonic()))):
            try:
                job = self._jobs.get(self._active_id or "")
                if job and job.state not in TERMINAL_STATES and job.state != "cleanup_pending" and not job.saved:
                    self._terminal_locked(job, "cancelled", "cancelled")
                self._condition.notify_all()
            finally:
                self._condition.release()
        thread = self._thread
        if thread and thread.ident is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, end-time.monotonic()))
        return (not thread or not thread.is_alive()) and self._active_id is None

    def _valid_locked(self, job, revision=None):
        self._expire_locked(job)
        if self._shutdown.is_set() and job.state not in TERMINAL_STATES and job.state != "cleanup_pending" and not job.saved:
            self._terminal_locked(job, "cancelled", "cancelled")
        return (job.state not in TERMINAL_STATES and job.state != "cleanup_pending" and not job.saved
                and (revision is None or job.revision == revision))

    def _close_on_worker(self, job, session):
        """Keep the profile occupied until close is confirmed, including errors.

        Three automatic attempts handle transient close errors. Further retries
        require an explicit request; they never export or persist credentials.
        This method stays on the same thread that owns the Playwright session.
        """
        if session is None:
            return
        while True:
            with self._condition:
                job.cleanup_retrying = True
                job.cleanup_retry_requested = False
            for attempt in range(3):
                with self._condition:
                    job.cleanup_attempts += 1
                try:
                    session.close()
                    with self._condition:
                        job.cleanup_retrying = False
                    return
                except Exception:
                    with self._condition:
                        job.state, job.code = "cleanup_pending", "cleanup_pending"
                        job.image = None
                        self._clear_verification_locked(job)
                        self._condition.notify_all()
                        if attempt < 2:
                            self._condition.wait(timeout=.2)
            with self._condition:
                job.cleanup_retrying = False
                self._condition.notify_all()
                while not job.cleanup_retry_requested:
                    self._condition.wait()

    def _worker(self):
        while True:
            with self._condition:
                while not self._pending_id and not self._shutdown.is_set():
                    self._condition.wait()
                if not self._pending_id:
                    return
                job = self._jobs[self._pending_id]
                self._pending_id = None
            self._run_job(job)

    def _run_job(self, job):
        session = None
        candidate = None
        revision = job.revision
        error_code = None
        phase = "launch"
        try:
            with self._condition:
                if not self._valid_locked(job): return
            session = self._factory()
            with self._condition:
                if not self._valid_locked(job): return
            phase = "open_login_page"
            session.open()
            while True:
                with self._condition:
                    if not self._valid_locked(job): break
                    refresh = job.revision != revision
                    revision = job.revision
                if refresh:
                    candidate = None
                    phase = "refresh"
                    session.refresh()
                with self._condition:
                    command = job.actions.pop(0) if job.actions else None
                if command is not None:
                    phase = "human_verification"
                    command_revision, screenshot_revision, generation, action, points = command
                    def still_current():
                        with self._condition:
                            return (self._valid_locked(job, command_revision)
                                    and job.state == "verification_required"
                                    and job.verification_revision == screenshot_revision)
                    performed = False
                    try:
                        if still_current():
                            performed = bool(session.interact(action, points, generation, still_current))
                    except Exception:
                        pass
                    with self._condition:
                        if job.revision == command_revision and job.state == "verification_required":
                            job.verification_busy = False
                            job.verification_image = None
                            job.verification_revision += 1
                            if not performed:
                                job.verification_error = "画面已变化或操作未完成，请等待更新后重试。"
                phase = "observe_login_page"
                observation = session.observe() if candidate is None else None
                if observation is not None and not isinstance(observation, LoginObservation):
                    raise QrLoginInvalid("invalid_observation")
                if observation is not None and observation.authenticated:
                    with self._condition:
                        if not self._valid_locked(job): break
                        if job.revision != revision: continue
                    # A strong page signal AND a valid unexpired session cookie
                    # are both required. uid_tt alone never qualifies.
                    if observation.login_signal not in {"creator_identity", "www_self_profile", "www_authenticated_header"}:
                        observation = LoginObservation()
                    else:
                        state = session.storage_state()
                        candidate = state if isinstance(state, bytes) else json.dumps(state, ensure_ascii=False).encode("utf-8")
                        validate_state(candidate, self._wall_clock())
                with self._condition:
                    if not self._valid_locked(job): break
                    if job.revision != revision:
                        candidate = None
                        continue
                    if candidate is not None:
                        validate_state(candidate, self._wall_clock())
                        job.state, job.code = "saving", "saving"
                        if job.image:
                            job.qr_revision += 1
                        job.image = None
                        self._clear_verification_locked(job)
                        public = self._public_locked(job)
                        if not self._valid_locked(job, revision): break
                        try:
                            result = self._persist(candidate, public)
                        except Exception:
                            self._terminal_locked(job, "error", "save_failed")
                            break
                        if result is not False:
                            job.saved = True
                            break
                        job.code = "waiting_for_runtime"
                    elif observation is not None:
                        code = observation.code if observation.code in _MESSAGES else "qr_not_ready"
                        if code in {"login_component_failed", "login_entry_failed"}:
                            self._terminal_locked(job, "error", code)
                            break
                        job.state = "verification_required" if code == "verification_required" else "waiting"
                        job.code = code
                        if code == "verification_required":
                            verification = observation.verification_png
                            width, height = observation.verification_width, observation.verification_height
                            if (isinstance(verification, bytes) and verification.startswith(PNG_SIGNATURE)
                                    and len(verification) <= 4 * MAX_QR_BYTES
                                    and type(width) is int and type(height) is int
                                    and 32 <= width <= 1600 and 32 <= height <= 1200):
                                if (job.verification_generation != observation.verification_generation
                                        or (job.verification_width, job.verification_height) != (width, height)):
                                    job.verification_revision += 1
                                job.verification_generation = observation.verification_generation
                                job.verification_image = verification
                                job.verification_width, job.verification_height = width, height
                            else:
                                job.verification_image = None
                        else:
                            self._clear_verification_locked(job)
                        image = observation.qr_png
                        if image is not None and (not isinstance(image, bytes) or not image.startswith(PNG_SIGNATURE) or len(image) > MAX_QR_BYTES):
                            image = None
                        if image != job.image:
                            job.qr_revision += 1
                            job.image = image
                    self._condition.wait(timeout=min(self._poll_interval, max(0, job.deadline-self._clock())))
        except ValueError:
            error_code = "invalid_state"
        except Exception as exc:
            error_code = "login_page_timeout" if phase == "open_login_page" and type(exc).__name__ == "TimeoutError" else "browser_failed"
            logging.getLogger(__name__).warning("扫码后台未完成：阶段=%s，异常类型=%s", phase, type(exc).__name__)
        finally:
            candidate = None
            with self._condition:
                if job.saved:
                    final_state, final_code = "success", "success"
                elif job.state in TERMINAL_STATES:
                    final_state, final_code = job.state, job.code
                else:
                    final_state, final_code = "error", error_code or "browser_failed"
            self._close_on_worker(job, session)
            with self._condition:
                self._terminal_locked(job, final_state, final_code)
                job.image = None
                job.cleanup_complete = True
                if self._active_id == job.job_id:
                    self._active_id = None
                self._condition.notify_all()
