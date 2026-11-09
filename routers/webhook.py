"""Webhook 控制台：只读状态与测试发送。响应里不包含地址或密钥。"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from auth_service import _check_auth
from webhook_service import notify_event, webhook_enabled, webhook_status

router = APIRouter()


def _status_payload() -> dict:
    status = webhook_status()
    return {
        "enabled": bool(status.get("enabled")),
        "debounce_seconds": int(status.get("debounce_seconds") or 0),
        "deliveries": [
            {
                "time": item.get("time"),
                "event": item.get("event"),
                "result": item.get("result"),
            }
            for item in status.get("deliveries") or []
            if isinstance(item, dict)
        ],
    }


@router.get("/api/webhook")
def api_webhook_status(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    return _status_payload()


@router.post("/api/webhook/test")
def api_webhook_test(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """发送一条 test_ping。未配置或未送达时只返回固定说明，不回传异常原文。"""
    _check_auth(token)
    if not webhook_enabled():
        raise HTTPException(status_code=400, detail="Webhook 未配置")
    delivered = notify_event("test_ping", {
        "summary": "控制台测试：Webhook 通道可用",
        "details": {"source": "console"},
    })
    if not delivered:
        raise HTTPException(status_code=400, detail="测试 Webhook 未送达")
    payload = _status_payload()
    payload["ok"] = True
    return payload
