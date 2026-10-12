"""认证服务：管理令牌、URL 口令、登录门、会话存储与鉴权。"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time

from fastapi import HTTPException

from app_common import _env, logger

AUTH_TOKEN = _env("AUTH_TOKEN")

# URL 令牌 / 口令哈希必须由 .env 显式提供，不再有源码内置默认值（防「源码即凭据」）
URL_ACCESS_TOKEN = _env("URL_ACCESS_TOKEN")
URL_PASSWORD_SALT = _env("URL_PASSWORD_SALT")
URL_PASSWORD_HASH = _env("URL_PASSWORD_HASH")
DEVICE_AUTH_TTL = 30 * 86400
SESSION_TTL = 86400

# 登录门总开关：off 时不做鉴权（仅限本机回环自用）
LOGIN_GATE = (_env("LOGIN_GATE") or "on").strip().lower() not in {"off", "0", "false", "no"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "ip6-localhost"}

# 已验证会话只保存在内存中，服务重启后自动失效。
_sessions: dict[str, float] = {}
_device_auth_tokens: dict[str, float] = {}
_auth_store_lock = threading.RLock()
_MAX_SESSIONS = 5000
_MAX_DEVICE_TOKENS = 5000


def _check_login_gate_safety() -> None:
    """关掉登录门时，强制服务只能监听回环地址。"""
    host = (_env("HOST") or "127.0.0.1").strip().lower()
    if host in LOOPBACK_HOSTS:
        return
    logger.error(
        "LOGIN_GATE=off 时 HOST 必须是回环地址（127.0.0.1 / localhost / ::1），"
        "当前为 %r —— 拒绝启动。无鉴权 + 对外监听 = 任何人都能操作后台并触发"
        "发送，后果不可撤回。要对外提供服务，请把 LOGIN_GATE 设回 on 并配好 AUTH_TOKEN。",
        host,
    )
    raise SystemExit("LOGIN_GATE=off 不允许监听非回环地址，已拒绝启动")


def _verify_url_password(password: str) -> bool:
    """验证 URL 令牌访问密码（SHA-256 + salt 恒定时间比较）。"""
    if not password:
        return False
    if not (URL_PASSWORD_SALT and URL_PASSWORD_HASH):
        return False  # 未配置则一律失败，绝不放行
    input_hash = hashlib.sha256((password + URL_PASSWORD_SALT).encode()).hexdigest()
    return hmac.compare_digest(input_hash, URL_PASSWORD_HASH)


def _check_auth_config() -> None:
    """检查认证所需配置是否齐备，缺失时拒绝启动。"""
    if not LOGIN_GATE:
        logger.warning(
            "登录门已关闭（LOGIN_GATE=off）：/api/* 不再校验任何令牌，"
            "打开页面直接进后台。仅限本机自用，切勿暴露到公网。"
        )
        _check_login_gate_safety()
        return

    problems: list[str] = []
    placeholder_prefixes = ("change_me", "changeme", "replace_me", "your_")
    url_login_ready = bool(
        URL_ACCESS_TOKEN and URL_PASSWORD_SALT and URL_PASSWORD_HASH
    )
    if AUTH_TOKEN.lower().startswith(placeholder_prefixes):
        problems.append("AUTH_TOKEN 仍是示例占位值，请生成随机令牌后再启动")
    if URL_ACCESS_TOKEN.lower().startswith(placeholder_prefixes):
        problems.append("URL_ACCESS_TOKEN 仍是示例占位值")
    if URL_PASSWORD_SALT.lower().startswith(placeholder_prefixes):
        problems.append("URL_PASSWORD_SALT 仍是示例占位值")
    if URL_ACCESS_TOKEN and not (URL_PASSWORD_SALT and URL_PASSWORD_HASH):
        problems.append(
            "已配置 URL_ACCESS_TOKEN 但缺少 URL_PASSWORD_SALT / URL_PASSWORD_HASH，"
            "URL 令牌登录将无法验证口令"
        )
    if URL_PASSWORD_HASH and not re.fullmatch(r"[0-9a-fA-F]{64}", URL_PASSWORD_HASH):
        problems.append("URL_PASSWORD_HASH 必须是 64 位 SHA-256 十六进制字符串")
    if not AUTH_TOKEN and not url_login_ready:
        problems.append(
            "没有任何可用登录方式：请至少配置 AUTH_TOKEN，"
            "或同时配置 URL_ACCESS_TOKEN + URL_PASSWORD_SALT + URL_PASSWORD_HASH"
        )
    if problems:
        for p in problems:
            logger.error("认证配置错误：%s", p)
        raise SystemExit("认证配置不完整，拒绝启动（详见上方日志）")

    logger.info(
        "认证方式：管理令牌登录=%s，URL 令牌+密码登录=%s",
        "启用" if AUTH_TOKEN else "禁用",
        "启用" if url_login_ready else "禁用",
    )
    if AUTH_TOKEN and len(AUTH_TOKEN) < 24:
        logger.warning("AUTH_TOKEN 长度不足 24 字符，建议更换为高强度随机令牌")


def _check_auth(token: str) -> None:
    # 登录门关闭：全部调用点直接放行。
    if not LOGIN_GATE:
        return
    # 恒定时间比较；支持 AUTH_TOKEN、网页登录会话与设备免登录会话。
    if not token:
        raise HTTPException(status_code=401, detail="未提供访问令牌")
    if AUTH_TOKEN and hmac.compare_digest(str(token), str(AUTH_TOKEN)):
        return
    if _session_valid(token):
        return
    raise HTTPException(status_code=401, detail="访问令牌不正确或已过期")


def _prune_store(store: dict, cap: int, now: float) -> None:
    """先删过期项；仍超上限时按过期时间淘汰最早的若干项（防止内存无界增长）。"""
    for k in [k for k, v in store.items() if v < now]:
        del store[k]
    if len(store) > cap:
        for k, _ in sorted(store.items(), key=lambda kv: kv[1])[: len(store) - cap]:
            del store[k]


def _clean_expired_sessions() -> None:
    now = time.time()
    with _auth_store_lock:
        _prune_store(_sessions, _MAX_SESSIONS, now)
        _prune_store(_device_auth_tokens, _MAX_DEVICE_TOKENS, now)


def _issue_session(*, device: bool = False) -> str:
    """颁发有界的内存会话；调用端只把令牌写入 HttpOnly Cookie。"""
    token = ("dev_" + secrets.token_hex(24)) if device else secrets.token_urlsafe(32)
    ttl = DEVICE_AUTH_TTL if device else SESSION_TTL
    store = _device_auth_tokens if device else _sessions
    cap = _MAX_DEVICE_TOKENS if device else _MAX_SESSIONS
    now = time.time()
    with _auth_store_lock:
        _prune_store(store, cap - 1, now)
        store[token] = now + ttl
    return token


def _remember_session(token: str, ttl: int = SESSION_TTL) -> None:
    """记录内部生成的临时会话（用于受信任 IP 旁路）。"""
    if not token:
        return
    now = time.time()
    with _auth_store_lock:
        _prune_store(_sessions, _MAX_SESSIONS - 1, now)
        _sessions[token] = now + max(1, int(ttl))


def _session_valid(token: str, *, device: bool | None = None) -> bool:
    if not token:
        return False
    now = time.time()
    stores = (
        (_device_auth_tokens,) if device is True
        else (_sessions,) if device is False
        else (_sessions, _device_auth_tokens)
    )
    with _auth_store_lock:
        for store in stores:
            expires = store.get(token, 0)
            if expires > now:
                return True
            if expires:
                store.pop(token, None)
    return False


def _revoke_session(token: str) -> None:
    if not token:
        return
    with _auth_store_lock:
        _sessions.pop(token, None)
        _device_auth_tokens.pop(token, None)
