"""Local, same-origin embedded QR login; credentials never leave the backend."""
from __future__ import annotations

import hashlib
import threading
import time
import uuid

from fastapi import APIRouter, Body, Header, HTTPException, Request, Response

import app_common
from auth_service import _check_auth
from request_origin import _canonical_origin, _effective_origin
from core import automation
from core.config import DATA_DIR, atomic_write_bytes, state_write_path
from core.runtime import update_runtime
from desktop.local_cookie_helper import validate_state

router = APIRouter()
_qr_manager = None
_manager_lock = threading.Lock()


def _check_request(request: Request, token: str, *, mutation=False) -> None:
    # Local entry and QR task capability checks remain mandatory without a password gate.
    _check_auth(token)
    expected = _effective_origin(request)
    if expected is None:
        raise HTTPException(status_code=403, detail="扫码仅允许本机直接访问，请使用控制台的本机地址")
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site", "") in {"cross-site", "same-site"}:
        raise HTTPException(status_code=403, detail="扫码接口仅允许同源访问")
    if origin is not None and _canonical_origin(origin) != expected:
        raise HTTPException(status_code=403, detail="扫码接口仅允许同源访问")
    if mutation and (origin is None or request.headers.get("x-spark-qr-request") != "1"):
        raise HTTPException(status_code=403, detail="扫码请求缺少同源确认，请刷新页面重试")


def _persist_qr_state(raw: bytes, job: dict) -> bool:
    """A busy sender delays commit. Failed validation/invalidation never replaces state."""
    validate_state(raw)
    if not app_common.run_lock.acquire(blocking=False):
        return False
    try:
        app_common._mark_run_started()
        if not automation.restart_browser(timeout=12):
            raise RuntimeError("credential_browser_reset_failed")
        atomic_write_bytes(state_write_path(), raw)
        try:
            update_runtime(session_status="ok", session_checked_at=time.time())
        except Exception:
            # The atomic credential commit already succeeded; do not report it as unsaved.
            app_common.logger.warning("扫码登录态已保存，运行状态更新将在下次检查时恢复")
        app_common.logger.info("面板扫码登录成功，已自动更新登录态并废弃旧浏览器会话")
        return True
    finally:
        app_common.run_lock.release()


def _get_qr_manager():
    global _qr_manager
    with _manager_lock:
        if _qr_manager is None:
            from core.qr_login import QrLoginManager
            _qr_manager = QrLoginManager(persist_state=_persist_qr_state, profile_data_dir=DATA_DIR)
        return _qr_manager


def qr_login_active() -> bool:
    return bool(_qr_manager is not None and _qr_manager.is_active())


def shutdown_qr_login() -> None:
    if _qr_manager is not None:
        _qr_manager.shutdown()


def _public(snapshot: dict, *, include_token=False) -> dict:
    safe = {key: snapshot[key] for key in (
        "job_id", "message", "code", "remaining_seconds", "qr_revision", "image_ready",
        "cleanup_complete", "count",
        "saved", "cleanup_attempts", "cleanup_retrying",
        "verification_ready", "verification_width", "verification_height",
        "verification_revision", "verification_busy", "verification_error",
    ) if key in snapshot}
    status = snapshot.get("state", snapshot.get("status", "idle"))
    if status == "error":
        status = "failed"
    elif status == "waiting" and snapshot.get("code") == "qr_expired":
        status = "expired"
    elif status == "waiting" and snapshot.get("image_ready"):
        status = "qr_ready"
    safe["status"] = status
    safe["deadline"] = snapshot.get("expires_at", snapshot.get("deadline", 0))
    if include_token and snapshot.get("job_token"):
        safe["job_token"] = snapshot["job_token"]
    return safe


def _invoke(method: str, *args):
    from core.qr_login import QrLoginBusy, QrLoginForbidden, QrLoginInvalid, QrLoginNotFound
    if _qr_manager is None:
        raise HTTPException(status_code=404, detail="本次扫码不存在，请重新开始")
    try:
        return getattr(_qr_manager, method)(*args)
    except QrLoginBusy:
        detail = "正在处理验证操作，请稍候" if method == "verification_action" else "另一次扫码正在处理，请等待结束"
        raise HTTPException(status_code=409, detail=detail) from None
    except QrLoginForbidden:
        raise HTTPException(status_code=403, detail="无法访问本次扫码，请从当前页面重新开始") from None
    except QrLoginNotFound:
        raise HTTPException(status_code=404, detail="本次扫码已结束，请重新开始") from None
    except QrLoginInvalid as exc:
        detail = "验证画面已更新，请等待新画面后重试。" if exc.code == "verification_changed" else "扫码请求无效，请刷新页面重试"
        raise HTTPException(status_code=400, detail=detail) from None


def _job_id(value) -> str:
    value = str(value or "")
    if len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise HTTPException(status_code=400, detail="扫码编号无效")
    return value


def _job_key(value: str) -> str:
    if not value or len(value) > 128:
        raise HTTPException(status_code=403, detail="缺少本次扫码确认信息")
    return value


