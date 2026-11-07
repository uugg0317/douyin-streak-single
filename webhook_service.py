"""Grok Bot Webhook：把运行事件 POST 给已配置的例程。

未设置 GROK_BOT_WEBHOOK_URL 时直接返回，不访问网络。
投递是 best-effort：短超时、失败只记日志，异常不向外抛。
发送密钥放在请求头 X-Webhook-Key（HTTP 头名大小写不敏感）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from core.send_day import business_now

logger = logging.getLogger("douyin-cloud-streak")

WEBHOOK_KEY_HEADER = "X-Webhook-Key"
WEBHOOK_TIMEOUT_SECONDS = 4
_MAX_DETAILS_BYTES = 8000

# 键名里出现这些词就整段丢掉，避免 cookie / token 进入外发 JSON。
_SENSITIVE_KEY = re.compile(
    r"cookie|token|password|passwd|secret|authorization|sessionid|smtp_pass|webhook_key",
    re.IGNORECASE,
)
_SECRET_ASSIGN = re.compile(
    r"(?i)\b(sessionid(?:_[a-z0-9]+)?|sid_guard|passport_csrf_token|token|password|"
    r"passwd|authorization|smtp_pass|webhook[-_]?key|cookie)\b"
    r"(\s*[:=]\s*['\"]?)([^\s'\",}]{4,})"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{6,}")


class _WebhookRequest(urllib.request.Request):
    """保留调用方写下的头名称。urllib 默认会把 X-Webhook-Key 收成 X-webhook-key。"""

    def add_header(self, key, val):
        self.headers[key] = val


def webhook_enabled() -> bool:
    return bool(os.environ.get("GROK_BOT_WEBHOOK_URL", "").strip())


def notify_event(event_type: str, payload: dict | None = None) -> bool:
    """POST 一条事件。成功返回 True；未启用、地址无效或投递失败返回 False。

    payload 只读取 summary（短文本）和 details（对象）。其他键忽略。
    """
    try:
        return _notify_event(event_type, payload)
    except Exception as exc:
        logger.warning("Webhook 通知失败（%s）：%s", event_type, _public_error(exc))
        return False


def _notify_event(event_type: str, payload: dict | None) -> bool:
    url = os.environ.get("GROK_BOT_WEBHOOK_URL", "").strip()
    if not url:
        logger.debug("Webhook 未配置，跳过 %s", event_type)
        return False
    if not isinstance(event_type, str) or not event_type.strip():
        return False
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        logger.warning("Webhook 地址不是 http(s) URL，已跳过 %s", event_type)
        return False

    body_payload = payload if isinstance(payload, dict) else {}
    summary = str(body_payload.get("summary") or "").strip() or event_type.strip()
    raw_details = body_payload.get("details")
    details = _sanitize(raw_details if isinstance(raw_details, dict) else {})
    if not isinstance(details, dict):
        details = {}
    encoded_details = json.dumps(details, ensure_ascii=False).encode("utf-8")
    if len(encoded_details) > _MAX_DETAILS_BYTES:
        details = {"truncated": True, "note": "细节过长已省略"}

    body = {
        "event": event_type.strip(),
        "time": business_now().isoformat(timespec="seconds"),
        "host": _hostname(),
        "summary": _redact_text(summary)[:180],
        "details": details,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "sparkkeeper-webhook",
    }
    key = os.environ.get("GROK_BOT_WEBHOOK_KEY", "").strip()
    if key:
        headers[WEBHOOK_KEY_HEADER] = key
    request = _WebhookRequest(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS) as response:
            response.read(1024)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Webhook 通知失败（%s）：%s", event_type, _public_error(exc))
        return False
    logger.info("Webhook 已通知：%s", event_type)
    return True


def _hostname() -> str:
    try:
        return socket.gethostname() or "unknown"
    except Exception:
        return "unknown"


def _public_error(exc: BaseException) -> str:
    text = str(exc)
    for env_name in ("GROK_BOT_WEBHOOK_URL", "GROK_BOT_WEBHOOK_KEY"):
        secret = os.environ.get(env_name, "").strip()
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:300]


def _sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key))


def _redact_text(text: str) -> str:
    text = _BEARER.sub("Bearer [redacted]", text)
    return _SECRET_ASSIGN.sub(lambda match: match.group(1) + match.group(2) + "[redacted]", text)


def _sanitize(value, depth: int = 0, key: str = ""):
    if depth > 4:
        return None
    if isinstance(value, dict):
        cleaned = {}
        for raw_key, item in list(value.items())[:40]:
            if not isinstance(raw_key, str) or _sensitive_key(raw_key):
                continue
            child = _sanitize(item, depth + 1, raw_key)
            if child is not None or item is None:
                cleaned[raw_key] = child
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth + 1, key) for item in list(value)[:30]]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        limit = 1200 if key == "stack_excerpt" else 400
        return _redact_text(value)[:limit]
    return _redact_text(str(value))[:200]
