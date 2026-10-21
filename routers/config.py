"""配置路由：读取/保存定时与拟人化配置。"""

from __future__ import annotations

from fastapi import APIRouter, Body, Header
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from auth_service import _check_auth
from core import scheduler
from core.config import DEFAULT_CONFIG, LOCKED_FIELDS, LockedConfigChange, load_config, save_config

router = APIRouter()

# 尚未实现的通道开关：开启后必定失败，禁止通过接口打开
_UNIMPLEMENTED_SWITCHES = {
    "allow_first_message": "通道 B（首条消息）尚未实现：core/sender.py 为空壳，开启后无会话好友必定发送失败",
}


def _reject_unimplemented_switches(raw_cfg: dict) -> None:
    """拒绝对未实现功能开关的开启操作（只拦 true，不拦显式 false）。"""
    if not isinstance(raw_cfg, dict):
        return
    for key, reason in _UNIMPLEMENTED_SWITCHES.items():
        if raw_cfg.get(key) is True:
            raise HTTPException(status_code=400, detail=f"拒绝开启 {key}：{reason}")


def _reject_unknown_config_keys(raw_cfg: dict) -> None:
    """拒绝未知配置项（/api/config 同时接受 {"config": {...}} 与裸配置体）。"""
    unknown = [k for k in raw_cfg if k not in DEFAULT_CONFIG]
    if unknown:
        raise HTTPException(
            status_code=400, detail="未知配置项：" + ", ".join(map(str, unknown))
        )


@router.get("/api/config")
def api_config(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    cfg = load_config()
    return {**cfg, "locked_fields": list(LOCKED_FIELDS), "effective_config": cfg}


@router.put("/api/config")
@router.post("/api/config")
def api_config_save(body: dict = Body(...), token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    raw_cfg = body.get("config") if isinstance(body, dict) and "config" in body and isinstance(body["config"], dict) else body
    if not isinstance(raw_cfg, dict):
        raise HTTPException(status_code=400, detail="配置体必须是 JSON 对象")
    _reject_unknown_config_keys(raw_cfg)
    _reject_unimplemented_switches(raw_cfg)
    try:
        with scheduler.auto_start_guard():
            cfg = save_config(raw_cfg, reject_locked_changes=True)
            scheduler.apply_schedule()
    except LockedConfigChange as error:
        return JSONResponse(status_code=400, content={
            "detail": str(error), "locked_fields": list(LOCKED_FIELDS),
            "changed_locked_fields": error.changed_fields, "effective_config": error.effective_config,
        })
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "config": cfg, "locked_fields": list(LOCKED_FIELDS), "effective_config": cfg}
