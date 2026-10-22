"""与无密码门版共用的入口来源规则；管理员认证仍由 auth_service 处理。"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

from fastapi import Request

from auth_cookies import cookie_names


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


def _allowed_api_host(actual: tuple[str, str, int]) -> bool:
    """精确匹配显式 API 别名；无端口项匹配主机名，有端口项限定端口。"""
    for item in os.environ.get("ALLOWED_HOSTS", "").split(","):
        item = item.strip()
        if not item or "*" in item:
            continue
        candidate = _host_origin(actual[0], item)
        if candidate is None or candidate[1] != actual[1]:
            continue
        parsed = urlsplit(f"{actual[0]}://{item}")
        if parsed.port is None or candidate[2] == actual[2]:
            return True
    return False


def _effective_origin(request: Request, *, allow_api_aliases: bool = False) -> tuple[str, str, int] | None:
    """仅对真实回环对端计算入口来源：本机直开，或显式配置的同机反代。"""
    if not _is_loopback(_client_ip(request)):
        return None
    for name in ("host", "origin", "sec-fetch-site", "x-auth-token", "x-requested-with", "x-forwarded-proto", "x-forwarded-host",
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

    if allow_api_aliases and _allowed_api_host(actual):
        # 别名只允许当前 Host，不以 forwarded-host 赋予权限，也不推断外部 HTTPS。
        # HTTPS→HTTP 同机反代仍须配置 QR_PUBLIC_ORIGIN，才能复用严格的入口及 Secure Cookie 契约。
        if forwarded_proto is not None and forwarded_proto != actual[0]:
            return None
        forwarded_host = request.headers.get("x-forwarded-host")
        if forwarded_host is not None and _host_origin(actual[0], forwarded_host) != actual:
            return None
        if not _forwarded_ips_valid(request.headers.get("x-forwarded-for", ""), chain=True):
            return None
        if not _forwarded_ips_valid(request.headers.get("x-real-ip", ""), chain=False):
            return None
        return actual

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


def _local_request_allowed(request: Request, *, allow_api_aliases: bool = False) -> bool:
    return _effective_origin(request, allow_api_aliases=allow_api_aliases) is not None


def _same_origin_allowed(request: Request, *, allow_api_aliases: bool = False) -> bool:
    """API 写请求需有同源证据，公开反代请求与配置的外部 Origin 比较。"""
    if not request.url.path.startswith("/api/"):
        return True
    fetch_site = request.headers.get("sec-fetch-site", "")
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return False
    origin = request.headers.get("origin")
    if origin is not None:
        actual = _effective_origin(request, allow_api_aliases=allow_api_aliases)
        return actual is not None and _canonical_origin(origin) == actual
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        return fetch_site == "same-origin"
    return True


def _trusted_cli_request(request: Request, *, management_token_valid: bool,
                         allow_api_aliases: bool = False) -> bool:
    """无浏览器元数据、无认证 Cookie 的 CLI 可显式提供管理令牌。"""
    return bool(
        management_token_valid
        and request.headers.get("origin") is None
        and request.headers.get("sec-fetch-site") is None
        and not any(name in request.cookies for name in cookie_names())
        and _effective_origin(request, allow_api_aliases=allow_api_aliases) is not None
    )

