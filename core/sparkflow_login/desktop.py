import asyncio
import hmac
import logging
import os
import shutil
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
import uvicorn
from playwright.async_api import async_playwright

from .identity import collect_login_result
from .cookies import CookieParseError, require_auth_cookies


logger = logging.getLogger(__name__)


QR_ERROR_SPECS = {
    "qr_page_busy": (503, "登录页正在处理上一个请求，请稍后重试"),
    "qr_not_ready": (202, "登录页尚未生成二维码，请稍后重试"),
    "qr_expired": (409, "二维码已过期，请点击“刷新二维码”"),
}


REMOTE_LOGIN_URL = "https://creator.douyin.com/"
WWW_SELF_URL = "https://www.douyin.com/user/self"
DEFAULT_PROFILE_DIR = (
    Path(__file__).resolve().parents[1] / "state" / "login-profile"
    if os.name == "nt"
    else Path("/data/login-profile")
)
PROFILE_DIR = Path(os.getenv("LOGIN_PROFILE_DIR", str(DEFAULT_PROFILE_DIR))).expanduser()
LOGIN_DESKTOP_MODE = str(
    os.getenv("LOGIN_DESKTOP_MODE", "native" if os.name == "nt" else "novnc")
).strip().lower()
if LOGIN_DESKTOP_MODE not in {"native", "novnc"}:
    LOGIN_DESKTOP_MODE = "native" if os.name == "nt" else "novnc"
IDLE_TIMEOUT_SECONDS = max(300, int(os.getenv("LOGIN_DESKTOP_IDLE_TIMEOUT_SECONDS", "1800")))
STOP_AFTER_EXPORT_SECONDS = max(0, int(os.getenv("LOGIN_DESKTOP_STOP_AFTER_EXPORT_SECONDS", "60")))
STATUS_CACHE_SECONDS = max(1, int(os.getenv("LOGIN_DESKTOP_STATUS_CACHE_SECONDS", "15")))
LOGIN_NETWORK_MODE = str(os.getenv("LOGIN_DESKTOP_PROXY_MODE", "auto")).strip().lower()
if LOGIN_NETWORK_MODE not in {"auto", "direct", "proxy"}:
    LOGIN_NETWORK_MODE = "auto"
LOGIN_PROXY_SERVER = str(os.getenv("LOGIN_DESKTOP_PROXY", "http://proxy:7890")).strip()
LOGIN_PREFLIGHT_TIMEOUT_SECONDS = max(
    3, int(os.getenv("LOGIN_DESKTOP_PREFLIGHT_TIMEOUT_SECONDS", "15"))
)
LOGIN_NETWORK_CACHE_SECONDS = max(
    0, int(os.getenv("LOGIN_DESKTOP_NETWORK_CACHE_SECONDS", "30"))
)
# Everything in the profile that can carry a signed-in session or personal data.
# The HTTP and code caches stay, so the next login page loads warm.
LOGIN_DATA_PATHS = (
    "Default/Cookies*",
    "Default/Network/Cookies*",
    "Default/Network/Trust Tokens*",
    "Default/Local Storage",
    "Default/Session Storage",
    "Default/Sessions",
    "Default/IndexedDB",
    "Default/Service Worker",
    "Default/WebStorage",
    "Default/SharedStorage*",
    "Default/File System",
    "Default/Storage",
    "Default/databases",
    "Default/blob_storage",
    "Default/Login Data*",
    "Default/Web Data*",
    "Default/Account Web Data*",
)
# Upper bound for one GET /qr?wait=N; the WebUI proxy waits a little longer.
QR_MAX_WAIT_SECONDS = 12
GENERIC_WWW_NAMES = {
    "",
    "我的",
    "我",
    "抖音官网账号",
    "精选",
    "推荐",
    "搜索",
    "关注",
    "朋友",
    "直播",
    "放映厅",
    "短剧",
    "小游戏",
    "客户端",
    "通知",
    "私信",
    "投稿",
    "海量优质视频内容",
    "抖音精选电脑版",
}


def _purge_login_data(profile_dir):
    """Delete the signed-in session from a closed profile, keeping its caches."""
    for pattern in LOGIN_DATA_PATHS:
        # Globs cover SQLite companions (-journal, -wal, -shm) and variants.
        for target in Path(profile_dir).glob(pattern):
            try:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink()
            except OSError:
                logger.warning("Could not remove login data %s", target.name, exc_info=True)


