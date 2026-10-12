"""运行状态与日志。运行结果持久化到 data/runtime.json，日志同时写文件与内存环形缓冲。"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler

from .config import DATA_DIR, atomic_write_text

RUNTIME_PATH = DATA_DIR / "runtime.json"
LOG_DIR = DATA_DIR / "logs"

# 单文件 5MB × (1 + 3 个轮转备份)，日志总量上限约 20MB。
# 历史实现用 FileHandler 直接追加、从不轮转：服务 7×24 常驻，
# app.log 会一直涨到把服务器磁盘写满（磁盘满 → 服务写不进状态文件 → 静默停摆）。
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

_lock = threading.RLock()
_ring: deque[str] = deque(maxlen=600)

# load_runtime 的进程内缓存：前端每 10 秒拉一次 /api/status，旧实现每次都
# read_text + json.loads（runtime.json 累积 30 条完整名单后可达几十 KB）。
# 用文件 mtime_ns 作失效依据：文件没变就直接返回深拷贝，变了才重新读盘。
_rt_cache: dict = {"mtime": None, "data": None}


def _default() -> dict:
    return {
        "session_status": "unknown", "session_checked_at": None,
        "running": False, "last_run": None, "history": [],
        # 熔断用：连续失败轮数、自动暂停截止时间戳（0 表示未暂停）
        "consecutive_failures": 0,
        "auto_paused_until": 0,
        "send_progress": None,
    }


def load_runtime() -> dict:
    try:
        mtime = RUNTIME_PATH.stat().st_mtime_ns
    except OSError:
        return _default()
    with _lock:
        if _rt_cache["mtime"] == mtime and _rt_cache["data"] is not None:
            return copy.deepcopy(_rt_cache["data"])
        rt = _default()
        try:
            data = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                rt.update(data)
        except Exception:
            pass
        _rt_cache["mtime"] = mtime
        _rt_cache["data"] = rt
        return copy.deepcopy(rt)


def _save(rt: dict) -> None:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(RUNTIME_PATH, json.dumps(rt, ensure_ascii=False, indent=2))
        # 写完立即刷新缓存，避免下一次 load 因 mtime 抖动多读一次盘
        try:
            _rt_cache["mtime"] = RUNTIME_PATH.stat().st_mtime_ns
        except OSError:
            _rt_cache["mtime"] = None
        _rt_cache["data"] = copy.deepcopy(rt)


def set_running(value: bool) -> None:
    with _lock:
        rt = load_runtime()
        rt["running"] = bool(value)
        _save(rt)


def _summarize_run(result: dict) -> dict:
    """历史只存计数摘要，不存完整好友名单（明细在日志里）。

    旧实现把含 ok/failed/skipped 名单的完整 result 存 30 份，runtime.json 累积到
    几十 KB；而 /api/status 只回 history_count、前端不读 history 内容——纯冗余，
    还被每 10 秒一次的轮询反复读盘。last_run 仍保留完整 result 供概览页展示。
    """
    return {
        "at": result.get("at"),
        "dry_run": bool(result.get("dry_run", False)),
        "ok_count": len(result.get("ok") or []),
        "failed_count": len(result.get("failed") or []),
        "skipped_count": len(result.get("skipped") or []),
        "deferred_count": len(result.get("deferred") or []),
        "logged_out": bool(result.get("logged_out", False)),
        "rate_limited": bool(result.get("rate_limited", False)),
    }


def record_run(result: dict) -> None:
    with _lock:
        rt = load_runtime()
        rt["last_run"] = result
        history = rt.get("history", [])
        history.insert(0, _summarize_run(result))
        rt["history"] = history[:30]

        # Dry runs and empty runs cannot confirm the current credential. Only a
        # real successful send or an explicit logged-out result refreshes this
        # timestamp; other task failures retain the last confirmed check time.
        if not result.get("dry_run"):
            if result.get("logged_out"):
                rt["session_status"] = "expired"
                rt["session_checked_at"] = time.time()
            elif result.get("ok"):
                rt["session_status"] = "partial" if result.get("failed") or result.get("deferred") else "ok"
                rt["session_checked_at"] = time.time()
            elif result.get("failed") or result.get("deferred"):
                rt["session_status"] = "failed"

        # ── 连续失败计数（熔断用）──
        # 待补发不等于失败；只有明确失败且一条未成功，或登录态掉线才触发。
        if not result.get("dry_run"):
            hard_fail = bool(result.get("failed")) and not result.get("ok")
            if result.get("logged_out") or hard_fail:
                rt["consecutive_failures"] = int(rt.get("consecutive_failures", 0)) + 1
            else:
                rt["consecutive_failures"] = 0
        _save(rt)


def record_contacts(data: dict) -> None:
    with _lock:
        rt = load_runtime()
        rt["contacts"] = data.get("names", [])
        rt["contacts_at"] = data.get("at")
        rt["contacts_error"] = data.get("error")
        _save(rt)


def update_runtime(**fields) -> None:
    with _lock:
        rt = load_runtime()
        rt.update(fields)
        _save(rt)


def record_harvest(harvest_last: dict | None) -> None:
    """持久化最近一次 creator 采集摘要，服务重启后不丢（台账数据本身持久化不受影响）。"""
    with _lock:
        rt = load_runtime()
        if harvest_last is None:
            rt.pop("harvest_last", None)
        else:
            rt["harvest_last"] = harvest_last
        _save(rt)


def load_harvest_last() -> dict | None:
    """读取持久化的采集摘要；无记录返回 None。"""
    return load_runtime().get("harvest_last")


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _ring.append(self.format(record))
        except Exception:
            pass


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("douyin-cloud-streak")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(
        LOG_DIR / "app.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    rh = RingHandler()
    rh.setFormatter(fmt)
    logger.addHandler(rh)
    return logger


def recent_logs(n: int = 300) -> list[str]:
    return list(_ring)[-n:]
