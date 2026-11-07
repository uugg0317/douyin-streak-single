"""小号收件：独立白名单、去重、限流，以及把私信转成 assistant_message。

这里不启动浏览器，也不读取主号 state.json。浏览器读写在 assistant_browser.py。
白名单为空时不转发、不代发，也不打开小号浏览器。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from core.config import atomic_write_text
from core.send_day import business_now
from webhook_service import notify_event, _redact_text

logger = logging.getLogger("douyin-cloud-streak")

JOB_ID = "assistant_inbox_poll"
MAX_FORWARDS_PER_HOUR = 20
FORWARD_WINDOW_SECONDS = 3600
REPLY_MIN_INTERVAL_SECONDS = 10
REPLY_MAX_CHARS = 500
MAX_SEEN = 4000
MAX_RECENT = 30
MAX_QUEUE = 20
PREVIEW_CHARS = 80
_STORE_NAME = "assistant_inbox.json"
_STATE_NAME = "assistant_state.json"
_PROFILE_NAME = "assistant-browser-profile"
_QR_PARENT_NAME = "assistant-session"
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_PULL_TIMEOUT = 4

_lock = threading.RLock()


def data_dir() -> Path:
    raw = os.environ.get("DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    from core.config import DATA_DIR
    return DATA_DIR


def state_path() -> Path:
    path = data_dir() / _STATE_NAME
    if path.name == "state.json":
        raise RuntimeError("assistant state path collided with the main account")
    return path


def profile_dir() -> Path:
    path = data_dir() / _PROFILE_NAME
    if path.name != _PROFILE_NAME:
        raise RuntimeError("assistant profile path is not isolated")
    return path


def qr_profile_parent() -> Path:
    """Parent directory whose qr-login-profile is used only for the assistant QR."""
    return data_dir() / _QR_PARENT_NAME


def store_path() -> Path:
    return data_dir() / _STORE_NAME


def enabled() -> bool:
    return os.environ.get("ASSISTANT_INBOX_ENABLED", "0").strip().lower() in {
        "1", "true", "on", "yes",
    }


def poll_seconds() -> int:
    raw = os.environ.get("ASSISTANT_POLL_SECONDS", "").strip()
    if not raw:
        return 90
    try:
        value = int(raw)
    except ValueError:
        return 90
    return max(60, min(value, 3600))


def allowlist() -> list[str]:
    raw = os.environ.get("ASSISTANT_ALLOWED_SENDERS", "")
    items: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        name = part.strip()
        if not name or name in seen or len(name) > 80:
            continue
        seen.add(name)
        items.append(name)
    return items


def sender_allowed(sender_id: str, sender_name: str, conversation_name: str,
                   allowed: list[str] | None = None) -> bool:
    allowed = allowlist() if allowed is None else allowed
    if not allowed:
        return False
    keys = set()
    for value in (sender_id, sender_name, conversation_name):
        text = str(value or "").strip()
        if text:
            keys.add(text)
    return any(item in keys for item in allowed)


def redact(text: object) -> str:
    cleaned = _redact_text(str(text or ""))
    for name in (
        "ASSISTANT_REPLY_PULL_URL",
        "ASSISTANT_REPLY_PULL_KEY",
        "GROK_BOT_WEBHOOK_URL",
        "GROK_BOT_WEBHOOK_KEY",
    ):
        secret = os.environ.get(name, "").strip()
        if secret and len(secret) >= 4:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned[:300]


def _empty_store() -> dict:
    return {
        "seen": [],
        "forwarded_at": [],
        "recent": [],
        "replies": [],
        "sent_reply_ids": [],
        "pulled_ids": [],
        "last_api_reply_at": 0,
        "last_send_at": 0,
        "last_poll": None,
        "session_ok": None,
        "last_error": "",
        "credential_alert_open": False,
    }


def _load() -> dict:
    path = store_path()
    if not path.exists():
        return _empty_store()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("小号收件记录无法读取，本轮按空记录继续")
        return _empty_store()
    if not isinstance(raw, dict):
        return _empty_store()
    store = _empty_store()
    store.update({key: raw.get(key, store[key]) for key in store})
    return store


def _save(store: dict) -> None:
    atomic_write_text(store_path(), json.dumps(store, ensure_ascii=False, indent=2))


def stable_message_id(raw: dict) -> str:
    supplied = str(raw.get("message_id") or raw.get("id") or "").strip()
    if supplied and _ID_RE.fullmatch(supplied):
        return supplied
    basis = "\n".join([
        str(raw.get("conversation_name") or ""),
        str(raw.get("sender_id") or ""),
        str(raw.get("sender_name") or ""),
        str(raw.get("message_time") or raw.get("time") or ""),
        str(raw.get("text") or ""),
        str(raw.get("share_url") or ""),
        str(raw.get("share_title") or ""),
    ])
    return "h:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _clean_share_url(value: object) -> str:
    text = str(value or "").strip()
    if not text.startswith("https://") or len(text) > 800:
        return ""
    if any(ord(char) <= 32 for char in text):
        return ""
    return text


def normalize_message(raw: dict) -> dict | None:
    """Keep an incoming text or share card. Drop outgoing and unknown direction."""
    if not isinstance(raw, dict):
        return None
    direction = str(raw.get("direction") or "").strip().lower()
    if raw.get("outgoing") is True or direction in {"out", "outgoing", "self"}:
        return None
    if direction not in {"in", "incoming"} and raw.get("outgoing") is not False:
        return None
    text = str(raw.get("text") or "").replace("\x00", "").strip()
    share_title = str(raw.get("share_title") or "").replace("\x00", "").strip()[:200]
    share_url = _clean_share_url(raw.get("share_url"))
    if not text and not share_title and not share_url:
        return None
    conversation = str(raw.get("conversation_name") or raw.get("sender_name") or "").strip()[:80]
    sender_name = str(raw.get("sender_name") or conversation).strip()[:80]
    sender_id = str(raw.get("sender_id") or "").strip()[:80]
    message_time = str(raw.get("message_time") or raw.get("time") or "").strip()[:40]
    message = {
        "message_id": stable_message_id({
            **raw,
            "conversation_name": conversation,
            "sender_name": sender_name,
            "sender_id": sender_id,
            "message_time": message_time,
            "text": text,
            "share_title": share_title,
            "share_url": share_url,
        }),
        "conversation_name": conversation,
        "sender_name": sender_name,
        "sender_id": sender_id,
        "text": text[:2000],
        "share_title": share_title,
        "share_url": share_url,
        "message_time": message_time,
    }
    return message


def webhook_payload(message: dict) -> dict:
    name = message.get("conversation_name") or message.get("sender_name") or "未知会话"
    preview = (message.get("text") or message.get("share_title") or "分享了一个视频")[:60]
    kind = "分享视频" if message.get("share_url") or message.get("share_title") else "文字"
    return {
        "summary": f"小号收到{kind}：{name}：{preview}"[:180],
        "details": {
            "conversation_name": message.get("conversation_name") or "",
            "sender_name": message.get("sender_name") or "",
            "sender_id": message.get("sender_id") or "",
            "message_text": message.get("text") or "",
            "share_title": message.get("share_title") or "",
            "share_url": message.get("share_url") or "",
            "message_time": message.get("message_time") or "",
            "message_id": message.get("message_id") or "",
        },
    }


def _preview(message: dict) -> str:
    source = message.get("text") or message.get("share_title") or message.get("share_url") or ""
    return str(source)[:PREVIEW_CHARS]


def _remember_recent(store: dict, message: dict) -> None:
    row = {
        "message_id": message["message_id"],
        "conversation_name": message.get("conversation_name") or "",
        "sender_id": message.get("sender_id") or "",
        "message_time": message.get("message_time") or "",
        "kind": "share" if message.get("share_url") or message.get("share_title") else "text",
        "preview": _preview(message),
        "chars": len(message.get("text") or ""),
    }
    recent = [item for item in store.get("recent") or [] if item.get("message_id") != row["message_id"]]
    recent.insert(0, row)
    store["recent"] = recent[:MAX_RECENT]


def _prune_forwards(store: dict, now: float) -> None:
    store["forwarded_at"] = [
        stamp for stamp in store.get("forwarded_at") or []
        if isinstance(stamp, (int, float)) and now - float(stamp) < FORWARD_WINDOW_SECONDS
    ]


def _mark_seen(store: dict, message_id: str) -> None:
    seen = [item for item in store.get("seen") or [] if item != message_id]
    seen.append(message_id)
    store["seen"] = seen[-MAX_SEEN:]


def accept_incoming(messages: list[dict], *, now: float | None = None,
                    notify=notify_event) -> dict:
    """Dedup, allowlist-filter, rate-limit, and forward. Does not open a browser."""
    now = time.time() if now is None else float(now)
    allowed = allowlist()
    forwarded = 0
    skipped = 0
    rate_limited = 0
    with _lock:
        store = _load()
        seen = set(store.get("seen") or [])
        _prune_forwards(store, now)
        for raw in messages or []:
            if not isinstance(raw, dict):
                skipped += 1
                continue
            message = normalize_message(raw)
            if message is None:
                skipped += 1
                continue
            if message["message_id"] in seen:
                skipped += 1
                continue
            if not sender_allowed(message["sender_id"], message["sender_name"],
                                  message["conversation_name"], allowed):
                skipped += 1
                _mark_seen(store, message["message_id"])
                seen.add(message["message_id"])
                continue
            if len(store["forwarded_at"]) >= MAX_FORWARDS_PER_HOUR:
                rate_limited += 1
                continue
            payload = webhook_payload(message)
            delivered = False
            try:
                delivered = bool(notify("assistant_message", payload))
            except Exception as exc:
                logger.warning("小号消息转发失败 id=%s：%s", message["message_id"], redact(exc))
            if not delivered:
                logger.info("小号消息尚未送达，下轮再试 id=%s chars=%s",
                            message["message_id"], len(message.get("text") or ""))
                continue
            _mark_seen(store, message["message_id"])
            seen.add(message["message_id"])
            store["forwarded_at"].append(now)
            _remember_recent(store, message)
            forwarded += 1
            logger.info(
                "小号消息已转发 id=%s sender=%s chars=%s share=%s",
                message["message_id"],
                message.get("sender_id") or message.get("conversation_name") or "",
                len(message.get("text") or ""),
                bool(message.get("share_url") or message.get("share_title")),
            )
        _save(store)
    return {"forwarded": forwarded, "skipped": skipped, "rate_limited": rate_limited}


def _validate_reply_fields(to: str, text: str, allowed: list[str]) -> str | None:
    recipient = str(to or "").strip()
    body = str(text or "").replace("\x00", "").strip()
    if not recipient or len(recipient) > 80:
        return "invalid_to"
    if not body:
        return "empty_text"
    if len(body) > REPLY_MAX_CHARS:
        return "too_long"
    if not allowed or not sender_allowed(recipient, recipient, recipient, allowed):
        return "not_allowed"
    return None


def submit_reply(to: str, text: str, *, now: float | None = None, source: str = "api",
                 reply_id: str | None = None) -> dict:
    """Validate and queue a reply. Manual API calls are limited to one per 10s."""
    now = time.time() if now is None else float(now)
    if not enabled():
        return {"ok": False, "error": "disabled"}
    allowed = allowlist()
    error = _validate_reply_fields(to, text, allowed)
    if error:
        return {"ok": False, "error": error}
    recipient = str(to).strip()
    body = str(text).replace("\x00", "").strip()
    with _lock:
        store = _load()
        if source == "api":
            last = float(store.get("last_api_reply_at") or 0)
            if last and now - last < REPLY_MIN_INTERVAL_SECONDS:
                return {"ok": False, "error": "rate_limited"}
        if len(store.get("replies") or []) >= MAX_QUEUE:
            return {"ok": False, "error": "queue_full"}
        item_id = str(reply_id or uuid.uuid4().hex)
        if any(item.get("id") == item_id for item in store.get("replies") or []):
            return {"ok": True, "id": item_id, "duplicate": True}
        if item_id in set(store.get("sent_reply_ids") or []):
            return {"ok": True, "id": item_id, "duplicate": True}
        store["replies"].append({
            "id": item_id,
            "to": recipient,
            "text": body,
            "enqueued_at": now,
            "source": "api" if source == "api" else "pull",
        })
        if source == "api":
            store["last_api_reply_at"] = now
        _save(store)
    logger.info("小号回复已排队 id=%s to=%s chars=%s source=%s", item_id, recipient, len(body), source)
    return {"ok": True, "id": item_id, "queued": True}


def _note_credential(store: dict, *, invalid: bool, reason: str, notify) -> None:
    if not invalid:
        store["credential_alert_open"] = False
        return
    if store.get("credential_alert_open"):
        return
    try:
        notify("credential_invalid", {
            "summary": "小号登录态失效，请在控制台重新扫码",
            "details": {
                "account": "assistant",
                "reason": (reason or "小号页面未登录")[:400],
                "logged_in": False,
            },
        }, bypass_debounce=True)
    except TypeError:
        notify("credential_invalid", {
            "summary": "小号登录态失效，请在控制台重新扫码",
            "details": {"account": "assistant", "logged_in": False},
        })
    except Exception as exc:
        logger.warning("小号登录态通知失败：%s", redact(exc))
        return
    store["credential_alert_open"] = True
    logger.warning("小号登录态失效，已通知一次")


def pull_replies() -> list[dict]:
    """GET the optional reply-pull URL. Returns [] when the URL is unset."""
    url = os.environ.get("ASSISTANT_REPLY_PULL_URL", "").strip()
    if not url:
        return []
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        logger.warning("小号回复拉取地址不是 http(s) URL，已跳过")
        return []
    request = urllib.request.Request(url, headers=_pull_headers(), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_PULL_TIMEOUT) as response:
            raw = response.read(200_000)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("小号回复拉取失败：%s", redact(exc))
        return []
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        logger.warning("小号回复拉取不是 JSON，已跳过")
        return []
    if isinstance(payload, dict):
        payload = payload.get("replies") or payload.get("items") or []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)][:MAX_QUEUE]


def ack_replies(ids: list[str]) -> None:
    url = os.environ.get("ASSISTANT_REPLY_PULL_URL", "").strip()
    if not url or not ids:
        return
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return
    body = json.dumps({"ack": [str(item)[:80] for item in ids[:MAX_QUEUE]]}, ensure_ascii=False).encode("utf-8")
    headers = _pull_headers()
    headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_PULL_TIMEOUT) as response:
            response.read(1024)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("小号回复确认失败：%s", redact(exc))


def _pull_headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "sparkkeeper-assistant"}
    key = os.environ.get("ASSISTANT_REPLY_PULL_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["X-Reply-Key"] = key
    return headers


def ingest_pulled(items: list[dict], *, now: float) -> list[str]:
    """Queue valid pulled replies and return every id that should be acked."""
    acked: list[str] = []
    allowed = allowlist()
    with _lock:
        store = _load()
        known = set(store.get("pulled_ids") or [])
        known.update(item.get("id") for item in store.get("replies") or [])
        known.update(store.get("sent_reply_ids") or [])
        for item in items:
            item_id = str(item.get("id") or "").strip()
            if not item_id or not _ID_RE.fullmatch(item_id):
                continue
            if item_id in known:
                acked.append(item_id)
                continue
            result = submit_reply(
                str(item.get("to") or ""),
                str(item.get("text") or ""),
                now=now,
                source="pull",
                reply_id=item_id,
            )
            # submit_reply takes the lock again; reload after it returns.
            store = _load()
            known.add(item_id)
            pulled = [value for value in store.get("pulled_ids") or [] if value != item_id]
            pulled.append(item_id)
            store["pulled_ids"] = pulled[-MAX_SEEN:]
            _save(store)
            acked.append(item_id)
            if not result.get("ok"):
                logger.info("小号拉取回复未入队 id=%s reason=%s", item_id, result.get("error"))
        return acked


def record_poll(session_ok, reason: str, *, notify=notify_event) -> None:
    with _lock:
        store = _load()
        store["last_poll"] = business_now().isoformat(timespec="seconds")
        if session_ok is True or session_ok is False:
            store["session_ok"] = bool(session_ok)
        store["last_error"] = redact(reason)[:180] if reason and session_ok is not True else ""
        _note_credential(store, invalid=session_ok is False, reason=reason or "", notify=notify)
        _save(store)


def _select_pending(allowed: list[str], now: float) -> tuple[dict | None, bool]:
    pending = None
    dropped_reply = False
    with _lock:
        store = _load()
        kept = []
        for candidate in store.get("replies") or []:
            if _validate_reply_fields(candidate.get("to", ""), candidate.get("text", ""), allowed):
                logger.info("已丢弃非白名单回复 id=%s", candidate.get("id"))
                dropped_reply = True
                continue
            kept.append(candidate)
        if dropped_reply:
            store["replies"] = kept
            store["last_error"] = "已丢弃不在白名单内的回复"
            _save(store)
        if kept:
            last_send = float(store.get("last_send_at") or 0)
            if not last_send or now - last_send >= REPLY_MIN_INTERVAL_SECONDS:
                candidate = kept[0]
                pending = {"to": candidate["to"], "text": candidate["text"], "id": candidate.get("id")}
    return pending, dropped_reply


def run_cycle(*, cycle=None, pull=None, ack=None, now: float | None = None,
              notify=notify_event) -> dict:
    """One poll: pull replies, then read and send at most one allowlisted reply."""
    now = time.time() if now is None else float(now)
    summary = {
        "enabled": enabled(),
        "forwarded": 0,
        "skipped": 0,
        "rate_limited": 0,
        "queued": 0,
        "send": "skipped",
        "session_ok": None,
    }
    if not enabled():
        summary["send"] = "disabled"
        return summary
    allowed = allowlist()
    if not allowed:
        record_poll(None, "", notify=notify)
        logger.info("小号白名单为空，本轮不打开浏览器、不转发、不回复")
        summary["send"] = "allowlist_empty"
        return summary
    if cycle is None:
        from assistant_browser import run_browser_cycle as cycle
    puller = pull_replies if pull is None else pull
    acker = ack_replies if ack is None else ack
    try:
        pulled = puller() or []
    except Exception as exc:
        logger.warning("小号回复拉取失败：%s", redact(exc))
        pulled = []
    if pulled:
        acked = ingest_pulled(pulled, now=now)
        summary["queued"] = len(acked)
        try:
            acker(acked)
        except Exception as exc:
            logger.warning("小号回复确认失败：%s", redact(exc))
    pending, dropped_reply = _select_pending(allowed, now)
    try:
        outcome = cycle(allowed, pending)
    except Exception as exc:
        record_poll(None, redact(exc), notify=notify)
        logger.warning("小号浏览器轮询失败：%s", redact(exc))
        summary["send"] = "browser_error"
        return summary
    if not isinstance(outcome, dict):
        outcome = {}
    session_ok = outcome.get("session_ok")
    record_poll(session_ok, str(outcome.get("reason") or ""), notify=notify)
    if dropped_reply and session_ok is not False:
        with _lock:
            store = _load()
            if not store.get("last_error"):
                store["last_error"] = "已丢弃不在白名单内的回复"
                _save(store)
    summary["session_ok"] = session_ok
    if session_ok is False:
        summary["send"] = "session_invalid"
        return summary
    accepted = accept_incoming(outcome.get("messages") or [], now=now, notify=notify)
    summary.update({"forwarded": accepted["forwarded"], "skipped": accepted["skipped"],
                    "rate_limited": accepted["rate_limited"]})
    if pending is not None and outcome.get("send"):
        send_info = outcome.get("send") if isinstance(outcome.get("send"), dict) else {"ok": False}
        status = dispatch_recorded_send(pending, send_info, now=now)
        summary["send"] = status
    elif pending is None:
        summary["send"] = "empty"
    else:
        summary["send"] = "not_attempted"
    return summary


def dispatch_recorded_send(pending: dict, send_info: dict, *, now: float) -> str:
    """Apply a browser send result that already happened inside the same session."""
    if send_info.get("ok"):
        with _lock:
            store = _load()
            store["replies"] = [row for row in store.get("replies") or [] if row.get("id") != pending.get("id")]
            sent = [value for value in store.get("sent_reply_ids") or [] if value != pending.get("id")]
            sent.append(pending.get("id"))
            store["sent_reply_ids"] = sent[-MAX_SEEN:]
            store["last_send_at"] = now
            store["last_error"] = ""
            _save(store)
        logger.info("小号回复已发送 id=%s to=%s chars=%s", pending.get("id"), pending.get("to"), len(pending.get("text") or ""))
        return "sent"
    permanent = bool(send_info.get("permanent"))
    with _lock:
        store = _load()
        if permanent:
            store["replies"] = [row for row in store.get("replies") or [] if row.get("id") != pending.get("id")]
        store["last_error"] = redact(send_info.get("reason") or "发送失败")[:180]
        _save(store)
    logger.info("小号回复未发出 id=%s permanent=%s", pending.get("id"), permanent)
    return "failed"


def session_file_ready() -> bool:
    path = state_path()
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, list):
        return False
    return any(isinstance(item, dict) and str(item.get("name") or "").startswith("sessionid")
               and str(item.get("value") or "").strip() for item in cookies)


def status_snapshot() -> dict:
    with _lock:
        store = _load()
        recent = []
        for item in (store.get("recent") or [])[:MAX_RECENT]:
            if not isinstance(item, dict):
                continue
            recent.append({
                "message_id": str(item.get("message_id") or "")[:200],
                "conversation_name": str(item.get("conversation_name") or "")[:80],
                "sender_id": str(item.get("sender_id") or "")[:80],
                "message_time": str(item.get("message_time") or "")[:40],
                "kind": "share" if item.get("kind") == "share" else "text",
                "preview": str(item.get("preview") or "")[:PREVIEW_CHARS],
                "chars": int(item.get("chars") or 0),
            })
        pending = len(store.get("replies") or [])
        return {
            "enabled": enabled(),
            "session_ok": store.get("session_ok"),
            "session_file": session_file_ready(),
            "last_poll": store.get("last_poll"),
            "poll_seconds": poll_seconds(),
            "allowlist": allowlist(),
            "recent": recent,
            "pending_replies": pending,
            "last_error": str(store.get("last_error") or "")[:180],
        }


def scheduled_poll() -> None:
    try:
        from assistant_login import assistant_qr_active
        if assistant_qr_active():
            logger.info("小号扫码进行中，本轮收件跳过")
            return
        summary = run_cycle()
        logger.info(
            "小号收件轮询结束 forwarded=%s skipped=%s rate_limited=%s send=%s session_ok=%s",
            summary.get("forwarded", 0),
            summary.get("skipped", 0),
            summary.get("rate_limited", 0),
            summary.get("send"),
            summary.get("session_ok"),
        )
    except Exception as exc:
        logger.warning("小号收件轮询失败：%s", redact(exc))


def start_poller() -> bool:
    if not enabled():
        logger.info("小号收件未启用（ASSISTANT_INBOX_ENABLED=0）")
        return False
    from core import scheduler
    scheduler.add_interval_job(scheduled_poll, poll_seconds(), JOB_ID)
    return True
