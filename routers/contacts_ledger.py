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
    IdentityDecisionBody,
    StreakDecisionBody,
)
from tasks_service import _start_fetch_contacts, _start_harvest_creator

def _identity_call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except ledger.IdentityConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from None


router = APIRouter()


@router.post('/api/ledger/streak-decision')
def api_streak_decision(body: StreakDecisionBody, token: str = Header(default='',alias='X-Auth-Token')) -> dict:
    _check_auth(token)
    if not app_common.run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409,detail='已有任务在运行，暂不能确认火花读数')
    try:return ledger.review_streak(body.entry_id,body.action,body.expected_ledger_version)
    except (ledger.IdentityConflict,ledger.LedgerVersionConflict) as error:
        raise HTTPException(status_code=409,detail=str(error)) from None
    except OSError:
        raise HTTPException(status_code=503,detail='台账保存失败，请刷新核对后再试') from None
    finally:app_common.run_lock.release()


@router.post('/api/ledger/identity-decision')
def api_identity_decision(body: IdentityDecisionBody, token: str = Header(default='', alias='X-Auth-Token')) -> dict:
    _check_auth(token)
    if not app_common.run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail='已有任务在运行，暂不能改变好友身份')
    try:
        return ledger.review_identity(body.old_entry_id, body.new_entry_id, body.action, body.expected_ledger_version)
    except (ledger.IdentityConflict, ledger.LedgerVersionConflict) as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except OSError:
        raise HTTPException(status_code=503, detail='台账保存失败，身份关联未完成，请刷新后重试') from None
    finally:
        app_common.run_lock.release()


@router.get("/api/contacts")
def api_contacts(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    rt = load_runtime()
    return {
        "contacts": rt.get("contacts", []),
        "contacts_at": rt.get("contacts_at"),
        "contacts_capture": rt.get('contacts_capture'),
        "contacts_error": rt.get("contacts_error"),
        "contacts_job_id": rt.get("contacts_job_id"),
        "contacts_job_status": rt.get("contacts_job_status", "idle"),
        "fetching": app_common.contacts_fetching,
    }


@router.post("/api/contacts/fetch")
def api_contacts_fetch(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    job_id = _start_fetch_contacts()
    return {"ok": True, "started": True, "job_id": job_id}


@router.post("/api/sync")
def api_sync(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """与前端同步接口对齐"""
    _check_auth(token)
    job_id = _start_fetch_contacts()
    return {"ok": True, "started": True, "job_id": job_id}


@router.get("/api/ledger")
def api_ledger(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    rt = load_runtime()
    snapshot = ledger.ledger_snapshot()
    entries = [{**entry,**ledger.streak_confirmation_age(entry)} for entry in snapshot["entries"]]
    contacts = []
    for e in entries:
        name = e.get("display_name") or e.get("nickname") or ""
        spark = int(e.get("streak_days") or e.get("spark_days") or 0)
        contacts.append({
            "entry_id": e.get("entry_id"),
            "identity_ambiguous": bool(e.get("identity_ambiguous")),
            "display_name": name,
            "nickname": name,
            "avatar": e.get("avatar") or "",
            "streak_days": spark,
            "spark_days": spark,
            **{key:e.get(key) for key in ('streak_status','streak_source','streak_seen_at','streak_confirmed_at','streak_label')},
            **{key:e.get(key) for key in ('streak_confirmed_date','streak_unconfirmed_days','streak_may_be_stale')},
            **{key:e.get(key) for key in ('streak_suspect_value','streak_suspect_since','streak_suspect_count','streak_reset_dismissed_until','streak_previous_days','streak_reset_accepted_at')},
            **{key:e.get(key) for key in ('observation_status','observation_at','last_capture_complete','identity_review_pending','rename_candidates','send_block_reason')},
            "selected": bool(e.get("selected", False)),
            # last_status 取真实结果，失败也会写 last_sent_at，不能据此判成功
            "last_status": e.get("last_status") or ("success" if e.get("last_sent_at") else "pending"),
            "last_sent_at": e.get("last_sent_at")
        })
    b_daily = rt.get("b_channel_daily") or {}
    return {
        "entries": entries,
        "ledger_version": snapshot["ledger_version"],
        "contacts": contacts,
        "selected_count": sum(1 for e in entries if e.get("selected")),
        "contacts_capture": rt.get('contacts_capture'),
        "pending_send": [
            {"entry_id": e.get("entry_id"), "display_name": e["display_name"], "send_channel": e["send_channel"]}
            for e in automation.compute_pending(selected_snapshot=[e for e in entries if e.get("selected")])
        ],
        "contacts_at": rt.get("contacts_at"),
        "contacts_error": rt.get("contacts_error"),
        "contacts_job_id": rt.get("contacts_job_id"),
        "contacts_job_status": rt.get("contacts_job_status", "idle"),
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
    _check_auth(token)
    if body.confirm_clear:
        if body.selected_names or body.selected_ids:
            raise HTTPException(status_code=400, detail="确认清空不能同时提交勾选名单，请刷新后重试")
        return {"ok": True, **ledger.clear_selection()}
    if not body.selected_names and not body.selected_ids:
        raise HTTPException(status_code=400, detail="selected_names / selected_ids 不能为空；清空全部勾选请使用确认清空名单操作")
    entries = ledger.load_ledger()
    ids = set(body.selected_ids)
    for key in ids:
        if ledger.resolve_entry(entries, entry_id=key) is None:
            raise HTTPException(status_code=409, detail="好友编号不存在，请刷新")
    for name in body.selected_names:
        target = _identity_call(ledger.resolve_entry, entries, display_name=name)
        if target is None:
            raise HTTPException(status_code=409, detail="好友不存在，请刷新")
        ids.add(target["entry_id"])
    changes = [{"entry_id": e["entry_id"], "display_name": e["display_name"], "selected": e["entry_id"] in ids} for e in entries]
    return {"ok": True, **_identity_call(ledger.set_selected, changes)}


@router.post("/api/ledger/delete")
def api_ledger_delete(body: DeleteFriendBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    name, key = body.display_name.strip(), body.entry_id.strip()
    if not name and not key:
        raise HTTPException(status_code=400, detail="entry_id / display_name 不能为空")
    ok = _identity_call(ledger.remove_entry, name, entry_id=key)
    return {"ok": ok, "deleted": ok, "display_name": name, "entry_id": key}


@router.post("/api/ledger/batch-delete")
def api_ledger_batch_delete(body: BatchDeleteBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    keys = set(key.strip() for key in body.entry_ids if key.strip())
    names = [name.strip() for name in body.display_names if name.strip()]
    if not keys and not names:
        raise HTTPException(status_code=400, detail="entry_ids / display_names 不能为空")
    # Identity resolution, intent protection and the single write share the
    # ledger mutation lock; another worker cannot create an intent in between.
    return {"ok": True, **_identity_call(ledger.remove_entries, entry_ids=keys, display_names=names)}


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
    changes = []
    for e in body.entries or []:
        name = str(e.get("display_name") or "").strip()
        key = str(e.get("entry_id") or "").strip()
        if (name or key) and isinstance(e.get("selected"), bool):
            changes.append({"entry_id": key, "display_name": name,
                            "selected": e["selected"], "selected_order": e.get("selected_order")})
    return {"ok": True, **_identity_call(ledger.set_selected, changes)}
