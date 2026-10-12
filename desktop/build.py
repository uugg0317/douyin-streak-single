# -*- coding: utf-8 -*-
"""抖音续火花 · 桌面版打包脚本（PyInstaller --onedir）

做四件事：
1. 校验 Python 版本，并安装锁定依赖 + 桌面额外依赖（pystray / pillow / psutil / pyinstaller）；
2. 确保 Playwright 的 Chromium 内核存在（缺失就自动下载）；
3. 只把只读的 static/ 与 Chromium 打进包，不携带真实 .env 或用户数据；
4. 调用 PyInstaller 打包，最后打印产物路径与体积。

用法（在项目根目录）：
    python desktop/build.py

产物：dist/DouyinStreak/DouyinStreak.exe
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # 项目根目录
DIST_NAME = "DouyinStreak"
BUILD_DIR = os.path.join(HERE, "_build")
WORK_DIR = os.path.join(BUILD_DIR, "pyinstaller")
VENV_DIR = os.path.join(BUILD_DIR, ".venv")
PYTHON = os.path.join(VENV_DIR, "Scripts", "python.exe")
ENTRY = os.path.join(HERE, "launcher.py")

# 桌面版在 requirements.txt 之外还需要的东西
EXTRA_PACKAGES = [
    "pystray>=0.19.5",
    "pillow>=10.0.0",
    "psutil>=7.0.0",
    "pyinstaller>=6.6",
]

# 只读资源可以随包分发。凭据与用户数据必须由首次运行在 exe 旁边创建。
DATA_DIRS = ["static"]

# uvicorn / apscheduler 存在运行时动态导入，PyInstaller 的静态分析会漏
HIDDEN_IMPORTS = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "apscheduler.schedulers.background",
    "apscheduler.triggers.cron",
    "apscheduler.executors.pool",
    "playwright_stealth",
    "pystray._win32",
]


def title(msg: str) -> None:
    print()
    print("=" * 62)
    print(f" {msg}")
    print("=" * 62)


def run(cmd: list[str], **kwargs) -> None:
    print(f"  $ {' '.join(cmd)}")
    subprocess.run(cmd, check=True, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 环境
# ─────────────────────────────────────────────────────────────────────────────

def check_python() -> None:
    title("[1/4] 检查 Python")
    major, minor = sys.version_info[:2]
    print(f"  解释器：{sys.executable}")
    print(f"  版本  ：{major}.{minor}.{sys.version_info[2]}")
    if (major, minor) < (3, 10):
        print(f"\n[错误] 需要 Python 3.10+，当前是 {major}.{minor}。")
        sys.exit(1)
    if (major, minor) >= (3, 13):
        print("  [提示] 3.13 上 PyInstaller 支持较新，若打包失败请改用 3.11/3.12。")


def ensure_venv() -> None:
    """把构建环境隔离到 desktop/_build/.venv。

    理由：PyInstaller 打包时会分析**当前解释器**的 site-packages，用全局环境
    会把一堆无关包一起扫进去，产物体积暴涨且容易误收冲突依赖。
    独立 venv 还让「调试运行.bat」有确定的解释器可用。
    """
    global PYTHON

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if in_venv and os.path.normcase(sys.executable) == os.path.normcase(PYTHON):
        print("  [信息] 已在构建虚拟环境中，跳过创建。")
        return

    print(f"  创建虚拟环境：{os.path.dirname(os.path.dirname(PYTHON))}")
    os.makedirs(BUILD_DIR, exist_ok=True)
    if not os.path.isfile(PYTHON):
        subprocess.run([sys.executable, "-m", "venv", VENV_DIR], check=True)

    print("  切换解释器，重新进入构建流程（这一步会再跑一次本脚本）...")
    sys.stdout.flush()
    rc = subprocess.run([PYTHON, os.path.abspath(__file__)], cwd=ROOT).returncode
    sys.exit(rc)


def install_deps() -> None:
    title("[2/4] 安装依赖")
    lock = os.path.join(ROOT, "requirements.lock")
    req = lock if os.path.isfile(lock) else os.path.join(ROOT, "requirements.txt")
    if os.path.isfile(req):
        run([sys.executable, "-m", "pip", "install", "-q", "-r", req], cwd=ROOT)
    else:
        print(f"  [警告] 找不到 {req}，跳过项目依赖")

    # pystray 依赖 pillow；pyinstaller 是构建工具
    run([sys.executable, "-m", "pip", "install", "-q", *EXTRA_PACKAGES], cwd=ROOT)
    print("  依赖安装完成")


def ensure_chromium() -> str:
    """返回 Chromium 所在的 browsers 根目录（含 chromium-XXXX 子目录）。"""
    title("[3/4] 准备 Chromium 内核")
    try:
        sys.path.insert(0, ROOT)
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[错误] playwright 未安装成功，无法继续。")
        sys.exit(1)

    exe_path = ""
    try:
        with sync_playwright() as p:
            exe_path = p.chromium.executable_path or ""
    except Exception as e:                                    # noqa: BLE001
        print(f"  [信息] 读取 Chromium 路径失败（{e}），尝试先安装。")

    if not exe_path or not os.path.isfile(exe_path):
        print("  未检测到 Chromium，开始下载（约 150MB，请保持网络畅通）...")
        run([sys.executable, "-m", "playwright", "install", "chromium"], cwd=ROOT)
        with sync_playwright() as p:
            exe_path = p.chromium.executable_path

    # .../<browsers>/chromium-1148/chrome-win/chrome.exe
    #      ↑ 需要的是这一层，把它整个复制过去才能让 PLAYWRIGHT_BROWSERS_PATH 生效
    chrome_dir = os.path.dirname(os.path.abspath(exe_path))            # chrome-win
    version_dir = os.path.dirname(chrome_dir)                          # chromium-1148
    browsers_root = os.path.dirname(version_dir)                       # <browsers>
    revision = os.path.basename(version_dir).split("-", 1)[-1]
    headless_dir = os.path.join(browsers_root, f"chromium_headless_shell-{revision}")
    if not os.path.isdir(headless_dir):
        # 新版 Playwright 的 headless=True 使用独立 headless shell；只检查完整
        # Chromium 会出现“打包成功、自动发送启动失败”。安装命令会补齐它。
        print("  缺少匹配版本的 Chromium headless shell，正在补齐...")
        run([sys.executable, "-m", "playwright", "install", "chromium"], cwd=ROOT)

    print(f"  Chromium 可执行文件：{exe_path}")
    print(f"  主 Chromium 目录    ：{version_dir}")
    if not os.path.isdir(version_dir):
        print("[错误] Chromium 目录不存在，无法打包。")
        sys.exit(1)

    staging = os.path.join(BUILD_DIR, "playwright-browsers")
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    selected = [version_dir]
    if os.path.isdir(headless_dir):
        selected.append(headless_dir)
    # ffmpeg / winldd revision 与 Chromium revision 不同，体积很小，全部随包带上。
    for name in os.listdir(browsers_root):
        path = os.path.join(browsers_root, name)
        if os.path.isdir(path) and name.startswith(("ffmpeg-", "winldd-")):
            selected.append(path)

    t0 = time.time()
    for source in dict.fromkeys(selected):
        dest = os.path.join(staging, os.path.basename(source))
        print(f"  复制到暂存区：{dest}")
        shutil.copytree(source, dest, symlinks=False)
    print(f"  复制完成，用时 {time.time() - t0:.1f}s")

    print(f"  实际来源根目录：{browsers_root}")
    return staging


# ─────────────────────────────────────────────────────────────────────────────
# 2. 打包
# ─────────────────────────────────────────────────────────────────────────────

def build_add_data_args() -> list[str]:
    """把 static/ 与 Chromium 放进 PyInstaller 只读资源目录。

    比打完包再复制更可靠：PyInstaller 保证 dist 目录结构，不会漏。
    sep 用 ';' 是 Windows 的路径分隔约定。
    """
    args: list[str] = []

    for name in DATA_DIRS:
        src = os.path.join(ROOT, name)
        if not os.path.isdir(src):
            print(f"  [警告] 缺少目录 {src}，跳过")
            continue
        args += ["--add-data", f"{src};{name}"]

    staging_browsers = os.path.join(BUILD_DIR, "playwright-browsers")
    if os.path.isdir(staging_browsers):
        args += ["--add-data", f"{staging_browsers};playwright-browsers"]

    return args


def prepare_first_run_files(exe: str) -> None:
    """创建干净的可写目录与配置模板，绝不复制构建机上的凭据。"""
    out_dir = os.path.dirname(exe)
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
    example = os.path.join(ROOT, ".env.example")
    if os.path.isfile(example):
        shutil.copy2(example, os.path.join(out_dir, ".env.example"))


def verify_clean_artifact(exe: str) -> None:
    """最后一道防线：产物中一旦出现凭据或账号数据就拒绝交付。"""
    out_dir = os.path.dirname(exe)
    sensitive = [
        os.path.join(out_dir, ".env"),
        os.path.join(out_dir, "state.json"),
        os.path.join(out_dir, "_internal", ".env"),
        os.path.join(out_dir, "_internal", "state.json"),
    ]
    data_dir = os.path.join(out_dir, "data")
    if os.path.isdir(data_dir):
        sensitive.extend(
            os.path.join(data_dir, name)
            for name in os.listdir(data_dir)
            if name != ".gitkeep"
        )
    leaked = [path for path in sensitive if os.path.exists(path)]
    if leaked:
        print("\n[错误] 构建产物包含不应分发的凭据/账号数据：")
        for path in leaked:
            print(f"  - {path}")
        print("请移走这些文件并重新构建；脚本不会自动删除用户数据。")
        raise RuntimeError("桌面产物敏感数据检查失败")


def run_pyinstaller() -> str:
    title("[4/4] PyInstaller 打包")
    os.makedirs(WORK_DIR, exist_ok=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",              # 必须 onedir，见 launcher.py 顶部说明
        "--console",             # 保留控制台便于排查；确认稳定后可改 --noconsole
        "--name", DIST_NAME,
        "--distpath", os.path.join(BUILD_DIR, "dist"),
        "--workpath", WORK_DIR,
        "--specpath", BUILD_DIR,
        "--paths", ROOT,
        "--paths", HERE,
        # 把用户数据目录钉在 exe 旁边（而不是 _internal 里）
        "--runtime-hook", os.path.join(HERE, "runtime_hook_data_dir.py"),
    ]
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]
    cmd += build_add_data_args()
    cmd.append(ENTRY)

    run(cmd, cwd=ROOT)

    exe = os.path.join(BUILD_DIR, "dist", DIST_NAME, f"{DIST_NAME}.exe")
    if not os.path.isfile(exe):
        print(f"\n[错误] 未生成 {exe}")
        sys.exit(1)
    return exe


def report(exe: str) -> None:
    out_dir = os.path.dirname(exe)
    total = 0
    for base, _dirs, files in os.walk(out_dir):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass

    print()
    print("=" * 62)
    print(" 打包完成")
    print("=" * 62)
    print(f"  可执行文件：{exe}")
    print(f"  整个目录  ：{out_dir}")
    print(f"  目录体积  ：{total / 1024 / 1024:.1f} MB")
    print()
    print("  分发方式：整个目录一起拷贝（不能只拷 exe）。")
    print("  双击 DouyinStreak.exe 后：托盘出现图标，浏览器自动打开管理后台。")
    print()


def main() -> int:
    print("抖音续火花 · 桌面版打包")
    print(f"项目根目录：{ROOT}")
    ensure_venv()          # 会切换到 desktop/_build/.venv 并重新执行本脚本
    check_python()
    install_deps()
    ensure_chromium()
    exe = run_pyinstaller()
    prepare_first_run_files(exe)
    verify_clean_artifact(exe)
    report(exe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
