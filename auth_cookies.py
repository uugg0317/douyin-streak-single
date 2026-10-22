"""管理 Cookie 按本实例实际监听端口命名，不读取凭据或运行数据。"""
from __future__ import annotations

import os
from typing import Optional, Tuple, Union


def cookie_names(port: Optional[Union[int, str]] = None) -> Tuple[str, str]:
    """默认使用进程 PORT（缺省8000）；设置、读取、注销及CLI识别共用。"""
    configured = os.environ.get("PORT", "8000") if port is None else port
    if isinstance(configured, bool):
        raise ValueError("管理 Cookie 的实例端口必须为1至65535的整数")
    try:
        number = int(str(configured).strip())
    except (TypeError, ValueError):
        raise ValueError("管理 Cookie 的实例端口必须为1至65535的整数") from None
    if not 1 <= number <= 65535:
        raise ValueError("管理 Cookie 的实例端口必须为1至65535的整数")
    return "session_auth_token_" + str(number), "device_auth_token_" + str(number)
