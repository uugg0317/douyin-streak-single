# -*- coding: utf-8 -*-
"""抖音续火花 · 桌面版启动器（系统托盘）

职责：
1. 把工作目录锚定到 exe 所在目录（打包后 __file__ 不可靠）；
2. 解析并注入 PLAYWRIGHT_BROWSERS_PATH，让 Playwright 找到随包分发的 Chromium；
3. 探测可用端口，后台线程跑 uvicorn（FastAPI + 管理后台）；
4. 主线程跑系统托盘图标：打开管理后台 / 提取登录凭证 / 重启服务 / 退出；
5. 任何情况下保证进程能干净退出，不留僵尸 Chromium。

约束：PyInstaller 必须用 --onedir。--onefile 会把自身解压到临时目录，
Chromium 的相对路径失效，且每次启动都要解压 300MB+。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import webbrowser

APP_NAME = "抖音续火花"
HOST = "127.0.0.1"
DEFAULT_PORT = 8000
PORT_SCAN_RANGE = 10          # 8000 被占就顺延试 8001..8009
CHROMIUM_DIR_NAME = "playwright-browsers"
SERVER_READY_TIMEOUT = 40.0


# ─────────────────────────────────────────────────────────────────────────────
# 1. 运行时环境
# ─────────────────────────────────────────────────────────────────────────────

def app_dir() -> str:
    """程序根目录。

    --onedir 打包后 sys.executable 指向 dist/DouyinStreak/DouyinStreak.exe，
    用户数据放在 exe 旁边；只读资源由 PyInstaller 放在 _internal/。
    源码直跑时就是 desktop/ 的上一级。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = app_dir()
RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)


def anchor_cwd() -> None:
    """锚定工作目录。

    必须做：双击 exe 时 cwd 可能是 C:\\Windows\\System32，
    而 app.py 的 _load_env() 与 nginx 示例都按相对路径找文件。
    """
    try:
        os.chdir(BASE_DIR)
    except OSError:
        pass


def load_desktop_env() -> None:
    """在选择端口前加载 exe 同级 .env；应用稍后仍会用同一规则再次幂等加载。"""
    env_path = os.environ.get("ENV_FILE_PATH") or os.path.join(BASE_DIR, ".env")
    try:
        with open(env_path, encoding="utf-8") as stream:
            for raw in stream:
                raw = raw.strip()
                if raw and not raw.startswith("#") and "=" in raw:
                    key, value = raw.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
    except OSError:
        pass


def setup_browsers_path() -> str | None:
    """让 Playwright 使用随包分发的 Chromium。

    Playwright 支持 PLAYWRIGHT_BROWSERS_PATH 指向自定义目录；设为 0 表示
    「装进包内」，但打包后解析不可靠，所以显式指向 exe 旁边的目录更稳。
    """
    for root in (RESOURCE_DIR, BASE_DIR):
        local = os.path.join(root, CHROMIUM_DIR_NAME)
        if os.path.isdir(local):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = local
            return local
    # 没随包分发就退回默认位置（%LOCALAPPDATA%\\ms-playwright 等），
    # 让 Playwright 自己去 %USERPROFILE% 下找。
    return None


def free_port(preferred: int = DEFAULT_PORT) -> int:
    """找一个能绑上的端口；全被占则抛错，避免静默失败。"""
    for port in range(preferred, preferred + PORT_SCAN_RANGE):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError(
        f"{preferred}~{preferred + PORT_SCAN_RANGE - 1} 全部被占用，"
        "请关闭占用这些端口的程序后重试"
    )


def port_in_use_by_us(port: int) -> bool:
    """端口上的服务确实是本应用才视为已有实例。"""
    try:
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(
            f"http://{HOST}:{port}/api/health", timeout=1.0
        ) as response:
            payload = json.load(response)
        return payload.get("ok") is True and payload.get("app") == "sparkkeeper"
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 2. 服务
# ─────────────────────────────────────────────────────────────────────────────

