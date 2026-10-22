"""认证服务：管理令牌、URL 口令、登录门、会话存储与鉴权。"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time

from fastapi import HTTPException

from app_common import _env, logger
import auth_config
from auth_config import _safe_equal, password_hash_kind, verify_password

AUTH_TOKEN = _env("AUTH_TOKEN")

# URL 令牌 / 口令哈希必须由 .env 显式提供，不再有源码内置默认值（防「源码即凭据」）
URL_ACCESS_TOKEN = _env("URL_ACCESS_TOKEN")
URL_PASSWORD_SALT = _env("URL_PASSWORD_SALT")
URL_PASSWORD_HASH = _env("URL_PASSWORD_HASH")
DEVICE_AUTH_TTL = 30 * 86400
SESSION_TTL = 86400

# 登录门总开关：off 时不做鉴权（仅限本机回环自用）
LOGIN_GATE = (_env("LOGIN_GATE") or "on").strip().lower() not in {"off", "0", "false", "no"}
# 裸域名密码登录：on 时打开网站首页直接显示访问密码框，无需 URL 访问令牌。
# 此时密码是唯一防线，必须配置高强度口令，且依赖失败锁定/限速拦截在线穷举。
PASSWORD_PUBLIC_LOGIN = (_env("PASSWORD_PUBLIC_LOGIN") or "").strip().lower() in {
    "on", "1", "true", "yes",
}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "ip6-localhost"}

# 已验证会话只保存在内存中，服务重启后自动失效。
_sessions: dict[str, float] = {}
_device_auth_tokens: dict[str, float] = {}
_auth_store_lock = threading.RLock()
_password_state_lock = threading.RLock()
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
    """验证自含盐 scrypt 或旧 SHA-256+salt 配置。"""
    with _password_state_lock:
        return verify_password(password, URL_PASSWORD_SALT, URL_PASSWORD_HASH)


def _verify_and_migrate_url_password(password: str) -> bool:
    """Authenticate first; a best-effort legacy rehash must never break login."""
    global URL_PASSWORD_SALT, URL_PASSWORD_HASH
    with _password_state_lock:
        # Exact input first preserves legacy whitespace passwords. The trimmed
        # fallback preserves the historical HTTP login input-normalization rule.
        candidates = [password]
        if isinstance(password, str) and password.strip() != password:
            candidates.append(password.strip())
        verified = next((candidate for candidate in candidates
                         if verify_password(candidate, URL_PASSWORD_SALT, URL_PASSWORD_HASH)), None)
        if verified is None:
            return False
        if password_hash_kind(URL_PASSWORD_SALT, URL_PASSWORD_HASH) != 'sha256':
            return True
        old_salt, old_hash = URL_PASSWORD_SALT, URL_PASSWORD_HASH
        try:
            import app_common

            def disk_credentials():
                values = {}
                for raw in app_common.ENV_PATH.read_text(encoding='utf-8').splitlines():
                    line = raw.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        values.setdefault(key.strip(), value.strip())
                return values.get('URL_PASSWORD_SALT', ''), values.get('URL_PASSWORD_HASH', '')

            # Acquire the plain config lock exactly once; the internal writer
            # expects its caller to own it. Other config writers use this lock.
            with app_common._env_file_lock:
                if disk_credentials() != (old_salt, old_hash):
                    logger.info('旧口令配置已变化，本次不覆盖磁盘配置')
                    return True
                upgraded = auth_config.rehash_legacy_password(verified)
                if upgraded is None:
                    logger.info('旧口令超出迁移支持范围，保留原格式和登录能力')
                    return True
                if disk_credentials() != (old_salt, old_hash):
                    logger.info('迁移期间配置已变化，本次保留原登录结果')
                    return True
                app_common._save_env_values_locked({'URL_PASSWORD_HASH': upgraded, 'URL_PASSWORD_SALT': ''})
                URL_PASSWORD_SALT, URL_PASSWORD_HASH = '', upgraded
            logger.info('旧口令已升级为 scrypt，口令内容保持不变')
        except Exception as error:
            # Avoid exception text/traceback: configuration and injected errors
            # may contain credential values. Generation and IO both land here.
            logger.warning('旧口令迁移未完成，保留本次成功登录（%s）', type(error).__name__)
        return True


def _secret_equal(supplied: str, expected: str) -> bool:
    """用字节做恒定时间比较；Unicode/畸形输入按无效凭据处理。"""
    return _safe_equal(supplied, expected)


def _management_token_valid(token: str) -> bool:
    """只认可部署管理令牌；用于无 Cookie 的 CLI 来源契约。"""
    return bool(AUTH_TOKEN and token and _secret_equal(token, AUTH_TOKEN))


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
    hash_kind = password_hash_kind(URL_PASSWORD_SALT, URL_PASSWORD_HASH)
    url_login_ready = bool(URL_ACCESS_TOKEN and hash_kind)
    if AUTH_TOKEN.lower().startswith(placeholder_prefixes):
        problems.append("AUTH_TOKEN 仍是示例占位值，请生成随机令牌后再启动")
    if URL_ACCESS_TOKEN.lower().startswith(placeholder_prefixes):
        problems.append("URL_ACCESS_TOKEN 仍是示例占位值")
    if not URL_PASSWORD_HASH.startswith("scrypt$") and URL_PASSWORD_SALT.lower().startswith(placeholder_prefixes):
        problems.append("URL_PASSWORD_SALT 仍是示例占位值")
    if URL_ACCESS_TOKEN and not hash_kind:
        problems.append(
            "已配置 URL_ACCESS_TOKEN 但缺少有效的 scrypt 或 SHA-256+salt 口令配置，"
            "URL 令牌登录将无法验证口令"
        )
    if URL_PASSWORD_HASH and not hash_kind:
        problems.append("URL_PASSWORD_HASH 格式无效或超出 scrypt 参数上限；旧 SHA-256 需配有效盐")
    if hash_kind == "scrypt" and not callable(getattr(hashlib, "scrypt", None)):
        problems.append("当前 Python 缺少 scrypt 支持，请使用包含 OpenSSL 的 Python")
    if PASSWORD_PUBLIC_LOGIN and not hash_kind:
        problems.append(
            "PASSWORD_PUBLIC_LOGIN=on 时必须配置有效 scrypt 或 SHA-256+salt 口令，"
            "否则直接密码登录无凭据可校验"
        )
    if not AUTH_TOKEN and not url_login_ready and not (PASSWORD_PUBLIC_LOGIN and hash_kind):
        problems.append(
            "没有任何可用登录方式：请至少配置 AUTH_TOKEN，"
            "或配置有效口令并开启直接密码登录 / 配置 URL_ACCESS_TOKEN"
        )
    if problems:
        for p in problems:
            logger.error("认证配置错误：%s", p)
        raise SystemExit("认证配置不完整，拒绝启动（详见上方日志）")

    if hash_kind == "sha256":
        logger.warning("正在兼容旧 SHA-256+salt 口令；成功登录后尝试兼容升级为 scrypt，现有密码不会重置")

    logger.info(
        "认证方式：管理令牌登录=%s，URL 令牌+密码登录=%s，裸域名密码登录=%s",
        "启用" if AUTH_TOKEN else "禁用",
        "启用" if url_login_ready else "禁用",
        "启用" if PASSWORD_PUBLIC_LOGIN else "禁用",
    )
    if PASSWORD_PUBLIC_LOGIN:
        logger.warning(
            "PASSWORD_PUBLIC_LOGIN 已启用：裸域名只凭访问密码即可登录，"
            "URL 访问令牌不再是必备入口；请确认口令足够强（建议 12 位以上随机串），"
            "失败锁定与限速保持开启"
        )
    if AUTH_TOKEN and len(AUTH_TOKEN) < 24:
        logger.warning("AUTH_TOKEN 长度不足 24 字符，建议更换为高强度随机令牌")


def _check_auth(token: str) -> None:
    # 登录门关闭：全部调用点直接放行。
    if not LOGIN_GATE:
        return
    # 恒定时间比较；支持 AUTH_TOKEN、网页登录会话与设备免登录会话。
    if not isinstance(token, str) or not token:
        raise HTTPException(status_code=401, detail="未提供访问令牌")
    if _management_token_valid(token):
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
    if not isinstance(token, str) or not token:
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
    if not isinstance(token, str) or not token:
        return
    with _auth_store_lock:
        _sessions.pop(token, None)
        _device_auth_tokens.pop(token, None)