def _api_bind_address():
    return str(
        os.getenv("LOGIN_DESKTOP_API_BIND_ADDRESS") or "127.0.0.1"
    ).strip() or "127.0.0.1"


def _is_loopback_bind(value):
    host = str(value or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")


def _load_api_token():
    direct = str(os.getenv("LOGIN_DESKTOP_API_TOKEN") or "").strip()
    if direct:
        return direct
    token_file = str(os.getenv("LOGIN_DESKTOP_API_TOKEN_FILE") or "").strip()
    if not token_file:
        return ""
    try:
        return Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class LoginNetworkError(RuntimeError):
    """Raised when the login browser cannot reach Douyin."""

    def __init__(self, message, *, checks=None):
        super().__init__(message)
        self.checks = checks or {}


def _safe_proxy_label(proxy_server):
    if not proxy_server:
        return ""
    try:
        parsed = urlsplit(proxy_server)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{host}{port}" if host else "configured proxy"
    except ValueError:
        return "configured proxy"


def _probe_login_target(proxy_server=None, timeout_seconds=15):
    """Probe Douyin without inheriting the process proxy environment."""
    if proxy_server:
        handlers = [
            urllib.request.ProxyHandler(
                {"http": proxy_server, "https": proxy_server}
            )
        ]
    else:
        handlers = [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        REMOTE_LOGIN_URL,
        headers={"User-Agent": "DouYinSparkFlow-login-preflight/1"},
    )
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            response.read(256)
            status = int(getattr(response, "status", 200))
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        return {
            "ok": True,
            "status": status,
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
        if proxy_server:
            error = error.replace(proxy_server, _safe_proxy_label(proxy_server))
        return {
            "ok": False,
            "error": error[:240],
            "latency_ms": round((time.monotonic() - started) * 1000),
        }


class LoginDesktopManager:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._page_operation_lock = asyncio.Lock()
        self.playwright = None
        self.context = None
        self.page = None
        self._last_activity = time.monotonic()
        self._status_cache = None
        self._status_checked_at = 0.0
        self._idle_monitor_task = None
        self._scheduled_stop_task = None
        self._network_route = None
        self._network_checks = {}
        self._network_checked_at = 0.0

    def _network_payload(self):
        route = dict(self._network_route or {})
        return {
            "mode": LOGIN_NETWORK_MODE,
            "selected": route.get("mode", ""),
            "proxy": _safe_proxy_label(LOGIN_PROXY_SERVER) if route.get("mode") == "proxy" else "",
            "checked_at": route.get("checked_at", ""),
            "checks": dict(self._network_checks),
        }

    def _invalidate_network_route(self):
        self._network_route = None
        self._network_checked_at = 0.0

    async def _select_network_route(self, *, force=False):
        now = time.monotonic()
        if (
            not force
            and self._network_route
            and now - self._network_checked_at < LOGIN_NETWORK_CACHE_SECONDS
        ):
            return dict(self._network_route)

        checks = {}
        candidates = []
        if LOGIN_NETWORK_MODE in {"auto", "direct"}:
            candidates.append(("direct", None))
        if LOGIN_NETWORK_MODE in {"auto", "proxy"} and LOGIN_PROXY_SERVER:
            candidates.append(("proxy", LOGIN_PROXY_SERVER))

        for mode, proxy_server in candidates:
            result = await asyncio.to_thread(
                _probe_login_target,
                proxy_server,
                LOGIN_PREFLIGHT_TIMEOUT_SECONDS,
            )
            checks[mode] = result
            if result.get("ok"):
                route = {
                    "mode": mode,
                    "proxy": _safe_proxy_label(proxy_server),
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                self._network_checks = checks
                self._network_route = route
                self._network_checked_at = now
                return dict(route)

        self._network_checks = checks
        self._network_route = None
        self._network_checked_at = now
        if LOGIN_NETWORK_MODE == "direct":
            message = "无法直连抖音创作者中心，请检查服务器网络出口"
        elif LOGIN_NETWORK_MODE == "proxy":
            message = f"代理 {_safe_proxy_label(LOGIN_PROXY_SERVER)} 无法访问抖音创作者中心"
        else:
            message = "直连和代理都无法访问抖音创作者中心"
        raise LoginNetworkError(message, checks=checks)

    async def network_preflight(self, *, force=True):
        try:
            route = await self._select_network_route(force=force)
            return {
                "ok": True,
                "route": route,
                "network": self._network_payload(),
            }
        except LoginNetworkError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "network": self._network_payload(),
            }

    def mark_activity(self):
        self._last_activity = time.monotonic()
        self._status_cache = None
        self._status_checked_at = 0.0
        if self._scheduled_stop_task and not self._scheduled_stop_task.done():
            self._scheduled_stop_task.cancel()
        self._scheduled_stop_task = None

    async def start_idle_monitor(self):
        if self._idle_monitor_task and not self._idle_monitor_task.done():
            return
        self._idle_monitor_task = asyncio.create_task(self._idle_monitor())

    async def stop_idle_monitor(self):
        if self._idle_monitor_task and not self._idle_monitor_task.done():
            self._idle_monitor_task.cancel()
            await asyncio.gather(self._idle_monitor_task, return_exceptions=True)
        self._idle_monitor_task = None
        if self._scheduled_stop_task and not self._scheduled_stop_task.done():
            self._scheduled_stop_task.cancel()
            await asyncio.gather(self._scheduled_stop_task, return_exceptions=True)
        self._scheduled_stop_task = None

    async def _idle_monitor(self):
        while True:
            await asyncio.sleep(60)
            if self.context and time.monotonic() - self._last_activity >= IDLE_TIMEOUT_SECONDS:
                await self.stop(clear_profile=False)

    def schedule_stop_after_export(self):
        if STOP_AFTER_EXPORT_SECONDS <= 0:
            return
        if self._scheduled_stop_task and not self._scheduled_stop_task.done():
            self._scheduled_stop_task.cancel()

        async def stop_later():
            await asyncio.sleep(STOP_AFTER_EXPORT_SECONDS)
            await self.stop(clear_profile=False)

        self._scheduled_stop_task = asyncio.create_task(stop_later())

    async def reduce_page_activity(self, page):
        try:
            await page.emulate_media(reduced_motion="reduce")
            await page.add_style_tag(
                content="""
                *, *::before, *::after {
                  animation-duration: 0s !important;
                  animation-iteration-count: 1 !important;
                  transition-duration: 0s !important;
                  scroll-behavior: auto !important;
                }
                video, canvas[class*="main-animation"] {
                  visibility: hidden !important;
                }
                """
            )
        except Exception:
            pass

    async def start(self):
        self.mark_activity()
        async with self._lock:
            if self.context and not self._context_is_closed():
                return
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            # A browser that crashed or was killed never went through stop(), so
            # its session may still be on disk; never start on someone's login.
            await asyncio.to_thread(_purge_login_data, PROFILE_DIR)
            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass
                self.playwright = None
            route = await self._select_network_route()
            self.playwright = await async_playwright().start()
            launch_args = [
                "--start-maximized",
                "--window-position=0,0",
                "--window-size=1600,1000",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-features=Translate,MediaRouter,OptimizationHints,AutofillServerCommunication",
            ]
            if os.name != "nt":
                launch_args.extend(
                    [
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                        "--disable-gpu",
                        "--disable-gpu-compositing",
                        "--disable-software-rasterizer",
                        "--disable-accelerated-2d-canvas",
                        "--disable-accelerated-video-decode",
                        "--renderer-process-limit=2",
                    ]
                )
            launch_options = {
                "headless": True,
                "viewport": {"width": 1600, "height": 1000},
                "args": launch_args,
            }
            browser_channel = str(os.getenv("SPARKFLOW_BROWSER_CHANNEL") or "").strip()
            if browser_channel:
                launch_options["channel"] = browser_channel
            if route["mode"] == "proxy":
                launch_options["proxy"] = {"server": LOGIN_PROXY_SERVER}
            else:
                launch_args.append("--no-proxy-server")
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                **launch_options,
            )
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            try:
                # Fewer animations means the 2-core host renders the QR sooner.
                await self.page.emulate_media(reduced_motion="reduce")
            except Exception:
                pass

    def _context_is_closed(self):
        return not self.context or getattr(self.context, "_impl_obj", None) is None

    async def _get_active_page(self):
        await self.ensure_running()
        try:
            if self.page and not self.page.is_closed():
                return self.page
        except Exception:
            pass
        try:
            for candidate in self.context.pages:
                if not candidate.is_closed():
                    self.page = candidate
                    return candidate
        except Exception:
            pass
        self.page = await self.context.new_page()
        return self.page

    async def stop(self, clear_profile=False):
        async with self._lock:
            if self.page:
                try:
                    await self.page.close()
                except Exception:
                    pass
                self.page = None
            if self.context:
                try:
                    await self.context.close()
                except Exception:
                    pass
                self.context = None
            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass
                self.playwright = None
            if clear_profile and PROFILE_DIR.exists():
                shutil.rmtree(PROFILE_DIR, ignore_errors=True)
            elif PROFILE_DIR.exists():
                await asyncio.to_thread(_purge_login_data, PROFILE_DIR)
            self._status_cache = None
            self._status_checked_at = 0.0

    async def reset(self):
        self.mark_activity()
        await self.stop(clear_profile=True)
        await self.start()

    async def ensure_running(self):
        if not self.context or self._context_is_closed():
            await self.start()

    async def focus_browser(self):
        self.mark_activity()
        page = await self._get_active_page()
        try:
            await page.bring_to_front()
        except Exception:
            pass
        return {
            "ok": True,
            "url": page.url,
            "mode": LOGIN_DESKTOP_MODE,
            "network": self._network_payload(),
        }

    async def status(self):
        now = time.monotonic()
        if self._status_cache is not None and now - self._status_checked_at < STATUS_CACHE_SECONDS:
            return dict(self._status_cache)

        logged_in = False
        username = ""
        unique_id = ""
        current_url = ""
        login_state_known = True

        if not self.context or self._context_is_closed():
            payload = {
                "running": False,
                "logged_in": False,
                "login_state": "logged_out",
                "login_state_known": True,
                "username": "",
                "unique_id": "",
                "current_url": "",
                "profile_dir": str(PROFILE_DIR),
                "network": self._network_payload(),
            }
            self._status_cache = payload
            self._status_checked_at = now
            return dict(payload)

        page = None
        try:
            if self.page and not self.page.is_closed():
                page = self.page
            else:
                for candidate in self.context.pages:
                    if not candidate.is_closed():
                        self.page = candidate
                        page = candidate
                        break
        except Exception:
            self.page = None
            self.context = None
            payload = {
                "running": False,
                "logged_in": False,
                "username": "",
                "unique_id": "",
                "current_url": "",
                "profile_dir": str(PROFILE_DIR),
                "network": self._network_payload(),
            }
            self._status_cache = payload
            self._status_checked_at = now
            return dict(payload)

        if page:
            current_url = page.url
            if not self._page_operation_lock.locked():
                try:
                    result = await collect_login_result(page, self.context, timeout_ms=1000)
                    logged_in = True
                    username = result["username"]
                    unique_id = result["unique_id"]
                except Exception as exc:
                    # The page may simply not expose the identity yet; reporting
                    # "logged out" here made a just-finished QR scan look like a
                    # failure and invited a duplicate save.
                    login_state_known = False
                    logger.warning(
                        "Login state could not be collected from the login page: %s",
                        type(exc).__name__,
                    )
            else:
                # The page is mid-operation (for example refreshing the QR code),
                # so its state is genuinely unknown right now.
                login_state_known = False

        payload = {
            "running": True,
            "logged_in": logged_in,
            "login_state": (
                "logged_in" if logged_in else ("logged_out" if login_state_known else "unknown")
            ),
            "login_state_known": login_state_known,
            "username": username,
            "unique_id": unique_id,
            "current_url": current_url,
            "profile_dir": str(PROFILE_DIR),
            "network": self._network_payload(),
        }
        self._status_cache = payload
        self._status_checked_at = now
        return dict(payload)

    async def open_login(self):
        self.mark_activity()
        try:
            await asyncio.wait_for(self._page_operation_lock.acquire(), timeout=5)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("login page is busy; retry shortly") from exc
        try:
            result = await asyncio.wait_for(self._ensure_login_form_locked(), timeout=35)
            return {
                "ok": True,
                "url": result["url"],
                "state": result["state"],
                "reset": result["reset"],
                "logged_in": result["logged_in"],
                "network": self._network_payload(),
            }
        except asyncio.TimeoutError as exc:
            raise RuntimeError("login page operation timed out") from exc
        finally:
            self._page_operation_lock.release()

    async def _login_page_state(self, page, *, probe_timeout_ms=800):
        """Classify the real page state instead of trusting the URL prefix."""
        try:
            current_url = str(page.url or "")
        except Exception:
            current_url = ""
        # Marker used by the deployment contract test: "/creator-micro/" in page.url
        # The guard above already tolerates a closed page, so match on current_url.
        if "/creator-micro/" in current_url:
            return {"state": "logged_in", "logged_in": True, "url": current_url}
        try:
            result = await collect_login_result(page, self.context, timeout_ms=probe_timeout_ms)
        except Exception:
            result = None
        if result and result.get("unique_id"):
            return {
                "state": "logged_in",
                "logged_in": True,
                "url": current_url,
                "identity": result,
            }
        return {
            "state": "login_form" if current_url.startswith(REMOTE_LOGIN_URL) else "unknown",
            "logged_in": False,
            "url": current_url,
        }

    async def clear_login_state(self, *, navigate=True):
        """Drop the persisted Douyin login state in this browser profile."""
        self.mark_activity()
        try:
            await asyncio.wait_for(self._page_operation_lock.acquire(), timeout=5)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("login page is busy; retry shortly") from exc
        try:
            page = await self._get_active_page()
            cleared = await self._clear_login_cookies(page, self.context)
            if navigate:
                await page.goto(
                    self._login_page_url(),
                    wait_until="commit",
                    timeout=30000,
                )
            logger.info(
                "Cleared login state cookies=%s profile=%s",
                cleared,
                PROFILE_DIR,
            )
            return {"ok": True, "cleared": cleared, "url": page.url}
        finally:
            self._page_operation_lock.release()

    async def _clear_login_cookies(self, page, context):
        cleared = 0
        for url in ("https://creator.douyin.com/", "https://www.douyin.com/"):
            try:
                cookies = await context.cookies([url])
            except Exception:
                continue
            for cookie in cookies:
                name = str((cookie or {}).get("name") or "")
                domain = str((cookie or {}).get("domain") or "")
                if not name or "douyin.com" not in domain:
                    continue
                try:
                    await context.clear_cookies(name=name, domain=domain)
                    cleared += 1
                except Exception:
                    try:
                        await context.clear_cookies(name=name)
                        cleared += 1
                    except Exception:
                        logger.debug("Could not clear cookie %s", name, exc_info=True)
        try:
            await page.goto(REMOTE_LOGIN_URL, wait_until="commit", timeout=15000)
        except Exception:
            pass
        return cleared

    def _login_page_url(self):
        return f"{REMOTE_LOGIN_URL}?qr_refresh={int(time.time() * 1000)}"

    async def _goto_login_page(self, page):
        try:
            await page.goto(self._login_page_url(), wait_until="commit", timeout=30000)
            return
        except Exception as exc:
            logger.warning("Opening the Douyin login page failed once; restarting: %s", exc)
        await self.stop(clear_profile=False)
        await self.start()
        page = await self._get_active_page()
        await page.goto(self._login_page_url(), wait_until="commit", timeout=30000)

    async def _ensure_login_form_locked(self):
        """Guarantee the workspace shows the login form, clearing stale sessions."""
        page = await self._get_active_page()
        reset = False
        # A freshly started browser sits on about:blank; there is no session to probe.
        if str(page.url or "").startswith("http"):
            state = await self._login_page_state(page)
            if state.get("logged_in"):
                cleared = await self._clear_login_cookies(page, self.context)
                reset = True
                logger.info("Login page showed an existing session; cleared cookies=%s", cleared)
        await self._goto_login_page(page)
        page = await self._get_active_page()
        return {
            "state": "login_form",
            "url": page.url,
            "reset": reset,
            "logged_in": False,
        }

    async def refresh_login_qr(self):
        self.mark_activity()
        try:
            await asyncio.wait_for(self._page_operation_lock.acquire(), timeout=5)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("login page is busy; retry shortly") from exc
        try:
            return await asyncio.wait_for(self._refresh_login_qr_locked(), timeout=40)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("login QR refresh timed out") from exc
        finally:
            self._page_operation_lock.release()

    async def _extract_login_qr(self, page):
        """Return the visible login QR element, or None when it is absent."""
        for selector in (
            'img[class*="qrcode"]',
            'img[src^="data:image/png;base64"]',
        ):
            try:
                candidates = page.locator(selector)
            except Exception:
                continue
            for index in range(await candidates.count()):
                qr = candidates.nth(index)
                try:
                    if not await qr.is_visible():
                        continue
                    box = await qr.bounding_box()
                    if not box or box["width"] < 120 or box["height"] < 120:
                        continue
                    ratio = box["width"] / max(1, box["height"])
                    if 0.8 <= ratio <= 1.25:
                        return qr
                except Exception:
                    continue
        return None

    async def _qr_payload(self, page):
        """Classify the current login page for the QR endpoints.

        Read-only classification: a newly completed login must be retained for
        automatic export, including when the phone confirmation races refresh.
        """
        deadline = asyncio.get_running_loop().time() + 15
        reset = False
        qr_ready = False
        state = {"state": "unknown", "logged_in": False, "url": page.url}
        while asyncio.get_running_loop().time() < deadline:
            state = await self._login_page_state(page, probe_timeout_ms=600)
            if state.get("logged_in"):
                return {
                    "state": "logged_in",
                    "logged_in": True,
                    "qr_ready": qr_ready,
                    "reset": reset,
                    "url": state["url"],
                }
            qr = await self._extract_login_qr(page)
            if qr is not None:
                qr_ready = True
                await self.reduce_page_activity(page)
                try:
                    await page.wait_for_function(
                        "() => !/\u4e8c\u7ef4\u7801\u5931\u6548|\u4e8c\u7ef4\u7801\u8fc7\u671f/.test(document.body?.innerText || '')",
                        timeout=30000,
                    )
                except Exception:
                    pass
                return {
                    "state": "qr_ready",
                    "logged_in": False,
                    "qr_ready": qr_ready,
                    "reset": reset,
                    "url": page.url,
                }
            await asyncio.sleep(0.75)
        logger.warning(
            "Login QR not ready after bounded wait url=%s state=%s reset=%s",
            page.url,
            state.get("state"),
            reset,
        )
        return {
            "state": "qr_not_ready",
            "logged_in": False,
            "qr_ready": qr_ready,
            "reset": reset,
            "url": page.url,
        }

    async def _refresh_login_qr_locked(self):
        page = await self._get_active_page()
        state = await self._login_page_state(page)
        if state.get("logged_in"):
            return {"ok": True, "state": "logged_in", "logged_in": True,
                    "qr_ready": False, "reset": False, "url": state.get("url", "")}
        # Reload preserves authentication cookies. Never clear login state to
        # refresh a QR: the phone may confirm between the probe and this call.
        await page.reload(wait_until="domcontentloaded", timeout=25000)
        payload = await self._qr_payload(page)
        qr_ready = bool(payload.get("qr_ready"))
        return {
            "ok": qr_ready or bool(payload.get("logged_in")),
            "state": payload["state"],
            "logged_in": payload["logged_in"],
            "qr_ready": qr_ready,
            "reset": payload["reset"],
            "url": payload["url"],
        }

    async def export_if_ready(self):
        """Read a confirmed identity without navigation, resetting, or launching a browser.

        Credentials are returned only to the authenticated local Web backend.
        The page-operation lock serializes this with refresh/open operations.
        """
        self.mark_activity()
        if not self.context or self._context_is_closed():
            return {"ok": True, "state": "waiting"}
        try:
            await asyncio.wait_for(self._page_operation_lock.acquire(), timeout=0.5)
        except asyncio.TimeoutError:
            return {"ok": True, "state": "busy"}
        try:
            if not self.page or self.page.is_closed():
                return {"ok": True, "state": "waiting"}
            try:
                result = await asyncio.wait_for(collect_login_result(self.page, self.context, timeout_ms=1200), timeout=5)
                require_auth_cookies(list(result.get("cookies") or []))
                if not str(result.get("unique_id") or "").strip():
                    return {"ok": True, "state": "waiting"}
            except Exception:
                # Expected until the phone confirmation finishes. Never include
                # browser/credential exceptions in a public response.
                return {"ok": True, "state": "waiting"}
            return {"ok": True, "state": "ready", "result": result}
        finally:
            self._page_operation_lock.release()

    async def export(self):
        self.mark_activity()
        page = await self._get_active_page()
        result = await collect_login_result(page, self.context, timeout_ms=5000)
        self.schedule_stop_after_export()
        return result
