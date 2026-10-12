"""联系人与台账路由：联系人、台账查询/选择/删除、头像代理、creator 采集。"""

from __future__ import annotations

import urllib.request
from urllib.parse import urlparse

import app_common
from fastapi import APIRouter, Header
from fastapi import HTTPException
from fastapi import Response

from auth_service import _check_auth
from core import automation, ledger
from core.runtime import load_harvest_last, load_runtime
from models import (
    BatchDeleteBody,
    DeleteFriendBody,
    LedgerBody,
    SelectionBody,
)
from tasks_service import _start_fetch_contacts, _start_harvest_creator

router = APIRouter()


@router.get("/api/contacts")
def api_contacts(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    rt = load_runtime()
    return {
        "contacts": rt.get("contacts", []),
        "contacts_at": rt.get("contacts_at"),
        "contacts_error": rt.get("contacts_error"),
        "fetching": app_common.contacts_fetching,
    }


@router.post("/api/contacts/fetch")
def api_contacts_fetch(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    _start_fetch_contacts()
    return {"ok": True, "started": True}


@router.post("/api/sync")
def api_sync(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """与前端同步接口对齐"""
    _check_auth(token)
    _start_fetch_contacts()
    return {"ok": True, "started": True}


@router.get("/api/ledger")
def api_ledger(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    rt = load_runtime()
    entries = ledger.load_ledger()
    contacts = []
    for e in entries:
        name = e.get("display_name") or e.get("nickname") or ""
        spark = int(e.get("streak_days") or e.get("spark_days") or 0)
        contacts.append({
            "display_name": name,
            "nickname": name,
            "avatar": e.get("avatar") or "",
            "streak_days": spark,
            "spark_days": spark,
            "selected": bool(e.get("selected", False)),
            # last_status 取真实结果，失败也会写 last_sent_at，不能据此判成功
            "last_status": e.get("last_status") or ("success" if e.get("last_sent_at") else "pending"),
            "last_sent_at": e.get("last_sent_at")
        })
    b_daily = rt.get("b_channel_daily") or {}
    return {
        "entries": entries,
        "contacts": contacts,
        "selected_count": sum(1 for e in entries if e.get("selected")),
        "pending_send": [
            {"display_name": e["display_name"], "send_channel": e["send_channel"]}
            for e in automation.compute_pending()
        ],
        "contacts_at": rt.get("contacts_at"),
        "contacts_error": rt.get("contacts_error"),
        "fetching": app_common.contacts_fetching,
        "harvesting": app_common.harvesting,
        "harvest_last": load_harvest_last(),
        "b_channel_daily": {
            "date": b_daily.get("date"),
            "count": b_daily.get("count", 0),
        },
    }


@router.post("/api/ledger/selection")
def api_ledger_selection(body: SelectionBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """一键保存选中的好友列表。安全红线：拒绝空列表（会清空勾选致静默停发）。"""
    _check_auth(token)
    if not body.selected_names:
        raise HTTPException(
            status_code=400,
            detail="selected_names 不能为空；该操作会清空全部勾选，已被拒绝",
        )
    selected_set = set(body.selected_names or [])
    entries = ledger.load_ledger()
    changes = []
    for e in entries:
        name = e.get("display_name") or e.get("nickname")
        if name:
            changes.append({
                "display_name": name,
                "selected": name in selected_set
            })
    stats = ledger.set_selected(changes)
    return {"ok": True, **stats}


@router.post("/api/ledger/delete")
def api_ledger_delete(body: DeleteFriendBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """删除单个好友台账条目"""
    _check_auth(token)
    name = (body.display_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="display_name 不能为空")
    ok = ledger.remove_entry(name)
    return {"ok": ok, "deleted": ok, "display_name": name}


@router.post("/api/ledger/batch-delete")
def api_ledger_batch_delete(body: BatchDeleteBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """批量删除勾选的好友台账条目"""
    _check_auth(token)
    names = [n.strip() for n in (body.display_names or []) if n.strip()]
    if not names:
        raise HTTPException(status_code=400, detail="display_names 不能为空")
    deleted = 0
    for name in names:
        if ledger.remove_entry(name):
            deleted += 1
    return {"ok": True, "deleted": deleted, "requested": len(names)}


# 抖音头像域名白名单（防盗链代理）
_ALLOWED_AVATAR_HOSTS = {
    "p3.douyinpic.com", "p9.douyinpic.com", "p11.douyinpic.com",
    "p26.douyinpic.com", "p3-pc.douyinpic.com", "p9-pc.douyinpic.com",
    "p26-pc.douyinpic.com", "p11-pc.douyinpic.com",
    "p3.huoshanimg.com", "p9.huoshanimg.com", "p11.huoshanimg.com",
    "p26.huoshanimg.com",
}
MAX_AVATAR_BYTES = 5 * 1024 * 1024  # 单张头像大小上限


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止跟随 302/301：防白名单域名跳转至元数据地址/内网形成 SSRF。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_AVATAR_OPENER = urllib.request.build_opener(_NoRedirectHandler)


@router.get("/api/avatar")
def api_avatar(
    url: str,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """代理抖音头像图片；<img> 会自动携带 HttpOnly 会话 Cookie。"""
    _check_auth(token)
    parsed = urlparse(url)
    # scheme 显式限定：file:// 可绕过白名单读本地文件
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="不允许的头像协议")
    if parsed.hostname not in _ALLOWED_AVATAR_HOSTS:
        raise HTTPException(status_code=400, detail="不允许的头像域名")
    try:
        req = urllib.request.Request(url, headers={
            "Referer": "https://www.douyin.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })
        with _AVATAR_OPENER.open(req, timeout=10) as resp:
            data = resp.read(MAX_AVATAR_BYTES + 1)  # 多读1字节判超限
            content_type = resp.headers.get("Content-Type", "image/webp")
        if len(data) > MAX_AVATAR_BYTES:
            raise HTTPException(status_code=413, detail="头像文件过大")
        if not str(content_type).lower().startswith("image/"):
            content_type = "image/webp"
        return Response(content=data, media_type=content_type)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"头像获取失败: {e}")


@router.post("/api/ledger/harvest-creator")
def api_harvest_creator(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    _start_harvest_creator()
    return {"ok": True, "started": True}


@router.get("/api/ledger/stats")
def api_ledger_stats(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    return ledger.stats()


@router.put("/api/ledger")
def api_ledger_save(body: LedgerBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    changes: list[dict] = []
    for e in body.entries or []:
        name = str(e.get("display_name", "")).strip()
        if name and isinstance(e.get("selected"), bool):
            changes.append({
                "display_name": name,
                "selected": e["selected"],
                "selected_order": e.get("selected_order"),
            })
    stats = ledger.set_selected(changes)
    return {"ok": True, **stats}
