"""全部 HTTP 中间件：白名单免登录、设备免登录、禁缓存、限速、安全头、IP 锁定。"""

from __future__ import annotations

import secrets
import time
from datetime import datetime

from fastapi.responses import HTMLResponse, JSONResponse

from app_common import logger
from auth_service import _remember_session, _session_valid
from ip_guard import (
    AUTH_FAIL_PATHS,
    LOCAL_IPS,
    LOCK_TTL_SECONDS,
    LOGIN_FAIL_LIMIT,
    LOCK_HTML,
    SENSITIVE_PATHS,  # noqa: F401  (保留语义导出)
    WHITELIST_IPS,
    _blacklist,
    _check_rate,
    _client_ip,
    _fail_counts,
    _lock_expire_at,
    _safe_log_field,
    _save_blacklist,
    _whitelist,
)

_WHITELIST_BYPASS_TOKEN = "wl_" + secrets.token_urlsafe(32)


def _set_request_header(request, name: bytes, value: bytes) -> None:
    """设置/替换请求头，保留其他头（含重复项）。ASGI headers 为 (name,value) 列表。"""
    lowered = name.lower()
    kept = [(k, v) for (k, v) in request.scope["headers"] if k.lower() != lowered]
    kept.append((name, value))
    request.scope["headers"] = kept


async def whitelist_ip_bypass(request, call_next):
    """白名单 IP 自动注入有效会话令牌，免登录访问（只信任 X-Real-IP）。"""
    if not WHITELIST_IPS:
        return await call_next(request)
    client_ip = _client_ip(request)
    if client_ip in WHITELIST_IPS:
        # 使用每次进程启动随机生成且从不返回客户端的内部令牌，避免旧版
        # md5(IP) 令牌可被知道白名单 IP 的外部请求伪造。
        _remember_session(_WHITELIST_BYPASS_TOKEN, 86400)
        _set_request_header(
            request, b"x-auth-token", _WHITELIST_BYPASS_TOKEN.encode()
        )
    return await call_next(request)


async def url_token_device_auth(request, call_next):
    """HttpOnly 会话 Cookie 有效时自动注入内部鉴权头。"""
    session_token = request.cookies.get("session_auth_token", "")
    device_token = request.cookies.get("device_auth_token", "")
    if _session_valid(session_token, device=False):
        _set_request_header(request, b"x-auth-token", session_token.encode())
    elif _session_valid(device_token, device=True):
        _set_request_header(request, b"x-auth-token", device_token.encode())
    return await call_next(request)


async def cache_policy(request, call_next):
    """HTML 禁缓存；体积较大的本地静态资源允许浏览器复用并重新验证。"""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    elif path == "/api/avatar":
        response.headers["Cache-Control"] = "private, max-age=86400"
    elif path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif path.startswith("/static/vendor/"):
        # 文件名尚未带内容哈希，因此不设 immutable；一周后重新校验可兼顾升级。
        response.headers["Cache-Control"] = "public, max-age=604800, must-revalidate"
    elif path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=86400, must-revalidate"
    return response


async def rate_limit(request, call_next):
    """全局限速：先于业务逻辑执行，认证失败请求同样计入。"""
    ip = _client_ip(request)
    if not _check_rate(ip, request.url.path):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试"})
    return await call_next(request)


async def security_headers(request, call_next):
    """补齐安全响应头（HSTS / nosniff / XFO / Referrer / CSP）。"""
    response = await call_next(request)
    response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; connect-src 'self'",
    )
    return response


async def ip_lock_guard(request, call_next):
    """同一外部 IP 连续输错密钥达阈值即拉黑；黑名单内 IP 一律 403。"""
    ip = _client_ip(request)
    is_local = ip in LOCAL_IPS or ip in _whitelist  # 本机或永久白名单：豁免
    # 已拉黑的外部 IP：直接拒绝，不进入任何业务接口
    if not is_local and ip in _blacklist:
        entry = _blacklist[ip]
        if time.time() > _lock_expire_at(entry):
            logger.info("IP %s 的封禁已到期，自动解封", _safe_log_field(ip, 64))
            del _blacklist[ip]
            _fail_counts.pop(ip, None)
            _save_blacklist()
        else:
            accept = request.headers.get("accept", "")
            if request.url.path.startswith("/api/") or "application/json" in accept:
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"该 IP 因连续 {LOGIN_FAIL_LIMIT} 次密钥错误已被锁定，请联系管理员解封"},
                    media_type="application/json; charset=utf-8",
                )
            page = LOCK_HTML.replace("__LIMIT__", str(LOGIN_FAIL_LIMIT))
            return HTMLResponse(page, status_code=403)
    response = await call_next(request)
    path = request.url.path
    # 仅认证端点参与计数与清零
    if any(path.startswith(p) for p in AUTH_FAIL_PATHS) and not is_local:
        if response.status_code == 401:
            _fail_counts[ip] = _fail_counts.get(ip, 0) + 1
            # 固定格式认证失败日志，供 fail2ban 过滤计数（AUTH_FAIL ip=<地址>）
            logger.warning(
                "AUTH_FAIL ip=%s path=%s fails=%s",
                _safe_log_field(ip, 64), _safe_log_field(path), _fail_counts[ip],
            )
            if _fail_counts[ip] >= LOGIN_FAIL_LIMIT and ip not in _blacklist:
                _blacklist[ip] = {
                    "locked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "fails": _fail_counts[ip],
                    "locked_until": time.time() + LOCK_TTL_SECONDS,
                }
                _save_blacklist()
                logger.warning("IP %s 连续认证失败 %s 次，已加入黑名单",
                               _safe_log_field(ip, 64), _fail_counts[ip])
        elif response.status_code < 400:
            _fail_counts.pop(ip, None)
    return response


def register_middleware(app) -> None:
    """按原始顺序注册中间件（后注册的更靠近外层）。"""
    app.middleware("http")(whitelist_ip_bypass)
    app.middleware("http")(url_token_device_auth)
    app.middleware("http")(cache_policy)
    app.middleware("http")(rate_limit)
    app.middleware("http")(security_headers)
    app.middleware("http")(ip_lock_guard)
