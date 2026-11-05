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


OPEN_INTENT_STATUSES = frozenset({"sending", "unconfirmed", "rejected"})


def business_today(now: datetime | None = None) -> date:
    current = now if now is not None else business_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=SEND_TIMEZONE)
    return current.astimezone(SEND_TIMEZONE).date()


def open_intent(entry: dict) -> tuple[str, date | None] | None:
    """未结束的发送意图及其业务日期；没有则返回 None。日期读不出时为 None。"""
    attempt = entry.get("send_attempt") or {}
    attempt_status = attempt.get("status") if attempt.get("status") in OPEN_INTENT_STATUSES else None
    legacy_status = entry.get("last_status") if entry.get("last_status") in OPEN_INTENT_STATUSES else None
    if attempt_status is None and legacy_status is None:
        return None
    # 新旧字段任一记为明确拒绝，就按拒绝处理，不能被另一处的“待核对”降级。
    status = "rejected" if "rejected" in {attempt_status, legacy_status} else (attempt_status or legacy_status)
    if attempt_status is not None:
        try:
            day = date.fromisoformat(str(attempt.get("business_date")))
        except ValueError:
            day = timestamp_date(attempt.get("started_at"))
    else:
        # 兼容 2.6 及更早记录：只写过 last_unconfirmed_at / last_attempt_at。
        day = timestamp_date(entry.get("last_unconfirmed_at")) or timestamp_date(entry.get("last_attempt_at"))
    return status, day


def daily_skip_code(entry: dict, *, now: datetime | None = None) -> str | None:
    """仅判断当日保护原因；干跑与显式 force 由调用方决定是否应用。

    防重复保护只管当天：昨天结果不明不能挡住今天这条，否则火花会断。
    同一天内的待核对、明确拒绝和已发送仍然阻止自动重发。日期读不出的记录无法证明已过期，
    保守按当天处理，由用户在界面上点“核对结果”解除。
    """
    today = business_today(now)
    intent = open_intent(entry)
    if intent is not None and intent[1] in {None, today}:
        return "rejected_unreviewed" if intent[0] == "rejected" else "unconfirmed_today"
    if timestamp_date(entry.get("last_unconfirmed_at")) == today:
        return "unconfirmed_today"
    if timestamp_date(entry.get("last_sent_at")) == today:
        return "already_sent_today"
    return None
