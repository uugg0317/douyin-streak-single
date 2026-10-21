"""卡死看门狗：同一次持锁任务长期没有进展才告警并退出。"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback

import app_common
from app_common import logger
from email_service import _render_template, _send_email, load_email_config

# 同一次持锁任务连续没有可观察进展达到阈值才判定卡死。
WATCHDOG_STUCK_SECONDS = max(60, int(os.environ.get("WATCHDOG_STUCK_SECONDS", "900")))
_WATCHDOG_CHECK_INTERVAL = 20
_watchdog_started = False


def _watchdog_capture() -> str:
    """无需外部 py-spy，直接抓取进程内所有 Python 线程栈。"""
    try:
        names = {thread.ident: thread.name for thread in threading.enumerate()}
        chunks = []
        for ident, frame in sys._current_frames().items():
            chunks.append(
                f"\n--- thread {names.get(ident, '?')} ({ident}) ---\n"
                + "".join(traceback.format_stack(frame))
            )
        stack = "".join(chunks)
        logger.critical("看门狗：卡死时刻线程栈如下\n%s", stack)
        return stack
    except Exception as e:
        logger.warning("看门狗：抓取线程栈失败：%s", e)
        return ""


def _watchdog_notify(age: float, stack: str) -> None:
    """看门狗触发时按页面配置发送告警邮件（best-effort：失败只记日志）。"""
    import socket
    from datetime import datetime

    try:
        cfg = load_email_config()
        host = socket.gethostname()
        try:
            host_ip = socket.gethostbyname(host)
        except Exception:
            host_ip = "?"
        values = {
            "host": host,
            "host_ip": host_ip,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "age": f"{age:.0f}",
            "threshold": str(WATCHDOG_STUCK_SECONDS),
            "stack": stack or "(未抓取到)",
        }
        subject = _render_template(cfg["subject"], values)
        body = _render_template(cfg["body"], values)
        _send_email(cfg, subject, body)
        logger.critical("看门狗：告警邮件已发送至 %s", cfg.get("mail_to"))
    except Exception as e:
        logger.warning("看门狗：告警邮件发送失败：%s", e)


def _watchdog_check_once() -> bool:
    snapshot = app_common.run_lock.stalled(WATCHDOG_STUCK_SECONDS)
    if snapshot is None:
        return False
    age = time.monotonic() - snapshot.progressed_at
    logger.critical(
        "看门狗：当前任务连续 %.0f 秒没有进展（阈值 %s 秒）；抓栈并通知后再次确认",
        age, WATCHDOG_STUCK_SECONDS,
    )
    stack = _watchdog_capture()
    _watchdog_notify(age, stack)
    time.sleep(2)
    # Notification can take time. Do not kill a task that progressed, finished,
    # or was replaced while diagnostics were being collected.
    return app_common.run_lock.if_still_stalled(
        snapshot, WATCHDOG_STUCK_SECONDS, lambda: os._exit(78)
    )


def _watchdog_loop() -> None:
    while True:
        time.sleep(_WATCHDOG_CHECK_INTERVAL)
        try:
            _watchdog_check_once()
        except Exception:
            logger.exception("看门狗检查异常")


def _start_watchdog() -> None:
    global _watchdog_started
    if _watchdog_started:
        return
    _watchdog_started = True
    threading.Thread(target=_watchdog_loop, name="watchdog", daemon=True).start()
    logger.info("看门狗已启动：同一任务连续 %s 秒没有进展才判定卡死", WATCHDOG_STUCK_SECONDS)
