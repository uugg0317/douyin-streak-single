#!/usr/bin/env bash
# 抖音续火花 2.8：仅在本机启动，不配置公网入口。
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 请先安装 Python 3.10 及以上及 venv 支持。"
  exit 1
fi
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else "需要 Python 3.10 及以上")'
export HOST=127.0.0.1
export PORT=8000
export SPARKKEEPER_BACKGROUND_TASKS=0
export PYTHONUTF8=1
export ENV_FILE_PATH="${ENV_FILE_PATH:-$SCRIPT_DIR/.env}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$SCRIPT_DIR/.cache/ms-playwright}"
if [ ! -f "$ENV_FILE_PATH" ]; then
  (umask 077; cp "$SCRIPT_DIR/.env.example" "$ENV_FILE_PATH")
fi
python3 tools/ensure_venv.py --project-root "$SCRIPT_DIR"
REQ_HASH="$(python3 -c 'import hashlib, pathlib; print(hashlib.sha256(pathlib.Path("requirements.lock").read_bytes()).hexdigest())')"
REQ_STAMP="venv/.requirements.sha256"
if [ ! -f "$REQ_STAMP" ] || [ "$(tr -d '\r\n' < "$REQ_STAMP")" != "$REQ_HASH" ]; then
  echo "[安装] 按锁定清单准备依赖。"
  venv/bin/python -m pip install -r requirements.lock
  printf '%s\n' "$REQ_HASH" > "$REQ_STAMP"
fi
venv/bin/python startup_check.py --dependencies-only
if ! venv/bin/python startup_check.py; then
  echo "[安装] 准备 Chromium；如系统库缺失，请先按提示安装所需系统库。"
  venv/bin/python -m playwright install chromium
  venv/bin/python startup_check.py
fi
echo "[版本] 抖音续火花 2.8（本机无密码门）"
echo "[本机地址] http://127.0.0.1:8000/"
echo "[运行模式] 后台任务暂停，手动操作由控制台发起。"
echo "[停止服务] 按 Ctrl+C。"
exec venv/bin/python app.py
