"""认证路由：管理令牌登录、URL 口令登录、注销与旧客户端兼容。"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Body, Header, Request, Response
from fastapi import HTTPException

from app_common import logger
from auth_service import (
    DEVICE_AUTH_TTL,
    SESSION_TTL,
    AUTH_TOKEN,
    URL_ACCESS_TOKEN,
    _check_auth,
    _issue_session,
    _revoke_session,
    _verify_url_password,
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


@router.post("/api/auth/login")
def api_auth_login(
    response: Response,
    request: Request,
    body: dict = Body(...),
) -> dict:
    """用部署时生成的高强度 AUTH_TOKEN 登录，颁发 24 小时 HttpOnly 会话。"""
    supplied = str(body.get("token", "")).strip()
    if not AUTH_TOKEN or not supplied or not hmac.compare_digest(supplied, AUTH_TOKEN):
        logger.warning("管理令牌登录失败（输入长度=%s）", len(supplied))
        raise HTTPException(status_code=401, detail="安全令牌不正确")
    session_token = _issue_session()
    response.set_cookie(
        "session_auth_token",
        session_token,
        max_age=SESSION_TTL,
        httponly=True,
        secure=request.url.scheme == "https",
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
    """URL 令牌 + 密码验证：通过后颁发30天设备免登录令牌。"""
    access_token = str(body.get("access_token", "")).strip()
    password = str(body.get("password", "")).strip()
    if not URL_ACCESS_TOKEN or not hmac.compare_digest(access_token, URL_ACCESS_TOKEN):
        raise HTTPException(status_code=401, detail="访问令牌不正确")
    if not _verify_url_password(password):
        raise HTTPException(status_code=401, detail="密码不正确")
    device_token = _issue_session(device=True)
    response.set_cookie(
        "device_auth_token",
        device_token,
        max_age=DEVICE_AUTH_TTL,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )
    return {"ok": True, "ttl_days": 30}


@router.post("/api/auth/logout")
def api_auth_logout(
    response: Response,
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    """注销当前会话 token。"""
    _revoke_session(token)
    response.delete_cookie("session_auth_token", path="/")
    response.delete_cookie("device_auth_token", path="/")
    return {"ok": True}
