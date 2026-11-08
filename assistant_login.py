"""QR login for the assistant account.

The capture profile is data/assistant-session/qr-login-profile. Cookies are
saved to data/assistant_state.json. The main account state file, QR profile,
and streak browser are not touched.
"""

from __future__ import annotations

import logging
import threading

from core.config import atomic_write_bytes
from desktop.local_cookie_helper import validate_state

logger = logging.getLogger("douyin-cloud-streak")

_manager = None
_manager_lock = threading.Lock()


def assistant_qr_active() -> bool:
    return bool(_manager is not None and _manager.is_active())


def _persist(raw: bytes, _job: dict) -> bool:
    from assistant_browser import profile_lock
    from assistant_inbox import state_path

    validate_state(raw)
    target = state_path()
    if target.name == "state.json":
        raise RuntimeError("refusing to write the main account state file")
    if not profile_lock.acquire(blocking=False):
        return False
    try:
        atomic_write_bytes(target, raw)
    finally:
        profile_lock.release()
    logger.info("小号扫码登录成功，已写入独立登录态（%s 字节）", len(raw))
    return True


def get_manager():
    global _manager
    with _manager_lock:
        if _manager is None:
            from core.qr_login import QrLoginManager
            from assistant_inbox import qr_profile_parent
            _manager = QrLoginManager(
                persist_state=_persist,
                profile_data_dir=qr_profile_parent(),
            )
        return _manager


def shutdown() -> None:
    if _manager is not None:
        _manager.shutdown()
