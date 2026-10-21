"""Validate the local Python environment without loading account configuration."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys


REQUIRED = {
    "fastapi": "FastAPI", "uvicorn": "Uvicorn", "playwright.sync_api": "Playwright",
    "apscheduler": "APScheduler",
    "multipart": "python-multipart", "pydantic": "Pydantic", "greenlet": "greenlet",
}


def _browser_component_paths() -> dict[str, str]:
    """Ask this installed Playwright's registry; do not guess revision paths.

    Playwright 1.60 bundles a registry namespace in coreBundle.js. Older
    installations expose the registry instance from lib/server/registry/index.js.
    This local Node query reads component metadata only, never launches a page.
    """
    from playwright._impl._driver import compute_driver_executable

    node, cli = compute_driver_executable()
    package = Path(cli).parent
    candidates = (package / "lib" / "coreBundle.js",
                  package / "lib" / "server" / "registry" / "index.js")
    module = next((path for path in candidates if path.is_file()), None)
    if module is None:
        raise RuntimeError("Playwright registry module unavailable")
    script = """
const exported = require(process.argv[1]);
const registry = exported.registry.registry || exported.registry;
const paths = {};
for (const name of ['chromium', 'chromium-headless-shell']) {
  const executable = registry.findExecutable(name);
  if (!executable) throw new Error('Required browser component missing from registry');
  paths[name] = executable.executablePath();
}
process.stdout.write(JSON.stringify(paths));
"""
    env = dict(os.environ)
    # A configured Node preload must not execute during this read-only preflight.
    env.pop("NODE_OPTIONS", None)
    env.pop("NODE_PATH", None)
    result = subprocess.run(
        [str(node), "-e", script, str(module)], check=True, capture_output=True,
        text=True, encoding="utf-8", timeout=15, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    paths = json.loads(result.stdout)
    if not isinstance(paths, dict) or any(
        not isinstance(paths.get(name), str) or not paths[name] or not Path(paths[name]).is_absolute()
        for name in ("chromium", "chromium-headless-shell")
    ):
        raise RuntimeError("Playwright registry returned incomplete browser paths")
    return paths


def check(*, require_browser: bool = True) -> list[str]:
    if sys.version_info[:2] < (3, 10):
        current = ".".join(str(value) for value in sys.version_info[:3])
        return [f"需要 Python 3.10 及以上，当前解释器为 Python {current}；请更换解释器后重试。"]
    problems = []
    for module, label in REQUIRED.items():
        try:
            importlib.import_module(module)
        except (ImportError, OSError) as error:
            problems.append(f"{label} 无法加载（{type(error).__name__}）")
    if not problems and require_browser:
        try:
            paths = _browser_component_paths()
            for name, label in (("chromium", "Chromium 完整浏览器"),
                                ("chromium-headless-shell", "Chromium headless shell（默认无头模式）")):
                if not Path(paths[name]).is_file():
                    problems.append(f"尚未安装当前 Playwright 所需的 {label}")
        except Exception as error:
            problems.append(f"Playwright 浏览器组件清单检查失败（{type(error).__name__}）")
    return problems


def main() -> int:
    problems = check(require_browser="--dependencies-only" not in sys.argv[1:])
    if problems:
        for problem in problems:
            print(f"[未就绪] {problem}")
        print("请先运行同文件夹中的“安装依赖.bat”，成功后再启动。")
        return 2
    print("[检查通过] Python 依赖与所需组件已就绪。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
