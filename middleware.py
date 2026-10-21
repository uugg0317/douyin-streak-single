"""Local request boundary, cache policy, request limits and response headers."""
from fastapi.responses import JSONResponse
from request_origin import _local_request_allowed, _same_origin_allowed
from ip_guard import _check_rate, _client_ip

async def api_origin_guard(request, call_next):
    if not _local_request_allowed(request):
        return JSONResponse(status_code=403,content={'detail':'此版本仅允许本机直接访问，不支持公网、反向代理或域名别名'})
    if request.url.path.startswith('/api/'):
        if request.method.upper() not in {'GET','HEAD','OPTIONS'} and request.headers.get('x-requested-with')!='sparkkeeper':
            return JSONResponse(status_code=403,content={'detail':'接口缺少有效请求标记，请刷新页面重试'})
        if not _same_origin_allowed(request):
            return JSONResponse(status_code=403,content={'detail':'接口仅允许同源请求，请刷新页面重试'})
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


def register_middleware(app) -> None:
    app.middleware('http')(cache_policy)
    app.middleware('http')(rate_limit)
    app.middleware('http')(security_headers)
    app.middleware('http')(api_origin_guard)
