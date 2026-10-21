"""登录态临期预警：每日定时体检，掉线 / Cookie 临期即邮件提醒（当天还有时间补救）。"""

from __future__ import annotations

import os
import socket
import threading
import time

import app_common
from app_common import logger
from core import automation
from core.runtime import load_runtime, update_runtime
from core.send_day import business_now
from email_service import _render_template, _send_email, load_email_config

# 每天体检时刻（HH:MM）；默认 12:30，距 0 点发送约 11.5 小时，便于在面板重新扫码
SESSION_CHECK_TIME = os.environ.get("SESSION_CHECK_TIME", "12:30").strip()

WARN_SUBJECT = "【预警】抖音登录态异常，请在面板重新扫码（{host}）"
WARN_BODY = (
    "抖音自动续火花检测到登录态异常，请在今日发送前处理：\n\n"
    "服务器：{host}（解析 IP {host_ip}）\n"
    "检测时间：{time}\n\n"
    "异常原因：{reason}\n\n"
    "处理方法：\n"
    "1. 打开面板「凭证」页，开始扫码登录；\n"
    "2. 使用抖音手机端扫码并确认，等待面板显示凭证已保存；\n"
    "3. 查看账号与登录状态，再由你决定是否手动发送；保存凭证不代表消息已投递。\n\n"
    "{cookie}\n"
)

FAILURE_SUBJECT = "【升级告警】抖音续火花连续 {failures} 轮未成功（{host}）"
FAILURE_BODY = (
    "抖音自动续火花已连续 {failures} 轮未成功，达到升级告警阈值 {threshold} 轮。\n\n"
    "服务器：{host}\n检查时间：{time}\n\n"
    "手动运行和续发也会计入轮次，此数字不代表连续自然日。\n"
    "请检查登录态、好友定位和最近运行日志。历史失败不会再使每日定时发送跳过；"
    "当前登录异常、验证码或限流仍会停止本轮，需要由你处理。\n"
)
_failure_warning_lock = threading.Lock()
_catch_up_notice_lock = threading.Lock()

CATCH_UP_SUBJECT = "【补跑已启动】抖音续火花（{host}）"
CATCH_UP_BODY = (
    "已启动今日补跑（业务日期：{date}，Asia/Shanghai）。\n\n"
    "服务器：{host}\n启动检查时间：{time}\n\n"
    "这封邮件只确认补跑任务已经启动，不代表消息已投递或火花已续上。"
    "实际成功、失败和待核对结果请查看控制台运行记录。"
    "同一天不会因为此通知再次启动补跑。\n"
)


def notify_catch_up_started(run_date: str) -> bool:
    """补跑启动通知独立去重，沿用预警邮件开关，发信成功后才标记。"""
    if not _warn_email_enabled():
        return False
    with _catch_up_notice_lock:
        rt = load_runtime()
        if rt.get("catch_up_notice_date") == run_date:
            return False
        values = {
            "host": socket.gethostname(), "date": run_date,
            "time": business_now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        cfg = load_email_config()
        _send_email(cfg, _render_template(CATCH_UP_SUBJECT, values), _render_template(CATCH_UP_BODY, values))
        update_runtime(catch_up_notice_date=run_date)
        logger.info("已发送今日补跑启动通知（%s）；不代表消息投递成功", run_date)
        return True


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
    today = business_now().strftime("%Y-%m-%d")
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
        "time": business_now().strftime("%Y-%m-%d %H:%M:%S"),
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
    # 补跑会重新获取 run_lock，必须等体检释放后再检查，避免把自己当成忙碌。
    try:
        from tasks_service import ensure_today_sent
        ensure_today_sent()
    except Exception:
        logger.exception("登录态体检后的今日补跑检查失败")


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
    if result.get("dry_run"):
        return
    try:
        if result.get("logged_out"):
            health = {"cookie": automation.scan_state_cookies()}
            _send_warning("今晨发送时发现登录态已失效（页面跳转登录 / 出现二维码）", health)
        elif (result.get("failed") or []) and not result.get("ok"):
            _send_warning("今晨发送 0 成功，可能登录态失效或页面结构变化", None)
    except Exception as e:
        logger.warning("发送后预警处理失败：%s", e)
    # 单独处理，普通预警今日已发或发送失败都不能吞掉升级告警。
    try:
        _send_failure_warning()
    except Exception as e:
        logger.warning("连续失败升级告警发送失败：%s", e)


def _send_failure_warning() -> bool:
    """连续失败达到阈值时每天独立告警一次；成功投递后才标记。"""
    if not _warn_email_enabled():
        return False
    from core.scheduler import FAILURE_BREAKER_THRESHOLD

    with _failure_warning_lock:
        rt = load_runtime()
        failures = int(rt.get("consecutive_failures") or 0)
        if failures < FAILURE_BREAKER_THRESHOLD:
            return False
        today = business_now().strftime("%Y-%m-%d")
        if rt.get("failure_warning_date") == today:
            return False
        values = {
            "host": socket.gethostname(),
            "time": business_now().strftime("%Y-%m-%d %H:%M:%S"),
            "failures": failures,
            "threshold": FAILURE_BREAKER_THRESHOLD,
        }
        cfg = load_email_config()
        _send_email(cfg, _render_template(FAILURE_SUBJECT, values), _render_template(FAILURE_BODY, values))
        update_runtime(failure_warning_date=today)
        logger.warning("已发送连续 %s 轮未成功的升级告警", failures)
        return True
