"""每天定时触发发送任务。"""

from __future__ import annotations

import logging
import os
import random
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor as SchedulerThreadPoolExecutor
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .config import load_config
from .harvester import creator_map
from .send_day import business_now

logger = logging.getLogger("douyin-cloud-streak")
TZ = "Asia/Shanghai"

# 连续失败达到此轮数时升级邮件告警，不再因为历史失败跳过每日发送。
FAILURE_BREAKER_THRESHOLD = int(os.environ.get("FAILURE_BREAKER_THRESHOLD", "3"))

_scheduler: BackgroundScheduler | None = None
_run_func: Callable | None = None
_harvest_func: Callable | None = None
_prewarm_func: Callable | None = None
_session_check_func: Callable | None = None
_retry_dispatch_lock = threading.RLock()
_retry_registered: dict[object, str] = {}
_retry_dispatching: dict[tuple[object, str], datetime] = {}
_auto_start_lock = threading.RLock()
_auto_generation = 0
_auto_context = threading.local()


def auto_start_guard():
    """Serialize configuration commits with the last automatic-start decision."""
    return _auto_start_lock


def auto_generation() -> int:
    with _auto_start_lock:
        return _auto_generation


def automatic_run_generation() -> int | None:
    return getattr(_auto_context, "generation", None)


@contextmanager
def automatic_run_context(generation: int | None = None):
    previous = automatic_run_generation()
    _auto_context.generation = auto_generation() if generation is None else generation
    try:
        yield
    finally:
        _auto_context.generation = previous


class _RetryTrackingExecutor(SchedulerThreadPoolExecutor):
    """Track submission before APScheduler removes its one-shot jobstore row."""
    def __init__(self):
        super().__init__(max_workers=1)
        self._retry_versions = {}

    def submit_job(self, job, run_times):
        version = job.kwargs.get("queue_version", "")
        with self._lock:
            # Keep the executor's existing per-job concurrency guarantee. An
            # old completion must finish its version cleanup before a new submit.
            if self._instances[job.id] >= job.max_instances:
                return super().submit_job(job, run_times)
            with _retry_dispatch_lock:
                if not version or _retry_registered.get(self._scheduler) != version:
                    raise RuntimeError("补发计划已撤销，拒绝派发旧队列")
                _retry_dispatching[(self._scheduler, version)] = run_times[-1]
                self._retry_versions[job.id] = version
            try:
                return super().submit_job(job, run_times)
            except BaseException:
                self._finish_version(job.id)
                raise

    def _finish_version(self, job_id):
        version = self._retry_versions.pop(job_id, None)
        with _retry_dispatch_lock:
            _retry_dispatching.pop((self._scheduler, version), None)

    def _run_job_success(self, job_id, events):
        # Also called for misfires, where the user callback never runs.
        with self._lock:
            self._finish_version(job_id)
            super()._run_job_success(job_id, events)

    def _run_job_error(self, job_id, exc, traceback=None):
        with self._lock:
            self._finish_version(job_id)
            super()._run_job_error(job_id, exc, traceback)


def retry_is_dispatching(queue_version: str, due_at: datetime | None = None) -> bool:
    with _retry_dispatch_lock:
        due = _retry_dispatching.get((_scheduler, queue_version))
        return bool(due is not None and _retry_registered.get(_scheduler) == queue_version
                    and (due_at is None or due == due_at))


def _fire_run(generation: int | None = None) -> None:
    """真正触发一次发送，并吞掉「上一轮还没跑完」这类正常冲突。

    app._start_run 在已有任务时会抛 HTTPException(409)。这是并发保护在正常
    工作（例如上一轮发送还没结束），不该被 APScheduler 记成任务异常刷栈。
    """
    if not _run_func:
        return
    if not load_config().get("auto_run_enabled", True):
        return
    if generation is not None and generation != auto_generation():
        return
    try:
        with automatic_run_context(generation):
            _run_func()
    except Exception as e:
        logger.warning("本次定时发送未启动：%s", e)


def _daily_job() -> None:
    with auto_start_guard():
        immediate = _prepare_daily_job()
        generation = auto_generation()
    if immediate:
        _fire_run(generation)


