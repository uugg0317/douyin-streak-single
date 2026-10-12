"""单实例锁：启动时自检，防旧实例 scheduler 残留重复发送。"""

from __future__ import annotations

import os

from app_common import PID_PATH, logger
from core.config import DATA_DIR, atomic_write_text


def _pid_alive(pid: int) -> bool:
    """检查 PID 对应的进程是否仍在运行（跨平台）。"""
    if os.name == "nt":  # Windows：os.kill(pid,0) 报 WinError 87，改用 tasklist
        try:
            import subprocess
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in r.stdout and "python" in r.stdout.lower()
        except Exception:
            return False
    # POSIX
    try:
        os.kill(pid, 0)  # signal 0 = 探测进程是否存在，不实际发信号
    except ProcessLookupError:
        return False  # 进程不存在
    except PermissionError:
        return True   # 进程存在但无权限发信号
    except OSError:
        return False
    return True


def _acquire_instance_lock() -> None:
    """若已有活跃实例运行则拒绝启动，避免多实例重复触发发送。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if PID_PATH.exists():
        try:
            old_pid = int(PID_PATH.read_text().strip())
        except (ValueError, OSError):
            old_pid = None
        if old_pid and _pid_alive(old_pid):
            logger.error(
                "检测到已有 sparkkeeper 实例在运行（PID %s），拒绝启动。"
                "请先停止旧实例再重试，避免多实例重复发送。",
                old_pid,
            )
            raise SystemExit(f"已有实例在运行（PID {old_pid}），请先停止旧实例")
        else:
            logger.info("发现旧 PID 文件但进程已退出（PID %s），可安全接管", old_pid)
    atomic_write_text(PID_PATH, str(os.getpid()))