@router.post("/api/credentials/qr/start")
def start_qr(request: Request, response: Response, body: dict = Body(...),
             token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_request(request, token, mutation=True)
    if set(body) != {"request_id"}:
        raise HTTPException(status_code=400, detail="扫码请求参数无效")
    try:
        request_id = str(uuid.UUID(str(body["request_id"])))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail="扫码请求编号无效") from None
    scoped = hashlib.sha256((token + "\0" + request_id).encode()).hexdigest()
    if not app_common.run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="后台任务正在运行，请结束后再开始扫码")
    try:
        _get_qr_manager()
        snapshot = _invoke("start", scoped)
    finally:
        app_common.run_lock.release()
    response.headers["Cache-Control"] = "no-store"
    return _public(snapshot, include_token=True)


@router.get("/api/credentials/qr/status")
def qr_status(request: Request, response: Response, job_id: str,
              job_token: str = Header(default="", alias="X-QR-Job-Token"),
              token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_request(request, token)
    snapshot = _invoke("status", _job_id(job_id), _job_key(job_token))
    response.headers["Cache-Control"] = "no-store"
    return _public(snapshot)


@router.get("/api/credentials/qr/image")
def qr_image(request: Request, job_id: str,
             job_token: str = Header(default="", alias="X-QR-Job-Token"),
             token: str = Header(default="", alias="X-Auth-Token")):
    _check_request(request, token)
    png, snapshot = _invoke("qr", _job_id(job_id), _job_key(job_token))
    if not png:
        from fastapi.responses import JSONResponse
        return JSONResponse(_public(snapshot), status_code=202, headers={"Cache-Control": "no-store"})
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.get("/api/credentials/qr/verification-image")
def verification_image(request: Request, job_id: str,
                       job_token: str = Header(default="", alias="X-QR-Job-Token"),
                       token: str = Header(default="", alias="X-Auth-Token")):
    _check_request(request, token)
    png, snapshot = _invoke("verification", _job_id(job_id), _job_key(job_token))
    if not png:
        from fastapi.responses import JSONResponse
        return JSONResponse(_public(snapshot), status_code=202, headers={"Cache-Control": "no-store"})
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"})


@router.post("/api/credentials/qr/verification-action")
def verification_action(request: Request, response: Response, body: dict = Body(...),
                        job_token: str = Header(default="", alias="X-QR-Job-Token"),
                        token: str = Header(default="", alias="X-Auth-Token")):
    _check_request(request, token, mutation=True)
    if set(body) != {"job_id", "action_id", "verification_revision", "action", "points"}:
        raise HTTPException(status_code=400, detail="验证操作参数无效")
    snapshot = _invoke("verification_action", _job_id(body.get("job_id")), _job_key(job_token),
                       body["action_id"], body["verification_revision"], body["action"], body["points"])
    response.headers["Cache-Control"] = "no-store"
    response.status_code = 202
    return _public(snapshot)


def _mutate_job(method: str, request: Request, response: Response, body: dict, token: str, job_token: str):
    _check_request(request, token, mutation=True)
    if set(body) != {"job_id"}:
        raise HTTPException(status_code=400, detail="扫码请求参数无效")
    snapshot = _invoke(method, _job_id(body.get("job_id")), _job_key(job_token))
    response.headers["Cache-Control"] = "no-store"
    return _public(snapshot)


@router.post("/api/credentials/qr/refresh")
def refresh_qr(request: Request, response: Response, body: dict = Body(...),
               job_token: str = Header(default="", alias="X-QR-Job-Token"),
               token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    return _mutate_job("refresh", request, response, body, token, job_token)


@router.post("/api/credentials/qr/cancel")
def cancel_qr(request: Request, response: Response, body: dict = Body(...),
              job_token: str = Header(default="", alias="X-QR-Job-Token"),
              token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    return _mutate_job("cancel", request, response, body, token, job_token)


@router.post("/api/credentials/qr/retry-cleanup")
def retry_cleanup_qr(request: Request, response: Response, body: dict = Body(...),
                     job_token: str = Header(default="", alias="X-QR-Job-Token"),
                     token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    return _mutate_job("retry_cleanup", request, response, body, token, job_token)


@router.post("/api/credentials/qr/recover-cleanup")
def recover_cleanup_qr(request: Request, response: Response, body: dict = Body(...),
                       token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """Recover closing authority after reload; never start or save a login."""
    _check_request(request, token, mutation=True)
    if not isinstance(body, dict) or body:
        raise HTTPException(status_code=400, detail="恢复清理请求必须为空对象")
    from core.qr_login import QrLoginInvalid
    if _qr_manager is None:
        raise HTTPException(status_code=409, detail="没有可恢复的浏览器清理任务")
    try:
        snapshot = _qr_manager.recover_cleanup()
    except QrLoginInvalid:
        raise HTTPException(status_code=409, detail="没有可恢复的浏览器清理任务") from None
    response.headers["Cache-Control"] = "no-store"
    return _public(snapshot, include_token=True)
