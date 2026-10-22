"""认证路由：管理令牌登录、URL 口令登录、注销与旧客户端兼容。"""

from __future__ import annotations

from fastapi import APIRouter, Body, Header, Request, Response
from fastapi import HTTPException

from app_common import logger
from auth_cookies import cookie_names
from request_origin import _effective_origin
from auth_service import (
    DEVICE_AUTH_TTL,
    SESSION_TTL,
    AUTH_TOKEN,
    LOGIN_GATE,
    PASSWORD_PUBLIC_LOGIN,
    URL_ACCESS_TOKEN,
    URL_PASSWORD_HASH,
    _check_auth,
    _issue_session,
    _management_token_valid,
    _revoke_session,
    _secret_equal,
    _verify_and_migrate_url_password,
)

router = APIRouter()


@router.get("/api/auth/challenge")
def api_auth_challenge() -> dict:
    """兼容旧客户端：公开公式不再作为认证方式。"""
    return {
        "auth_mode": "token",
        "deprecated": True,
        "detail": "挑战公式已停用，请使用 AUTH_TOKEN 登录",
    }


@router.get("/api/auth/mode")
def api_auth_mode() -> dict:
    """公开端点：告知首页登录门应展示哪种登录方式（不含任何凭据）。"""
    password_ready = bool(URL_PASSWORD_HASH)
    return {
        "login_gate": bool(LOGIN_GATE),
        # 裸域名直接显示密码框：开关开启且密码哈希已配置
        "password_public": bool(PASSWORD_PUBLIC_LOGIN and password_ready),
        # 带 URL 访问令牌的私密入口是否可用
        "token_entry": bool(URL_ACCESS_TOKEN and password_ready),
    }


@router.post("/api/auth/login")
def api_auth_login(
    response: Response,
    request: Request,
    body: dict = Body(...),
) -> dict:
    """用部署时生成的高强度 AUTH_TOKEN 登录，颁发 24 小时 HttpOnly 会话。"""
    supplied = str(body.get("token", "")).strip()
    if not _management_token_valid(supplied):
        logger.warning("管理令牌登录失败（输入长度=%s）", len(supplied))
        raise HTTPException(status_code=401, detail="安全令牌不正确")
    session_token = _issue_session()
    response.set_cookie(
        cookie_names()[0],
        session_token,
        max_age=SESSION_TTL,
        httponly=True,
        secure=(request.url.scheme == "https" or (_effective_origin(request) or (None,))[0] == "https"),
        samesite="lax",
        path="/",
    )
    logger.info("管理令牌验证成功，已颁发短期会话")
    return {"ok": True, "expires_in": SESSION_TTL}


@router.post("/api/auth/url-token-login")
def api_url_token_login(
    response: Response,
    request: Request,
    body: dict = Body(...),
) -> dict:
    """URL 令牌 + 密码验证：通过后颁发30天设备免登录令牌。

    PASSWORD_PUBLIC_LOGIN=on 时允许不带访问令牌（裸域名密码框），仅校验密码；
    未开启时必须同时提供正确的 URL 访问令牌。
    """
    access_token = str(body.get("access_token", "")).strip()
    password = str(body.get("password", ""))
    if access_token:
        if not URL_ACCESS_TOKEN or not _secret_equal(access_token, URL_ACCESS_TOKEN):
            raise HTTPException(status_code=401, detail="访问令牌不正确")
    elif not PASSWORD_PUBLIC_LOGIN:
        raise HTTPException(status_code=401, detail="访问令牌不正确")
    if not _verify_and_migrate_url_password(password):
        raise HTTPException(status_code=401, detail="密码不正确")
    device_token = _issue_session(device=True)
    response.set_cookie(
        cookie_names()[1],
        device_token,
        max_age=DEVICE_AUTH_TTL,
        httponly=True,
        secure=(request.url.scheme == "https" or (_effective_origin(request) or (None,))[0] == "https"),
        samesite="lax",
        path="/",
    )
    return {"ok": True, "ttl_days": 30}


@router.post("/api/auth/logout")
def api_auth_logout(
    response: Response,
    request: Request,
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    """吊销本次请求携带的全部会话，再删除两种浏览器 Cookie。"""
    presented = {token, *request.headers.getlist("x-auth-token")}
    presented.update(request.scope.get("spark_presented_auth_tokens", ()))
    presented.update(request.cookies.get(name, "") for name in cookie_names())
    for current in presented:
        _revoke_session(current)
    for name in cookie_names():
        response.delete_cookie(name, path="/")
    return {"ok": True}