class ServerThread(threading.Thread):
    """在后台线程里跑 uvicorn。

    uvicorn.run() 会自己接管信号处理，在非主线程里跑不干净；所以直接用
    uvicorn.Server + Config，并显式设置 should_exit 来优雅停机。
    """

    def __init__(self, port: int) -> None:
        super().__init__(name="uvicorn", daemon=True)
        self.port = port
        self._server = None
        self._error: BaseException | None = None

    def run(self) -> None:
        try:
            import uvicorn

            from app import app as fastapi_app

            config = uvicorn.Config(
                fastapi_app,
                host=HOST,
                port=self.port,
                log_level="info",
                access_log=False,      # 本地自用，关掉访问日志省点 IO
            )
            self._server = uvicorn.Server(config)
            self._server.run()
        except BaseException as e:      # noqa: BLE001 - 要连 SystemExit 一起兜住
            self._error = e

    @property
    def error(self) -> BaseException | None:
        return self._error

    def stop(self, timeout: float = 8.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        self.join(timeout=timeout)


def wait_until_ready(port: int, timeout: float = SERVER_READY_TIMEOUT) -> bool:
    """等服务真的开始监听，避免浏览器打开时是白页。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((HOST, port)) == 0:
                return True
        time.sleep(0.25)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 3. 图标
# ─────────────────────────────────────────────────────────────────────────────

def build_icon_image(hot: bool = False):
    """生成托盘图标。

    优先用项目自带的 fire-icon.png（登录门也在用它，风格一致），
    读不到就画一个火焰色圆点兜底，绝不因为缺图标而启动失败。
    """
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if hot:
        bg, flame = (192, 38, 38), (255, 236, 210)     # 红色：正在发送
    else:
        bg, flame = (24, 20, 16), (255, 107, 53)       # 深底 + 火花橙

    d.ellipse((2, 2, size - 2, size - 2), fill=bg)

    src = os.path.join(RESOURCE_DIR, "static", "fire-icon.png")
    if os.path.isfile(src):
        try:
            with Image.open(src) as im:
                im = im.convert("RGBA")
                im.thumbnail((size - 16, size - 16), Image.LANCZOS)
                img.alpha_composite(im, ((size - im.width) // 2,
                                         (size - im.height) // 2))
                return img
        except Exception:
            pass

    # 兜底：一个火焰形状的近似多边形
    d.polygon(
        [(32, 12), (40, 26), (46, 34), (46, 44), (32, 54), (18, 44), (18, 34), (26, 24)],
        fill=flame,
    )
    d.ellipse((26, 34, 38, 48), fill=bg)
    return img


# ─────────────────────────────────────────────────────────────────────────────
# 4. 托盘
# ─────────────────────────────────────────────────────────────────────────────

def open_admin(port: int) -> None:
    webbrowser.open(f"http://{HOST}:{port}/")


def open_credentials_tab(port: int) -> None:
    """直达凭证页的思路：管理后台是单页应用，用 hash/query 无法直接切 tab，
    所以直接打开根地址，由用户点「凭证」。这里额外把提取接口提示出来。"""
    webbrowser.open(f"http://{HOST}:{port}/")


def kill_child_browsers() -> None:
    """退出时清理本程序拉起的 Chromium。

    项目有「浏览器常驻」逻辑（KEEP_BROWSER_ALWAYS / KEEP_PREWARMED_SECONDS），
    托盘退出时如果不收尾，Chromium 会变成孤儿进程一直占内存。
    只杀本进程的子进程，不动用户自己的 Chrome。
    """
    try:
        import psutil
    except ImportError:
        return
    try:
        me = psutil.Process()
        for child in me.children(recursive=True):
            name = (child.name() or "").lower()
            if "chrome" in name or "chromium" in name or "node" in name:
                try:
                    child.kill()
                except Exception:
                    pass
    except Exception:
        pass


class TrayApp:
    def __init__(self, port: int) -> None:
        self.port = port
        self.server: ServerThread | None = None
        self._icon = None
        self._stopping = False

    # ── 服务生命周期 ────────────────────────────────────────────────────
    def start_server(self) -> bool:
        self.server = ServerThread(self.port)
        self.server.start()
        if not wait_until_ready(self.port):
            err = self.server.error
            self.notify("服务启动失败", str(err) if err else "等待监听超时")
            return False
        return True

    def restart_server(self, icon=None, item=None) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None
        time.sleep(0.6)
        if self.start_server():
            self.notify("服务已重启", f"地址 http://{HOST}:{self.port}/")

    def stop_everything(self, icon=None, item=None) -> None:
        if self._stopping:
            return
        self._stopping = True
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
        if self.server is not None:
            self.server.stop()
        kill_child_browsers()

    def notify(self, title: str, message: str) -> None:
        """Windows 气泡通知；pystray 的 notify 在部分后端不可用，故整体兜住。"""
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title)
        except Exception:
            pass

    # ── 入口 ────────────────────────────────────────────────────────────
    def run(self, open_browser_on_start: bool = True) -> int:
        try:
            import pystray
        except ImportError as e:
            print(f"[致命] 缺少 pystray：{e}")
            print("请先执行： pip install pystray pillow")
            return 2

        if not self.start_server():
            err = self.server.error if self.server else None
            print(f"[致命] 服务启动失败：{err}")
            return 3

        print(f"[就绪] {APP_NAME} 已在 http://{HOST}:{self.port}/ 运行，托盘图标已就位。")

        if open_browser_on_start:
            open_admin(self.port)

        menu = pystray.Menu(
            pystray.MenuItem(
                "打开管理后台",
                lambda icon, item: open_admin(self.port),
                default=True,                      # 双击托盘图标即执行
            ),
            pystray.MenuItem(
                "提取登录凭证（扫码）",
                lambda icon, item: open_credentials_tab(self.port),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("重启服务", self.restart_server),
            pystray.MenuItem("退出", self.stop_everything),
        )

        self._icon = pystray.Icon(
            "douyin_streak",
            icon=build_icon_image(),
            title=f"{APP_NAME} · 127.0.0.1:{self.port}",
            menu=menu,
        )
        try:
            self._icon.run()          # 阻塞，直到 stop()
        except KeyboardInterrupt:
            self.stop_everything()
        finally:
            if not self._stopping:
                self.stop_everything()
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# 5. main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    anchor_cwd()
    load_desktop_env()

    browsers = setup_browsers_path()
    print(f"[信息] 程序目录：{BASE_DIR}")
    print(f"[信息] Chromium 目录：{browsers or '（随包未分发，回退到系统默认位置）'}")

    try:
        existing = int(os.environ.get("PORT", str(DEFAULT_PORT)))
        if not 1 <= existing <= 65526:
            raise ValueError
    except ValueError:
        print(f"[警告] PORT 配置非法，回退到 {DEFAULT_PORT}。")
        existing = DEFAULT_PORT

    # 单实例：端口已被监听就认为已在运行，直接打开后台，不再起第二个服务。
    if port_in_use_by_us(existing):
        print(f"[信息] 检测到 {existing} 端口上已有服务，直接打开管理后台。")
        open_admin(existing)
        return 0

    try:
        port = free_port(existing)
    except RuntimeError as e:
        print(f"[致命] {e}")
        return 4

    if port != existing:
        print(f"[信息] {existing} 被占用，改用 {port}。")

    app = TrayApp(port)
    try:
        return app.run(open_browser_on_start=True)
    except Exception as e:                        # noqa: BLE001
        print(f"[致命] 未捕获异常：{e!r}")
        app.stop_everything()
        return 1


if __name__ == "__main__":
    sys.exit(main())
