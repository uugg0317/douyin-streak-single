# -*- coding: utf-8 -*-
"""PyInstaller 路径钩子：用户数据与可选 SMTP 配置留在 exe 旁边。

本机面板固定监听回环地址，不创建管理凭据；后台任务沿用既有开关与配置。
本文件在应用代码前执行，不导入项目业务模块。
"""
import os
import sys

try:
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        os.environ.setdefault("DATA_DIR", os.path.join(exe_dir, "data"))
        env_path = os.path.join(exe_dir, ".env")
        os.environ.setdefault("ENV_FILE_PATH", env_path)
        os.environ["HOST"] = "127.0.0.1"
except Exception:
    # 钩子失败时由应用的默认路径和回环监听检查处理。
    pass
