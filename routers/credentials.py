"""凭证与日志路由：登录态上传、扫码提取、日志查看。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import app_common
from fastapi import APIRouter, File, UploadFile
from fastapi import HTTPException
from fastapi.responses import FileResponse

from app_common import logger
from core.config import (
    DATA_DIR,
    STATE_PATH,
    atomic_write_bytes,
    atomic_write_text,
    state_write_path,
)
from core.runtime import recent_logs, update_runtime

router = APIRouter()


def _state_write_path() -> Path:
    return state_write_path()


@router.post("/api/upload-state")
@router.post("/api/credentials/upload")
async def api_upload_state(
    file: UploadFile = File(...),
) -> dict:
    raw = await file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="不是合法的 JSON 文件")
    if not isinstance(data, dict) or not isinstance(data.get("cookies"), list) or not data["cookies"]:
        raise HTTPException(status_code=400, detail="缺少 cookies 字段，请确认是 Playwright 导出的登录态文件")
    # 结构白名单校验：仅接受 Playwright storage_state 形状
    if not all(
        isinstance(c, dict) and c.get("name") and c.get("domain") and isinstance(c.get("path", ""), str)
        for c in data["cookies"]
    ):
        raise HTTPException(status_code=400, detail="cookies 字段结构不合法")
    if "origins" in data and not isinstance(data["origins"], list):
        raise HTTPException(status_code=400, detail="origins 字段必须是数组")
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
        if not automation.restart_browser(timeout=12):
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
def api_logs(n: int = 300) -> dict:
    return {"logs": "\n".join(recent_logs(max(10, min(n, 600))))}


# Legacy standalone helper metadata is retained for compatibility.
# The main credentials page uses the embedded QR routes in qr_credentials.py.
LOCAL_HELPER_URL = "http://127.0.0.1:17653"
LOCAL_COOKIE_MODE = app_common._env("COOKIE_EXTRACT_MODE") or "local-client"


@router.get("/api/credentials/local-helper-info")
def api_local_helper_info() -> dict:
    if LOCAL_COOKIE_MODE != "local-client":
        raise HTTPException(status_code=400, detail="COOKIE_EXTRACT_MODE 请设置为 local-client。")
    return {"mode": "local-client", "helper_url": LOCAL_HELPER_URL,
            "automatic_upload": False, "automatic_close": True}


@router.post("/api/credentials/extract")
def api_credentials_extract() -> dict:
    raise HTTPException(status_code=410, detail="提取已改为本机助手。请启动电脑上的启动本地Cookie助手.bat，并刷新页面使用本机提取按钮。")


@router.get("/api/credentials/extract-status")
def api_credentials_extract_status() -> dict:
    return {"running": False, "status": "idle", "count": 0, "error": None,
            "mode": "local-client", "automatic_upload": False}


@router.get("/api/credentials/extract-screenshot")
def api_credentials_extract_screenshot():
    raise HTTPException(status_code=410, detail="登录窗口显示在你的电脑上，本机助手不向网站提供登录截图。")
