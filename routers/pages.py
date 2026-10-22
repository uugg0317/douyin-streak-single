"""页面与通用路由：首页、favicon、健康检查、浏览器控制、运行状态。"""

from __future__ import annotations

from fastapi import Header
from fastapi.responses import FileResponse, HTMLResponse

from app_common import STATIC_DIR, VERSION, logger
from auth_service import (
    LOGIN_GATE,
    _check_auth,
    _clean_expired_sessions,
)
from core import automation, ledger, scheduler
from core.send_day import business_now, timestamp_date
from core.config import get_valid_state_path
from fastapi import APIRouter
from tasks_service import retry_plan_view

router = APIRouter()


def _today_summary() -> dict:
    """Count stable ledger identities, never infer delivery from a run record."""
    today = business_now().date()
    selected, accepted, unconfirmed, unsafe = set(), set(), set(), set()
    historical = False
    for entry in ledger.load_ledger():
        key = str(entry.get("entry_id") or "")
        if not key:
            continue
        if entry.get("selected"):
            selected.add(key)
        if ledger.identity_block_reason(entry) or not entry.get("has_conversation"):
            unsafe.add(key)
        attempt = entry.get("send_attempt") or {}
        pending_intent = attempt.get("status") in {"sending", "unconfirmed"}
        legacy_unknown = (entry.get("last_status") in {"sending", "unconfirmed"}
                          or timestamp_date(entry.get("last_unconfirmed_at")) == today)
        if pending_intent or legacy_unknown:
            unconfirmed.add(key)
            when = (attempt.get("started_at") or attempt.get("business_date")) if pending_intent else (
                entry.get("last_unconfirmed_at") or entry.get("last_attempt_at"))
            historical = historical or timestamp_date(when) != today
        if timestamp_date(entry.get("last_sent_at")) == today:
            accepted.add(key)
    accepted -= unconfirmed
    return {"business_date": today.isoformat(), "accepted": len(accepted),
            "unconfirmed": len(unconfirmed), "pending": len(selected - unsafe - accepted - unconfirmed),
            "selected": len(selected), "includes_historical_unconfirmed": historical}


@router.get("/")
def index() -> HTMLResponse:
    html_file = STATIC_DIR / "index.html"
    html_content = html_file.read_text(encoding="utf-8")
    # 安全红线：绝不在返回的 HTML 中注入 AUTH_TOKEN
    return HTMLResponse(
        content=html_content,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


@router.get("/favicon.ico")
async def _favicon_ico():
    """兼容浏览器默认请求 /favicon.ico：返回多尺寸 ICO 图标。"""
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")


@router.get("/api/health")
def health() -> dict:
    return {"ok": True, "app": "sparkkeeper", "version": VERSION}


@router.get("/api/browser/status")
def api_browser_status(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """查询浏览器常驻状态（是否存活、已运行多久）。"""
    _check_auth(token)
    return automation.get_browser_status()


@router.post("/api/browser/restart")
def api_browser_restart(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """关闭当前浏览器，下次发送时自动冷启动（用于浏览器卡死时手动恢复）。"""
    _check_auth(token)
    ok = automation.restart_browser()
    return {"ok": ok}


@router.get("/api/status")
def api_status(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    _clean_expired_sessions()  # 前端每 10 秒轮询本接口，借它定期回收过期令牌
    rt = retry_plan_view()
    valid_state = get_valid_state_path()
    stopping = bool(rt.get("stopping") or automation.send_is_stopping())
    return {
        "state_file_exists": valid_state is not None,
        "session_status": rt.get("session_status", "unknown"),
        "session_checked_at": rt.get("session_checked_at"),
        "running": bool(rt.get("running", False) or stopping),
        "stopping": stopping,
        "stop_reason": rt.get("stop_reason") if stopping else None,
        "last_run": rt.get("last_run"),
        "today_summary": _today_summary(),
        "pending_retry_entries": rt.get("pending_retry_entries") or [],
        "pending_retry_count": len(rt.get("pending_retry_entries") or []),
        "retry_status": rt.get("retry_status", "none"),
        "retry_dispatching": bool(rt.get("retry_dispatching")),
        "retry_due_at": rt.get("retry_due_at"),
        "retry_business_date": rt.get("retry_business_date"),
        "retry_queue_version": rt.get("retry_queue_version"),
        "retry_reason": rt.get("retry_reason"),
        "background_tasks_enabled": scheduler.background_tasks_enabled(),
        "next_run": scheduler.next_run_time(),
        "next_harvest": scheduler.next_harvest_time(),
        "history_count": len(rt.get("history", [])),
        "consecutive_failures": int(rt.get("consecutive_failures", 0)),
        "auto_paused_until": 0,  # 兼容旧客户端字段；历史失败不再暂停每日发送。
        "send_progress": rt.get("send_progress"),
        "auth_required": bool(LOGIN_GATE),
        "version": VERSION,
    }
