"""回环连接与显式同源入口边界、缓存策略、限速及安全响应头。"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from ip_guard import (
    SENSITIVE_PATHS,  # noqa: F401
    _check_rate,
    _client_ip,
    _local_request_allowed,
    _same_origin_allowed,
)


async def local_request_guard(request, call_next):
    """仅接受回环连接上的本机直开或显式配置入口；API 另检查同源证据。"""
    if not _local_request_allowed(request):
        return JSONResponse(
            status_code=403,
            content={"detail": "入口来源不被允许，请检查本机地址或公开入口配置"},
            headers={"Cache-Control": "no-store"},
        )
    if not _same_origin_allowed(request):
        return JSONResponse(
            status_code=403,
            content={"detail": "接口仅允许从当前同源页面操作"},
            headers={"Cache-Control": "no-store"},
        )
    return await call_next(request)


async def cache_policy(request, call_next):
    """HTML 禁缓存；本地静态资源允许复用并重新验证。"""
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
        response.headers["Cache-Control"] = "public, max-age=604800, must-revalidate"
    elif path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=86400, must-revalidate"
    return response


async def rate_limit(request, call_next):
    """全局内存限速：先于业务逻辑执行。"""
    if not _check_rate(_client_ip(request), request.url.path):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试"})
    return await call_next(request)


async def security_headers(request, call_next):
    """补齐安全响应头。"""
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


def register_middleware(app) -> None:
    """后注册的中间件位于外层；业务处理前先完成本机/同源检查。"""
    app.middleware("http")(cache_policy)
    app.middleware("http")(rate_limit)
    app.middleware("http")(local_request_guard)
    app.middleware("http")(security_headers)
