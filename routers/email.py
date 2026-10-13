"""邮箱配置路由：读取/保存 SMTP 配置、发送测试邮件。"""

from __future__ import annotations

import socket
from datetime import datetime

from fastapi import APIRouter
from fastapi import HTTPException

from app_common import logger
from email_service import (
    _render_template,
    _send_email,
    load_email_config,
    save_email_config,
)
from models import EmailConfigBody
from watchdog_service import WATCHDOG_STUCK_SECONDS

router = APIRouter()


def _email_config_public(cfg: dict) -> dict:
    """返回给前端的邮箱配置：授权码只回掩码，绝不回传明文。"""
    out = dict(cfg)
    out["has_pass"] = bool(cfg.get("smtp_pass"))
    out["smtp_pass"] = "******" if cfg.get("smtp_pass") else ""
    return out


@router.get("/api/email-config")
def api_email_config_get() -> dict:
    return _email_config_public(load_email_config())


@router.post("/api/email-config")
def api_email_config_save(body: EmailConfigBody) -> dict:
    try:
        cfg = save_email_config(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "config": _email_config_public(cfg)}


@router.post("/api/email-test")
def api_email_test() -> dict:
    """按当前配置发一封测试邮件（主题加【测试】前缀，栈位用占位文本）。"""
    cfg = load_email_config()
    values = {
        "host": socket.gethostname(),
        "host_ip": "127.0.0.1",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "age": "0",
        "threshold": str(WATCHDOG_STUCK_SECONDS),
        "stack": "（测试邮件：服务运行正常，无真实卡死线程栈）",
    }
    subject = "【测试】" + _render_template(cfg["subject"], values)
    body = _render_template(cfg["body"], values)
    try:
        _send_email(cfg, subject, body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"测试邮件发送失败：{e}")
    return {"ok": True, "sent_to": cfg.get("mail_to")}
