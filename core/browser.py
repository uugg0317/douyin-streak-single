"""共享的 Playwright 浏览器启动器。

统一集成：
- 反爬对抗参数与真实 Chrome 指纹；
- playwright_stealth 自动注入（若安装）；
- 中文环境（zh-CN）与 Asia/Shanghai 时区模拟；
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

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_COMMON_ARGS = [
    # 非 root 用户运行时启用 Chromium 原生沙箱（不再 --no-sandbox）
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
]


def _apply_stealth(page) -> None:
    """反自动化指纹处理。

    注意：旧版 playwright_stealth 的 stealth_sync 注入会与抖音自身的反爬脚本
    冲突（触发 utils/opts is not defined 等 17 个 JS 错误，导致聊天列表不渲染），
    因此【禁止】回退到旧版 stealth_sync。仅当安装了提供 Stealth 类的新版时才启用，
    否则不注入——配合自定义 UA / locale / --disable-blink-features 已足够。
    """
    try:
        from playwright_stealth import Stealth  # noqa: F401
        Stealth().apply_stealth_sync(page)
    except Exception:
        # 新版不可用就完全不注入，绝不使用会导致抖音页面崩溃的旧 stealth_sync
        pass


@contextmanager
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
            "user_agent": _CHROME_UA,
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
        _apply_stealth(page)

        yield p, browser, context, page
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        try:
            p.stop()
        except Exception:
            pass
