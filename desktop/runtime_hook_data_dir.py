# -*- coding: utf-8 -*-
"""PyInstaller 运行时钩子：修正打包后的路径与环境变量。

为什么需要它：
- onedir 打包后，应用代码与静态资源位于 _internal/；
- 用户数据和可编辑配置不能埋在 _internal/，必须留在 exe 旁边；
- 桌面启动器只绑定 127.0.0.1，因此没有 .env 的首次启动可以关闭登录门，
  避免把示例口令当成真实凭据。

做法：分别指定 DATA_DIR 与 ENV_FILE_PATH；服务监听地址固定为回环地址。

本文件在应用代码执行前被 PyInstaller 导入；它不能 import 项目内的任何模块。
"""

import os
import sys

try:
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        os.environ.setdefault("DATA_DIR", os.path.join(exe_dir, "data"))
        env_path = os.path.join(exe_dir, ".env")
        os.environ.setdefault("ENV_FILE_PATH", env_path)
        os.environ.setdefault("HOST", "127.0.0.1")
        if not os.path.isfile(env_path):
            os.environ.setdefault("LOGIN_GATE", "off")
except Exception:
    # 钩子失败不能阻断启动；config.py 会退回默认的 BASE_DIR/data
    pass
