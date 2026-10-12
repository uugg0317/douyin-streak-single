import json
import os
import urllib.request
import urllib.error

if os.environ.get("RUN_LIVE_API_TESTS") != "1":
    raise SystemExit("这是会修改真实配置的联调脚本；确认后设置 RUN_LIVE_API_TESTS=1")

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "").strip()
TEST_MAIL_TO = os.environ.get("TEST_MAIL_TO", "").strip()
if not AUTH_TOKEN or not TEST_MAIL_TO:
    raise SystemExit("还需通过环境变量提供 AUTH_TOKEN 与 TEST_MAIL_TO")

BASE = "http://127.0.0.1:8000"


def req(method, path, body=None, headers=None):
    data = None
    h = {"X-Auth-Token": AUTH_TOKEN}
    h.update(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


# 1. 安全响应头
s, h, _ = req("GET", "/api/health")
assert h.get("x-content-type-options") == "nosniff", h
assert "max-age=63072000" in h.get("strict-transport-security", ""), h
print("1 安全头 OK")

# 2. 邮箱配置：掩码保留原密码，修改收件人
s, _, b = req("GET", "/api/email-config")
cfg = json.loads(b)
assert cfg["smtp_pass"] == "******" and cfg["has_pass"] is True
payload = dict(cfg)
payload.pop("has_pass", None)
payload["mail_to"] = TEST_MAIL_TO
s, _, b = req("POST", "/api/email-config", payload)
assert s == 200, b
new_cfg = json.loads(b)["config"]
assert new_cfg["smtp_pass"] == "******" and new_cfg["has_pass"] is True
payload["mail_to"] = cfg["mail_to"]
s, _, b = req("POST", "/api/email-config", payload)
assert s == 200, b
print("2 邮箱掩码保留 OK")

# 3. 配置校验：未知键 400，合法 200
s, _, b = req("POST", "/api/config", {"hack_key": 1})
assert s == 400, (s, b)
s, _, b = req("POST", "/api/config", {"schedule_time": "12:00"})
assert s == 200, (s, b)
print("3 配置校验 OK")

# 4. 管理令牌登录全流程
s, _, b = req("GET", "/api/auth/challenge")
ch = json.loads(b)
assert ch["auth_mode"] == "token" and "nums" not in ch
s, _, b = req("POST", "/api/auth/login", {"token": AUTH_TOKEN})
assert s == 200, (s, b)
# 错误令牌应 401
s3, _, _ = req("POST", "/api/auth/login", {"token": "wrong"})
assert s3 == 401, s3
print("4 令牌登录 OK，错误令牌 401 OK")

# 5. 重置接口
s, _, b = req("POST", "/api/reset-running", headers={"X-Auth-Token": AUTH_TOKEN})
assert s == 200, (s, b)
print("5 重置 OK")

print("ALL_API_TESTS_PASSED")
