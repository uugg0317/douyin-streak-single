"""每天定时触发发送任务。"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .config import load_config
from .runtime import load_runtime, update_runtime

logger = logging.getLogger("douyin-cloud-streak")
TZ = "Asia/Shanghai"

# 连续失败达到此轮数即熔断暂停（默认 3 轮；定时任务每天一轮，即连挂 3 天才停）
FAILURE_BREAKER_THRESHOLD = int(os.environ.get("FAILURE_BREAKER_THRESHOLD", "3"))
# 熔断冷却时长（秒），默认 6 小时；冷却结束自动恢复并清零计数
FAILURE_BREAKER_COOLDOWN = int(os.environ.get("FAILURE_BREAKER_COOLDOWN", str(6 * 3600)))

_scheduler: BackgroundScheduler | None = None
_run_func: Callable | None = None
_harvest_func: Callable | None = None
_prewarm_func: Callable | None = None
_session_check_func: Callable | None = None


def _fire_run() -> None:
    """真正触发一次发送，并吞掉「上一轮还没跑完」这类正常冲突。

    app._start_run 在已有任务时会抛 HTTPException(409)。这是并发保护在正常
    工作（例如上一轮发送还没结束），不该被 APScheduler 记成任务异常刷栈。
    """
    if not _run_func:
        return
    try:
        _run_func()
    except Exception as e:
        logger.warning("本次定时发送未启动：%s", e)


def _daily_job() -> None:
    cfg = load_config()
    if not bool(cfg.get("auto_run_enabled", True)):
        logger.info("自动运行已关闭（auto_run_enabled=false），本次定时任务跳过")
        return

    # ── 熔断：连续失败达阈值则暂停自动发送，冷却期结束自动恢复 ──
    # 只拦定时任务；手动「立即续火花」走 /api.run 不经此处，仍可强制试一次。
    # 不改写 config.json 的 auto_run_enabled（那会污染用户配置且无法区分是谁关的），
    # 暂停状态只存 runtime.json 的 auto_paused_until 时间戳。
    st = load_runtime()
    now = time.time()
    paused_until = float(st.get("auto_paused_until") or 0)
    if paused_until and now < paused_until:
        logger.warning(
            "自动发送处于熔断暂停中，剩余 %.0f 分钟（连续失败 %s 轮）",
            (paused_until - now) / 60, st.get("consecutive_failures"),
        )
        return
    if paused_until and now >= paused_until:
        # 冷却结束：清零计数与暂停标记，自动恢复，今天正常尝试一次
        update_runtime(auto_paused_until=0, consecutive_failures=0)
        st["consecutive_failures"] = 0  # 同步本地快照，避免下面立即又触发熔断
        logger.info("熔断冷却期结束，已自动恢复自动发送")
    if int(st.get("consecutive_failures") or 0) >= FAILURE_BREAKER_THRESHOLD:
        update_runtime(auto_paused_until=now + FAILURE_BREAKER_COOLDOWN)
        logger.error(
            "连续失败 %s 轮已达阈值，暂停自动发送 %s 分钟（手动「立即续火花」仍可用）",
            st.get("consecutive_failures"), FAILURE_BREAKER_COOLDOWN // 60,
        )
        return

    # 注意：不能写 `cfg.get("jitter_minutes") or 30`。
    # 配置里合法的 0（＝不抖动、0 点准时发）是 falsy，会被 or 吞掉变回 30。
    # 线上日志即为证据：配置 jitter_minutes=0，却打印「随机延迟 1205 秒后开始发送」，
    # 实际发送从 00:00 拖到 00:20。
    try:
        jitter = max(0, int(cfg.get("jitter_minutes") or 0))
    except (TypeError, ValueError):
        jitter = 0
    if jitter and _scheduler is not None:
        # 抖动窗口用「再排一个一次性任务」实现，而不是在当前调度线程里 sleep：
        # APScheduler 的线程池是有限资源，sleep 满 jitter 分钟会一直占着一个
        # 工作线程，把同一时刻的其他任务（预启动、周级采集、补发）一起顶住。
        delay = random.uniform(0, jitter * 60)
        logger.info("随机延迟 %.0f 秒后开始发送（抖动窗口 %s 分钟）", delay, jitter)
        _scheduler.add_job(
            _fire_run,
            DateTrigger(run_date=datetime.now() + timedelta(seconds=delay), timezone=TZ),
            id="daily_send_jitter",
            replace_existing=True,
            misfire_grace_time=600,
        )
        return
    _fire_run()


def _prewarm_job() -> None:
    """浏览器预启动任务：在每日发送前 PREWARM_LEAD_MINUTES 分钟调用，提前启动浏览器并加载页面。"""
    cfg = load_config()
    if not bool(cfg.get("auto_run_enabled", True)):
        logger.info("自动运行已关闭，跳过浏览器预启动")
        return
    if _prewarm_func:
        try:
            _prewarm_func()
        except Exception as e:
            logger.warning("浏览器预启动任务异常: %s", e)


def _session_check_job() -> None:
    """登录态体检任务：调用注册的体检函数（掉线/临期由其负责发预警邮件）。"""
    if not _session_check_func:
        return
    try:
        _session_check_func()
    except Exception as e:
        logger.warning("登录态体检任务异常: %s", e)


def background_tasks_enabled() -> bool:
    """Process-only switch for starting a local console without account jobs."""
    return os.environ.get("SPARKKEEPER_BACKGROUND_TASKS", "1").strip().lower() not in {"0", "false", "off", "no"}


def configure(
    run_func: Callable,
    harvest_func: Callable | None = None,
    prewarm_func: Callable | None = None,
    session_check_func: Callable | None = None,
) -> None:
    """注册每日发送任务与（可选）周级采集、浏览器预启动、登录态体检任务。"""
    global _scheduler, _run_func, _harvest_func, _prewarm_func, _session_check_func
    _run_func = run_func
    _harvest_func = harvest_func
    _prewarm_func = prewarm_func
    _session_check_func = session_check_func
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=TZ)
        _scheduler.start()
    apply_schedule()


def _parse_hhmm(value) -> tuple[int, int]:
    """解析 HH:MM 配置；非法值告警并回退 00:00，避免调度器因脏数据起不来。"""
    try:
        hh_s, mm_s = str(value).split(":")
        hh, mm = int(hh_s), int(mm_s)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(f"越界: {value!r}")
        return hh, mm
    except Exception as e:
        logger.warning("schedule_time 配置非法（%s），已回退为 00:00", e)
        return 0, 0


def _parse_check_time() -> tuple[int, int]:
    """解析登录态体检时刻 SESSION_CHECK_TIME（HH:MM）；非法回退 12:30。"""
    raw = os.environ.get("SESSION_CHECK_TIME", "12:30").strip()
    try:
        hh_s, mm_s = raw.split(":")
        hh, mm = int(hh_s), int(mm_s)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except Exception:
        pass
    logger.warning("SESSION_CHECK_TIME 配置非法（%r），已回退 12:30", raw)
    return 12, 30


def apply_schedule() -> None:
    if _scheduler is None:
        return
    cfg = load_config()
    hh, mm = _parse_hhmm(cfg.get("schedule_time", "21:00"))
    _scheduler.add_job(
        _daily_job,
        CronTrigger(hour=hh, minute=mm, timezone=TZ),
        id="daily_send",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )
    logger.info("定时任务已更新：每天 %02d:%02d (%s)", hh, mm, TZ)

    # 浏览器预启动任务：在发送时间前 PREWARM_LEAD_MINUTES 分钟触发，提前启动
    # 浏览器、打开聊天页并等待联系人列表加载完成。
    #
    # 提前量由环境变量 PREWARM_LEAD_MINUTES 控制（默认 1 分钟；实测冷启动到
    # 列表就绪仅约 9 秒）。当前设置为 30 分钟：23:30 预启动、00:00 发送。
    if _prewarm_func:
        # 通用计算：发送时刻往前推 PREWARM_LEAD_MINUTES 分钟（自动处理跨小时/跨天）
        prewarm_lead = max(0, int(os.environ.get("PREWARM_LEAD_MINUTES", "1")))
        prewarm_total = hh * 60 + mm - prewarm_lead
        prewarm_hh = prewarm_total // 60 % 24
        prewarm_mm = prewarm_total % 60
        prewarm_ss = 0
        _scheduler.add_job(
            _prewarm_job,
            CronTrigger(hour=prewarm_hh, minute=prewarm_mm, second=prewarm_ss, timezone=TZ),
            id="daily_prewarm",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=180,
        )
        logger.info("浏览器预启动任务已更新：每天 %02d:%02d:%02d (%s)（发送前%s分钟）",
                    prewarm_hh, prewarm_mm, prewarm_ss, TZ, prewarm_lead)
    elif _scheduler.get_job("daily_prewarm"):
        _scheduler.remove_job("daily_prewarm")
        logger.info("浏览器预启动任务已移除")

    # 登录态体检：每天固定时刻启动一次性浏览器验证，掉线 / Cookie 临期提前邮件预警
    if _session_check_func:
        check_hh, check_mm = _parse_check_time()
        _scheduler.add_job(
            _session_check_job,
            CronTrigger(hour=check_hh, minute=check_mm, timezone=TZ),
            id="daily_session_check",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info("登录态体检任务已更新：每天 %02d:%02d (%s)", check_hh, check_mm, TZ)
    elif _scheduler.get_job("daily_session_check"):
        _scheduler.remove_job("daily_session_check")
        logger.info("登录态体检任务已移除")

    # 周级 creator 抖音号采集（默认周一 03:00；off/空 = 关闭）
    day = str(cfg.get("schedule_harvest_day") or "off").strip().lower()
    if day in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"} and _harvest_func:
        _scheduler.add_job(
            _harvest_func,
            CronTrigger(day_of_week=day, hour=3, minute=0, timezone=TZ),
            id="weekly_harvest",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info("周级采集已更新：每周 %s 03:00 (%s)", day, TZ)
    elif _scheduler.get_job("weekly_harvest"):
        _scheduler.remove_job("weekly_harvest")
        logger.info("周级采集已关闭")


def next_run_time() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job("daily_send")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def next_harvest_time() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job("weekly_harvest")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def schedule_retry(run_func: Callable, delay_minutes: int = 45) -> None:
    if _scheduler is None:
        return
    if _scheduler.get_job("retry_send"):
        return
    run_at = datetime.now() + timedelta(minutes=delay_minutes)
    _scheduler.add_job(
        run_func,
        DateTrigger(run_date=run_at, timezone=TZ),
        id="retry_send",
        replace_existing=True,
    )
    logger.info("已安排 %s 分钟后自动补发本次失败的好友", delay_minutes)


def cancel_retry() -> None:
    if _scheduler and _scheduler.get_job("retry_send"):
        _scheduler.remove_job("retry_send")
        logger.info("已取消待执行的补发任务")


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
