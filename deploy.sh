#!/usr/bin/env bash
# 2.8 本机无密码门版不包含公网服务部署流程。
# 保留旧文件名作为本机启动兼容入口，不修改系统服务或网络配置。
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
echo "[说明] 2.8 仅提供本机控制台，接下来按 start.sh 启动。"
echo "[说明] 不安装 systemd 服务、不开放端口、不配置反向代理。"
exec bash "$SCRIPT_DIR/start.sh"
