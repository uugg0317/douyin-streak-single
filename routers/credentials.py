"""凭证与日志路由：登录态上传、扫码提取、日志查看。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import app_common
from fastapi import APIRouter, File, Header, UploadFile
from fastapi import HTTPException

from app_common import logger
from auth_service import _check_auth
from core.config import (
    atomic_write_bytes,
    state_write_path,
)
from core.runtime import recent_logs, update_runtime
from state_validation import MAX_STATE, validate_storage_state

router = APIRouter()


def _state_write_path() -> Path:
    return state_write_path()


@router.post("/api/upload-state")
@router.post("/api/credentials/upload")
async def api_upload_state(
    file: UploadFile = File(...),
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    _check_auth(token)
    raw = await file.read(MAX_STATE + 1)
    if len(raw) > MAX_STATE:
        raise HTTPException(status_code=400, detail="文件过大")
    try:
        validate_storage_state(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="登录态文件结构不合法，请使用完整的 Playwright storage_state 文件") from None
    # The worker owns acquisition AND release. Cancelling the HTTP request
    # cannot unlock a browser close or file replacement that is still running.
    return await asyncio.to_thread(_persist_uploaded_state, raw)


def _persist_uploaded_state(raw: bytes) -> dict:
    # 写入前原子获取运行锁：读取/校验上传文件期间不阻塞任务，但绝不在发送过程中
    # 替换登录态。安全模式可把目标指向 /dev/shm/state.json。
    if not app_common.run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="后台任务运行中，请结束后再替换登录态")
    try:
        from routers.qr_credentials import qr_login_active
        from core import automation
        if qr_login_active():
            raise HTTPException(status_code=409, detail="扫码登录进行中，请先取消扫码再手动上传")
        app_common._mark_run_started()
        # A Future timeout does not stop Playwright's owner thread. Wait for
        # actual completion here; unrelated HTTP remains responsive throughout.
        if not automation.restart_browser(timeout=None):
            raise HTTPException(status_code=500, detail="旧浏览器会话无法关闭，原登录态保留，请稍后重试")
        target = _state_write_path()
        try:
            atomic_write_bytes(target, raw)
        except Exception:
            raise HTTPException(status_code=500, detail="无法写入登录态文件，请检查目录权限")
        # Structural validation and a successful write do not prove this new
        # credential is logged in. Do not inherit a previous credential's check.
        update_runtime(session_status="unknown", session_checked_at=None)
    finally:
        app_common.run_lock.release()
    logger.info("已更新登录态 state.json（%s 字节）", len(raw))
    return {"ok": True, "size": len(raw)}


@router.get("/api/logs")
def api_logs(n: int = 300, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    return {"logs": "\n".join(recent_logs(max(10, min(n, 600))))}


# Legacy standalone helper metadata is retained for compatibility.
# The main credentials page uses the embedded QR routes in qr_credentials.py.
LOCAL_HELPER_URL = "http://127.0.0.1:17653"
LOCAL_COOKIE_MODE = app_common._env("COOKIE_EXTRACT_MODE") or "local-client"


@router.get("/api/credentials/local-helper-info")
def api_local_helper_info(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    if LOCAL_COOKIE_MODE != "local-client":
        raise HTTPException(status_code=400, detail="COOKIE_EXTRACT_MODE 请设置为 local-client。")
    return {"mode": "local-client", "helper_url": LOCAL_HELPER_URL,
            "automatic_upload": False, "automatic_close": True}


@router.post("/api/credentials/extract")
def api_credentials_extract(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    raise HTTPException(status_code=410, detail="提取已改为本机助手。请启动电脑上的启动本地Cookie助手.bat，并刷新页面使用本机提取按钮。")


@router.get("/api/credentials/extract-status")
def api_credentials_extract_status(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    return {"running": False, "status": "idle", "count": 0, "error": None,
            "mode": "local-client", "automatic_upload": False}


@router.get("/api/credentials/extract-screenshot")
def api_credentials_extract_screenshot(token: str = Header(default="", alias="X-Auth-Token")):
    _check_auth(token)
    raise HTTPException(status_code=410, detail="登录窗口显示在你的电脑上，本机助手不向网站提供登录截图。")
