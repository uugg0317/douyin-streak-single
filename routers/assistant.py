"""Assistant inbox API: status, allowlisted replies, and a second QR login."""

from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Body, File, Header, HTTPException, Request, Response, UploadFile

from assistant_inbox import enabled, status_snapshot, submit_reply
from assistant_login import assistant_qr_active, get_manager, shutdown
from auth_service import _check_auth
from routers.qr_credentials import (
    _check_request,
    _job_id,
    _job_key,
    _public,
    qr_login_active,
)
from state_validation import MAX_STATE, validate_storage_state

router = APIRouter()

_REPLY_ERRORS = {
    "disabled": (400, "小号收件未启用"),
    "invalid_to": (400, "回复对象无效"),
    "empty_text": (400, "回复内容不能为空"),
    "too_long": (400, "回复不能超过 500 字"),
    "not_allowed": (400, "只能回复白名单中的会话"),
    "rate_limited": (429, "回复过于频繁，请 10 秒后再试"),
    "queue_full": (429, "回复队列已满，请稍后再试"),
}


def shutdown_assistant() -> None:
    shutdown()


@router.get("/api/assistant/status")
def api_assistant_status(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    payload = status_snapshot()
    payload["qr_active"] = assistant_qr_active()
    return payload


@router.post("/api/assistant/reply")
def api_assistant_reply(body: dict = Body(...),
                        token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    if not isinstance(body, dict) or set(body) != {"to", "text"}:
        raise HTTPException(status_code=400, detail="回复参数无效")
    if not isinstance(body.get("to"), str) or not isinstance(body.get("text"), str):
        raise HTTPException(status_code=400, detail="回复参数无效")
    result = submit_reply(body["to"], body["text"], source="api")
    if not result.get("ok"):
        status, detail = _REPLY_ERRORS.get(result.get("error"), (400, "回复未能排队"))
        raise HTTPException(status_code=status, detail=detail)
    return {"ok": True, "id": result.get("id"), "queued": True}


@router.post("/api/assistant/state")
async def api_assistant_state(file: UploadFile = File(...),
                              token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """Replace only the assistant cookie file. The streak account is not restarted."""
    _check_auth(token)
    raw = await file.read(MAX_STATE + 1)
    if len(raw) > MAX_STATE:
        raise HTTPException(status_code=400, detail="文件过大")
    try:
        validate_storage_state(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="登录态文件结构不合法") from None
    if assistant_qr_active():
        raise HTTPException(status_code=409, detail="小号扫码进行中，请先取消再上传")
    from assistant_browser import profile_lock
    from assistant_inbox import state_path
    from core.config import atomic_write_bytes

    target = state_path()
    if target.name == "state.json":
        raise HTTPException(status_code=500, detail="小号登录态路径无效")
    if not profile_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="小号浏览器正忙，请稍后再上传")
    try:
        atomic_write_bytes(target, raw)
    except Exception:
        raise HTTPException(status_code=500, detail="无法写入小号登录态") from None
    finally:
        profile_lock.release()
    return {"ok": True, "size": len(raw)}


def _manager_ready():
    if not enabled():
        raise HTTPException(status_code=400, detail="小号收件未启用")
    return get_manager()


def _invoke(method: str, *args):
    from core.qr_login import QrLoginBusy, QrLoginForbidden, QrLoginInvalid, QrLoginNotFound
    _manager_ready()
    try:
        return getattr(get_manager(), method)(*args)
    except QrLoginBusy:
        detail = "正在处理验证操作，请稍候" if method == "verification_action" else "另一次小号扫码正在处理，请等待结束"
        raise HTTPException(status_code=409, detail=detail) from None
    except QrLoginForbidden:
        raise HTTPException(status_code=403, detail="无法访问本次扫码，请从当前页面重新开始") from None
    except QrLoginNotFound:
        raise HTTPException(status_code=404, detail="本次扫码已结束，请重新开始") from None
    except QrLoginInvalid as exc:
        detail = "验证画面已更新，请等待新画面后重试。" if exc.code == "verification_changed" else "扫码请求无效，请刷新页面重试"
        raise HTTPException(status_code=400, detail=detail) from None


@router.post("/api/assistant/qr/start")
def start_assistant_qr(request: Request, response: Response, body: dict = Body(...),
                       token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_request(request, token, mutation=True)
    if not enabled():
        raise HTTPException(status_code=400, detail="小号收件未启用")
    if set(body) != {"request_id"}:
        raise HTTPException(status_code=400, detail="扫码请求参数无效")
    try:
        request_id = str(uuid.UUID(str(body["request_id"])))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail="扫码请求编号无效") from None
    if qr_login_active():
        raise HTTPException(status_code=409, detail="主账号扫码进行中，请结束后再登录小号")
    scoped = hashlib.sha256((token + "\0assistant\0" + request_id).encode()).hexdigest()
    _manager_ready()
    snapshot = _invoke("start", scoped)
    response.headers["Cache-Control"] = "no-store"
    return _public(snapshot, include_token=True)


@router.get("/api/assistant/qr/status")
def assistant_qr_status(request: Request, response: Response, job_id: str,
                        job_token: str = Header(default="", alias="X-QR-Job-Token"),
                        token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_request(request, token)
    snapshot = _invoke("status", _job_id(job_id), _job_key(job_token))
    response.headers["Cache-Control"] = "no-store"
    return _public(snapshot)


@router.get("/api/assistant/qr/image")
def assistant_qr_image(request: Request, job_id: str,
                       job_token: str = Header(default="", alias="X-QR-Job-Token"),
                       token: str = Header(default="", alias="X-Auth-Token")):
    _check_request(request, token)
    png, snapshot = _invoke("qr", _job_id(job_id), _job_key(job_token))
    if not png:
        from fastapi.responses import JSONResponse
        return JSONResponse(_public(snapshot), status_code=202, headers={"Cache-Control": "no-store"})
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.get("/api/assistant/qr/verification-image")
def assistant_verification_image(request: Request, job_id: str,
                                 job_token: str = Header(default="", alias="X-QR-Job-Token"),
                                 token: str = Header(default="", alias="X-Auth-Token")):
    _check_request(request, token)
    png, snapshot = _invoke("verification", _job_id(job_id), _job_key(job_token))
    if not png:
        from fastapi.responses import JSONResponse
        return JSONResponse(_public(snapshot), status_code=202, headers={"Cache-Control": "no-store"})
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.post("/api/assistant/qr/verification-action")
def assistant_verification_action(request: Request, response: Response, body: dict = Body(...),
                                  job_token: str = Header(default="", alias="X-QR-Job-Token"),
                                  token: str = Header(default="", alias="X-Auth-Token")):
    _check_request(request, token, mutation=True)
    if set(body) != {"job_id", "action_id", "verification_revision", "action", "points"}:
        raise HTTPException(status_code=400, detail="验证操作参数无效")
    snapshot = _invoke(
        "verification_action",
        _job_id(body.get("job_id")),
        _job_key(job_token),
        body["action_id"],
        body["verification_revision"],
        body["action"],
        body["points"],
    )
    response.headers["Cache-Control"] = "no-store"
    response.status_code = 202
    return _public(snapshot)


def _mutate(method: str, request: Request, response: Response, body: dict, token: str, job_token: str):
    _check_request(request, token, mutation=True)
    if set(body) != {"job_id"}:
        raise HTTPException(status_code=400, detail="扫码请求参数无效")
    snapshot = _invoke(method, _job_id(body.get("job_id")), _job_key(job_token))
    response.headers["Cache-Control"] = "no-store"
    return _public(snapshot)


@router.post("/api/assistant/qr/refresh")
def refresh_assistant_qr(request: Request, response: Response, body: dict = Body(...),
                         job_token: str = Header(default="", alias="X-QR-Job-Token"),
                         token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    return _mutate("refresh", request, response, body, token, job_token)


@router.post("/api/assistant/qr/cancel")
def cancel_assistant_qr(request: Request, response: Response, body: dict = Body(...),
                        job_token: str = Header(default="", alias="X-QR-Job-Token"),
                        token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    return _mutate("cancel", request, response, body, token, job_token)


@router.post("/api/assistant/qr/retry-cleanup")
def retry_assistant_cleanup(request: Request, response: Response, body: dict = Body(...),
                            job_token: str = Header(default="", alias="X-QR-Job-Token"),
                            token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    return _mutate("retry_cleanup", request, response, body, token, job_token)


@router.post("/api/assistant/qr/recover-cleanup")
def recover_assistant_cleanup(request: Request, response: Response, body: dict = Body(...),
                              token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_request(request, token, mutation=True)
    if not isinstance(body, dict) or body:
        raise HTTPException(status_code=400, detail="恢复清理请求必须为空对象")
    from core.qr_login import QrLoginInvalid
    from assistant_login import _manager
    if _manager is None:
        raise HTTPException(status_code=409, detail="没有可恢复的浏览器清理任务")
    try:
        snapshot = get_manager().recover_cleanup()
    except QrLoginInvalid:
        raise HTTPException(status_code=409, detail="没有可恢复的浏览器清理任务") from None
    response.headers["Cache-Control"] = "no-store"
    return _public(snapshot, include_token=True)
