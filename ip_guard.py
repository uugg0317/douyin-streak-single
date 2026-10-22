"""IP 防护：访客 IP 识别、失败锁定黑名单、永久白名单、限速。"""

from __future__ import annotations

import ipaddress
import json
import re
import time
from datetime import datetime

from fastapi import Request

from app_common import _env, logger
from core.config import DATA_DIR, atomic_write_text

LOCAL_IPS = {"127.0.0.1", "::1", "localhost"}

# 白名单 IP 从环境变量读取（逗号分隔），未配置时为空、免登录自动失效
WHITELIST_IPS: set[str] = {
    ip.strip() for ip in _env("WHITELIST_IPS").split(",") if ip.strip()
}

LOGIN_FAIL_LIMIT = 10  # 同一 IP 连续认证失败达到此次数即拉黑
LOCK_TTL_SECONDS = int(_env("LOCK_TTL_SECONDS") or "1800")  # 锁定自动过期，避免永久拉黑

# 只有这些认证端点的 401 计入连续失败、成功才清零（防借 /api/health 刷零计数）
AUTH_FAIL_PATHS = (
    "/api/auth/login",
    "/api/auth/url-token-login",
)


def _safe_log_field(value, limit: int = 120) -> str:
    """净化写入日志的外部输入：去控制字符（防日志注入/嫁祸拉黑）并截断。"""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "?", str(value))
    return cleaned[:limit]


def _client_ip(request: Request) -> str:
    """获取真实访客 IP：仅在直连对端是本机反代时信任 X-Real-IP，忽略 X-Forwarded-For。"""
    direct = request.client.host if request.client else "unknown"
    if direct in LOCAL_IPS:
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            try:
                return str(ipaddress.ip_address(real_ip))
            except ValueError:
                logger.warning("忽略非法的 X-Real-IP 头：%s", _safe_log_field(real_ip))
    return direct


def _lock_expire_at(entry: dict) -> float:
    """黑名单条目的解锁时间戳；兼容旧格式，无法解析时视为已过期（避免永久锁定）。"""
    locked_until = entry.get("locked_until")
    if isinstance(locked_until, (int, float)) and locked_until > 0:
        return float(locked_until)
    try:
        locked_at = datetime.strptime(str(entry.get("locked_at", "")), "%Y-%m-%d %H:%M:%S")
        return locked_at.timestamp() + LOCK_TTL_SECONDS
    except Exception:
        return 0.0


BLACKLIST_PATH = DATA_DIR / "ip_blacklist.json"
WHITELIST_PATH = DATA_DIR / "ip_whitelist.json"
_fail_counts: dict = {}   # ip -> 连续失败次数
_blacklist: dict = {}     # ip -> {"locked_at": ..., "fails": ...}
_whitelist: set = set()   # 永久信任的白名单 IP


def _load_blacklist() -> None:
    global _blacklist
    try:
        _blacklist = json.loads(BLACKLIST_PATH.read_text(encoding="utf-8"))
    except Exception:
        _blacklist = {}


def _save_blacklist() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            BLACKLIST_PATH, json.dumps(_blacklist, ensure_ascii=False, indent=2)
        )
    except Exception:
        logger.exception("写入 IP 黑名单失败")


def _load_whitelist() -> None:
    """加载永久白名单（JSON 列表或字典均可），白名单内 IP 永不锁定、失败不计数。"""
    global _whitelist
    try:
        data = json.loads(WHITELIST_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            _whitelist = {str(x).strip() for x in data if str(x).strip()}
        elif isinstance(data, dict):
            _whitelist = {str(k).strip() for k in data.keys() if str(k).strip()}
        else:
            _whitelist = set()
    except Exception:
        _whitelist = set()


_load_blacklist()
_load_whitelist()

# ── 限速（进程内内存滑动窗口）────────────────────────────────────────────────
RATE_GENERAL = {"limit": 120, "window": 60}   # 每 IP 每分钟总请求上限
RATE_SENSITIVE = {"limit": 5, "window": 60}   # 敏感端点每 IP 每分钟上限
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
_MAX_RATE_KEYS = 20000  # _hits 键数上限，超出时淘汰最久未活动的键（防内存无界增长）


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


# 被拉黑 IP 用浏览器直接访问时展示的友好中文锁定页
LOCK_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>访问已锁定</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
         min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: linear-gradient(135deg, #1e293b 0%, #0f172a 60%, #111827 100%); padding: 20px; }
  .card { width: 100%; max-width: 380px; background: #fff; border-radius: 16px; padding: 38px 30px;
          text-align: center; box-shadow: 0 24px 70px rgba(0,0,0,.45); }
  .icon { font-size: 46px; margin-bottom: 14px; }
  h2 { color: #0f172a; font-size: 20px; margin-bottom: 12px; }
  p { color: #475569; font-size: 14px; line-height: 1.7; }
  .muted { color: #94a3b8; font-size: 12.5px; margin-top: 14px; line-height: 1.7; }
</style>
</head>
<body>
  <div class="card">
    <div class="icon">🔒</div>
    <h2>访问已被锁定</h2>
    <p>当前网络因连续 <strong>__LIMIT__</strong> 次密钥输入错误，已被系统自动拉黑。</p>
    <p class="muted">请联系管理员</p>
  </div>
</body>
</html>"""
