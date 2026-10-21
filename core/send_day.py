"""发送自然日判定：与调度器的 Asia/Shanghai 口径一致，不读取运行数据。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TZ_NAME = "Asia/Shanghai"
try:
    SEND_TIMEZONE = ZoneInfo(TZ_NAME)
except ZoneInfoNotFoundError:
    # Windows 可能没有 IANA 时区库；当前业务自然日为中国标准时间 UTC+08。
    # 不依赖系统本地时区，也不为此引入新的安装依赖。
    SEND_TIMEZONE = timezone(timedelta(hours=8), name=TZ_NAME)


def business_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(SEND_TIMEZONE)


def timestamp_date(value: object) -> date | None:
    """带偏移时间转业务日期；旧版无偏移时间按业务时区解释，坏值不报错。"""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SEND_TIMEZONE)
        return parsed.astimezone(SEND_TIMEZONE).date()
    except (ValueError, OverflowError):
        return None


def daily_skip_code(entry: dict, *, now: datetime | None = None) -> str | None:
    """仅判断当日保护原因；干跑与显式 force 由调用方决定是否应用。"""
    # New durable intents remain unresolved across midnight until an explicit
    # force operation confirms the user's decision; a date change is no evidence.
    if (entry.get("send_attempt") or {}).get("status") in {"sending", "unconfirmed"}:
        return "unconfirmed_today"
    current = now if now is not None else business_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=SEND_TIMEZONE)
    today = current.astimezone(SEND_TIMEZONE).date()
    unconfirmed = [entry.get("last_unconfirmed_at")]
    if entry.get("last_status") == "unconfirmed":
        # 兼容已有 2.6 的待核对记录，升级前只写过 last_attempt_at。
        unconfirmed.append(entry.get("last_attempt_at"))
    if any(timestamp_date(value) == today for value in unconfirmed):
        return "unconfirmed_today"
    if timestamp_date(entry.get("last_sent_at")) == today:
        return "already_sent_today"
    return None
