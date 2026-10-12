"""Recognize dead browser sessions without exposing driver error details."""
from __future__ import annotations


class BrowserSessionUnavailable(RuntimeError):
    code = "browser_session_closed"

    def __init__(self):
        super().__init__("自动化浏览器会话已关闭，本轮已停止；请重新运行模拟演练检查。")



class MessageDeliveryUnconfirmed(RuntimeError):
    code = "delivery_unconfirmed"

    def __init__(self):
        super().__init__("已尝试发送，但结果尚未确认；请在抖音核对后再手动重试，本次不会自动补发该好友。")


def raise_if_browser_unavailable(error: BaseException) -> None:
    """Re-raise only unrecoverable Playwright lifecycle errors.

    Normal missing elements, navigation timeouts and detached frames must keep
    their original retry/fallback behavior. No message is replayed here.
    """
    current = error
    visited = set()
    for _ in range(5):
        if not isinstance(current, BaseException) or id(current) in visited:
            return
        visited.add(id(current))
        if isinstance(current, BrowserSessionUnavailable):
            raise current
        text = str(current).lower()
        fatal = type(current).__name__ == "TargetClosedError" or any(marker in text for marker in (
            "event loop is closed",
            "target page, context or browser has been closed",
            "browser has been closed",
            "connection closed while reading from the driver",
            "connection closed while reading from driver",
            "playwright connection closed",
        ))
        if fatal:
            raise BrowserSessionUnavailable() from error
        current = current.__cause__ or current.__context__
