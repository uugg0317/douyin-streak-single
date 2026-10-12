"""后台任务：发送、失败补发、联系人同步、creator 采集。"""

from __future__ import annotations

import threading
from datetime import datetime

import app_common
from app_common import _mark_run_started, logger
from core import automation, ledger, scheduler
from core.harvester import creator_map
from core.send_results import contact_names
from core.runtime import (
    load_runtime,
    record_contacts,
    record_harvest,
    record_run,
    set_running,
    update_runtime,
)
from fastapi import HTTPException
from session_service import notify_after_run


def _acquire_lock(blocking: bool = True) -> bool:
    """获取全局运行锁：发送 / 同步联系人 / creator 采集三者共用同一把闸门（原子）。"""
    return app_common.run_lock.acquire(blocking=blocking)


def _release_lock() -> None:
    try:
        app_common.run_lock.release()
    except RuntimeError:
        # 防御性兜底：正常情况下锁只会由持有它的 worker 释放。
        pass


def _start_run(dry: bool, only_names: list[str] | None = None) -> None:
    if not _acquire_lock(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行")
    _mark_run_started()

    def worker() -> None:
        try:
            set_running(True)
            try:
                result = automation.run_send(dry_run=dry, only_names=only_names)
                record_run(result)
                system_failures = sum(
                    1 for item in result.get("failed", [])
                    if isinstance(item, dict) and item.get("name") == "_system"
                )
                unconfirmed = [item for item in result.get("skipped", [])
                               if isinstance(item, dict) and item.get("code") == "delivery_unconfirmed"]
                logger.info("%s完成：通过 %s 人，失败 %s 人，待处理 %s 人，待核对 %s 人，系统异常 %s 项",
                            "模拟演练（未发送消息）" if dry else "本次发送",
                            len(contact_names(result.get("ok", []))),
                            len(contact_names(result.get("failed", []))),
                            len(contact_names(result.get("deferred", []))),
                            len(contact_names(unconfirmed)), system_failures)
                if (not dry and (result.get("failed") or result.get("deferred"))
                        and not result.get("logged_out")):
                    _schedule_retry(result)
                elif not dry:
                    scheduler.cancel_retry()
                if not dry:
                    notify_after_run(result)
            finally:
                set_running(False)
        finally:
            _release_lock()

    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception:
        _release_lock()
        raise


def _collect_retry_names(result: dict) -> list[str]:
    names = [
        item["name"]
        for key in ("failed", "deferred")
        for item in result.get(key, [])
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item["name"] not in {"", "_system"}
    ]
    # 保序去重：同一联系人可能既失败又因限流进入 deferred。
    return list(dict.fromkeys(names))


def _schedule_retry(result: dict) -> None:
    """安排 45 分钟后补发失败或因预算延期的好友。"""
    failed_names = _collect_retry_names(result)
    if not failed_names:
        return
    rt = load_runtime()
    today = datetime.now().date().isoformat()
    if rt.get("retry_date") != today:
        update_runtime(retry_date=today)
        scheduler.schedule_retry(lambda: _start_run(False, failed_names))


def _start_fetch_contacts() -> None:
    if not _acquire_lock(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行")
    _mark_run_started()

    def worker() -> None:
        try:
            app_common.contacts_fetching = True
            try:
                data = automation.fetch_chat_contacts()
                record_contacts(data)
                if data.get("names"):
                    stats = ledger.merge_consumer_contacts(data["names"])
                    logger.info("台账已同步：新增 %s 人，更新 %s 人，共 %s 人",
                                 stats["added"], stats["updated"], stats["total"])
            finally:
                app_common.contacts_fetching = False
        finally:
            _release_lock()

    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception:
        _release_lock()
        raise


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
