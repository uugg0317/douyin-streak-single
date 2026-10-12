"""配置体系统一——本地功能验证：
1. 旧 email_config.json 迁移（-> .migrated；.env 不被覆盖；策略进 config.json）
2. load/save 行为：假授权码被拒、策略保存落 config.json、连接信息不落 config.json
"""
import json
import os
import sys
from pathlib import Path

if os.environ.get("RUN_DESTRUCTIVE_CONFIG_TESTS") != "1":
    raise SystemExit(
        "该脚本会迁移并改写真实配置；确认备份后设置 RUN_DESTRUCTIVE_CONFIG_TESTS=1"
    )

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from email_service import (  # noqa: E402
    LEGACY_EMAIL_CONFIG_PATH,
    load_email_config,
    migrate_legacy_email_config,
    save_email_config,
)
from core.config import CONFIG_PATH  # noqa: E402

# ── 1. 迁移 ──
assert LEGACY_EMAIL_CONFIG_PATH.exists(), "测试需要旧 email_config.json 存在"
env_before = (ROOT / ".env").read_text(encoding="utf-8")
migrate_legacy_email_config()
assert not LEGACY_EMAIL_CONFIG_PATH.exists(), "迁移后原文件应改名"
assert (ROOT / "data" / "email_config.json.migrated").exists(), "应留档 .migrated"
assert (ROOT / ".env").read_text(encoding="utf-8") == env_before, ".env 已有键，迁移不应改动"
print("1. 迁移 OK：原文件改名 .migrated，.env 未被改动")

cfg = load_email_config()
assert cfg["smtp_host"] == "smtp.qq.com"
assert len(cfg["smtp_pass"]) == 16, "授权码应仍来自 .env（16位）"
assert cfg["mail_to"], "收件策略应已迁入 config.json"
print("2. 组装 OK：连接来自 .env，策略来自 config.json，mail_to =", cfg["mail_to"])

# config.json 里绝不能出现 SMTP 连接/授权码字段
disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
for forbidden in ("smtp_host", "smtp_user", "smtp_pass"):
    assert forbidden not in disk, f"config.json 不应含 {forbidden}"
print("3. 隔离 OK：连接/授权码未写入 config.json")

# ── 4. 假授权码被拒（且不产生任何写入）──
before_cfg = CONFIG_PATH.read_text(encoding="utf-8")
before_env = (ROOT / ".env").read_text(encoding="utf-8")
try:
    save_email_config({"smtp_pass": "317317"})
except ValueError as e:
    assert "授权码" in str(e)
else:
    raise AssertionError("6位数字假授权码应被拒绝")
assert CONFIG_PATH.read_text(encoding="utf-8") == before_cfg
assert (ROOT / ".env").read_text(encoding="utf-8") == before_env
print("4. 防线 OK：6位数字授权码被拒，文件均未改动")

# ── 5. 策略保存：只改收件地址，应落 config.json；连接信息不动 ──
original_to = disk.get("mail_to", "")
save_email_config({"mail_to": "policy-test@example.com"})
disk2 = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
assert disk2["mail_to"] == "policy-test@example.com"
assert (ROOT / ".env").read_text(encoding="utf-8") == before_env, "仅改策略不应动 .env"
print("5. 策略保存 OK：mail_to 落 config.json，.env 未动")

# 还原
save_email_config({"mail_to": original_to})
print("6. 已还原 mail_to =", original_to or "(空)")
print("CONFIG_UNIFY_TESTS_PASS")
