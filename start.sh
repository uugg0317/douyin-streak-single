#!/bin/bash
# 抖音续火花 - 单账号本机启动脚本
# 用法: bash start.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 后端保持回环监听；后台任务沿用既有开关与配置。
export HOST="127.0.0.1"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "[错误] 未找到 python3，请先安装 Python 3.10+"
    exit 1
fi

# 检查虚拟环境，没有则创建
if [ ! -d "venv" ]; then
    echo "[1/3] 创建 Python 虚拟环境..."
    python3 -m venv venv
fi

# 固定浏览器内核位置，避免以 root 安装、以 douyin 用户运行时找不到 Chromium。
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$SCRIPT_DIR/.cache/ms-playwright}"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"

# 与 deploy.sh 共用同一份配置；源码直跑则继续使用项目根目录 .env。
if [ -z "${ENV_FILE_PATH:-}" ]; then
    if [ -f "$SCRIPT_DIR/data/service.env" ]; then
        export ENV_FILE_PATH="$SCRIPT_DIR/data/service.env"
    else
        export ENV_FILE_PATH="$SCRIPT_DIR/.env"
    fi
fi
if [ ! -f "$ENV_FILE_PATH" ]; then
    cp "$SCRIPT_DIR/.env.example" "$ENV_FILE_PATH"
    chmod 600 "$ENV_FILE_PATH"
    echo "[可选配置] 已生成 $ENV_FILE_PATH，可稍后填写 SMTP 等设置；现在继续启动本机面板。"
fi

# 仅在依赖清单变化时安装，避免每次启动都联网并改动运行环境。
REQ_FILE="requirements.txt"
if [ -f "requirements.lock" ]; then
    REQ_FILE="requirements.lock"
fi
REQ_HASH="$(python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$REQ_FILE")"
REQ_STAMP="venv/.requirements.sha256"
if [ ! -f "$REQ_STAMP" ] || [ "$(tr -d '\r\n' < "$REQ_STAMP")" != "$REQ_HASH" ]; then
    echo "[2/3] 安装依赖..."
    venv/bin/python -m pip install --quiet --upgrade pip
    venv/bin/python -m pip install --quiet -r "$REQ_FILE"
    printf '%s\n' "$REQ_HASH" > "$REQ_STAMP"
else
    echo "[2/3] 依赖未变化，跳过安装"
fi

# 安装 Playwright 浏览器（首次运行或 Playwright 升级后所需 revision 变化时）
if ! venv/bin/python -c 'import os; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); path=p.chromium.executable_path; p.stop(); raise SystemExit(0 if os.path.isfile(path) else 1)'; then
    echo "  安装 Playwright Chromium 浏览器..."
    venv/bin/python -m playwright install chromium
fi

# 启动应用
echo "[3/3] 启动抖音续火花服务..."
echo "  管理后台: http://127.0.0.1:8000"
echo "  面板直接打开；后台任务遵循现有开关与配置。"
echo "  按 Ctrl+C 停止"
echo ""

exec venv/bin/python app.py
