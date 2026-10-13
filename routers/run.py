"""运行路由：立即续火花与遗留界面状态清理。"""

from __future__ import annotations

import app_common
from fastapi import APIRouter
from fastapi import HTTPException

from app_common import logger
from core import ledger
from core.config import load_config
from core.runtime import set_running
from models import RunBody
from tasks_service import _start_run

router = APIRouter()


@router.post("/api/run")
def api_run(body: RunBody) -> dict:
    targets = ledger.get_selected()
    if not targets:
        cfg = load_config()
        if not cfg.get("friends"):
            raise HTTPException(status_code=400, detail="未勾选任何好友！请先在「好友与消息」中勾选好友后再执行。")
    _start_run(bool(body.dry or body.dry_run))
    return {"ok": True, "started": True}


@router.post("/api/reset-running")
def api_reset_running() -> dict:
    """仅修复无持锁线程时遗留的界面状态，绝不释放其它线程持有的锁。"""
    if app_common.run_lock.locked():
        raise HTTPException(
            status_code=409,
            detail=(
                "后台任务仍持有运行锁，不能在任务线程外强制释放；"
                "看门狗会在超时后自动重启，需立即终止请重启服务"
            ),
        )
    app_common.harvesting = False
    app_common.contacts_fetching = False
    set_running(False)
    logger.info("已清理无活动任务时遗留的后台状态")
    return {"ok": True, "message": "遗留运行状态已清理"}
