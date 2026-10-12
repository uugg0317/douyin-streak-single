"""登录态临期预警：每日定时体检，掉线 / Cookie 临期即邮件提醒（当天还有时间补救）。"""

from __future__ import annotations

import os
import socket
import time
from datetime import datetime

import app_common
from app_common import logger
from core import automation
from core.runtime import load_runtime, update_runtime
from email_service import _render_template, _send_email, load_email_config

# 每天体检时刻（HH:MM）；默认 12:30，距 0 点发送约 11.5 小时，足够重新部署登录态
SESSION_CHECK_TIME = os.environ.get("SESSION_CHECK_TIME", "12:30").strip()

WARN_SUBJECT = "【预警】抖音登录态异常，请尽快重新部署（{host}）"
WARN_BODY = (
    "抖音自动续火花检测到登录态异常，请在今日发送前处理：\n\n"
    "服务器：{host}（解析 IP {host_ip}）\n"
    "检测时间：{time}\n\n"
    "异常原因：{reason}\n\n"
    "处理方法：\n"
    "1. 在电脑浏览器登录抖音网页版，重新导出登录态 state.json；\n"
    "2. 打开面板「凭证」页上传新的 state.json；\n"
    "3. 可在「概览」页点「立即续火花」手动验证一次。\n\n"
    "{cookie}\n"
)


def _cookie_summary(health: dict | None) -> str:
    ci = (health or {}).get("cookie") or {}
    if not ci.get("has_state"):
        return "登录态文件：缺失"
    if not ci.get("has_session"):
        return "登录态文件：未包含 sessionid Cookie（可能已失效）"
    if ci.get("expired"):
        return "Cookie 状态：关键 Cookie 已过期"
    if ci.get("expiring_soon"):
        return "Cookie 状态：临期 —— " + ci.get("details", "")
    if ci.get("min_days") is not None:
        return "Cookie 状态：正常，" + ci.get("details", "")
    return "Cookie 状态：会话型 Cookie，无法预知到期（以实际页面检测为准）"


def _warn_email_enabled() -> bool:
    """登录态预警邮件总开关 SESSION_WARN_EMAIL_ENABLED（默认开启）。

    本机调试没有真实登录态，定时体检/手动运行会反复触发误报，本地 .env 可设为 off；
    看门狗卡死告警邮件不走此开关，不受影响。
    """
    return app_common._env("SESSION_WARN_EMAIL_ENABLED").lower() not in {
        "off", "0", "false", "no"
    }


def _send_warning(reason: str, health: dict | None = None, force: bool = False) -> bool:
    """按邮箱配置发送登录态预警；同一天内默认只发一次（force 可覆盖）。"""
    if not _warn_email_enabled():
        logger.info("登录态预警邮件已被 SESSION_WARN_EMAIL_ENABLED=off 关闭，跳过发送")
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    rt = load_runtime()
    if not force and rt.get("session_warn_date") == today:
        logger.info("登录态预警今日已发送，跳过重复邮件")
        return False
    cfg = load_email_config()
    host = socket.gethostname()
    try:
        host_ip = socket.gethostbyname(host)
    except Exception:
        host_ip = "?"
    values = {
        "host": host,
        "host_ip": host_ip,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
        "cookie": _cookie_summary(health),
    }
    _send_email(
        cfg,
        _render_template(WARN_SUBJECT, values),
        _render_template(WARN_BODY, values),
    )
    update_runtime(session_warn_date=today)
    logger.warning("登录态预警邮件已发送至 %s：%s", cfg.get("mail_to"), reason)
    return True


def run_session_check() -> None:
    """定时体检入口：此刻有任务在跑则跳过（任务本身已验证登录态）。"""
    if not app_common.run_lock.acquire(blocking=False):
        logger.info("登录态体检：有任务正在运行，本次跳过")
        return
    try:
        app_common._mark_run_started()
        _run_session_check_locked()
    finally:
        app_common.run_lock.release()


def _run_session_check_locked() -> None:
    """Hold the credential replacement lock until this check's result is saved."""
    try:
        from core.config import load_config

        if not bool(load_config().get("auto_run_enabled", True)):
            logger.info("登录态体检：自动运行已关闭，本次跳过")
            return
    except Exception:
        pass

    logger.info("开始登录态体检...")
    try:
        health = automation.check_session_health()
    except Exception as e:
        logger.warning("登录态体检执行失败：%s", e)
        return

    if health.get("infra_error"):
        logger.warning("登录态体检基础设施异常（非登录态问题），本次不发预警：%s", health.get("reason"))
        return

    logged_in = health.get("logged_in")
    if logged_in is True or logged_in is False:
        update_runtime(
            session_status="ok" if logged_in else "expired",
            session_checked_at=time.time(),
        )

    ci = health.get("cookie") or {}
    problems: list[str] = []
    if not health.get("logged_in"):
        problems.append(health.get("reason") or "页面登录检测未通过")
    if not ci.get("has_state"):
        problems.append("登录态文件缺失")
    elif not ci.get("has_session"):
        problems.append("state.json 中无 sessionid Cookie")
    elif ci.get("expired"):
        problems.append("关键 Cookie 已过期")
    elif ci.get("expiring_soon"):
        problems.append("关键 Cookie 临期：" + ci.get("details", ""))

    if problems:
        try:
            _send_warning("；".join(problems), health)
        except Exception as e:
            logger.warning("登录态预警邮件发送失败：%s", e)
    else:
        extra = f"（{ci.get('details', '')}）" if ci.get("details") else ""
        logger.info("登录态体检正常%s", extra)
        update_runtime(session_warn_date="")  # 恢复健康：清除当日已发标记


def notify_after_run(result: dict) -> None:
    """真实发送后调用：掉线 / 0 成功时补一封预警（best-effort，失败只记日志）。"""
    try:
        if result.get("logged_out"):
            health = {"cookie": automation.scan_state_cookies()}
            _send_warning("今晨发送时发现登录态已失效（页面跳转登录 / 出现二维码）", health)
        elif (result.get("failed") or []) and not result.get("ok"):
            _send_warning("今晨发送 0 成功，可能登录态失效或页面结构变化", None)
    except Exception as e:
        logger.warning("发送后预警处理失败：%s", e)
