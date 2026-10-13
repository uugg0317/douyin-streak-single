"""回环连接、显式公开来源与进程内限速。"""

from __future__ import annotations

import ipaddress
import os
import time
from urllib.parse import urlsplit

from fastapi import Request


def _is_loopback(host: str) -> bool:
    if not isinstance(host, str) or not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
        mapped = getattr(address, "ipv4_mapped", None)
        return bool((mapped or address).is_loopback)
    except ValueError:
        return False


def _check_local_bind(host: str | None = None) -> None:
    """检查正常入口及 lifespan 配置；本版只允许监听回环地址。"""
    configured = os.environ.get("HOST", "127.0.0.1") if host is None else host
    if not _is_loopback(configured):
        raise SystemExit("本机版 HOST 必须是回环地址（127.0.0.1 / localhost / ::1），已拒绝启动")


def _client_ip(request: Request) -> str:
    """只使用直连对端，不以代理头授予本机访问权限。"""
    peer = request.scope.get("client")
    return peer[0] if peer else "unknown"


def _canonical_origin(value: str) -> tuple[str, str, int] | None:
    try:
        if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            return None
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment):
            return None
        host = parsed.hostname.lower().encode("idna").decode("ascii")
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            return None
        return parsed.scheme, host, port
    except (ValueError, UnicodeError):
        return None


def _host_origin(scheme: str, authority: str) -> tuple[str, str, int] | None:
    """Host 必须是单个 authority，按入口协议解释省略的默认端口。"""
    if not authority or any(c.isspace() or c in "/?#@," for c in authority):
        return None
    return _canonical_origin(f"{scheme}://{authority}")


def _forwarded_ips_valid(value: str, *, chain: bool) -> bool:
    """校验代理声明的客户地址；这些地址从不授予访问权限。"""
    if not value:
        return True
    parts = value.split(",") if chain else [value]
    if len(value) > 2048 or len(parts) > 16:
        return False
    try:
        for part in parts:
            ipaddress.ip_address(part.strip())
        return True
    except ValueError:
        return False


def _effective_origin(request: Request) -> tuple[str, str, int] | None:
    """仅对真实回环对端计算入口来源：本机直开，或显式配置的同机反代。"""
    if not _is_loopback(_client_ip(request)):
        return None
    for name in ("host", "origin", "x-forwarded-proto", "x-forwarded-host",
                 "x-forwarded-for", "x-real-ip", "forwarded"):
        if len(request.headers.getlist(name)) > 1:
            return None
    if request.headers.get("forwarded"):
        return None
    actual = _canonical_origin(str(request.base_url).rstrip("/"))
    if actual is None:
        return None
    authority = request.headers.get("host", "")
    if _host_origin(actual[0], authority) != actual:
        return None

    configured_text = os.environ.get("QR_PUBLIC_ORIGIN", "").strip()
    configured = _canonical_origin(configured_text) if configured_text else None
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if configured is not None and _host_origin(configured[0], authority) == configured:
        if forwarded_proto is not None:
            # 单个协议值且与配置完全一致；绝不解析逗号链或外部对端的代理头。
            if forwarded_proto.lower() != configured[0] or forwarded_proto != forwarded_proto.strip():
                return None
            forwarded_host = request.headers.get("x-forwarded-host")
            if forwarded_host is not None and _host_origin(configured[0], forwarded_host) != configured:
                return None
            if not _forwarded_ips_valid(request.headers.get("x-forwarded-for", ""), chain=True):
                return None
            if not _forwarded_ips_valid(request.headers.get("x-real-ip", ""), chain=False):
                return None
            return configured
        if not _is_loopback(actual[1]):
            return None

    # 配置公开入口后，本机直开仍保留原来的严格来源边界。
    if not _is_loopback(actual[1]) or request.headers.get("x-forwarded-host"):
        return None
    if forwarded_proto is not None and forwarded_proto.lower() != actual[0]:
        return None
    for name in ("x-real-ip", "x-forwarded-for"):
        value = request.headers.get(name, "")
        if value and any(not _is_loopback(part.strip()) for part in value.split(",")):
            return None
    return actual


def _local_request_allowed(request: Request) -> bool:
    return _effective_origin(request) is not None


def _same_origin_allowed(request: Request) -> bool:
    """API 写请求需有同源证据，公开反代请求与配置的外部 Origin 比较。"""
    if not request.url.path.startswith("/api/"):
        return True
    fetch_site = request.headers.get("sec-fetch-site", "")
    if fetch_site in {"cross-site", "same-site"}:
        return False
    origin = request.headers.get("origin")
    if origin is not None:
        actual = _effective_origin(request)
        return actual is not None and _canonical_origin(origin) == actual
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        return fetch_site == "same-origin"
    return True


RATE_GENERAL = {"limit": 120, "window": 60}
RATE_SENSITIVE = {"limit": 5, "window": 60}
SENSITIVE_PATHS = (
    "/api/run",
    "/api/sync",
    "/api/contacts/fetch",
    "/api/credentials/extract",
    "/api/ledger/harvest-creator",
    "/api/reset-running",
    "/api/upload-state",
    "/api/credentials/upload",
    "/api/config",
)
_hits: dict = {}
_MAX_RATE_KEYS = 20000


def _check_rate(ip: str, path: str) -> bool:
    now = time.time()
    sensitive = any(path.startswith(p) for p in SENSITIVE_PATHS)
    rule = RATE_SENSITIVE if sensitive else RATE_GENERAL
    key = f"{ip}:{path}" if sensitive else ip
    q = _hits.get(key)
    if q is None:
        if len(_hits) >= _MAX_RATE_KEYS:
            stale = sorted(_hits.items(), key=lambda kv: kv[1][-1] if kv[1] else 0)
            for k, _ in stale[: _MAX_RATE_KEYS // 10]:
                _hits.pop(k, None)
        q = _hits.setdefault(key, [])
    while q and q[0] <= now - rule["window"]:
        q.pop(0)
    if len(q) >= rule["limit"]:
        return False
    q.append(now)
    return True
