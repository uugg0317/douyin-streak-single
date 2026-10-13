"""应用公共基础：路径、环境变量加载、日志、全局锁与运行状态。"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# 确保在 Windows 控制台下输出 Unicode/Emoji 正常
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from core.config import DATA_DIR
from core.runtime import setup_logging

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
# 服务部署和桌面打包都可以把可写配置放到代码目录之外。未显式指定时，
# 源码运行继续使用项目根目录 .env，保持向后兼容。
_env_file_override = os.environ.get("ENV_FILE_PATH", "").strip()
if _env_file_override:
    ENV_PATH = Path(_env_file_override).expanduser().resolve()
elif getattr(sys, "frozen", False):
    ENV_PATH = Path(sys.executable).resolve().parent / ".env"
else:
    ENV_PATH = BASE_DIR / ".env"
# 版本号单一来源：发版只改这里；/api/health、/api/status 与前端界面均读取此值
VERSION = "2.6"
PID_PATH = DATA_DIR / "server.pid"  # 单实例锁文件：防旧实例 scheduler 残留再发消息
# 允许用环境变量覆盖锁文件路径（测试/本地演练时避免抢占生产实例的锁）
if os.environ.get("INSTANCE_LOCK_PATH", "").strip():
    PID_PATH = Path(os.environ["INSTANCE_LOCK_PATH"].strip())

logger = setup_logging()
run_lock = threading.Lock()
_env_file_lock = threading.Lock()
contacts_fetching = False
harvesting = False
_run_started_at = 0.0  # 当前持锁任务的开始时间戳，供看门狗识别真正卡死


def _mark_run_started() -> None:
    global _run_started_at
    _run_started_at = time.time()


# ── 环境变量 ──────────────────────────────────────────────────────────────


def _load_env() -> None:
    env_path = ENV_PATH
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def _env(name: str) -> str:
    """读取环境变量并 strip。缺失时返回空字符串。"""
    return os.environ.get(name, "").strip()


def _save_env_values_locked(updates: dict) -> None:
    """更新 BASE_DIR/.env 中给定键并原子写回，保留其他行、注释与文件权限。

    用于网页端保存 SMTP 连接设置（主机/端口/账号/授权码）：密钥类配置只落
    .env，不进 config.json、不进 git。
    """
    env_path = ENV_PATH
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if env_path.exists():
        old_text = env_path.read_text(encoding="utf-8")
        nl = "\r\n" if "\r\n" in old_text else "\n"
        old_mode = env_path.stat().st_mode & 0o777
    else:
        old_text, nl, old_mode = "", "\n", 0o600

    pending: dict[str, str] = {}
    for raw_key, raw_value in updates.items():
        key, value = str(raw_key), str(raw_value)
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            raise ValueError(f"非法环境变量名：{key!r}")
        if "\n" in value or "\r" in value:
            raise ValueError(f"环境变量 {key} 不能包含换行符")
        pending[key] = value
    env_sync = dict(pending)
    out_lines: list[str] = []
    for line in old_text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            if k in pending:
                out_lines.append(f"{k}={pending.pop(k)}")
                continue
        out_lines.append(line)
    for k, v in pending.items():
        out_lines.append(f"{k}={v}")

    new_text = nl.join(out_lines)
    if not old_text or old_text.endswith(nl):
        new_text += nl
    fd, tmp_name = tempfile.mkstemp(
        dir=str(env_path.parent), prefix=f".{env_path.name}.tmp."
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(new_text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, env_path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    try:
        env_path.chmod(old_mode)
    except OSError:
        pass
    for k, v in env_sync.items():
        os.environ[k] = v


def save_env_values(updates: dict) -> None:
    """线程安全地更新部署环境文件，避免两个管理请求互相覆盖。"""
    with _env_file_lock:
        _save_env_values_locked(updates)


_load_env()
