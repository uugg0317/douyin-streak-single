"""An offline restore must not inherit permission to start background jobs.

保护标记只在写入当天有效：它防的是"当天可能重复发送"，跨天后自动失效，
避免一次损坏或恢复让所有好友永久停发、火花全部中断。
"""
import json
from datetime import date, datetime
from pathlib import Path

from .send_day import SEND_TIMEZONE, business_today, timestamp_date

RESTORE_PAUSE_NAME = 'restore-paused.json'
LEDGER_RECOVERY_NAME = 'ledger-recovery-required.json'
_TIME_FIELDS = ('detected_at', 'restored_at', 'created_at', 'paused_at')


def marker_business_day(path) -> date | None:
    """标记写入的业务日期：优先读内容里的时间，读不出时用文件修改时间。"""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            for key in _TIME_FIELDS:
                day = timestamp_date(data.get(key))
                if day is not None:
                    return day
    except (OSError, ValueError):
        pass
    try:
        return datetime.fromtimestamp(path.lstat().st_mtime, SEND_TIMEZONE).date()
    except (OSError, ValueError, OverflowError):
        return None


def _file_business_day(path: Path) -> date | None:
    try:
        return datetime.fromtimestamp(path.lstat().st_mtime, SEND_TIMEZONE).date()
    except (OSError, ValueError, OverflowError):
        return None


def marker_active(path, *, today: date | None = None) -> bool:
    """标记存在，且内容时间或文件写入时间较晚的那天是今天（或都无法判断）时生效。

    从备份按原字节还原的旧标记，文件是恢复当天新写的，因此恢复当天仍然生效。
    """
    path = Path(path)
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    known = [day for day in (marker_business_day(path), _file_business_day(path)) if day is not None]
    return not known or max(known) >= (today or business_today())


def restore_paused(data_dir) -> bool:
    # Content is diagnostic only; malformed files fall back to their file time.
    return marker_active(Path(data_dir) / RESTORE_PAUSE_NAME)
