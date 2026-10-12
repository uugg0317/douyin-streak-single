import json
import time
from pathlib import Path

from core import automation

tmp = Path("data/_fake_state.json")
now = time.time()
fake = {"cookies": [
    {"name": "sessionid", "expires": now + 3 * 86400},      # 3 天后到期 → 临期
    {"name": "sessionid_ss", "expires": now + 90 * 86400},  # 90 天
    {"name": "other_cookie", "expires": now + 2 * 86400},   # 非关键，忽略
    {"name": "sid_guard", "expires": -1},                   # 会话型，跳过
]}
tmp.write_text(json.dumps(fake), encoding="utf-8")
info = automation.scan_state_cookies(tmp)
print("fake scan:", info)
assert info["has_state"] and info["has_session"]
assert info["expiring_soon"] and not info["expired"]
assert abs(info["min_days"] - 3.0) < 0.1

# 过期场景
fake["cookies"][0]["expires"] = now - 86400
tmp.write_text(json.dumps(fake), encoding="utf-8")
info2 = automation.scan_state_cookies(tmp)
print("expired scan:", info2)
assert info2["expired"]

tmp.unlink()
print("SCAN_LOGIC_OK")
