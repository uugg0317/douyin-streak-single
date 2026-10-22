"""本机启动前补齐缺失密码；保留已有有效密码及其它配置。"""

from __future__ import annotations

import getpass
import os
import secrets
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Callable
from auth_config import hash_password, password_hash_kind
from env_bootstrap import resolve_env_path


class PasswordSetupError(ValueError):
    """未能安全地初始化密码配置。"""


def resolve_setup_path() -> Path:
    """Use the service path without following a user-selected config symlink for writing."""
    override = os.environ.get("ENV_FILE_PATH", "").strip()
    if override and Path(override).expanduser().is_symlink():
        raise PasswordSetupError(".env 必须是普通配置文件，不会修改链接或目录。")
    return resolve_env_path()


def _read_password(prompt: str) -> str:
    # getpass 在没有交互式终端时可能退回明文输入；此处拒绝这种回退。
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning as exc:
            raise PasswordSetupError("无法隐藏密码输入，请在本机终端或启动 BAT 中操作。") from exc


def _publish_without_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        # Windows rename 是原子操作，目标已存在会失败；不使用会覆盖的 replace。
        os.rename(source, destination)
    else:
        os.link(source, destination)


def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), value.strip())
    return values


def _usable_secret(value: str) -> bool:
    return bool(value and not value.lower().startswith(("change_me", "changeme", "replace_me", "your_")))


def _password_ready(values: dict[str, str]) -> bool:
    return bool(password_hash_kind(values.get("URL_PASSWORD_SALT", ""), values.get("URL_PASSWORD_HASH", "")))


def _merge_values(text: str, updates: dict[str, str]) -> str:
    """保留非本次更新的行、注释和换行格式。"""
    newline = "\r\n" if "\r\n" in text else "\n"
    remaining = dict(updates)
    lines = []
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            lines.append(f"{key}={updates[key]}")
            remaining.pop(key, None)
        else:
            lines.append(line)
    lines.extend(f"{key}={value}" for key, value in remaining.items())
    return newline.join(lines) + newline


def initialize_env(
    destination: Path,
    *,
    password_reader: Callable[[str], str] = _read_password,
    emit: Callable[[str], None] = print,
) -> bool:
    """返回配置是否更新；成功返回时指定文件可用于本机直接密码登录。"""
    destination = Path(destination)
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise PasswordSetupError(".env 必须是普通配置文件，不会修改链接或目录。")
    if not destination.parent.is_dir():
        raise PasswordSetupError("配置目录不存在，请在完整的源码文件夹中运行。")
    try:
        original = destination.read_bytes() if destination.exists() else None
        text = original.decode("utf-8") if original is not None else ""
    except (OSError, UnicodeError) as exc:
        raise PasswordSetupError("无法读取 .env，原文件未修改。") from exc
    values = _parse_env(text)
    updates: dict[str, str] = {}
    if not _password_ready(values):
        emit("[密码设置] 当前配置缺少可用访问密码；只补齐密码，保留其它配置和个人数据。")
        first = password_reader("设置密码（至少 12 个字符）: ")
        if len(first) < 12:
            raise PasswordSetupError("密码至少需要 12 个字符，配置未修改。")
        if first != first.strip():
            raise PasswordSetupError("密码首尾不能包含空格，配置未修改。")
        second = password_reader("再次输入密码: ")
        if not secrets.compare_digest(first.encode("utf-8"), second.encode("utf-8")):
            raise PasswordSetupError("两次密码不一致，配置未修改，请重新运行。")
        try:
            encoded_password = hash_password(first)
        except ValueError as error:
            raise PasswordSetupError(str(error)) from error
        updates.update(URL_PASSWORD_SALT="", URL_PASSWORD_HASH=encoded_password)
    for key in ("AUTH_TOKEN", "URL_ACCESS_TOKEN"):
        if not _usable_secret(values.get(key, "")):
            updates[key] = secrets.token_urlsafe(48)
    defaults = {
        "HOST": "127.0.0.1", "PORT": "8000", "LOGIN_GATE": "on",
        "PASSWORD_PUBLIC_LOGIN": "on", "ALLOW_PUBLIC_NO_GATE": "off", "WHITELIST_IPS": "",
        "QR_PUBLIC_ORIGIN": "", "COOKIE_EXTRACT_MODE": "local-client", "QR_LOGIN_BROWSER_CHANNEL": "",
        "QR_LOGIN_NETWORK_MODE": "auto", "SPARKKEEPER_BACKGROUND_TASKS": "0",
        "SESSION_WARN_EMAIL_ENABLED": "off", "TZ": "Asia/Shanghai",
    }
    for key, value in defaults.items():
        if key not in values:
            updates[key] = value
    if not updates:
        emit("[配置] 已有可用密码和令牌，保留原配置，不重新设置密码。")
        return False
    if original is None:
        text = "# 本机初始化生成；请保留此文件，不要公开或上传。\n"
    content = _merge_values(text, updates)
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".env-setup-", dir=destination.parent)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # 首次创建不可覆盖竞争者；补齐旧配置时校验快照，拒绝覆盖期间发生的修改。
        if original is None:
            try:
                _publish_without_replace(temporary, destination)
            except FileExistsError as exc:
                raise PasswordSetupError(".env 已由另一进程创建，请重新运行检查；该文件未覆盖。") from exc
        else:
            if destination.is_symlink() or destination.read_bytes() != original:
                raise PasswordSetupError(".env 在密码设置期间已改变，未覆盖，请重新运行。")
            os.chmod(temporary, destination.stat().st_mode & 0o777)
            os.replace(temporary, destination)
    except OSError as exc:
        raise PasswordSetupError("无法安全创建 .env，请确认目录可写并使用本机磁盘。") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    emit("[完成] 本机密码配置已就绪，原始密码未写入配置。已有其它配置已保留。")
    return True


def main() -> int:
    try:
        initialize_env(resolve_setup_path())
    except (PasswordSetupError, EOFError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[取消] 未完成密码设置。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
