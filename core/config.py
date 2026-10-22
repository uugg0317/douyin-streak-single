"""配置读写。配置保存在 data/config.json，由网页端编辑。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import os
import tempfile

BASE_DIR = Path(__file__).resolve().parent.parent
# 支持多账号：通过环境变量 DATA_DIR 指定数据目录，默认 data/
# 例如 set DATA_DIR=accounts/账号1  即可隔离每个账号的数据
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data"))).resolve()
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"


def atomic_write_text(path: Path, text: str) -> None:
    """原子写文本：先写同目录 .tmp 再 os.replace，外部读者只会看到旧版或完整新版。

    直接 `path.write_text(...)` 在写入过程中若进程被杀、磁盘满、断电，文件会被截断
    成半截 JSON。本项目 data/state.json 是十余万字节的完整登录 Cookie、ledger.json
    是好友台账，一旦半截化：state.json 会让 storage_state 加载失败（被迫重新扫码），
    ledger.json 会被读成空列表（一整晚不发却显示「正常完成」）。同盘 rename 是原子的。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".tmp."
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_bytes(path: Path, raw: bytes) -> None:
    """原子写字节：见 atomic_write_text。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".tmp."
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _state_looks_valid(p: Path) -> bool:
    """state.json 是否真能用：能解析为 dict 且 cookies 是非空数组。

    旧校验只看 `st_size > 30`，一个被截断成 10KB 的半截 JSON 也能通过，随后在
    Playwright storage_state 加载时抛异常，表现为发送持续失败却不知道原因。
    """
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get("cookies"), list) and bool(data["cookies"])


# 安全模式：通过环境变量 STATE_FILE_PATH 指定 state.json 路径（如 /dev/shm/state.json）
# 用于将解密后的登录态放在 tmpfs 内存中，磁盘上不留明文
_EXTERNAL_STATE = os.environ.get("STATE_FILE_PATH", "").strip()


def state_write_path() -> Path:
    """Resolve after .env is loaded, so QR, upload, and sending use the same file."""
    external = os.environ.get("STATE_FILE_PATH", "").strip()
    return Path(external).expanduser().resolve() if external else STATE_PATH


def get_valid_state_path() -> Path | None:
    """返回一个「真能用」的登录态文件路径；凭据只认 DATA_DIR（或 STATE_FILE_PATH）。

    历史版本会在找不到时回退拷贝项目根目录 state.json，造成凭据散落两份；该回退已
    移除，根目录遗留文件应手工删除。
    """
    path = state_write_path()
    return path if path.exists() and _state_looks_valid(path) else None

DEFAULT_CONFIG = {
    "schedule_time": "00:00",   # 每天发送时间 HH:MM（服务器时区 Asia/Shanghai）
    "jitter_minutes": 0,        # 时间抖动窗口；0 表示准点开始
    "send_gap_min": 1,          # 相邻两个好友之间的最小间隔（秒）
    "send_gap_max": 2,          # 相邻两个好友之间的最大间隔（秒）
    "max_friends_per_run": 20,  # 每次最多发送的好友数（0 表示不限制）
    "friends": [],              # 好友列表：聊天列表里显示的备注 / 昵称 / 抖音号
    "messages": ["[续火花吧]"],
    # creator 页抖音号采集（P1）：
    "creator_user_detail_path": "aweme/v1/creator/im/user_detail/",  # user_detail 接口路径前缀（接口变动只改这里）
    "creator_max_scrolls": 80,  # 单次采集最大滚动轮数
    # 通道 B / 调度（P2）：
    "auto_run_enabled": True,  # 自动运行总开关：关闭后定时任务不发送（手动「立即续火花」不受影响）
    "allow_first_message": False,  # 允许对无会话好友发送首条消息（通道 B，高风险，默认关闭）
    "first_message_daily_limit": 1,  # 通道 B 单日上限
    "schedule_harvest_day": "mon",  # 周级 creator 采集：mon/tue/.../sun 或空字符串关闭，默认周一 03:00
    # 告警邮件策略（SMTP 连接与授权码在 .env 的 SMTP_*，属部署密钥不在这里）：
    "mail_to": "",  # 告警收件地址（多个用英文逗号分隔；空则回退发信账号）
    "email_subject": "【告警】douyin-streak 卡死已自动重启（{host}）",
    "email_body": (
        "douyin-streak 服务看门狗触发，进程已退出并由 systemd 自动拉起。\n\n"
        "服务器：{host}（解析 IP {host_ip}）\n"
        "触发时间：{time}\n"
        "任务持锁时长：{age} 秒（阈值 {threshold} 秒）\n"
        "\n卡死时刻线程栈：\n{stack}\n"
    ),
}

_lock = threading.RLock()

LOCKED_FIELDS = ("messages", "schedule_time", "jitter_minutes", "send_gap_min", "send_gap_max")


class LockedConfigChange(ValueError):
    def __init__(self, changed_fields: list[str], effective_config: dict):
        self.changed_fields = changed_fields
        self.effective_config = effective_config
        super().__init__("这些配置已锁定，未保存任何改动：" + "、".join(changed_fields))


def _locked_value(key: str, value):
    """Compare effective values, so an equivalent number/time is not an edit."""
    try:
        if key in {"jitter_minutes", "send_gap_min", "send_gap_max"}:
            return int(value)
        if key == "schedule_time":
            hh, mm = str(value).split(":")
            return f"{int(hh):02d}:{int(mm):02d}"
        if key == "messages" and isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
    except (ValueError, TypeError):
        pass
    return value


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    return cfg


def load_persisted_config() -> dict:
    """只读取磁盘上真实持久化过的键值（不含 DEFAULT_CONFIG 兜底）。

    用途：判断某项「服务器上到底有没有存过」。load_config() 会把
    DEFAULT_CONFIG 合并进来，因此永远无法区分「存过」与「没存过」。
    """
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_config_locked(cfg: dict | None) -> dict:
    persisted = load_persisted_config()
    merged = dict(DEFAULT_CONFIG)
    # API 允许提交局部配置；必须先叠加磁盘值，否则只改一个开关会把好友列表、
    # 限额等未携带字段悄悄重置为默认值。
    merged.update(persisted)
    if cfg:
        merged.update(cfg)

    merged["friends"] = [str(x).strip() for x in merged.get("friends", []) if str(x).strip()]
    # 发送文案永久固定：始终以服务器磁盘上已保存的 messages 为准，
    # 忽略网页或直接调接口传入的任何 messages 修改，保证发送内容不被改动
    fixed_messages = persisted.get("messages") or ["[续火花吧]"]
    merged["messages"] = [str(x) for x in fixed_messages if str(x).strip()]

    # 定时与发送节奏永久锁定：这些项已在前端禁用，且直接决定「0 点准时、
    # 1 分钟内发完」。前端页面可能带着默认值（21:00 / 30 / 6 / 12）整体回存，
    # 这里强制以服务器磁盘上已保存的值为准，杜绝被默认值覆盖。
    # 仅当磁盘上确实没有配置（首次部署）时，才使用安全默认值 00:00 / 0 / 1 / 2。
    #
    # 注意：判断「磁盘上有没有」必须用 load_persisted_config()。
    # 旧实现用的是 load_config()，而它必然含有 DEFAULT_CONFIG 的全部键，
    # 于是 else 分支永远不可达 —— 首次部署会落到 DEFAULT_CONFIG 的
    # 21:00 / 30 / 6 / 12，与「0 点准时发」的设计意图正好相反。
    #
    # gap 取值说明（2026-09-18 调整 2/4 → 1/2）：
    # 单条发送动作实测约 1.2 秒，gap 均值 1.5 秒 → 单人约 2.7 秒，
    # 10 人端到端约 31 秒，1 分钟目标留有充足余量（即便个别好友走慢路径）。
    # 注意 gap 是「好友之间」的间隔，不是发送频率上限，1~2 秒仍在拟人范围。
    locked_defaults = {
        "schedule_time": "00:00",
        "jitter_minutes": 0,
        "send_gap_min": 1,
        "send_gap_max": 2,
    }
    for _key, _safe_default in locked_defaults.items():
        merged[_key] = persisted.get(_key, _safe_default)

    schedule = str(merged.get("schedule_time", "00:00"))
    try:
        hh, mm = schedule.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError
        merged["schedule_time"] = f"{int(hh):02d}:{int(mm):02d}"
    except Exception:
        raise ValueError("schedule_time 必须是 HH:MM 格式")

    for key in ("jitter_minutes", "send_gap_min", "send_gap_max", "max_friends_per_run", "creator_max_scrolls", "first_message_daily_limit"):
        try:
            merged[key] = max(0, int(merged.get(key, DEFAULT_CONFIG[key])))
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是整数")
    if merged["send_gap_max"] < merged["send_gap_min"]:
        merged["send_gap_max"] = merged["send_gap_min"]
    merged["auto_run_enabled"] = bool(merged.get("auto_run_enabled"))
    merged["allow_first_message"] = bool(merged.get("allow_first_message"))
    day = str(merged.get("schedule_harvest_day") or "").strip().lower()
    merged["schedule_harvest_day"] = day if day in {"mon", "tue", "wed", "thu", "fri", "sat", "sun", "off"} else "off"

    # 邮件策略键若本次请求未携带（例如在定时页保存，表单里没有邮件字段），
    # 必须沿用磁盘已存值，不能被 DEFAULT_CONFIG 默认值冲掉
    for _pk in ("mail_to", "email_subject", "email_body"):
        if not cfg or _pk not in cfg:
            merged[_pk] = persisted.get(_pk, DEFAULT_CONFIG[_pk])

    with _lock:
        atomic_write_text(CONFIG_PATH, json.dumps(merged, ensure_ascii=False, indent=2))
    return merged


def save_config(cfg: dict | None, *, reject_locked_changes: bool = False) -> dict:
    """串行化完整的读取-合并-写入事务，避免并发管理请求互相覆盖。"""
    with _lock:
        if reject_locked_changes:
            effective = load_config()
            changed = [key for key in LOCKED_FIELDS if cfg and key in cfg
                       and _locked_value(key, cfg[key]) != _locked_value(key, effective.get(key))]
            if changed:
                raise LockedConfigChange(changed, effective)
        return _save_config_locked(cfg)
