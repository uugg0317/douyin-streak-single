"""Resolve startup configuration before importing path-dependent services."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def resolve_env_path() -> Path:
    override = os.environ.get('ENV_FILE_PATH', '').strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent / '.env'
    return Path(__file__).resolve().parent / '.env'


ENV_PATH = resolve_env_path()


def load_environment(path: Path | None = None) -> None:
    """Keep existing first-key/process-environment precedence; never write config."""
    selected = ENV_PATH if path is None else Path(path)
    if not selected.exists():
        return
    for line in selected.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            if key.strip() == 'TZ' and os.name == 'nt':
                # Windows 的 C 运行库不认识 Asia/Shanghai 这类时区名，会把本地时间
                # 当成 UTC，日志时间因此慢 8 小时。业务日期另有固定时区，不依赖 TZ。
                continue
            os.environ.setdefault(key.strip(), value.strip())
