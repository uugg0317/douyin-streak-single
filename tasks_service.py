"""后台任务：发送、失败补发、联系人同步、creator 采集。"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta

import app_common
from app_common import _mark_run_started, logger
from core import automation, ledger, scheduler
from core.harvester import creator_map
from core.config import load_config
from core.send_results import contact_names
from core.send_day import business_now, timestamp_date, daily_skip_code
from core.runtime import (
    load_runtime,
    record_contacts,
    record_harvest,
    record_run,
    set_running,
    update_runtime,
)
from fastapi import HTTPException
from session_service import notify_after_run, notify_catch_up_started


_catch_up_lock = threading.Lock()
_catch_up_started_date = ""
_retry_state_lock = threading.RLock()


def _qr_login_active() -> bool:
    # 路由会导入 tasks_service，因此仅在运行时查询，避免模块初始化循环。
    from routers.qr_credentials import qr_login_active
    return qr_login_active()


def ensure_today_sent(*, stop_event: threading.Event | None = None) -> bool:
    """仅补启动今天错过的任务；返回 True 不代表消息投递成功。"""
    global _catch_up_started_date
    if stop_event is not None and stop_event.is_set():
        return False
    if not scheduler.background_tasks_enabled():
        return False
    if app_common._env("CATCH_UP_MISSED_RUN").strip().lower() in {"off", "0", "false", "no"}:
        return False
    if not _catch_up_lock.acquire(blocking=False):
        return False
    started_date = None
    try:
        auto_generation = scheduler.auto_generation()
        cfg = load_config()
        if not bool(cfg.get("auto_run_enabled", True)):
            return False
        current = business_now()
        today = current.date().isoformat()
        try:
            hour, minute = map(int, str(cfg.get("schedule_time", "00:00")).split(":"))
            due = current.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(minutes=2)
        except (TypeError, ValueError):
            logger.warning("今日补跑检查跳过：每日执行时间配置无效")
            return False
        if current < due:
            return False
        rt = load_runtime()
        if rt.get("last_real_run_date") == today:
            return False
        # 旧 runtime 尚未写新字段时，已有当日非干跑记录也不能被误判为漏跑。
        previous = [rt.get("last_run")] + list(rt.get("history") or [])
        if any(isinstance(item, dict) and not item.get("dry_run")
               and timestamp_date(item.get("at")) == current.date() for item in previous):
            return False
        if rt.get("catch_up_date") == today or _catch_up_started_date == today:
            return False
        if app_common.run_lock.locked() or rt.get("running") or _qr_login_active():
            return False
        # 启动检查可能在线程池排队；关闭服务或跨越自然日后不能继续旧决策。
        if stop_event is not None and stop_event.is_set():
            return False
        if business_now().date().isoformat() != today:
            return False
        try:
            # _start_run 自己原子获取运行锁；并发手动任务抢先后不能记作补跑成功。
            with scheduler.automatic_run_context(auto_generation):
                if stop_event is None:
                    _start_run(False, require_qr_idle=True)
                else:
                    _start_run(False, require_qr_idle=True, stop_event=stop_event)
        except HTTPException as error:
            logger.info("今日补跑未启动：%s", error.detail)
            return False
        except Exception:
            logger.exception("今日补跑未启动，保留下次检查机会")
            return False
        _catch_up_started_date = started_date = today
        try:
            update_runtime(catch_up_date=today)
        except Exception:
            # 已启动的事实不能撤回；进程内继续阻止重复补跑，同时明确报告存储错误。
            logger.exception("已启动今日补跑，但未能保存补跑日期；本进程不会重复启动")
        logger.info("已启动今日补跑（%s）；实际发送结果仍以运行记录为准", today)
    except Exception:
        logger.exception("今日补跑检查失败，未确认启动")
        return False
    finally:
        _catch_up_lock.release()
    if started_date is not None:
        try:
            notify_catch_up_started(started_date)
        except Exception:
            logger.exception("今日补跑已启动，但启动通知未发送成功")
        return True
    return False


def _acquire_lock(blocking: bool = True) -> bool:
    """获取全局运行锁：发送 / 同步联系人 / creator 采集三者共用同一把闸门（原子）。"""
    return app_common.run_lock.acquire(blocking=blocking)


def _release_lock() -> None:
    try:
        app_common.run_lock.release()
    except RuntimeError:
        # 防御性兜底：正常情况下锁只会由持有它的 worker 释放。
        pass


def _start_run(dry: bool, only_names: list[str] | None = None,
               only_ids: list[str] | None = None, force: bool = False, *,
               require_qr_idle: bool = False, stop_event: threading.Event | None = None,
               expected_ledger_version: str | None = None) -> None:
    if not _acquire_lock(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行")
    with scheduler.auto_start_guard():
        _start_run_locked(dry, only_names=only_names, only_ids=only_ids, force=force,
                          require_qr_idle=require_qr_idle, stop_event=stop_event,
                          expected_ledger_version=expected_ledger_version)


def _check_automatic_start(required: bool = False) -> None:
    generation = scheduler.automatic_run_generation()
    if required or generation is not None:
        if not load_config().get("auto_run_enabled", True):
            raise HTTPException(status_code=409, detail="自动运行已关闭，未启动自动发送")
        if generation is not None and generation != scheduler.auto_generation():
            raise HTTPException(status_code=409, detail="自动计划已撤销，忽略旧任务")


def _start_run_locked(dry: bool, only_names: list[str] | None = None,
                       only_ids: list[str] | None = None, force: bool = False, *,
                       require_qr_idle: bool = False, stop_event: threading.Event | None = None,
                       expected_ledger_version: str | None = None) -> None:
    try:
        _check_automatic_start(required=require_qr_idle)
    except Exception:
        _release_lock()
        raise
    if require_qr_idle or stop_event is not None:
        try:
            if require_qr_idle and _qr_login_active():
                raise HTTPException(status_code=409, detail="扫码登录仍在进行，暂不启动自动补跑")
            if stop_event is not None and stop_event.is_set():
                raise HTTPException(status_code=409, detail="服务正在关闭，未启动自动补跑")
        except Exception:
            _release_lock()
            raise
    if expected_ledger_version is not None:
        try:
            snapshot = ledger.ledger_snapshot(expected_ledger_version)
            targets = [entry for entry in snapshot["entries"] if entry.get("selected")]
            if not targets:
                raise HTTPException(status_code=400, detail="未勾选任何好友！请先保存好友勾选后再执行。")
        except ledger.LedgerVersionConflict as error:
            _release_lock()
            raise HTTPException(status_code=409, detail=str(error)) from None
        except Exception:
            _release_lock()
            raise
        _begin_run_locked(dry, only_names=only_names, only_ids=only_ids, force=force,
                          selected_snapshot=targets)
    else:
        # Legacy callers keep their saved-ledger/config.friends migration contract.
        _begin_run_locked(dry, only_names=only_names, only_ids=only_ids, force=force)


def _begin_run_locked(dry: bool, only_names: list[str] | None = None,
                      only_ids: list[str] | None = None, force: bool = False, *,
                      selected_snapshot: list[dict] | None = None) -> None:
    """Owns the already-acquired run lock, including all failure cleanup."""
    _mark_run_started()
    old_pending = None
    try:
        if not dry:
            rt = load_runtime()
            with _retry_state_lock:
                old_pending = {key: rt.get(key) for key in ("pending_retry_entries", "retry_due_at",
                              "retry_business_date", "retry_queue_version", "retry_reason")}
                scheduler.cancel_retry()
                # Invalidate the plan before the worker starts; stale callbacks
                # must not consume a newer queue after this one finishes.
                update_runtime(pending_retry_entries=[], retry_status="none", retry_due_at=None,
                               retry_business_date=None, retry_queue_version=None, retry_reason=None)
    except Exception:
        _release_lock()
        raise

    def worker() -> None:
        try:
            set_running(True)
            try:
                kwargs = {"dry_run": dry, "only_names": only_names}
                if only_ids is not None:
                    kwargs["only_ids"] = only_ids
                if force:
                    kwargs["force"] = True
                if selected_snapshot is not None:
                    kwargs["selected_snapshot"] = selected_snapshot
                result = automation.run_send(**kwargs)
                system_failures = sum(
                    1 for item in result.get("failed", [])
                    if isinstance(item, dict) and item.get("name") == "_system"
                )
                unconfirmed = [item for item in result.get("skipped", [])
                               if isinstance(item, dict) and item.get("code") in {"delivery_unconfirmed", "unconfirmed_today"}]
                logger.info("%s完成：通过 %s 人，失败 %s 人，待处理 %s 人，待核对 %s 人，系统异常 %s 项",
                            "模拟演练（未发送消息）" if dry else "本次发送",
                            len(contact_names(result.get("ok", []))),
                            len(contact_names(result.get("failed", []))),
                            len(contact_names(result.get("deferred", []))),
                            len(contact_names(unconfirmed)), system_failures)
                if not dry:
                    _schedule_retry(result)
                record_run(result)
                if not dry:
                    notify_after_run(result)
            finally:
                set_running(False)
        finally:
            _release_lock()

    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception:
        try:
            if old_pending is not None:
                update_runtime(**old_pending, retry_status="manual" if old_pending["pending_retry_entries"] else "none")
        finally:
            _release_lock()
        raise


def _collect_retry_names(result: dict) -> list[str]:
    return list(dict.fromkeys(item["name"] for item in _collect_retry_entries(result)))


def _collect_retry_entries(result: dict) -> list[dict]:
    """Only unsent contacts or explicitly retryable pre-send failures are safe."""
    entries = []
    seen = set()
    resolved_names = contact_names(result.get("ok", []))
    blocked_names = contact_names(result.get("skipped", []))
    for field in ("failed", "deferred"):
        for item in result.get(field, []):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip() or name == "_system":
                continue
            if (name in resolved_names or name in blocked_names
                    or item.get("retryable") is False
                    or item.get("code") in {"delivery_unconfirmed", "delivery_rejected", "identity_ambiguous"}
                    or (field == "failed" and item.get("retryable") is not True)):
                continue
            entry_id = str(item.get("entry_id") or "")
            key = ("id", entry_id) if entry_id else ("name", name)
            if key not in seen:
                seen.add(key)
                entries.append({"entry_id": entry_id, "name": name})
    return entries


def _start_pending_retry(*, expected_queue_version: str | None = None) -> None:
    """Resolve the saved queue to current selected IDs before starting a run."""
    if not _acquire_lock(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行")
    with scheduler.auto_start_guard():
        _begin_pending_retry_locked(expected_queue_version=expected_queue_version)


def _begin_pending_retry_locked(*, expected_queue_version: str | None = None) -> None:
    try:
        _check_automatic_start(required=expected_queue_version is not None)
        with _retry_state_lock:
            rt = load_runtime()
            if expected_queue_version is not None:
                if rt.get("retry_queue_version") != expected_queue_version:
                    raise HTTPException(status_code=409, detail="补发队列已更新，忽略旧任务")
                if rt.get("retry_status") not in {"scheduled", "scheduling"}:
                    raise HTTPException(status_code=409, detail="补发计划已转为手动处理")
                if rt.get("retry_business_date") != business_now().date().isoformat():
                    _manual_retry_locked("补发计划已跨业务日期，请手动处理")
                    raise HTTPException(status_code=400, detail="补发计划已跨业务日期")
                _manual_retry_locked("补发计划正在尝试启动")
            ids = _pending_ids_locked()
    except Exception:
        _release_lock()
        raise
    # Queue read, identity resolution and consumption use the same lock epoch.
    # A stale concurrent request cannot replay a queue after another run consumed it.
    _begin_run_locked(False, only_ids=ids)


def _safe_retry_entries(pending: list[dict]) -> list[dict]:
    selected = ledger.get_selected()
    by_id = {str(e.get("entry_id")): e for e in selected if e.get("entry_id")}
    entries = []
    seen = set()
    current = business_now()
    for item in pending:
        if not isinstance(item, dict):
            continue
        entry_id = str(item.get("entry_id") or "")
        if entry_id:
            match = by_id.get(entry_id)
        else:
            # Historical results may only contain names. Never guess between
            # two equal display names when resolving an old result.
            matches = [e for e in selected if e.get("display_name") == item.get("name")]
            match = matches[0] if len(matches) == 1 else None
        if (match and match.get("entry_id") and not ledger.identity_block_reason(match)
                and not daily_skip_code(match, now=current) and match["entry_id"] not in seen):
            entries.append({"entry_id": str(match["entry_id"]), "name": match["display_name"]})
            seen.add(match["entry_id"])
    return entries


def _pending_ids_locked() -> list[str]:
    rt = load_runtime()
    ids = [entry["entry_id"] for entry in _safe_retry_entries(rt.get("pending_retry_entries") or [])]
    if not ids:
        raise HTTPException(status_code=400, detail="没有可安全继续的未完成好友，请先检查好友勾选和身份")
    return ids


def _scheduled_pending_retry(queue_version: str | None = None) -> None:
    # A consumed/busy job is no longer an automatic promise. Keep the queue
    # available for an explicit continuation if the worker cannot acquire its lock.
    try:
        with scheduler.automatic_run_context():
            if queue_version is None:
                update_runtime(retry_status="manual")
                _start_pending_retry()
            else:
                _start_pending_retry(expected_queue_version=queue_version)
    except HTTPException as error:
        with _retry_state_lock:
            if queue_version is None or load_runtime().get("retry_queue_version") == queue_version:
                _manual_retry_locked(str(error.detail))
        logger.warning("补发未启动，保留待处理名单：%s", error.detail)


def _retry_due(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        return parsed if isinstance(parsed, datetime) and parsed.utcoffset() is not None else None
    except (ValueError, TypeError):
        return None


def _manual_retry_locked(reason: str) -> None:
    rt = load_runtime()
    update_runtime(retry_status="manual" if rt.get("pending_retry_entries") else "none", retry_reason=reason)


def cancel_automatic_retry() -> None:
    """Called under the automatic-start guard; never acquire/wait for run_lock here."""
    with _retry_state_lock:
        scheduler.cancel_retry()
        rt = load_runtime()
        pending = rt.get("pending_retry_entries") or []
        update_runtime(retry_status="manual" if pending else "none", retry_due_at=None,
                       retry_queue_version=uuid.uuid4().hex if pending else None,
                       retry_reason="自动运行已关闭，未完成好友保留为手动处理" if pending else None)


def _register_retry_locked(due: datetime, version: str, business_date: str) -> bool:
    """Persist a recoverable reservation, then register and confirm the real job."""
    update_runtime(retry_due_at=due.isoformat(), retry_queue_version=version,
                   retry_business_date=business_date, retry_status="scheduling", retry_reason=None)
    try:
        registered = scheduler.schedule_retry(_scheduled_pending_retry, due_at=due, queue_version=version)
        if not registered or not scheduler.retry_job_matches(version, due):
            raise RuntimeError("调度器未持有对应补发任务")
        update_runtime(retry_status="scheduled", retry_date=business_date, retry_reason=None)
        return True
    except Exception:
        scheduler.cancel_retry()
        logger.exception("未能确认补发计划，保留待处理名单")
        try:
            _manual_retry_locked("补发计划注册或保存失败，请手动继续")
        except Exception:
            # The prepared queue remains durable, even if the status rewrite
            # also fails. Startup recovery or the status view will reconcile it.
            logger.exception("补发降级状态未能保存，原队列仍保留")
        return False


def restore_retry_plan() -> None:
    """Startup after intent recovery: restore safe, future plans; retain the rest manually."""
    with _retry_state_lock:
        rt = load_runtime()
        scheduler.cancel_retry()
        entries = _safe_retry_entries(rt.get("pending_retry_entries") or [])
        if not entries:
            update_runtime(pending_retry_entries=[], retry_status="none", retry_due_at=None,
                           retry_business_date=None, retry_queue_version=None, retry_reason=None)
            return
        update_runtime(pending_retry_entries=entries)
        due = _retry_due(rt.get("retry_due_at"))
        current = business_now()
        if (rt.get("retry_status") not in {"scheduled", "scheduling"} or due is None
                or due <= current or rt.get("retry_business_date") != current.date().isoformat()
                or not scheduler.background_tasks_enabled() or not load_config().get("auto_run_enabled", True)
                or app_common.run_lock.locked()):
            _manual_retry_locked("原补发计划已过期、缺少完整信息或当前不能自动恢复，请手动继续")
            return
        _register_retry_locked(due, str(rt.get("retry_queue_version") or uuid.uuid4().hex), rt["retry_business_date"])


def retry_plan_view() -> dict:
    """Reconcile a vanished/mismatched one-shot job before showing 'scheduled'."""
    with _retry_state_lock:
        rt = load_runtime()
        if rt.get("retry_status") in {"scheduled", "scheduling"}:
            due = _retry_due(rt.get("retry_due_at"))
            version = str(rt.get("retry_queue_version") or "")
            if due is not None and scheduler.retry_is_dispatching(version, due):
                rt.update(retry_status="dispatching", retry_dispatching=True)
            elif due is None or not scheduler.retry_job_matches(version, due):
                fields = {"retry_status": "manual" if rt.get("pending_retry_entries") else "none",
                          "retry_reason": "自动补发任务已不存在，请手动继续"}
                rt.update(fields)
                try:
                    update_runtime(**fields)
                except Exception:
                    logger.exception("补发状态校准未能保存，接口按实际任务返回")
        return rt


def _schedule_retry(result: dict) -> None:
    """Persist a safe continuation queue; claim scheduling only after success."""
    with scheduler.auto_start_guard(), _retry_state_lock:
        entries = _collect_retry_entries(result)
        scheduler.cancel_retry()
        result["pending_retry_count"] = len(entries)
        result["retry_status"] = "manual" if entries else "none"
        current = business_now()
        today = current.date().isoformat()
        version = uuid.uuid4().hex if entries else None
        rt = load_runtime()
        update_runtime(pending_retry_entries=entries, retry_status=result["retry_status"], retry_due_at=None,
                       retry_business_date=today if entries else None, retry_queue_version=version, retry_reason=None)
        if not entries:
            return
        if (scheduler.background_tasks_enabled() and load_config().get("auto_run_enabled", True)
                and not result.get("logged_out")
                and not result.get("stopped") and not result.get("intent_write_failed")
                and not result.get("credentials_missing")
                and not result.get("rate_limited") and rt.get("retry_date") != today):
            if _register_retry_locked(current + timedelta(minutes=45), version, today):
                result["retry_status"] = "scheduled"
        if result["retry_status"] == "manual":
            logger.info("保留 %s 位未完成好友，可在概览点击“继续未完成”", len(entries))


def _start_fetch_contacts() -> str:
    if not _acquire_lock(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行")
    job_id = uuid.uuid4().hex
    try:
        _mark_run_started()
        update_runtime(contacts_job_id=job_id, contacts_job_status="running", contacts_error=None)
        app_common.contacts_fetching = True
    except Exception:
        _release_lock()
        raise

    def worker() -> None:
        try:
            try:
                data = automation.fetch_chat_contacts()
                if not isinstance(data, dict) or not isinstance(data.get("names", []), list):
                    raise ValueError("联系人同步返回结构无效")
                if not data.get("error") and (data.get("names") or data.get('capture')):
                    stats = ledger.merge_consumer_contacts(data["names"], capture=data.get('capture') or {'complete':False})
                    logger.info("台账已同步：新增 %s 人，更新 %s 人，共 %s 人",
                                 stats["added"], stats["updated"], stats["total"])
                # A successful terminal state is published only after ledger merge.
                record_contacts(data, job_id=job_id)
            except Exception as error:
                logger.warning("联系人同步未完成（%s）", type(error).__name__)
                try:
                    record_contacts({"names": [], "error": "本次联系人同步失败，请检查凭证或稍后重试"}, job_id=job_id)
                except Exception:
                    logger.warning("联系人失败状态无法保存，本次结果保持未确认")
            finally:
                app_common.contacts_fetching = False
        finally:
            _release_lock()

    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception:
        app_common.contacts_fetching = False
        try:
            record_contacts({"names": [], "error": "联系人同步线程未能启动"}, job_id=job_id)
        finally:
            _release_lock()
        raise
    return job_id


def _start_harvest_creator() -> None:
    """后台线程执行 creator 抖音号采集 + 台账合并（只读，不发送消息）。"""
    if not _acquire_lock(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行，请稍后再试")
    app_common.harvesting = True
    _mark_run_started()

    def worker() -> None:
        try:
            res = creator_map.collect_short_id_map()
            merge_stats = None
            if res.get("mapping"):
                merge_stats = ledger.merge_creator_map(res["mapping"])
                res["merge"] = merge_stats
                logger.info("creator 采集合并完成：%s 条映射，join %s 人，新增 %s 人，共 %s 人",
                             res["count"], merge_stats["joined"], merge_stats["added"],
                             merge_stats["total"])
            harvest_last = {
                "at": res.get("at"), "count": res.get("count"),
                "hit": res.get("hit"), "error": res.get("error"), "merge": merge_stats,
            }
            record_harvest(harvest_last)
        finally:
            app_common.harvesting = False
            _release_lock()

    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception:
        app_common.harvesting = False
        _release_lock()
        raise


def _scheduled_harvest() -> None:
    try:
        _start_harvest_creator()
    except HTTPException as e:
        logger.warning("周级采集跳过：%s", e.detail)
