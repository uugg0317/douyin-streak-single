"""共享的 Playwright 浏览器启动器。

统一集成：
- 使用已安装 Chromium 的默认用户代理和浏览器行为；
- 中文环境（zh-CN）与 Asia/Shanghai 时区；
- 完善的生命周期管理与异常兜底。
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import sync_playwright

from .config import DATA_DIR, get_valid_state_path

logger = logging.getLogger("douyin-cloud-streak")

# 确保 DISPLAY 环境变量设置（Linux 无图形界面服务器需要 xvfb）
if not os.environ.get("DISPLAY"):
    os.environ["DISPLAY"] = ":99"

_STATE_PATH = DATA_DIR / "state.json"

_COMMON_ARGS = [
    # 非 root 用户运行时启用 Chromium 原生沙箱（不再 --no-sandbox）
    "--disable-dev-shm-usage",
    "--disable-gpu",
]


def open_browser(
    state_path: Path | str | None = None,
    headless: bool = True,
    use_state: bool = True,
    **ctx_kwargs,
):
    """启动 Chromium 并返回 (playwright, browser, context, page)。

    用法::

        with open_browser() as (p, browser, context, page):
            page.goto(url)
            ...

    退出 with 块时自动关闭浏览器和 playwright。
    state_path 默认只认 data/state.json（或 STATE_FILE_PATH 指向的文件）。
    use_state=False：完全不加载登录态（扫码登录提取通行证时必须用，
    否则会带上过期 Cookie，直接进不了扫码页）。
    """
    cleanup_state = {}
    owner = _open_browser(cleanup_state, state_path, headless, use_state, **ctx_kwargs)
    # The cache retains this owner. Its exit can report individual cleanup
    # outcomes without retrying an already exhausted generator contextmanager.
    owner._cleanup_state = cleanup_state
    return owner


def close_browser_resources(p, browser, cleanup_state: dict) -> bool:
    """Close on the owning thread; strict failures keep unfinished steps retryable."""
    if not cleanup_state.get("browser_closed"):
        try:
            if browser is not None:
                browser.close()
            cleanup_state["browser_closed"] = True
        except Exception as error:
            logger.debug("浏览器关闭未完成（%s）", type(error).__name__)
            if cleanup_state.get("strict"):
                # Keep the driver live so a later attempt can retry browser.close.
                return False
    if not cleanup_state.get("driver_stopped"):
        try:
            p.stop()
            cleanup_state["driver_stopped"] = True
        except Exception as error:
            logger.debug("浏览器驱动停止未完成（%s）", type(error).__name__)
    return bool(cleanup_state.get("browser_closed") and cleanup_state.get("driver_stopped"))


@contextmanager
def _open_browser(cleanup_state, state_path, headless, use_state, **ctx_kwargs):
    if not use_state:
        valid_state = None
    elif state_path:
        valid_state = Path(state_path)
    else:
        valid_state = get_valid_state_path()
    state_file = str(valid_state) if valid_state and valid_state.exists() else None

    p = sync_playwright().start()
    browser = None
    try:
        browser = p.chromium.launch(headless=headless, args=_COMMON_ARGS)
        defaults = {
            "viewport": {"width": 1366, "height": 768},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            # 登录态包含完整 Cookie，生产链路必须校验证书，避免 HTTPS 被劫持。
            "ignore_https_errors": False,
        }
        if state_file:
            defaults["storage_state"] = state_file
        defaults.update(ctx_kwargs)

        context = browser.new_context(**defaults)
        page = context.new_page()

        yield p, browser, context, page
    finally:
        close_browser_resources(p, browser, cleanup_state)
