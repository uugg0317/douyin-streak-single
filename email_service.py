"""告警邮件：模板渲染、SMTP 投递与旧配置迁移。

配置归属（配置体系统一，每个设置只有一个权威存放处）：
- SMTP 连接与授权码（SMTP_HOST/PORT/USER/PASS）在 .env，属部署密钥；
- 告警策略（收件地址 mail_to、主题 email_subject、正文 email_body）在 data/config.json；
- 不再使用 data/email_config.json：旧文件在应用启动时自动迁移，随后改名留档。
"""

from __future__ import annotations

import json

from app_common import _env, logger, save_env_values
from core.config import (
    DATA_DIR,
    DEFAULT_CONFIG,
    atomic_write_text,
    load_config,
    save_config,
)

LEGACY_EMAIL_CONFIG_PATH = DATA_DIR / "email_config.json"


def _redact_legacy_archive(path) -> None:
    """旧配置留档只能保留非密钥字段，避免 SMTP 授权码形成第二份明文副本。"""
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "smtp_pass" in data:
            data["smtp_pass"] = "[redacted]"
            atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning("旧邮箱配置留档脱敏失败：%s", e)


def load_email_config() -> dict:
    """组装邮件配置：连接信息取 .env，告警策略取 config.json。"""
    biz = load_config()
    smtp_user = _env("SMTP_USER")
    try:
        port = int(_env("SMTP_PORT") or "465")
    except ValueError:
        port = 465
    return {
        "smtp_host": _env("SMTP_HOST"),
        "smtp_port": port,
        "smtp_user": smtp_user,
        "smtp_pass": _env("SMTP_PASS"),
        "mail_to": str(biz.get("mail_to") or "").strip() or smtp_user,
        "subject": biz.get("email_subject") or DEFAULT_CONFIG["email_subject"],
        "body": biz.get("email_body") or DEFAULT_CONFIG["email_body"],
    }


def _pass_looks_valid(p: str) -> bool:
    """授权码格式粗校验：非纯数字且 8 位以上（主流邮箱授权码均为 16 位字母）。"""
    return bool(p) and not str(p).isdigit() and len(str(p)) >= 8


def save_email_config(raw: dict) -> dict:
    """保存邮件配置：连接/授权码写 .env，告警策略写 config.json。

    smtp_pass 留空或为掩码时保留原值；纯数字/过短的假授权码直接拒绝。
    """
    new_pass = str(raw.get("smtp_pass", "") or "")
    if new_pass and new_pass != "******" and not _pass_looks_valid(new_pass):
        raise ValueError(
            "这不是 SMTP 授权码：授权码通常是 16 位左右的字母组合，"
            "6 位数字是开启服务时的验证码，请勿填入授权码框"
        )

    env_updates: dict = {}
    host = str(raw.get("smtp_host", "") or "").strip()
    user = str(raw.get("smtp_user", "") or "").strip()
    port_raw = str(raw.get("smtp_port", "") or "").strip()
    if host:
        env_updates["SMTP_HOST"] = host
    if user:
        env_updates["SMTP_USER"] = user
    if port_raw:
        try:
            port = int(port_raw)
        except ValueError:
            raise ValueError("SMTP 端口必须是数字")
        if not 1 <= port <= 65535:
            raise ValueError("SMTP 端口必须在 1~65535 之间")
        env_updates["SMTP_PORT"] = port_raw
    if new_pass and new_pass != "******":
        env_updates["SMTP_PASS"] = new_pass
    if env_updates:
        save_env_values(env_updates)

    biz_updates: dict = {}
    mail_to = str(raw.get("mail_to", "") or "").strip()
    subject = str(raw.get("subject", "") or "").strip()
    body = str(raw.get("body", "") or "").strip()
    if mail_to:
        biz_updates["mail_to"] = mail_to
    if subject:
        biz_updates["email_subject"] = subject
    if body:
        biz_updates["email_body"] = body
    if biz_updates:
        cur = load_config()
        cur.update(biz_updates)
        save_config(cur)

    return load_email_config()


