"""Playwright 自动化：在抖音网页版私信页面给指定好友发送消息。

发送逻辑参考 douyin-cloud-streak（MIT），要点：
- 点击联系人后校验右侧会话确实切换（防止限流时错发给上一个人）；
- 列表点击失败时用搜索框兜底；
- 检测"操作频繁 / 安全验证"等提示，命中即停本轮；
- 发送前绑定目标会话及输入框，发送后核对该会话中的新己方消息与失败提示。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .browser import open_browser, close_browser_resources, _COMMON_ARGS
from . import ledger
from playwright.sync_api import sync_playwright
from .config import DATA_DIR, get_valid_state_path, load_config
from .guard import detect_rate_limit
from .msg_builder import build_message
from .runtime import load_runtime, update_runtime
from .sender import creator_channel
from .browser_errors import (
    BrowserSessionUnavailable, MessageDeliveryUnconfirmed, raise_if_browser_unavailable,
)
from .send_results import completed_count, resolved_contact_names, contact_entry_ids
from .run_progress import progress
from .send_day import business_now, daily_skip_code

logger = logging.getLogger("douyin-cloud-streak")

STATE_PATH = DATA_DIR / "state.json"
SCREENSHOT_PATH = DATA_DIR / "last_error.png"
CHAT_URL = "https://www.douyin.com/chat"
LOGIN_TEXTS = ["扫码登录", "验证码登录", "登录后查看", "登录后即可"]


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    """安全读取整型配置：非法值回退到 default，0 是合法值。

    不能用 `cfg.get(key, default) or default` 这种写法：配置里合法的 0
    （例如 max_friends_per_run=0「不限制」、jitter_minutes=0「不抖动」）
    是 falsy，会被 or 悄悄换成 default，让「0」这个档位永远无法生效。
    """
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        logger.warning("配置项 %s=%r 不是整数，已按默认值 %s 处理", key, cfg.get(key), default)
        return default

# ── 专用 Playwright 工作线程 ──────────────────────────────────────────────
# 所有 Playwright Sync API 操作（预启动 / 发送 / 采集联系人）都在这一个
# 单线程执行器里串行运行，解决两个问题：
#   1. APScheduler / uvicorn 线程里跑着 asyncio 事件循环，直接调用
#      Playwright Sync API 会报 "Sync API inside the asyncio loop"；
#   2. Playwright 对象绑定创建它的线程，预启动与发送必须在同一线程，
#      否则无法复用预启动的浏览器。
def _pw_thread_initializer() -> None:
    # 显式给工作线程绑定一个「新建但未运行」的事件循环：
    # get_event_loop() 会返回它，is_running() 为 False，Playwright Sync API
    # 的 asyncio 冲突检查即可通过；Playwright 随后在自己的 greenlet 里运行
    # 专属循环，与此外壳循环互不干扰。
    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
    except Exception:
        logger.debug('初始化线程事件循环失败，继续使用现有线程环境')


_pw_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="pw-worker",
    initializer=_pw_thread_initializer,
)


SEND_HARD_TIMEOUT_SECONDS = max(1, _cfg_int(os.environ, "SEND_HARD_TIMEOUT_SECONDS", 600))
_send_context = threading.local()
_active_send_stop: threading.Event | None = None
_send_control_lock = threading.Lock()


class SendStopped(RuntimeError):
    """Cooperative stop; this does not claim that another thread was killed."""


class SendIntentWriteFailed(RuntimeError):
    """No durable intent means the Enter action must never be reached."""


def send_is_stopping() -> bool:
    with _send_control_lock:
        return bool(_active_send_stop is not None and _active_send_stop.is_set())


def request_send_stop(reason: str = "发送等待超时，正在停止；如一直未结束，请退出程序后重启并核对发送记录。") -> bool:
    with _send_control_lock:
        event = _active_send_stop
        if event is None:
            return False
        event.set()
        try:
            update_runtime(running=True, stopping=True, stop_reason=reason)
        except Exception:
            logger.exception("停止请求已生效，但停止状态写入失败；运行保护仍保留")
    logger.warning("%s", reason)
    return True


def _check_send_stop() -> None:
    event = getattr(_send_context, "stop_event", None)
    if event is not None and event.is_set():
        raise SendStopped("已请求停止，本轮不再提交后续消息")


def _persist_current_intent() -> None:
    _check_send_stop()
    intent = getattr(_send_context, "intent", None)
    if intent is not None and not intent.get("attempt_id"):
        try:
            intent["attempt_id"] = ledger.begin_send_attempt(intent["entry_id"], force=intent.get("force", False),
                                                            expected_display_name=intent.get('display_name'))
        except Exception as error:
            raise SendIntentWriteFailed("发送意图未能保存，消息未提交") from error
    _check_send_stop()


def _run_with_send_control(func, event, args, kwargs):
    _send_context.stop_event = event
    try:
        return func(*args, **kwargs)
    finally:
        _send_context.stop_event = None


def _run_in_pw_thread(func, *args, **kwargs):
    """A send timeout requests stop but waits for the same worker to really exit."""
    if func is not _run_send_impl:
        return _pw_executor.submit(func, *args, **kwargs).result()
    global _active_send_stop
    event = threading.Event()
    with _send_control_lock:
        if _active_send_stop is not None:
            raise RuntimeError("上一发送线程尚未结束")
        _active_send_stop = event
    try:
        future = _pw_executor.submit(_run_with_send_control, func, event, args, kwargs)
        timed_out = False
        try:
            result = future.result(timeout=SEND_HARD_TIMEOUT_SECONDS)
        except FutureTimeout:
            if future.done():
                return future.result()  # The operation itself raised TimeoutError.
            timed_out = True
            request_send_stop()
            # Never cancel or close Playwright from this coordinating thread.
            # Keeping this waiter alive also keeps tasks_service's run lock held.
            result = future.result()
        if isinstance(result, dict) and event.is_set():
            result["stopped"] = True
            result["timed_out"] = timed_out
        return result
    finally:
        with _send_control_lock:
            if _active_send_stop is event:
                _active_send_stop = None


# 浏览器预启动实例（全局，由 scheduler 在发送前触发）
_prewarmed_browser = None  # {"p", "browser", "context", "page", "ready", "created_at"}
_prewarm_lock = threading.Lock()  # 防止并发预启动


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _screenshot(page) -> None:
    try:
        page.screenshot(path=str(SCREENSHOT_PATH), timeout=5000)
        logger.info("已保存页面截图: %s", SCREENSHOT_PATH)
    except Exception:
        logger.debug('辅助截图失败，继续保留本轮处理结果')


# ── 登录检测 ──────────────────────────────────────────────────────────────


def check_login(page) -> tuple[bool, str]:
    """返回 (是否已登录, 说明)。宁可误报掉线，也不要带着过期登录态硬跑。"""
    url = page.url
    if "login" in url.lower() or "passport" in url.lower():
        return False, f"页面已跳转到登录页（{url}）"

    try:
        qr = page.locator("#animate_qrcode_container")
        if qr.count() and qr.first.is_visible():
            return False, "页面出现扫码登录二维码，登录态已过期"
    except Exception as error:
        raise_if_browser_unavailable(error)
        pass

    for text in LOGIN_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(min(loc.count(), 3)):
                if loc.nth(i).is_visible():
                    return False, f"页面出现登录提示「{text}」"
        except Exception as error:
            raise_if_browser_unavailable(error)
            continue

    cookies = page.context.cookies()
    if not any(c["name"].startswith("sessionid") for c in cookies):
        return False, "未检测到 sessionid Cookie"
    return True, "ok"


# ── 登录态体检（Cookie 到期扫描 + 一次性浏览器验证）──────────────────────

# 关键 Cookie 剩余有效期低于该天数即判为「临期」并邮件预警
SESSION_WARN_DAYS = max(1, int(os.environ.get("SESSION_WARN_DAYS", "7")))


def scan_state_cookies(state_path=None) -> dict:
    """静态解析 state.json 中 sessionid* Cookie 的到期时间（不启动浏览器）。

    expires=-1/0 为会话型 Cookie，无法预知到期，跳过；
    返回字段：has_state / has_session / min_days / expiring_soon / expired / details。
    """
    p = state_path or get_valid_state_path()
    info = {
        "has_state": False, "has_session": False, "min_days": None,
        "expiring_soon": False, "expired": False, "details": "",
    }
    if not p:
        return info
    try:
        cookies = json.loads(Path(p).read_text(encoding="utf-8")).get("cookies") or []
    except Exception:
        return info
    info["has_state"] = True
    now = time.time()
    days = []
    for c in cookies:
        name = c.get("name", "")
        if not name.startswith("sessionid"):
            continue
        info["has_session"] = True
        try:
            exp = float(c.get("expires", -1) or -1)
        except (TypeError, ValueError):
            continue
        if exp > 0:
            days.append((name, (exp - now) / 86400))
    if days:
        name, d = min(days, key=lambda x: x[1])
        info["min_days"] = round(d, 1)
        info["details"] = f"{name} 剩余约 {info['min_days']} 天到期"
        info["expiring_soon"] = d < SESSION_WARN_DAYS
        info["expired"] = d <= 0
    return info


def check_session_health() -> dict:
    """公共入口（任意线程可调用）：在 pw 工作线程启动一次性浏览器验证登录态，用完即关。"""
    return _run_in_pw_thread(_check_session_health_impl)


def _check_session_health_impl() -> dict:
    valid = get_valid_state_path()
    cookie_info = scan_state_cookies(valid)
    out = {"logged_in": False, "reason": "", "cookie": cookie_info, "infra_error": False}
    if not valid:
        out["reason"] = "尚未上传登录态 state.json"
        return out
    p = browser = context = page = None
    try:
        p = sync_playwright().start()
        browser = p.chromium.launch(headless=True, args=_COMMON_ARGS)
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            ignore_https_errors=False,
            storage_state=str(valid),
        )
        page = context.new_page()
        page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            time.sleep(2)
        logged, why = check_login(page)
        out["logged_in"] = logged
        out["reason"] = why
    except Exception as e:
        # 浏览器启动失败等基础设施问题与登录态无关：标记后由调用方决定，不发掉线预警
        out["infra_error"] = True
        out["reason"] = f"体检异常：{e}"
    finally:
        for closer in (lambda: browser.close() if browser else None,
                       lambda: p.stop() if p else None):
            try:
                closer()
            except Exception:
                logger.debug('体检浏览器资源清理失败，继续收尾')
    return out


# ── 浏览器预启动 ──────────────────────────────────────────────────────────

def prewarm_browser() -> bool:
    """公共入口（可在任意线程调用）：把预启动提交到专用 Playwright 工作线程。

    fire-and-forget：不阻塞调用线程（scheduler / uvicorn），任务在单线程
    执行器里排队；随后的 run_send 同样提交到该执行器，因此会天然等待
    预启动完成后再执行，并直接复用同一个浏览器实例。
    """
    future = _pw_executor.submit(_prewarm_browser_impl)
    # 不阻塞等待，但捕获异常避免静默
    future.add_done_callback(lambda f: f.exception())
    return True


def _prewarm_browser_impl() -> bool:
    """在专用 Playwright 工作线程中预启动浏览器并加载聊天页面。"""
    global _prewarmed_browser

    # 单线程执行器内本就串行，这里用非阻塞锁再兜底防止重复预启动
    if not _prewarm_lock.acquire(blocking=False):
        logger.info("预启动正在进行中，跳过重复调用")
        return False

    try:
        if (_prewarmed_browser and _prewarmed_browser.get("cleanup_pending")
                and not _close_prewarmed(strict=True)):
            logger.warning("旧浏览器清理未完成，暂不预启动新会话")
            return False
        if _prewarmed_browser and _prewarmed_browser.get("ready"):
            if _get_prewarmed_page() is not None:
                logger.info("已有预启动页面通过复核，跳过重复启动")
                return True
            logger.info("原预启动页面无法复用，重新启动")

        p = browser = context = page = None
        try:
            logger.info("开始预启动浏览器...")
            valid_state = get_valid_state_path()
            state_file = str(valid_state) if valid_state and valid_state.exists() else None

            p = sync_playwright().start()
            browser = p.chromium.launch(headless=True, args=_COMMON_ARGS)
            defaults = {
                "viewport": {"width": 1366, "height": 768},
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
                "ignore_https_errors": False,
            }
            if state_file:
                defaults["storage_state"] = state_file
            context = browser.new_context(**defaults)
            page = context.new_page()

            # 打开聊天页面
            logger.info("预启动：打开抖音聊天页面...")
            page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                logger.debug('预热页面等待失败，继续后续就绪检查')
            try:
                page.wait_for_selector("input", timeout=15000)
            except Exception:
                time.sleep(2)

            # 验证登录态
            logged, why = check_login(page)
            if not logged:
                logger.warning("预启动浏览器登录态无效：%s", why)
                try:
                    browser.close()
                    p.stop()
                except Exception:
                    logger.debug('失效预热资源清理失败，返回预热失败')
                return False

            # 充分等待联系人列表出现（带 reload 重试），确保发送时页面完全就绪
            list_ready = False
            for attempt in range(3):
                try:
                    page.wait_for_selector(".conversationConversationItemtitle", timeout=30000)
                    list_ready = True
                    break
                except Exception:
                    logger.info("预启动：联系人列表未出现，刷新重试（第%s次）", attempt + 1)
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_selector("input", timeout=10000)
                    except Exception:
                        time.sleep(2)
            if list_ready:
                logger.info("预启动：联系人列表已加载，页面完全就绪")
                time.sleep(0.5)
            else:
                logger.warning("预启动：联系人列表等待超时，发送时将继续等待")

            _prewarmed_browser = {
                "p": p, "browser": browser, "context": context, "page": page,
                "ready": True, "created_at": time.time()
            }
            logger.info("浏览器预启动成功，已加载聊天页面并验证登录态")
            return True

        except Exception as e:
            logger.error("浏览器预启动失败：%s", e)
            try:
                if browser:
                    browser.close()
                if p:
                    p.stop()
            except Exception:
                logger.debug('预热异常后的资源清理失败，返回原失败结果')
            return False
    finally:
        try:
            _prewarm_lock.release()
        except RuntimeError:
            pass


def _close_prewarmed(*, strict: bool = False) -> bool:
    """关闭预启动的浏览器实例（必须在专用 Playwright 工作线程内调用）。"""
    global _prewarmed_browser
    if not _prewarmed_browser:
        return True
    owned = _prewarmed_browser
    strict = strict or bool(owned.get("cleanup_pending"))
    owner = owned.get("context_manager")
    state = (getattr(owner, "_cleanup_state", None) if owner is not None
             else owned.setdefault("cleanup_state", {}) if strict else {})
    if strict:
        # Retain failed resources, but never make them available for reuse.
        owned.update(ready=False, cleanup_pending=True)
        if not isinstance(state, dict):
            logger.warning("无法确认浏览器容器的清理状态，保留原凭证与资源引用")
            return False
        state["strict"] = True
    else:
        # Preserve the usual best-effort teardown; only credential changes
        # require positive cleanup evidence and retryable resource ownership.
        _prewarmed_browser = None
    try:
        if owner is not None and not owned.get("context_manager_exited"):
            # Generator owners may exit only once, even when cleanup failed.
            owned["context_manager_exited"] = True
            owner.__exit__(None, None, None)
            closed = bool(isinstance(state, dict) and state.get("browser_closed") and state.get("driver_stopped"))
        else:
            closed = close_browser_resources(owned["p"], owned["browser"], state)
    except Exception as error:
        logger.warning("浏览器清理未完成（%s）", type(error).__name__)
        return False
    if strict and not closed:
        logger.warning("旧浏览器清理未完成，保留原凭证；可稍后重试或退出程序后重启")
        return False
    if strict:
        _prewarmed_browser = None
    if closed:
        logger.info("预启动浏览器已关闭")
    return closed


# 本次运行结束后，预启动实例保留多久（秒）。期间再次运行可直接复用。
# 设为 0 表示维持旧行为（每次运行结束立即关闭浏览器）。
# 移植自 A 的性能优化：旧行为下每次 run_send 结束即关闭，手动「立即续火花」
# 每点一次都要重新付约 20 秒冷启动。
KEEP_PREWARMED_SECONDS = max(0, int(os.environ.get("KEEP_PREWARMED_SECONDS", "600")))

# 浏览器常驻模式：发送完成后不关闭浏览器，下次发送直接复用，彻底消除冷启动。
# 开启后发送结束保留浏览器；空闲后的页面仍按同一阈值刷新并复核。
# 环境变量 KEEP_BROWSER_ALWAYS=true 开启，默认关闭。
KEEP_BROWSER_ALWAYS = os.environ.get("KEEP_BROWSER_ALWAYS", "false").lower() in ("1", "true", "yes")
# 复用前的最大空闲秒数，默认 600；超过先刷新并复核，失败才关闭重建。
# 显式设置为 0 关闭空闲刷新，但每次复用仍检查登录状态。
BROWSER_MAX_IDLE_SECONDS = max(0, int(os.environ.get(
    "BROWSER_MAX_IDLE_SECONDS", "600"
)))


def _keep_prewarmed_alive(seconds: int = KEEP_PREWARMED_SECONDS) -> bool:
    """保留预启动实例供下次复用（必须在专用 Playwright 工作线程内调用）。

    把 created_at 重置为当前时刻，实例进入「刚预热」状态：
    - `_get_prewarmed_page()` 复用前检查空闲时长及登录状态；
    - 下一次 `_prewarm_browser_impl()` 会因为 age 很小而跳过重复启动。

    安全性：复用前 `_get_prewarmed_page()` 会重新 check_login；
    且每次发送前 `_locate_contact()` 都会校验右侧会话确实切换成功，
    不会因为页面停留在上一个会话而错发。
    """
    global _prewarmed_browser
    if seconds <= 0 or not _prewarmed_browser or not _prewarmed_browser.get("ready"):
        return False
    _prewarmed_browser["created_at"] = time.time()
    _prewarmed_browser["reused_at"] = time.time()
    if KEEP_BROWSER_ALWAYS:
        logger.info("常驻模式：浏览器永久保持，下次发送直接复用（免冷启动）")
    else:
        logger.info("预启动浏览器保留 %s 秒供下次复用（避免重复冷启动）", seconds)
    return True


def _get_prewarmed_page():
    """Worker-only reuse: refresh stale pages, wait for contacts, then verify login."""
    global _prewarmed_browser
    if (_prewarmed_browser and _prewarmed_browser.get("cleanup_pending")
            and not _close_prewarmed(strict=True)):
        raise RuntimeError("旧浏览器清理未完成，暂不创建新会话；请稍后重试或退出程序后重启")
    if not _prewarmed_browser or not _prewarmed_browser.get("ready"):
        return None

    try:
        _check_send_stop()
        page = _prewarmed_browser["page"]
        age = max(0, time.time() - _prewarmed_browser.get("created_at", 0))
        stale = BROWSER_MAX_IDLE_SECONDS > 0 and age >= BROWSER_MAX_IDLE_SECONDS
        if stale:
            logger.info("预启动页面已空闲 %.0f 秒，刷新后复核", age)
            page.reload(wait_until="domcontentloaded", timeout=60000)
            _check_send_stop()
            page.wait_for_selector(".conversationConversationItemtitle", timeout=15000)
            _check_send_stop()
        logged, why = check_login(page)
        if logged:
            if stale:
                _prewarmed_browser["created_at"] = time.time()
                _prewarmed_browser["refreshed_at"] = time.time()
            logger.info("使用预启动浏览器（已预热 %.0f 秒）", age)
            return _prewarmed_browser
        logger.warning("预启动浏览器登录态失效：%s", why)
    except SendStopped:
        _close_prewarmed()
        raise
    except Exception as e:
        try:
            raise_if_browser_unavailable(e)
        except BrowserSessionUnavailable:
            logger.warning("预启动浏览器会话已关闭，发送前关闭缓存并重建")
        else:
            logger.warning("预启动浏览器异常：%s", e)

    _close_prewarmed()
    return None


# ── 联系人定位 ────────────────────────────────────────────────────────────


def _find_contact(page, name: str):
    """Only a unique conversation-list title can identify a name; never global .first."""
    target = _norm_text(name)
    candidates = page.locator(".conversationConversationItemtitle")
    matching = [i for i, text in enumerate(candidates.all_text_contents() or []) if _norm_text(text) == target]
    if len(matching) != 1:
        return None
    return candidates.nth(matching[0])


def _verify_in_conversation(page, name: str) -> bool:
    """右侧会话顶部标题区域（x>300 且 y<100）出现目标昵称才算切换成功。

    【只用精确匹配】旧实现先试 exact=True，不中再试 exact=False，而后者是
    子串语义：「张三」会命中标题「张三丰」。于是当某次点击没生效、页面仍停在
    上一个好友「张三丰」时，本函数照样返回 True，_click_and_verify 判定
    「已切换」，消息就发给了上一个人。
    """
    try:
        loc = page.get_by_text(name, exact=True)
        for i in range(loc.count()):
            try:
                box = loc.nth(i).bounding_box()
            except Exception as error:
                raise_if_browser_unavailable(error)
                continue
            if box and box.get("x", 0) > 300 and box.get("y", 0) < 100:
                return True
    except Exception as error:
        raise_if_browser_unavailable(error)
        pass
    return False


def _norm_text(s) -> str:
    """规范化文本用于比较：不间断空格转普通空格并去首尾空白。"""
    return (s or "").replace("\xa0", " ").strip()


def _verify_item_active(page, name: str) -> bool:
    """即时切换判据：被点击的会话项 wrapper 会立即带上
    conversationConversationItemcurConversation（当前会话）class。

    该 class 由前端在点击瞬间添加，不依赖右侧消息区/标题渲染，
    比 _verify_in_conversation 的顶部标题文本更快、更稳，可消除
    连续切换时标题渲染延迟导致的空等（实测个别好友曾因此空等十余秒）。
    """
    try:
        active_title = page.locator(
            ".conversationConversationItemcurConversation .conversationConversationItemtitle"
        ).first
        if active_title.count() == 0:
            return False
        cur = _norm_text(active_title.inner_text(timeout=1000))
        target = _norm_text(name)
        # 【严格相等】必须两边都过 _norm_text 之后再比，缺一不可：
        #   1) 旧写法 `cur == target or cur in target or target in cur` 是子串语义，
        #      「张三」与「张三丰」会互相判为同一人。连续发送时只要某次点击没生效、
        #      页面仍停在上一个好友，本函数就返回 True，消息发给上一个人 —— 这是
        #      「发错人」的实际入口（已用假页面复现）。
        #   2) 但若图省事写成 `cur == name`（不过 _norm_text）又会反过来漏发：
        #      台账里真实存在带不间断空格的昵称（如『某好友\xa0.☹』），原始字节不等。
        return bool(cur) and bool(target) and cur == target
    except Exception as error:
        raise_if_browser_unavailable(error)
        return False


def _wait_any(locator, timeout: float, interval: float = 0.15) -> bool:
    """轮询等待 locator 出现，最长 timeout 秒。

    替代固定 time.sleep(N)：条件早满足就早返回，最坏情况与固定等待等长。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if locator.count():
                return True
        except Exception as error:
            raise_if_browser_unavailable(error)
            pass
        time.sleep(interval)
    try:
        return bool(locator.count())
    except Exception as error:
        raise_if_browser_unavailable(error)
        return False