def _prepare_daily_job() -> bool:
    cfg = load_config()
    if not bool(cfg.get("auto_run_enabled", True)):
        logger.info("自动运行已关闭（auto_run_enabled=false），本次定时任务跳过")
        return

    # 历史连续失败只用于升级告警；当前登录、验证码和限流仍由发送链路检查并停止。

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
            DateTrigger(run_date=business_now() + timedelta(seconds=delay), timezone=TZ),
            id="daily_send_jitter",
            replace_existing=True,
            misfire_grace_time=600,
            kwargs={"generation": auto_generation()},
        )
        return False
    return True


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
    return os.environ.get("SPARKKEEPER_BACKGROUND_TASKS", "0").strip().lower() in {"1", "true", "on", "yes"}


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
    with auto_start_guard():
        _apply_schedule_locked()


def _apply_schedule_locked() -> None:
    global _auto_generation
    cfg = load_config()
    if not cfg.get("auto_run_enabled", True):
        # Invalidate queued and already-dispatched callbacks before removing jobs.
        _auto_generation += 1
        if _scheduler is not None and _scheduler.get_job("daily_send_jitter"):
            _scheduler.remove_job("daily_send_jitter")
        from tasks_service import cancel_automatic_retry
        cancel_automatic_retry()
    if _scheduler is None:
        return
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
    # 提前量由环境变量 PREWARM_LEAD_MINUTES 控制，默认 2 分钟；
    # 每天 00:00 发送时默认在 23:58 预热，显式环境配置优先。
    if _prewarm_func:
        # 通用计算：发送时刻往前推 PREWARM_LEAD_MINUTES 分钟（自动处理跨小时/跨天）
        prewarm_lead = max(0, int(os.environ.get("PREWARM_LEAD_MINUTES", "2")))
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
    if (day in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"} and _harvest_func
            and getattr(creator_map, "implemented", False)):
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


def schedule_retry(run_func: Callable, delay_minutes: int = 45, *,
                   due_at: datetime | None = None, queue_version: str = "") -> bool:
    if _scheduler is None:
        return False
    run_at = due_at if due_at is not None else business_now() + timedelta(minutes=delay_minutes)
    if due_at is not None and due_at.utcoffset() is None:
        raise ValueError("补发计划时间必须带时区")
    options = {}
    if queue_version:
        if not getattr(_scheduler, "_spark_retry_executor_ready", False):
            _scheduler.add_executor(_RetryTrackingExecutor(), alias="retry")
            _scheduler._spark_retry_executor_ready = True
        with _retry_dispatch_lock:
            _retry_registered[_scheduler] = queue_version
        options["executor"] = "retry"
    try:
        _scheduler.add_job(
            run_func,
            DateTrigger(run_date=run_at, timezone=TZ),
            id="retry_send",
            replace_existing=True,
            kwargs={"queue_version": queue_version} if queue_version else {},
            **options,
        )
    except BaseException:
        with _retry_dispatch_lock:
            if _retry_registered.get(_scheduler) == queue_version:
                _retry_registered.pop(_scheduler, None)
        raise
    logger.info("已安排 %s 分钟后自动补发本次失败的好友", delay_minutes)
    return True


def retry_job_matches(queue_version: str, due_at: datetime | None = None) -> bool:
    """A persisted promise must correspond to the actual one-shot scheduler job."""
    if _scheduler is None or not queue_version:
        return False
    job = _scheduler.get_job("retry_send")
    return bool(job is not None and job.kwargs.get("queue_version") == queue_version
                and job.next_run_time is not None
                and (due_at is None or job.next_run_time == due_at))


def cancel_retry() -> None:
    with _retry_dispatch_lock:
        version = _retry_registered.pop(_scheduler, None)
        _retry_dispatching.pop((_scheduler, version), None)
    if _scheduler and _scheduler.get_job("retry_send"):
        _scheduler.remove_job("retry_send")
        logger.info("已取消待执行的补发任务")


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        cancel_retry()
        _scheduler.shutdown(wait=False)
        _scheduler = None