def migrate_legacy_email_config() -> None:
    """一次性迁移旧 data/email_config.json。

    连接信息并入 .env、告警策略并入 config.json，完成后原文件改名
    email_config.json.migrated 留档；.env 已有的键不覆盖，无效授权码
    （如误存的 6 位验证码）丢弃。
    """
    p = LEGACY_EMAIL_CONFIG_PATH
    archive = p.with_name("email_config.json.migrated")
    _redact_legacy_archive(archive)
    if not p.exists():
        return
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("旧 email_config.json 解析失败，跳过迁移：%s", e)
        return
    if not isinstance(raw, dict):
        return

    env_updates: dict = {}
    if not _env("SMTP_HOST") and raw.get("smtp_host"):
        env_updates["SMTP_HOST"] = str(raw["smtp_host"]).strip()
    if not _env("SMTP_USER") and raw.get("smtp_user"):
        env_updates["SMTP_USER"] = str(raw["smtp_user"]).strip()
    if not _env("SMTP_PORT") and raw.get("smtp_port"):
        env_updates["SMTP_PORT"] = str(raw["smtp_port"]).strip()
    legacy_pass = str(raw.get("smtp_pass", "") or "")
    if not _env("SMTP_PASS") and _pass_looks_valid(legacy_pass):
        env_updates["SMTP_PASS"] = legacy_pass
    if env_updates:
        save_env_values(env_updates)

    biz = load_config()
    changed = False
    if not biz.get("mail_to") and raw.get("mail_to"):
        biz["mail_to"] = str(raw["mail_to"]).strip()
        changed = True
    if biz.get("email_subject") in (None, "", DEFAULT_CONFIG["email_subject"]) and raw.get("subject"):
        biz["email_subject"] = str(raw["subject"])
        changed = True
    if biz.get("email_body") in (None, "", DEFAULT_CONFIG["email_body"]) and raw.get("body"):
        biz["email_body"] = str(raw["body"])
        changed = True
    if changed:
        save_config(biz)

    try:
        archived = dict(raw)
        if "smtp_pass" in archived:
            archived["smtp_pass"] = "[redacted]"
        atomic_write_text(archive, json.dumps(archived, ensure_ascii=False, indent=2))
        p.unlink()
        logger.info("已迁移旧 email_config.json，并生成脱敏留档")
    except OSError as e:
        logger.warning("旧 email_config.json 脱敏留档失败：%s", e)


def _render_template(tpl: str, values: dict) -> str:
    """安全渲染模板：未知占位符原样保留，格式错误时返回原文。"""
    try:
        class _Default(dict):
            def __missing__(self, key):
                return "{" + key + "}"
        return str(tpl).format_map(_Default(values))
    except Exception:
        return str(tpl)


def _send_email(cfg: dict, subject: str, body: str) -> None:
    """按配置实际投递一封邮件，配置不全或重试后仍失败时抛异常。

    连接级故障（QQ SMTP 临时限流会主动掐连接，表现为
    Connection unexpectedly closed）自动重试 3 次、退避等待；
    认证失败等非连接错误不重试。
    """
    import smtplib
    import time
    from email.message import EmailMessage

    smtp_host = cfg.get("smtp_host", "")
    smtp_user = cfg.get("smtp_user", "")
    smtp_pass = cfg.get("smtp_pass", "")
    mail_to = cfg.get("mail_to", "") or smtp_user
    if not (smtp_host and smtp_user and smtp_pass and mail_to):
        raise RuntimeError(
            "邮箱配置不完整（需要 SMTP 服务器、发信地址、授权码、收件地址）"
        )
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = mail_to
    msg.set_content(body)
    port = int(cfg.get("smtp_port", 465))

    last_err: Exception | None = None
    for attempt in (1, 2, 3):
        stage = "连接"
        s = None
        try:
            if port == 465:
                s = smtplib.SMTP_SSL(smtp_host, port, timeout=15)
            else:
                s = smtplib.SMTP(smtp_host, port, timeout=15)
                s.ehlo()
                s.starttls()
            stage = "登录"
            s.login(smtp_user, smtp_pass)
            stage = "发送"
            s.send_message(msg)
            s.quit()
            return
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
                ConnectionError, OSError) as e:
            last_err = e
            logger.warning("邮件%s阶段连接异常（第%s次，将重试）：%s", stage, attempt, e)
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
            time.sleep(2 * attempt)
    assert last_err is not None
    raise last_err