def _wait_in_conversation(page, name: str, timeout: float) -> bool:
    """轮询等待会话真正切到目标好友，最长 timeout 秒（原为固定 sleep）。

    原实现在点击后硬等 3~4 秒才开始校验；实测切换通常几百毫秒就完成，
    这里改成条件满足即返回，最坏情况仍不超过原来的固定等待。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _verify_item_active(page, name) and _verify_in_conversation(page, name):
            return True
        time.sleep(0.15)
    return False


def _search_and_open(page, name: str) -> bool:
    """搜索兜底：好友不在聊天列表时，用搜索框找到并打开会话。

    本路径原先有 4+4 秒（或 3+3 秒）固定 sleep，一旦有好友走到这里，
    整轮耗时会被直接顶破 1 分钟。现全部改为「轮询到条件满足即返回」，
    最长等待与旧版一致，但正常情况可省下数秒。
    """
    try:
        box = page.get_by_placeholder("搜索", exact=False).first
        if box.count() == 0:
            return False
        box.click()
        box.fill(name)
        btn = page.get_by_text("发消息", exact=False).first
        _wait_any(btn, 4)                      # 原：sleep(4)
        if btn.count():
            btn.click(force=True)
            _wait_in_conversation(page, name, 4)   # 原：sleep(4)
            return True
        candidate = page.get_by_text(name, exact=True).first
        if candidate.count() == 0:
            # 兜底不再用 exact=False：那是子串语义，会把「张三」点成「张三丰」。
            # 改为复用 _find_contact 的「规范化后严格相等」查找（它同样返回
            # Locator/None）。查不到就放弃本轮定位，交由上层判失败并跳过，
            # 宁可漏发也不发错人。本路径原本就极少触发。
            candidate = _find_contact(page, name)
        if candidate is None or candidate.count() == 0:
            return False
        candidate.click(force=True)
        _wait_in_conversation(page, name, 3)   # 原：sleep(3)
        btn = page.get_by_text("发消息", exact=False).first
        if btn.count():
            btn.click(force=True)
            _wait_in_conversation(page, name, 3)   # 原：sleep(3)
        return True
    except Exception as e:
        raise_if_browser_unavailable(e)
        logger.info("搜索打开 %s 失败: %s", name, e)
        return False


_JS_SCROLL_TOP = (
    "() => { const item=document.querySelector('.conversationConversationItemwrapper');"
    " if(!item) return false; let n=item;"
    " while(n){ const s=getComputedStyle(n);"
    " if((s.overflowY==='auto'||s.overflowY==='scroll')&&n.scrollHeight>n.clientHeight+20)"
    " { n.scrollTop=0; return true; } n=n.parentElement; } return false; }"
)

_JS_SCROLL_DOWN = (
    "() => { const item=document.querySelector('.conversationConversationItemwrapper'); if(!item) return null;"
    " let n=item; while(n){ const s=getComputedStyle(n);"
    " if((s.overflowY==='auto'||s.overflowY==='scroll') && n.scrollHeight>n.clientHeight+20) break;"
    " n=n.parentElement; } if(!n) return null;"
    " const before=n.scrollTop; n.scrollTop=before + n.clientHeight*0.9;"
    " return {before, after:n.scrollTop, height:n.scrollHeight, client:n.clientHeight}; }"
)


def _scroll_list_to_top(page) -> None:
    """把会话虚拟列表滚回顶部（慢路径起点）。"""
    try:
        page.evaluate(_JS_SCROLL_TOP)
    except Exception as error:
        raise_if_browser_unavailable(error)
        pass


def _scroll_list_one_page(page):
    """会话列表向下滚动约一屏，返回滚动位置信息（用于判断是否到底）。"""
    try:
        return page.evaluate(_JS_SCROLL_DOWN)
    except Exception as error:
        raise_if_browser_unavailable(error)
        return None


def _click_and_verify(page, target, name: str) -> bool:
    """点击会话项并确认切换成功（选中 class 优先，顶部标题兜底）。"""
    try:
        target.click(force=True, timeout=10000)
    except Exception as error:
        raise_if_browser_unavailable(error)
        return False
    # 轮询间隔 0.3 → 0.15 秒（好友间每人省约 0.15 秒），但**总上限只增不减**：
    # 28 × 0.15 = 4.2 秒，与旧实现 14 × 0.3 = 4.2 秒完全相同。
    # 不能改成 14 × 0.15 = 2.1 秒 —— 那会把「渲染慢但本来能成功」的切换误判成
    # 失败，回退到 _locate_contact 的慢路径（回顶 + 逐屏遍历），反而更慢。
    # 这里保持「先 sleep 再检查」的顺序，等 DOM 稳定一拍再读，
    # 等左侧选中与右侧标题都确认，避免只高亮新会话却仍在旧输入框发送。
    for _ in range(28):
        time.sleep(0.15)
        if _verify_item_active(page, name) and _verify_in_conversation(page, name):
            return True
    return False


def _order_targets_by_list(page, targets: list) -> list:
    """按好友在会话列表中的上下位置排序发送顺序。

    虚拟列表只渲染可视区，若发送顺序在列表顶部/底部之间跳跃，就要反复
    回顶+滚底。按列表位置从上到下发送，使定位滚动基本单调向下；
    当前不可见（未渲染、需滚动/搜索）的好友统一排到最后。
    """
    try:
        titles = [_norm_text(t) for t in
                  page.locator(".conversationConversationItemtitle").all_text_contents()]

        def position(entry):
            nm = _norm_text(entry.get("display_name", ""))
            for i, t in enumerate(titles):
                # 严格相等，且两边都已过 _norm_text（titles 在上方构造时已归一化）。
                # 旧写法带 `nm in t or t in nm`：虽然只影响发送顺序、不影响发给谁，
                # 但「张三」会被排到「张三丰」的位置。统一判据口径，避免这份
                # 「位置匹配」日后被当成定位依据或与其它判据行为不一致。
                if nm and nm == t:
                    return i
            return 9999

        ordered = sorted(targets, key=position)
        return ordered
    except Exception as error:
        raise_if_browser_unavailable(error)
        return targets


def _locate_contact(page, name: str) -> bool:
    """定位并打开好友会话。

    抖音左侧会话列表是虚拟滚动（只渲染可视区），且打开/发送后列表停留位置、
    顺序会变化。旧逻辑只会单调向下滚，一旦列表停在底部而目标在顶部就永远找不到，
    最终落到很慢的搜索兜底（十余秒）。这里改为：
      1) 快路径：当前可视区直接点；
      2) 慢路径：先滚回顶部，再一屏屏向下系统遍历整个列表；
      3) 仍找不到才用搜索框兜底。
    """
    # 快路径：当前可视区（_find_contact 现在可能返回 None，必须先判空）
    try:
        target = _find_contact(page, name)
        if target is not None and target.count() and _click_and_verify(page, target.first, name):
            return True
    except Exception as e:
        raise_if_browser_unavailable(e)
        logger.info("快路径定位 %s 异常: %s", name, str(e)[:100])

    # 慢路径：回顶后逐屏遍历（好友仅十余人，几屏内必覆盖）
    _scroll_list_to_top(page)
    time.sleep(0.35)
    for _ in range(12):
        try:
            target = _find_contact(page, name)
            if target is not None and target.count() and _click_and_verify(page, target.first, name):
                return True
            info = _scroll_list_one_page(page)
            time.sleep(0.45)  # 等虚拟列表渲染新的一屏
            if info and info.get("after", 0) + info.get("client", 0) >= info.get("height", 0) - 5:
                # 已滚到底，最后在底部再确认一次
                target = _find_contact(page, name)
                if target is not None and target.count() and _click_and_verify(page, target.first, name):
                    return True
                break
        except Exception as e:
            raise_if_browser_unavailable(e)
            logger.info("遍历定位 %s 异常: %s", name, str(e)[:100])
            time.sleep(0.4)

    # 搜索兜底（极少触发）
    if _search_and_open(page, name):
        time.sleep(random.uniform(0.5, 1))
        return _verify_item_active(page, name) and _verify_in_conversation(page, name)
    return False


# ── 消息输入与发送 ────────────────────────────────────────────────────────


def _wait_text_in_box(input_box, msg_text: str, timeout: float = 1.0) -> bool:
    """轮询等待文字真正进入输入框。

    替代原先「固定 sleep(0.4) 再检查一次」：渲染快时几毫秒就返回，
    渲染慢时最多等 timeout 秒。注意 timeout 比原来的 0.4 秒更宽裕，
    因此既能提速、又比旧实现更不容易误判「文字未进入输入框」。
    """
    deadline = time.time() + timeout
    while True:
        try:
            if msg_text in (input_box.inner_text() or ""):
                return True
        except Exception as error:
            raise_if_browser_unavailable(error)
            pass
        if time.time() >= deadline:
            return False
        time.sleep(0.03)


def _type_and_send(page, input_box, msg_text: str, *, before_enter=None, before_submit=None) -> bool:
    """把文字输入输入框并按 Enter 发送，返回文字是否成功进入输入框。"""
    enter_attempted = False
    try:
        input_box.click()
        time.sleep(0.06)
        if before_enter is not None:
            before_enter()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        time.sleep(0.06)
        # 消息很短（如「[续火花吧]」5 字符），40ms 逐字延迟既保留拟人节奏，
        # 又比原来 100ms 快一半多
        page.keyboard.type(msg_text, delay=40)
        if not _wait_text_in_box(input_box, msg_text, timeout=1.0):
            try:
                cur = (input_box.inner_text() or "")[:30]
            except Exception as error:
                raise_if_browser_unavailable(error)
                cur = "<读取失败>"
            logger.warning("文字未进入输入框，当前内容: %r", cur)
            return False
        if before_enter is not None:
            before_enter()
        # Once this call begins, a driver error cannot prove the message was
        # not delivered. Never convert that uncertainty into a retryable False.
        if before_submit is not None:
            before_submit()
        enter_attempted = True
        page.keyboard.press("Enter")
        return True
    except (MessageDeliveryUnconfirmed, SendIntentWriteFailed, SendStopped):
        raise
    except Exception as e:
        if enter_attempted:
            raise MessageDeliveryUnconfirmed() from e
        raise_if_browser_unavailable(e)
        logger.info("输入/发送异常: %s", str(e)[:100])
        return False


def _wait_input_cleared(input_box, msg_text: str, wait: float = 6) -> bool:
    """Legacy input-state helper only. Clearing alone never confirms acceptance.

    Delivery confirmation uses _confirm_delivery with new outgoing-message
    evidence and failure observation; callers must not treat this bool as success.
    """
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.15)
        try:
            cur = input_box.inner_text() or ""
            if msg_text not in cur:
                return True
        except Exception as error:
            raise_if_browser_unavailable(error)
            pass
    return False



class MessageDeliveryRejected(RuntimeError):
    code = "delivery_rejected"
    def __init__(self):
        super().__init__("按下发送后页面明确提示发送失败，请人工核对；不会自动重发")


# 选择器集中维护；无法证明会话边界时停止，不能凭输入框清空或旧消息判成功。
# 显式标记仅由本地模拟页面验证，未声称真实平台一定提供这些属性。
# 正向证据限制在绑定会话；失败提示可能通过 portal 挂在会话外，仍全局观察
# 并保守否决成功。全局失败观察绝不能反过来作为发送成功的依据。
CURRENT_CHAT_SCOPE_SELECTOR = '[data-conversation-active="true"]'
CHAT_TITLE_SELECTOR = '[data-conversation-title]'
CHAT_MESSAGE_LIST_SELECTOR = '[data-message-list]'
CHAT_INPUT_SELECTOR = 'div[contenteditable="true"]'
ACTIVE_CHAT_ROW_TITLE_SELECTOR = '.conversationConversationItemcurConversation .conversationConversationItemtitle'
CHAT_ROW_TITLE_SELECTOR = '.conversationConversationItemtitle'
OUTGOING_MESSAGE_SELECTOR = ('[data-message-id][data-is-self="true"], '
                             '[data-message-id][data-sender="self"], '
                             '[data-message-id][data-direction="outgoing"]')
MESSAGE_TEXT_SELECTOR = '[data-message-text], [data-testid="message-text"]'
MESSAGE_FAILED_SELECTOR = ('[data-send-status="failed"], [data-send-status="rejected"], '
                           '[aria-label="发送失败"], [title="发送失败"]')
MESSAGE_PENDING_SELECTOR = ('[data-send-status="pending"], [data-send-status="sending"], '
                            '[data-send-status="queued"]')
SEND_ERROR_SELECTOR = '[role="alert"], [data-send-error]'

_CHAT_SCOPE_CONTRACT = {
    "scope": CURRENT_CHAT_SCOPE_SELECTOR, "title": CHAT_TITLE_SELECTOR,
    "message_list": CHAT_MESSAGE_LIST_SELECTOR, "input": CHAT_INPUT_SELECTOR,
    "active_row": ACTIVE_CHAT_ROW_TITLE_SELECTOR, "row_title": CHAT_ROW_TITLE_SELECTOR,
}

_BIND_DELIVERY_SCOPE_JS = r"""({selectors, target}) => {
  const visible = el => !!(el.isConnected && el.getClientRects().length
    && !['hidden', 'collapse'].includes(getComputedStyle(el).visibility));
  const norm = text => (text || '').replace(/\u00a0/g, ' ').trim();
  const scopes = Array.from(document.querySelectorAll(selectors.scope)).filter(visible);
  if (scopes.length === 0) return null;
  if (scopes.length !== 1 || !target) return {invalid: true};
  const container = scopes[0];
  const own = selector => Array.from(container.querySelectorAll(selector))
    .filter(el => visible(el) && el.closest(selectors.scope) === container);
  const titles = own(selectors.title), lists = own(selectors.message_list), inputs = own(selectors.input);
  const conversationId = norm(container.getAttribute('data-conversation-id'));
  if (!conversationId || titles.length !== 1 || norm(titles[0].textContent) !== target
      || lists.length !== 1 || inputs.length !== 1) return {invalid: true};
  return {container, messageList: lists[0], inputBox: inputs[0], conversationId,
    title: titles[0], adapter: 'explicit'};
}"""

# 结构适配只复用已有的选中会话项、精确标题位置和输入框选择器；标题与输入
# 的最低共同祖先必须是独立右侧会话区域，不能把整个网页当作会话边界。
_BIND_STRUCTURAL_SCOPE_JS = r"""({selectors, target, headers}) => {
  const visible = el => !!(el && el.isConnected && el.getClientRects().length
    && !['hidden', 'collapse'].includes(getComputedStyle(el).visibility));
  const norm = text => (text || '').replace(/\u00a0/g, ' ').trim();
  const rows = Array.from(document.querySelectorAll(selectors.active_row)).filter(visible);
  const inputs = Array.from(document.querySelectorAll(selectors.input)).filter(visible);
  if (!target || rows.length !== 1 || norm(rows[0].textContent) !== target
      || headers.length !== 1 || inputs.length !== 1) return null;
  const title = headers[0], inputBox = inputs[0];
  if (!visible(title) || norm(title.textContent) !== target) return null;
  let container = title.parentElement;
  while (container && !container.contains(inputBox)) container = container.parentElement;
  if (!container || /^(BODY|HTML)$/.test(container.tagName) || !visible(container)
      || container.querySelector(selectors.row_title)) return null;
  const ownInputs = Array.from(container.querySelectorAll(selectors.input)).filter(visible);
  if (ownInputs.length !== 1 || ownInputs[0] !== inputBox) return null;
  return {container, messageList: container, inputBox, conversationId: '', title,
    rowTitle: rows[0], adapter: 'structural'};
}"""

_DELIVERY_SCOPE_CURRENT_JS = r"""(container, {selectors, target, conversationId, messageList, inputBox, title, focus}) => {
  const visible = el => !!(el && el.isConnected && el.getClientRects().length
    && !['hidden', 'collapse'].includes(getComputedStyle(el).visibility));
  const norm = text => (text || '').replace(/\u00a0/g, ' ').trim();
  const scopes = Array.from(document.querySelectorAll(selectors.scope)).filter(visible);
  if (scopes.length !== 1 || scopes[0] !== container || !visible(container)
      || norm(container.getAttribute('data-conversation-id')) !== conversationId) return false;
  const own = selector => Array.from(container.querySelectorAll(selector))
    .filter(el => visible(el) && el.closest(selectors.scope) === container);
  const titles = own(selectors.title), lists = own(selectors.message_list), inputs = own(selectors.input);
  if (titles.length !== 1 || titles[0] !== title || norm(titles[0].textContent) !== target
      || lists.length !== 1 || lists[0] !== messageList || inputs.length !== 1 || inputs[0] !== inputBox) return false;
  return !focus || document.activeElement === inputBox || inputBox.contains(document.activeElement);
}"""

_STRUCTURAL_SCOPE_CURRENT_JS = r"""(container, {selectors, target, inputBox, title, rowTitle, headers, focus}) => {
  const visible = el => !!(el && el.isConnected && el.getClientRects().length
    && !['hidden', 'collapse'].includes(getComputedStyle(el).visibility));
  const norm = text => (text || '').replace(/\u00a0/g, ' ').trim();
  const rows = Array.from(document.querySelectorAll(selectors.active_row)).filter(visible);
  const inputs = Array.from(document.querySelectorAll(selectors.input)).filter(visible);
  if (!visible(container) || !visible(title) || !visible(inputBox)
      || rows.length !== 1 || rows[0] !== rowTitle || norm(rowTitle.textContent) !== target
      || headers.length !== 1 || headers[0] !== title || norm(title.textContent) !== target
      || inputs.length !== 1 || inputs[0] !== inputBox || container.querySelector(selectors.row_title)
      || /^(BODY|HTML)$/.test(container.tagName)) return false;
  let ancestor = title.parentElement;
  while (ancestor && !ancestor.contains(inputBox)) ancestor = ancestor.parentElement;
  if (ancestor !== container) return false;
  return !focus || document.activeElement === inputBox || inputBox.contains(document.activeElement);
}"""


@dataclass
class _DeliveryScope:
    container: object
    message_list: object
    input_box: object
    conversation_id: str
    target_name: str
    page: object
    adapter: str
    title: object
    row_title: object = None

    def dispose(self):
        for handle in (self.input_box, self.message_list, self.container, self.title, self.row_title):
            if handle is None:
                continue
            try:
                handle.dispose()
            except Exception:
                logger.debug("释放发送会话元素引用失败")


def _right_title_handles(page, target_name: str) -> list:
    """复用现有标题位置规则，只保留可见且文字精确匹配的候选。"""
    headers = []
    candidates = page.get_by_text(target_name, exact=True).element_handles()
    completed = False
    try:
        for handle in candidates:
            box = handle.bounding_box()
            if handle.is_visible() and box and box.get("x", 0) > 300 and box.get("y", 0) < 100:
                headers.append(handle)
        completed = True
        return headers
    finally:
        for handle in candidates:
            if not completed or handle not in headers:
                try:
                    handle.dispose()
                except Exception:
                    logger.debug("释放会话标题候选引用失败")


def _bind_delivery_scope(page, target_name: str) -> _DeliveryScope | None:
    if not _norm_text(target_name):
        return None
    binding = None
    handles = []
    headers = []
    try:
        binding = page.evaluate_handle(_BIND_DELIVERY_SCOPE_JS,
                                       {"selectors": _CHAT_SCOPE_CONTRACT, "target": _norm_text(target_name)})
        if binding.evaluate("value => value === null"):
            binding.dispose()
            binding = None
            headers = _right_title_handles(page, _norm_text(target_name))
            binding = page.evaluate_handle(_BIND_STRUCTURAL_SCOPE_JS, {
                "selectors": _CHAT_SCOPE_CONTRACT, "target": _norm_text(target_name), "headers": headers,
            })
            if binding.evaluate("value => value === null"):
                return None
        if binding.evaluate("value => value.invalid === true"):
            return None
        for key in ("container", "messageList", "inputBox", "title"):
            handle = binding.get_property(key).as_element()
            if handle is None:
                return None
            handles.append(handle)
        identity_handle = binding.get_property("conversationId")
        try:
            conversation_id = identity_handle.json_value()
        finally:
            identity_handle.dispose()
        if not isinstance(conversation_id, str) or not conversation_id:
            if binding.evaluate("value => value.adapter") != "structural":
                return None
        adapter = binding.evaluate("value => value.adapter")
        row_title = None
        if adapter == "structural":
            row_title = binding.get_property("rowTitle").as_element()
            if row_title is None:
                return None
            handles.append(row_title)
        scope = _DeliveryScope(handles[0], handles[1], handles[2], conversation_id,
                               _norm_text(target_name), page, adapter, handles[3], row_title)
        handles = []  # 返回的会话对象接管这些元素引用。
        return scope
    except Exception as error:
        raise_if_browser_unavailable(error)
        return None
    finally:
        for handle in handles + headers + ([binding] if binding is not None else []):
            try:
                handle.dispose()
            except Exception:
                logger.debug("释放会话绑定临时引用失败")


def _delivery_scope_current(scope: _DeliveryScope, *, require_focus: bool = False) -> bool:
    headers = []
    try:
        payload = {
            "selectors": _CHAT_SCOPE_CONTRACT, "target": scope.target_name,
            "conversationId": scope.conversation_id, "messageList": scope.message_list,
            "inputBox": scope.input_box, "title": scope.title, "focus": require_focus,
        }
        script = _DELIVERY_SCOPE_CURRENT_JS
        if scope.adapter == "structural":
            headers = _right_title_handles(scope.page, scope.target_name)
            payload.update(rowTitle=scope.row_title, headers=headers)
            script = _STRUCTURAL_SCOPE_CURRENT_JS
        return scope.container.evaluate(script, payload) is True
    except Exception as error:
        raise_if_browser_unavailable(error)
        return False
    finally:
        for handle in headers:
            try:
                handle.dispose()
            except Exception:
                logger.debug("释放当前会话标题引用失败")


_DELIVERY_SNAPSHOT_JS = r"""(container, {messageList, selectors}) => {
  const visible = el => !!(el.isConnected && el.getClientRects().length
    && !['hidden', 'collapse'].includes(getComputedStyle(el).visibility));
  const messages = Array.from(messageList.querySelectorAll(selectors.outgoing))
    .filter(el => visible(el)
      && (!messageList.matches(selectors.message_list) || el.closest(selectors.message_list) === messageList)
      && (!el.closest(selectors.scope) || el.closest(selectors.scope) === container)).map(el => {
    const content = el.querySelector(selectors.text);
    const value = (content ? content.textContent : el.textContent || '').trim();
    const failed = el.getAttribute('data-send-status') === 'failed' || el.getAttribute('data-send-status') === 'rejected'
      || !!el.querySelector(selectors.failed);
    const pending = /^(pending|sending|queued)$/.test(el.getAttribute('data-send-status') || '')
      || !!el.querySelector(selectors.pending);
    return {id: el.getAttribute('data-message-id'), text: value, failed, pending};
  });
  const failures = Array.from(document.querySelectorAll(selectors.errors)).filter(visible)
    .map(el => (el.textContent || '').trim()).filter(t => /发送失败|消息发送失败|操作过于频繁|暂时无法发送|已被禁言/.test(t));
  return {messages, failures};
}"""


def _delivery_snapshot(page, msg_text: str, *, scope: _DeliveryScope) -> dict:
    try:
        if not _delivery_scope_current(scope):
            return {"messages": [], "failures": [], "unavailable": True}
        value = scope.container.evaluate(_DELIVERY_SNAPSHOT_JS, {
            "messageList": scope.message_list, "selectors": {
                **_CHAT_SCOPE_CONTRACT, "outgoing": OUTGOING_MESSAGE_SELECTOR,
                "text": MESSAGE_TEXT_SELECTOR, "failed": MESSAGE_FAILED_SELECTOR,
                "pending": MESSAGE_PENDING_SELECTOR, "errors": SEND_ERROR_SELECTOR,
            },
        })
        # 读取证据期间 DOM 也可能变化；再次确认，不能接受旧会话中的结果。
        if not _delivery_scope_current(scope):
            return {"messages": [], "failures": [], "unavailable": True}
        if isinstance(value, dict) and isinstance(value.get("messages"), list) and isinstance(value.get("failures"), list):
            return value
    except Exception as error:
        raise_if_browser_unavailable(error)
    return {"messages": [], "failures": [], "unavailable": True}


def _confirm_delivery(page, input_box, msg_text: str, baseline: dict, timeout: float = 8,
                      quiet_window: float = 1.2, *, scope: _DeliveryScope) -> tuple[bool, str]:
    old_ids = {m.get("id") for m in baseline.get("messages", []) if isinstance(m, dict)}
    old_failures = list(baseline.get("failures", []))
    deadline = time.monotonic() + timeout
    candidate_id = None
    candidate_seen = None
    try:
        while time.monotonic() < deadline:
            snapshot = _delivery_snapshot(page, msg_text, scope=scope)
            if snapshot.get("unavailable") or baseline.get("unavailable"):
                raise MessageDeliveryUnconfirmed()
            new_messages = [m for m in snapshot["messages"] if isinstance(m, dict)
                            and m.get("id") and m.get("id") not in old_ids and m.get("text") == msg_text]
            # New failure signals after this Enter block retries even if input cleared.
            if any(m.get("failed") for m in new_messages) or snapshot["failures"] != old_failures and snapshot["failures"]:
                raise MessageDeliveryRejected()
            cleared = msg_text not in (input_box.inner_text() or "")
            confirmed_messages = [m for m in new_messages if not m.get("pending")]
            if confirmed_messages and cleared and not snapshot["failures"]:
                current_id = confirmed_messages[-1]["id"]
                if candidate_id != current_id:
                    candidate_id, candidate_seen = current_id, time.monotonic()
                elif time.monotonic() - candidate_seen >= quiet_window:
                    if not _delivery_scope_current(scope):
                        raise MessageDeliveryUnconfirmed()
                    progress()
                    return True, "accepted"
            else:
                candidate_id = candidate_seen = None
            time.sleep(0.1)
    except (MessageDeliveryRejected, MessageDeliveryUnconfirmed):
        raise
    except Exception as error:
        raise MessageDeliveryUnconfirmed() from error
    raise MessageDeliveryUnconfirmed()


def _send_message(page, msg_text: str, dry_run: bool, *, target_name: str = "") -> tuple[bool, str]:
    if detect_rate_limit(page):
        return False, "发送前检测到验证提示"
    scope = _bind_delivery_scope(page, target_name)
    if scope is None:
        raise MessageDeliveryUnconfirmed(enter_attempted=False)
    try:
        if not _delivery_scope_current(scope):
            raise MessageDeliveryUnconfirmed(enter_attempted=False)
        if dry_run:
            return True, "dry-run"
        baseline = _delivery_snapshot(page, msg_text, scope=scope)
        if baseline.get("unavailable"):
            raise MessageDeliveryUnconfirmed(enter_attempted=False)

        def before_enter():
            if not _delivery_scope_current(scope, require_focus=True):
                raise MessageDeliveryUnconfirmed(enter_attempted=False)

        if not _type_and_send(page, scope.input_box, msg_text, before_enter=before_enter,
                              before_submit=_persist_current_intent):
            return False, "文字未能输入到输入框"
        return _confirm_delivery(page, scope.input_box, msg_text, baseline, scope=scope)
    finally:
        scope.dispose()


def send_to_contact(page, name: str, msg_text: str, dry_run: bool) -> tuple[bool, str]:
    """完整流程：定位好友 → 校验切换 → 发送消息。"""
    if not _locate_contact(page, name):
        return False, "未能切换到该好友会话（名字不在聊天列表，或页面结构变化）"

    progress()

    # ★ 发送前最终防线：在「输入文字并按 Enter」之前再确认一次当前会话是目标好友。
    #
    # 定位阶段已校验过一次，这里为什么还要再来一次：定位校验与真正打字之间还隔着
    # detect_rate_limit、输入框解析等若干次页面交互，页面状态存在被改变的可能；
    # 这是发错人之前最后一道可以便宜拦住的关口。
    #
    # 判据直接复用 _click_and_verify 用的那一对函数（二者都已收紧为严格相等），
    # 重用定位时的两个判据，避免查找与发送使用不同身份规则。
    # 左侧选中与右侧会话标题必须同时匹配；只改变左侧高亮不证明输入框已切换。
    # 页面结构不支持确认时取消本次发送，不能以旧会话中的输入框继续。
    #
    # 该防线是 fail-closed 的：宁可漏发也不发错人。「判据失效导致全员漏发」
    # 这种新失败模式由 run_send 结尾的「0 成功」ERROR 告警兜住。
    if not (_verify_item_active(page, name) and _verify_in_conversation(page, name)):
        logger.warning("发送前最终校验失败：目标「%s」不是当前会话，已取消发送", name)
        return False, f"发送前校验失败：当前会话不是「{name}」，为避免发错人已取消发送"

    if detect_rate_limit(page):
        return False, "检测到「操作频繁 / 安全验证」提示"
    return _send_message(page, msg_text, dry_run, target_name=name)


# ── 联系人同步 ────────────────────────────────────────────────────────────

from .contact_capture import EXTRACT_JS as _EXTRACT_JS
from .contact_capture import scan_contacts


def _open_chat_page(page) -> bool:
    """打开抖音私信页并等待加载，返回是否成功。"""
    for attempt in range(3):
        try:
            page.goto(CHAT_URL, timeout=90000, wait_until="domcontentloaded")
            return True
        except Exception as e:
            logger.info("打开页面失败（第 %s 次）: %s", attempt + 1, str(e)[:80])
            time.sleep(5)
    return False


def _scroll_and_extract(page, collected: list[dict], max_rounds: int = 20) -> dict:
    """Read stable pairs and report partial coverage when the list is unproven."""
    return scan_contacts(page, collected, extract_js=_EXTRACT_JS, max_rounds=max_rounds, progress=progress)


def fetch_chat_contacts() -> dict:
    """公共入口（可在任意线程调用）：在专用 Playwright 工作线程中采集联系人。"""
    return _run_in_pw_thread(_fetch_chat_contacts_impl)


_CONTACT_TITLE_SELECTOR = ".conversationConversationItemtitle"


def _wait_for_contacts(page, timeout_ms: int = 20000) -> bool:
    """等待联系人列表渲染完成，就绪立即返回 True。

    移植自 A 的性能优化：替代固定 `page.wait_for_timeout(10000)`——那是无条件
    等待，无论页面 3 秒还是 10 秒就绪都要坐满 10 秒。这里复用本模块已有的
    `_wait_any`（条件早满足就早返回，最坏情况与固定等待等长），
    单次采集通常可省下 5-8 秒。
    """
    return _wait_any(page.locator(_CONTACT_TITLE_SELECTOR), timeout_ms / 1000.0)


def _fetch_chat_contacts_impl() -> dict:
    """从抖音私信页左侧聊天列表读取联系人（含火花天数），供网页端勾选。"""
    result = {"at": _now(), "names": [], "error": None}
    if not get_valid_state_path():
        result["error"] = "尚未上传登录态 state.json"
        return result

    try:
        with open_browser() as (p, browser, context, page):
            if not _open_chat_page(page):
                result["error"] = "无法打开抖音私信页面"
                return result

            # 事件驱动等待列表渲染（旧写法无条件等 10 秒）
            if not _wait_for_contacts(page):
                logger.info("联系人列表 20 秒内未出现，仍继续尝试检查登录态")

            logged, why = check_login(page)
            if not logged:
                result["error"] = why
                return result

            collected: list[dict] = []
            for attempt in range(3):
                try:
                    page.wait_for_selector(_CONTACT_TITLE_SELECTOR, timeout=45000)
                except Exception:
                    logger.info("第 %s 次等待联系人列表超时", attempt + 1)

                result['capture'] = _scroll_and_extract(page, collected)

                if collected:
                    break
                try:
                    page.reload(wait_until="domcontentloaded", timeout=90000)
                    _wait_for_contacts(page, timeout_ms=15000)  # 旧写法固定等 12 秒
                except Exception:
                    logger.debug('联系人页面重载失败，沿用已有同步处理')

            result["names"] = collected
            summary = result.get('capture') or {}
            logger.info("已读取联系人 %s 行；确认火花 %s 行，未更新 %s 行，列表完整=%s",
                        len(collected), summary.get('confirmed',0), summary.get('unknown',0), summary.get('complete',False))
    except Exception as e:
        logger.error("获取联系人异常: %s", e)
        result["error"] = f"获取联系人异常: {e}"
    return result


# ── 通道选择 ───────────────────────────────────────────────────────────────


def _b_channel_daily() -> tuple[str, int]:
    """通道 B 今日已发条数：优先 runtime 计数，跨天自动归零。"""
    today = datetime.now().astimezone().date().isoformat()
    rec = load_runtime().get("b_channel_daily") or {}
    if rec.get("date") != today:
        return today, 0
    return today, int(rec.get("count", 0) or 0)


def compute_pending(cfg: dict | None = None, *, dry_run: bool = False, force: bool = False,
                    selected_snapshot: list[dict] | None = None) -> list[dict]:
    """预测本次运行会真实发送的名单（与 run_send 通道判定一致）。"""
    cfg = cfg or load_config()
    entries = ledger.get_selected() if selected_snapshot is None else selected_snapshot
    daily_limit = max(1, _cfg_int(cfg, "first_message_daily_limit", 1))
    _, creator_sent_today = _b_channel_daily()
    allow_first = bool(cfg.get("allow_first_message"))
    pending: list[dict] = []
    current = business_now()
    for e in entries:
        if ledger.identity_block_reason(e):
            continue
        if not dry_run and not force and daily_skip_code(e, now=current):
            continue
        if e.get("has_conversation"):
            pending.append({**e, "send_channel": "consumer"})
        elif allow_first and creator_sent_today < daily_limit:
            pending.append({**e, "send_channel": "creator"})
    return pending


# ── 主发送流程 ─────────────────────────────────────────────────────────────


def _send_consumer(page, entry: dict, msg: str, dry_run: bool, result: dict) -> None:
    name, key = entry["display_name"], entry.get("entry_id", "")
    if ledger.identity_block_reason(entry):
        result["skipped"].append({"entry_id": key, "name": name, "code": ledger.identity_block_reason(entry),
                                  "retryable": False, "reason": "当前会话重名、本次未观察到或关联历史发送受保护；本轮不自动发送"})
        return
    previous_intent = getattr(_send_context, "intent", None)
    intent = {"entry_id": key, "display_name":name, "attempt_id": "", "force": getattr(_send_context, "force", False)}
    _send_context.intent = None if dry_run else intent

    def persist(ok, *, status=None):
        if not dry_run:
            options = {"entry_id": key}
            if status is not None:
                options["status"] = status
            if intent["attempt_id"]:
                options["attempt_id"] = intent["attempt_id"]
            ledger.update_send_result(name, ok, _now(), msg=msg, **options)

    def unknown(error, *, attempted=True):
        result["skipped"].append({"entry_id": key, "name": name, "code": "delivery_unconfirmed",
                                  "retryable": False, "enter_attempted": attempted, "reason": str(error)})
        result["delivery_unconfirmed"] = True
        try:
            persist(False, status="unconfirmed")
        except Exception:
            # The pre-submit sending record is already durable. Never rewrite
            # it as unsent or allow a result-storage failure to report success.
            logger.exception("待核对结果未能保存，保留原发送意图以阻止自动重发")

    try:
        _check_send_stop()
        try:
            ok, why = send_to_contact(page, name, msg, dry_run)
        except MessageDeliveryUnconfirmed as error:
            unknown(error, attempted=error.enter_attempted)
            return
        except MessageDeliveryRejected as error:
            try:
                persist(False, status="rejected")
            except Exception:
                unknown(error)
                return
            result["failed"].append({"entry_id": key, "name": name, "code": error.code, "retryable": False, "reason": str(error)})
            result["delivery_rejected"] = True
            return
        except SendIntentWriteFailed as error:
            result["failed"].append({"entry_id": key, "name": name, "code": "intent_write_failed",
                                     "retryable": True, "reason": str(error)})
            result["intent_write_failed"] = True
            return
        except Exception as error:
            if intent["attempt_id"]:
                unknown(error)
                if isinstance(error, SendStopped):
                    result["stopped"] = True
                return
            raise
        if not ok and intent["attempt_id"]:
            unknown("已建立发送意图，但未取得可靠的发送结果")
            return
        try:
            persist(ok)
        except Exception as error:
            if ok or intent["attempt_id"]:
                unknown(error)
                return
            raise
        if ok:
            result["ok"].append(name)
            result.setdefault("ok_entries", []).append({"entry_id": key, "name": name})
            if not dry_run:
                try:
                    ledger.confirm_join(name, entry_id=key)
                except Exception:
                    logger.exception("发送结果已保存，但会话关联标记更新失败")
            logger.info("页面确认发送给 %s：%s", name, "(干跑)" if dry_run else "accepted")
        elif entry.get("channel") == "creator":
            result["skipped"].append({"entry_id": key, "name": name, "retryable": False,
                                      "reason": "consumer 定位失败（该好友为 creator-only），已降级跳过"})
            if not dry_run:
                ledger.mark_no_consumer_conversation(name, entry_id=key)
        else:
            result["failed"].append({"entry_id": key, "name": name, "retryable": True, "reason": why})
            if detect_rate_limit(page):
                result["rate_limited"] = True
                result["failed"][-1]["retryable"] = False
    finally:
        _send_context.intent = previous_intent
        progress()


def _protect_unresolved_results(result: dict) -> None:
    """A durable, unfinished submission can never become a retryable deferred row."""
    try:
        unresolved = {e["entry_id"]: e for e in ledger.load_ledger()
                      if (e.get("send_attempt") or {}).get("status") in {"sending", "unconfirmed"}}
    except Exception:
        logger.exception("无法复核发送意图，禁止本轮自动补发")
        for field in ("failed", "deferred"):
            for item in result.get(field, []):
                if isinstance(item, dict):
                    item["retryable"] = False
        result["delivery_unconfirmed"] = True
        return
    for field in ("failed", "deferred"):
        safe = []
        for item in result.get(field, []):
            if isinstance(item, dict) and item.get("entry_id") in unresolved:
                if not any(isinstance(row, dict) and row.get("entry_id") == item["entry_id"] for row in result["skipped"]):
                    result["skipped"].append({**item, "code": "delivery_unconfirmed", "retryable": False,
                                              "reason": "存在未决发送意图，请人工核对；不会自动重发"})
                result["delivery_unconfirmed"] = True
            else:
                safe.append(item)
        result[field] = safe


def _send_creator(entry: dict, msg: str, dry_run: bool, result: dict, p) -> None:
    """通道 B：creator 首条消息。"""
    name = entry["display_name"]
    if ledger.identity_block_reason(entry):
        result["skipped"].append({"entry_id": entry.get("entry_id", ""), "name": name,
                                  "code": ledger.identity_block_reason(entry), "retryable": False,
                                  "reason": "同名或身份冲突，无法确定目标"})
        return
    if creator_channel is None:
        # core/sender.py 仍是空壳：这里若直接调用会抛 AttributeError，
        # 被上层 except 捕获后整轮发送就此中断（后面所有好友都不再发）。
        # 只把该好友记为 skipped，其余好友继续。开关侧另有 /api/config 拦截。
        result["skipped"].append(
            {"entry_id": entry.get("entry_id", ""), "name": name, "retryable": False, "reason": "通道 B（creator 首条消息）尚未实现"}
        )
        return
    cfg = load_config()
    allow_first = bool(cfg.get("allow_first_message"))
    daily_limit = max(1, _cfg_int(cfg, "first_message_daily_limit", 1))
    today, count = _b_channel_daily()

    if not allow_first:
        result["skipped"].append({"entry_id": entry.get("entry_id", ""), "name": name, "retryable": False, "reason": "无会话且未开启「允许首条消息」"})
        return
    if count >= daily_limit:
        result["skipped"].append({
            "entry_id": entry.get("entry_id", ""),
            "retryable": False,
            "name": name,
            "reason": f"今日已发送首条消息 {count}/{daily_limit}",
        })
        return

    ok, why = creator_channel.send_first_message(entry, msg, dry_run, p)
    if ok:
        result["ok"].append(name)
        result.setdefault("ok_entries", []).append({"entry_id": entry.get("entry_id", ""), "name": name})
        logger.info("通道 B 已发送给 %s：%s", name, msg if not dry_run else "(干跑)")
        if not dry_run:
            ledger.update_send_result(name, True, _now(), via_creator=True, msg=msg, entry_id=entry.get("entry_id", ""))
            update_runtime(b_channel_daily={"date": today, "count": count + 1})
    else:
        result["failed"].append({"entry_id": entry.get("entry_id", ""), "name": name, "retryable": False, "reason": f"通道B: {why}"})
        logger.warning("通道 B 发送给 %s 失败：%s", name, why)
        if "限流" in why or "停止" in why:
            result["rate_limited"] = True
            logger.warning("通道 B 触发限流，停止本轮")


def run_send(dry_run: bool = False, only_names: list[str] | None = None, only_ids: list[str] | None = None,
             force: bool = False, *, selected_snapshot: list[dict] | None = None) -> dict:
    """公共入口（可在任意线程调用）：在专用 Playwright 工作线程中执行发送。

    与预启动任务共用同一个单线程执行器，因此会天然等待预启动完成，
    并复用其浏览器实例，不会出现两个任务同时操作浏览器。
    """
    if selected_snapshot is None:
        result = _run_in_pw_thread(_run_send_impl, dry_run, only_names, only_ids, force)
    else:
        result = _run_in_pw_thread(_run_send_impl, dry_run, only_names, only_ids, force,
                                   selected_snapshot=selected_snapshot)

    # 「本轮 0 成功」告警。
    #
    # 会话定位与校验现在全部是 fail-closed（宁可漏发也不发错人），因此
    # 「抖音改版导致判据整体失效」这类问题的新表现不再是「发错人」，而是
    # 「本轮一个人都没发出去」。这种情况必须用 ERROR 显式报出来：
    # 否则日志里只有每人一行 warning，得等到第二天发现火花断了才知道，白断一晚。
    if isinstance(result, dict) and not dry_run:
        try:
            failed = result.get("failed") or []
            if failed and not result.get("ok"):
                logger.error(
                    "本轮发送 0 成功：失败 %s 人、跳过 %s 人 —— 请检查登录态是否过期，"
                    "或抖音页面结构变化导致会话定位/校验判据失效",
                    len(failed), len(result.get("skipped") or []),
                )
        except Exception:
            logger.debug('发送结果辅助诊断失败，保留原发送结果')
    return result


# ── 发送时间预算（移植自 A 的性能优化）───────────────────────────────────
# 目标：让一轮发送在指定秒数内完成，且不牺牲安全间隔。
#
# 做法不是砍固定等待（那些已压到接近下限），而是按「剩余时间 / 剩余人数」
# 动态分配好友之间的间隔：人多时自动收紧、人少时放宽到上限，恒定不超过
# SEND_BUDGET_SECONDS。预算耗尽就停止本轮，剩余好友留给后台补发任务，
# 避免把本轮无限拖长（原实现固定 random(gap_min, gap_max)，10 人约 30 秒
# 间隔、20 人约 60 秒，人数一多必然超时）。
SEND_BUDGET_SECONDS = max(0, int(os.environ.get("SEND_BUDGET_SECONDS", "60")))
# 任意两个好友之间的间隔上下限（秒）。实际取值以配置的 send_gap_min/max 为准，
# 并进一步受时间预算约束：SEND_INTERVAL_MAX **只是配置缺失时的兜底上限**，
# 不再反过来夹住配置（旧实现的 min(SEND_INTERVAL_MAX, gap_max) 正是那个 bug）。
SEND_INTERVAL_MIN = max(0.3, float(os.environ.get("SEND_INTERVAL_MIN", "1.0")))
SEND_INTERVAL_MAX = max(SEND_INTERVAL_MIN, float(os.environ.get("SEND_INTERVAL_MAX", "4.0")))
# 每个好友的固定开销估算（秒）：_click_and_verify 轮询 + _type_and_send + 输入框清空校验。
# 依据 2026-09-18 实测（00:20:07→00:20:59，11 人 56 秒，间隔均值约 3s）反推约 1.5 秒。
# 仅用于「剩余时间是否还塞得下一个人」的预检，不参与实际等待时长计算。
_PER_FRIEND_FIXED_COST = max(0.0, float(os.environ.get("PER_FRIEND_FIXED_COST", "1.5")))


def _send_interval(deadline: float | None, remaining: int, gap_max: float, *, gap_min: float | None = None) -> float:
    """按剩余时间预算动态决定下一个好友之前的等待秒数。

    - deadline=None：返回 0，由调用方走原来的随机间隔逻辑；
    - 剩余时间充足：间隔上限取配置的 gap_max；
    - 时间偏紧仍遵守配置下限；调用方在预算不足时延后剩余对象。
    """
    if deadline is None:
        return 0.0
    left = deadline - time.time()
    if left <= 0:
        return 0.0
    avg = left / max(1, remaining)

    # hi = 本次间隔的上限，**以配置的 gap_max 为准**。
    #
    # 旧写法 `min(SEND_INTERVAL_MAX, max(gap_max, SEND_INTERVAL_MIN))` 与本函数
    # 文档「取较大者」正好相反：硬编码的 4.0 会把配置里更大的 gap_max 夹掉，
    # 配置成 6~12 时实际最多只等 4 秒，「间隔上限」形同虚设（与 deadline=None
    # 那条路径行为不一致，同样的配置在两处得到不同节奏）。
    #
    # 但也不能简单反过来写 max(SEND_INTERVAL_MAX, gap_max)：gap_max 现在是 2，
    # 那样会把间隔顶到 4 秒，10 人从约 30 秒拖到 35 秒以上，与「1 分钟内发完」冲突。
    # 正确语义只有一条 —— 配置说了算。gap_max 由 _run_send_impl 从
    # data/config.json 读出，且已保证 >= gap_min >= 1；SEND_INTERVAL_MAX
    # 仅在它缺失（0/None）时才兜底。
    cap = float(gap_max) if gap_max else SEND_INTERVAL_MAX
    floor = SEND_INTERVAL_MIN if gap_min is None else max(1.0, float(gap_min))
    hi = max(floor, cap)
    center = max(floor, min(hi, avg))
    # 抖动后必须再次夹紧：否则 ±25% 会突破 hi 上限
    jittered = center * random.uniform(0.75, 1.25)
    return max(floor, min(hi, jittered))


def _record_browser_session_failure(result: dict) -> None:
    """Stop a broken session once; never replay a contact within this batch."""
    reason = str(BrowserSessionUnavailable())
    result["browser_unavailable"] = True
    result["failed"].append({"name": "_system", "reason": reason})
    logger.warning("%s", reason)
    _close_prewarmed()


def _run_send_impl(dry_run: bool = False, only_names: list[str] | None = None, only_ids: list[str] | None = None,
                   force: bool = False, *, selected_snapshot: list[dict] | None = None) -> dict:
    """主入口：从好友台账读取勾选目标，逐个发送（在专用工作线程内运行）。"""
    cfg = load_config()
    messages = cfg.get("messages") or ["🔥"]
    # max_friends_per_run=0 在配置里表示「不限制」，必须保留 0 的语义，
    # 不能被 `or 20` 换成 20 —— 否则「不限制」这一档永远失效。
    max_n = max(0, _cfg_int(cfg, "max_friends_per_run", 20))
    gap_min = max(1, _cfg_int(cfg, "send_gap_min", 6))
    gap_max = max(gap_min, _cfg_int(cfg, "send_gap_max", 12))

    result = {
        "at": _now(), "dry_run": bool(dry_run),
        "ok": [], "ok_entries": [], "failed": [], "skipped": [], "deferred": [],
        "logged_out": False, "rate_limited": False, "total": 0, "done": 0,
        "delivery_unconfirmed": False,
    }
    planned_total = 0
    active_target_names: list[str] = []
    active_targets: list[dict] = []

    targets = ledger.get_selected() if selected_snapshot is None else list(selected_snapshot)
    if selected_snapshot is None and not targets and cfg.get("friends"):
        stats = ledger.import_config_friends(cfg["friends"], only_if_missing=True)
        logger.info("已从 config.friends 迁移进台账：新增 %s 人，勾选 %s 人",
                     stats["added"], stats["selected"])
        targets = ledger.get_selected()
    if only_ids is not None:
        targets = [t for t in targets if t.get("entry_id") in set(only_ids)]
    elif only_names is not None:
        targets = [t for t in targets if t.get("display_name") in only_names and not t.get("identity_ambiguous")]
    planned_total = len(targets)
    if not dry_run and not force:
        current = business_now()
        pending = []
        for target in targets:
            code = daily_skip_code(target, now=current)
            if code:
                result["skipped"].append({"entry_id": target.get("entry_id", ""),
                                          "name": target.get("display_name", ""), "code": code,
                                          "retryable": False,
                                          "reason": "今天已发，已自动跳过" if code == "already_sent_today" else "存在发送结果待核对，已自动跳过；请先人工确认"})
            else:
                pending.append(target)
        targets = pending
        if not targets and result["skipped"]:
            # 当日保护无需读取凭证或打开浏览器；全部已发属于健康的幂等运行。
            result["total"] = planned_total
            result["done"] = completed_count(result)
            return result
    if only_names is None and only_ids is None and max_n > 0 and len(targets) > max_n:
        overflow = targets[max_n:]
        result["deferred"].extend({
            "entry_id": t.get("entry_id", ""),
            "retryable": True,
            "name": t.get("display_name", ""),
            "reason": f"超过单轮上限 {max_n}，等待后续处理",
        } for t in overflow if t.get("display_name"))
        targets = targets[:max_n]
    active_targets = list(targets)
    active_target_names = [
        str(t.get("display_name", "")) for t in targets if t.get("display_name")
    ]

    if not targets:
        if dry_run:
            logger.info("没有勾选任何好友，演练未执行发送")
        else:
            logger.warning("没有勾选任何好友，本轮未执行发送")
            result["failed"].append({
                "name": "_system", "code": "no_selected_contacts", "retryable": False,
                "reason": "没有勾选任何好友；未尝试发送",
            })
        return result

    if not get_valid_state_path():
        result["credentials_missing"] = True
        result["failed"].append({"name": "_system", "code": "credentials_missing", "retryable": False,
                                  "reason": "尚未上传登录态 state.json；未尝试发送"})
        result["deferred"].extend({"entry_id": t.get("entry_id", ""), "name": t["display_name"],
                                   "code": "credentials_missing", "retryable": True,
                                   "reason": "缺少凭证，尚未尝试；恢复凭证后可继续"} for t in targets)
        result["total"] = planned_total
        result["done"] = completed_count(result)
        return result

    using_prewarmed = False
    browser_ctx = None
    p = browser = context = page = None
    try:
        _check_send_stop()
        # 优先使用预启动的浏览器（预启动已打开页面并等待联系人列表，节省数分钟）
        prewarmed = _get_prewarmed_page()
        if prewarmed:
            p, browser, context, page = (
                prewarmed["p"], prewarmed["browser"], prewarmed["context"], prewarmed["page"]
            )
            using_prewarmed = True
            try:
                logged, why = check_login(page)
            except Exception as e:
                try:
                    raise_if_browser_unavailable(e)
                except BrowserSessionUnavailable:
                    logger.warning("预启动会话在发送前已关闭，关闭缓存并重建")
                else:
                    logger.warning("预启动 page 探活异常（%s），关闭后改走冷启动", type(e).__name__)
                # page 已崩（TargetClosedError 等）：get_browser_status 只看缓存标志、
                # 探不出这种情况。关闭预启动实例，回落到下面的冷启动分支重建，
                # 而不是让异常冒泡成「运行异常」直接结束本轮。
                _close_prewarmed()
                using_prewarmed = False
                prewarmed = None
                p = browser = context = page = None
            else:
                if not logged:
                    result["logged_out"] = True
                    result["failed"].append({"name": "_system", "reason": why})
                    _screenshot(page)
                    return result
                logger.info("使用预启动浏览器，待发送好友 %s 人，dry_run=%s", len(targets), dry_run)

        if not using_prewarmed:
            # 冷启动兜底（无预启动实例，或预启动 page 探活异常时）
            logger.info("未检测到可用预启动浏览器，执行冷启动...")
            browser_ctx = open_browser()
            p, browser, context, page = browser_ctx.__enter__()
            if not _open_chat_page(page):
                result["failed"].append({"name": "_system", "reason": "无法打开抖音私信页面"})
                return result

            # 智能等待：DOM 加载 + 输入框出现
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                logger.debug('发送页面初始等待失败，继续就绪检查')
            try:
                page.wait_for_selector("input", timeout=8000)
            except Exception:
                time.sleep(2)
            logged, why = check_login(page)
            if not logged:
                result["logged_out"] = True
                result["failed"].append({"name": "_system", "reason": why})
                _screenshot(page)
                return result

            # 阻塞等待左侧联系人列表渲染完成（最多 3 轮，每轮 45 秒）
            list_ready = False
            for _ in range(3):
                try:
                    page.wait_for_selector(".conversationConversationItemtitle", timeout=45000)
                    list_ready = True
                    break
                except Exception:
                    logger.info("联系人列表尚未出现，刷新后重试等待")
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=90000)
                        try:
                            page.wait_for_selector("input", timeout=10000)
                        except Exception:
                            time.sleep(3)
                    except Exception:
                        logger.debug('发送页面重载失败，继续后续检查')
            if list_ready:
                logger.info("联系人列表已加载完成")
                time.sleep(0.5)
            else:
                logger.warning("等待联系人列表超时，仍尝试继续发送")
            logger.info("冷启动完成，待发送好友 %s 人，dry_run=%s", len(targets), dry_run)

        # 按好友在会话列表中的上下位置排序，使定位滚动基本单调向下，
        # 避免在虚拟列表顶部/底部之间来回滚动（底部好友统一留到最后）
        targets = _order_targets_by_list(page, targets)
        logger.info("发送顺序（按列表位置）：%s",
                     [t.get("display_name") for t in targets])

        # ★ 发送循环：预启动 / 冷启动两条路径共用（修复原先预启动成功反而不发送的 bug）
        # 建立时间预算：整轮在 SEND_BUDGET_SECONDS 内完成（0 表示不限制，走原随机间隔）
        # 补发任务只包含上一轮未完成的联系人，不再套第二次 60 秒预算；否则同一批
        # 联系人会再次被截断，而每日只安排一次补发，最终仍会漏发。
        deadline = (
            time.time() + SEND_BUDGET_SECONDS
            if SEND_BUDGET_SECONDS > 0 and only_names is None and only_ids is None
            else None
        )
        n_targets = len(targets)
        if deadline is not None:
            logger.info("本轮发送预算 %s 秒，目标 %s 人（人均约 %.1f 秒）",
                        SEND_BUDGET_SECONDS, n_targets, SEND_BUDGET_SECONDS / max(1, n_targets))

        # 实时进度（前端运行中轮询显示）；收尾在 finally 统一清空
        update_runtime(send_progress={
            "total": n_targets, "done": 0, "current": None, "ok": 0, "failed": 0,
        })

        for idx, entry in enumerate(targets):
            _check_send_stop()
            cur_name = entry.get("display_name", "")
            update_runtime(send_progress={
                "total": n_targets, "done": idx, "current": cur_name,
                "ok": len(result["ok"]), "failed": len(result["failed"]),
            })
            # The inter-contact gap was already reserved and waited below.
            # Recheck only the next action cost here, avoiding a double gap.
            # 余量用于吸收固定开销估算误差——否则最后一个人的开销会落在预算之外
            # （实测：余量 1.15 时 45 秒档位会到 45.8 秒；1.35 可全覆盖各档位）。
            if deadline is not None and idx > 0:
                left = deadline - time.time()
                if left < _PER_FRIEND_FIXED_COST * 1.35:
                    remaining_targets = targets[idx:]
                    result["deferred"].extend({
                        "entry_id": t.get("entry_id", ""),
                        "retryable": True,
                        "name": t.get("display_name", ""),
                        "reason": f"本轮 {SEND_BUDGET_SECONDS} 秒预算不足，等待后续处理",
                    } for t in remaining_targets if t.get("display_name"))
                    result["skipped"].append({
                        "name": "_system",
                        "reason": (f"本轮 {SEND_BUDGET_SECONDS} 秒预算不足以容纳后续 "
                                   f"{n_targets - idx} 人，等待后续处理"),
                    })
                    logger.warning("预算不足（剩余 %.1fs），跳过后续 %s 人，等待后续处理",
                                   left, n_targets - idx)
                    break

            msg = build_message(messages, last_sent_msg=str(entry.get("last_msg", "")))
            previous_force = getattr(_send_context, "force", False)
            _send_context.force = force
            try:
                if entry.get("has_conversation"):
                    _send_consumer(page, entry, msg, dry_run, result)
                else:
                    _send_creator(entry, msg, dry_run, result, p)
            finally:
                _send_context.force = previous_force
            progress()

            if result.get("delivery_unconfirmed") or result.get("delivery_rejected") or result.get("intent_write_failed"):
                result["deferred"].extend({
                    "entry_id": t.get("entry_id", ""),
                    "retryable": True,
                    "name": t.get("display_name", ""),
                    "reason": "前一位发送结果待核对，本轮尚未尝试该好友",
                } for t in targets[idx + 1:] if t.get("display_name"))
                break

            if result["rate_limited"]:
                # 当前好友已在 failed 中；后续尚未尝试的联系人要显式进入补发队列。
                result["deferred"].extend({
                    "entry_id": t.get("entry_id", ""),
                    "retryable": True,
                    "name": t.get("display_name", ""),
                    "reason": "本轮触发限流，等待后续处理",
                } for t in targets[idx + 1:] if t.get("display_name"))
                break

            remaining = n_targets - idx - 1
            if remaining <= 0:
                break

            # 预算耗尽：停止本轮
            if deadline is not None and deadline - time.time() < gap_min + _PER_FRIEND_FIXED_COST * 1.35:
                result["deferred"].extend({
                    "entry_id": t.get("entry_id", ""),
                    "retryable": True,
                    "name": t.get("display_name", ""),
                    "reason": f"本轮 {SEND_BUDGET_SECONDS} 秒预算已用尽，等待后续处理",
                } for t in targets[idx + 1:] if t.get("display_name"))
                result["skipped"].append({
                    "name": "_system",
                    "reason": f"本轮 {SEND_BUDGET_SECONDS} 秒预算已用尽，剩余 {remaining} 人等待后续处理",
                })
                logger.warning("发送预算已用尽，剩余 %s 人本轮跳过（等待后续处理）", remaining)
                break

            event = getattr(_send_context, "stop_event", None)
            if deadline is None:
                # 未设预算：保持原有随机间隔行为
                wait = random.uniform(max(1.0, gap_min), max(gap_min, gap_max))
            else:
                wait = _send_interval(deadline, remaining, gap_max, gap_min=gap_min)
                # The prior guard ensures this cap can still honor gap_min.
                wait = min(wait, max(gap_min, deadline - time.time() - _PER_FRIEND_FIXED_COST * 1.35))
            event.wait(wait) if event is not None else time.sleep(wait)

        if deadline is not None:
            logger.info("本轮发送结束，用时 %.1f 秒（预算 %s 秒）",
                        time.time() - (deadline - SEND_BUDGET_SECONDS), SEND_BUDGET_SECONDS)
    except SendStopped:
        result["stopped"] = True
    except BrowserSessionUnavailable:
        _record_browser_session_failure(result)
    except Exception as e:
        try:
            raise_if_browser_unavailable(e)
        except BrowserSessionUnavailable:
            _record_browser_session_failure(result)
        else:
            logger.error("运行异常: %s", e)
            result["failed"].append({"name": "_system", "reason": f"运行异常: {e}"})
    finally:
        event = getattr(_send_context, "stop_event", None)
        if event is not None and event.is_set():
            result["stopped"] = True
        # 浏览器启动、页面加载等系统级故障可能发生在第一个好友之前。旧逻辑只留下
        # _system，补发器拿不到姓名，整批联系人就静默漏发。把尚未产生明确结果的
        # 活跃目标列为 deferred；登录态失效时虽不会立刻补发，也能如实呈现待处理量。
        has_system_failure = any(
            isinstance(item, dict) and item.get("name") == "_system"
            for item in result.get("failed", [])
        )
        if (has_system_failure or result.get("stopped")) and active_target_names:
            resolved_ids = set().union(*(contact_entry_ids(result.get(field, [])) for field in ("ok_entries", "failed", "skipped", "deferred")))
            resolved_names = resolved_contact_names(result)
            result["deferred"].extend(
                {"entry_id": t.get("entry_id", ""), "name": t["display_name"], "retryable": True,
                 "reason": "任务提前结束，尚未尝试，等待后续处理"}
                for t in active_targets
                if (t.get("entry_id") not in resolved_ids if t.get("entry_id") else t["display_name"] not in resolved_names)
            )
        if not dry_run:
            _protect_unresolved_results(result)
        result["total"] = planned_total
        result["done"] = completed_count(result)
        # 无论正常结束、预算 break、限流还是异常，都清空进度，避免前端卡在“进行中”
        try:
            update_runtime(send_progress=None)
        except Exception:
            logger.debug('发送进度清理失败，保留本轮结果并继续收尾')
        # 收尾策略（移植自 A 的性能优化）：
        # - 冷启动路径：由原 contextmanager 关闭；常驻时必须连 owner 一起保留。
        # - 预启动路径：成功且未限流时保留实例，供短时间内再次运行复用；
        #   失败/限流则立即关闭，避免带着异常页面继续用。
        if using_prewarmed:
            keep = (
                KEEP_PREWARMED_SECONDS > 0
                and not result.get("rate_limited")
                and not result.get("logged_out")
                and not result.get("browser_unavailable")
                and not result.get("delivery_unconfirmed")
                and not result.get("stopped")
                    and not result.get("delivery_rejected")
                and not has_system_failure
            )
            if not (keep and _keep_prewarmed_alive()):
                _close_prewarmed()
        elif browser_ctx is not None:
            if (KEEP_BROWSER_ALWAYS
                    and not result.get("rate_limited")
                    and not result.get("logged_out")
                    and not result.get("browser_unavailable")
                    and not result.get("delivery_unconfirmed")
                    and not result.get("stopped")
                    and not result.get("delivery_rejected")
                    and not has_system_failure
                    and browser is not None):
                # 常驻模式：把冷启动的浏览器转为预启动状态保存，下次直接复用
                global _prewarmed_browser
                _prewarmed_browser = {
                    "p": p, "browser": browser, "context": context, "page": page,
                    "ready": True, "created_at": time.time(),
                    "context_manager": browser_ctx,
                }
                logger.info("常驻模式：浏览器已保留，下次发送直接复用（免冷启动）")
            else:
                try:
                    browser_ctx.__exit__(None, None, None)
                except Exception:
                    logger.debug('发送浏览器上下文退出失败，继续返回本轮结果')
    for field in ("failed", "skipped", "deferred"):
        for item in result.get(field, []):
            if isinstance(item, dict) and item.get("name") != "_system":
                matches = [t for t in active_targets if t.get("display_name") == item.get("name")]
                if not item.get("entry_id") and len(matches) == 1:
                    item["entry_id"] = matches[0].get("entry_id", "")
                item.setdefault("retryable", field == "deferred")
    return result


# ── 浏览器常驻模式管理 ──────────────────────────────────────────────────────

def get_browser_status() -> dict:
    """查询当前浏览器状态（可在任意线程调用，只读元数据不操作 page）。"""
    if _prewarmed_browser and _prewarmed_browser.get("ready"):
        age = time.time() - _prewarmed_browser.get("created_at", 0)
        return {
            "alive": True,
            "age_seconds": int(age),
            "keep_always": KEEP_BROWSER_ALWAYS,
            "max_idle_seconds": BROWSER_MAX_IDLE_SECONDS,
        }
    return {"alive": False, "keep_always": KEEP_BROWSER_ALWAYS}


def restart_browser(timeout: float | None = None) -> bool:
    """关闭当前浏览器；凭据更新可设置等待上限，失败时不得替换登录态。"""
    def _do_close():
        return _close_prewarmed(strict=True)
    try:
        return _pw_executor.submit(_do_close).result(timeout=timeout)
    except Exception as e:
        logger.warning("关闭浏览器失败：%s", e)
        return False


# 别名兼容
sync_contacts = fetch_chat_contacts
