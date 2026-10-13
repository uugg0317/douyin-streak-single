#!/bin/bash
# 抖音续火花 - Linux 本机 systemd 安装/升级脚本

set -euo pipefail

INSTALL_DIR="/opt/douyin-streak"
SERVICE_USER="douyin"
SERVICE_GROUP="douyin"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_ENV="$INSTALL_DIR/data/service.env"
BROWSER_DIR="$INSTALL_DIR/.cache/ms-playwright"
CHROME_CONFIG_DIR="$INSTALL_DIR/.config"

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "[错误] 部署需要 root 权限，请使用 sudo bash deploy.sh"
  exit 1
fi

echo "[1/7] 安装系统依赖..."
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-pip rsync curl xvfb xauth \
  libnss3 libnspr4 libasound2 libatk1.0-0 libatk-bridge2.0-0 \
  libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
  libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
  >/dev/null

echo "[2/7] 准备专用服务用户与目录..."
if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
  groupadd --system "$SERVICE_GROUP"
fi
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --gid "$SERVICE_GROUP" --home-dir "$INSTALL_DIR" \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o root -g root -m 0755 "$INSTALL_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 \
  "$INSTALL_DIR/data" "$BROWSER_DIR" "$CHROME_CONFIG_DIR"

# 首次部署接受源码目录的 .env；升级永远保留服务器自己的 service.env。
if [ ! -f "$SERVICE_ENV" ]; then
  if [ -f "$SOURCE_DIR/.env" ]; then
    install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 \
      "$SOURCE_DIR/.env" "$SERVICE_ENV"
  else
    install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 \
      "$SOURCE_DIR/.env.example" "$SERVICE_ENV"
    echo "[可选配置] 已生成 $SERVICE_ENV，SMTP 等设置可在本机稍后填写。"
  fi
fi

# 只安装回环后端服务。面板无管理员登录门，默认本机访问。
# 同机反代需在 service.env 显式设置 QR_PUBLIC_ORIGIN，并自行限制后台访问范围。
# 本脚本不配置 Nginx 或证书；SMTP 等可选配置无需先填写即可启动。
# 后台任务沿用既有开关与配置，不增加暂停默认值。
SERVICE_PORT="$(STREAK_SERVICE_ENV="$SERVICE_ENV" python3 -c '
import os
port = 8000
with open(os.environ["STREAK_SERVICE_ENV"], encoding="utf-8") as stream:
    for raw in stream:
        if "=" in raw and raw.split("=", 1)[0].strip() == "PORT":
            try:
                candidate = int(raw.split("=", 1)[1].strip())
                if 1 <= candidate <= 65535:
                    port = candidate
            except ValueError:
                pass
print(port)
')"

echo "[3/7] 同步本机程序文件（保留 data、可选配置、浏览器与 venv）..."
if systemctl cat douyin-streak.service >/dev/null 2>&1; then
  systemctl stop douyin-streak.service
fi
rsync -a --delete \
  --exclude '/.git/' \
  --exclude '/.env' \
  --exclude '/state.json' \
  --exclude '/data/' \
  --exclude '/venv/' \
  --exclude '/.cache/' \
  --exclude '/.config/' \
  --exclude '/build/' \
  --exclude '/dist/' \
  --exclude '/desktop/_build/' \
  "$SOURCE_DIR/" "$INSTALL_DIR/"

echo "[4/7] 创建/更新 Python 虚拟环境..."
if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/python" -m pip install --quiet --upgrade pip
REQ_FILE="$INSTALL_DIR/requirements.lock"
if [ ! -f "$REQ_FILE" ]; then
  REQ_FILE="$INSTALL_DIR/requirements.txt"
fi
"$INSTALL_DIR/venv/bin/python" -m pip install --quiet -r "$REQ_FILE"

echo "[5/7] 安装 Playwright Chromium 到服务实际使用的目录..."
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/data"
chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/.cache" "$BROWSER_DIR"
chown "$SERVICE_USER:$SERVICE_GROUP" "$CHROME_CONFIG_DIR"
chmod 0700 "$CHROME_CONFIG_DIR"
runuser -u "$SERVICE_USER" -- env PLAYWRIGHT_BROWSERS_PATH="$BROWSER_DIR" \
  "$INSTALL_DIR/venv/bin/python" -m playwright install chromium
chmod 0600 "$SERVICE_ENV"

echo "[6/7] 安装并启动 systemd 服务..."
install -o root -g root -m 0644 \
  "$INSTALL_DIR/douyin-streak.service" /etc/systemd/system/douyin-streak.service
systemctl daemon-reload
systemctl enable douyin-streak.service >/dev/null
systemctl restart douyin-streak.service

echo "[7/7] 检查服务状态..."
for _ in $(seq 1 20); do
  if curl --silent --fail "http://127.0.0.1:$SERVICE_PORT/api/health" >/dev/null; then
    echo "本机服务安装完成：回环健康检查通过。请在本机打开 http://127.0.0.1:$SERVICE_PORT/。"
    exit 0
  fi
  sleep 1
done

echo "[错误] 服务未在 20 秒内通过健康检查。"
systemctl --no-pager --full status douyin-streak.service || true
exit 3
