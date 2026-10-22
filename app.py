"""sparkkeeper 单账号自动服务入口（FastAPI）。

业务逻辑已拆分到各服务模块与 routers/ 路由包，本文件只负责应用装配与生命周期。
"""

from __future__ import annotations

import os
import asyncio
import threading
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app_common import PID_PATH, STATIC_DIR, logger
from auth_service import _check_auth_config
from request_origin import _check_local_bind
from core import automation, ledger, scheduler
from core.runtime import set_running, update_runtime
from email_service import migrate_legacy_email_config
from instance_lock import _acquire_instance_lock, _clear_instance_metadata
from middleware import register_middleware
from session_service import run_session_check
from routers import (
    auth as auth_router,
    config as config_router,
    contacts_ledger as contacts_ledger_router,
    credentials as credentials_router,
    email as email_router,
    pages as pages_router,
    qr_credentials as qr_credentials_router,
    run as run_router,
)
from tasks_service import _scheduled_harvest, _start_run, ensure_today_sent, restore_retry_plan
from watchdog_service import _start_watchdog


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _check_auth_config()
    _acquire_instance_lock()
    # Recovery must finish before any scheduled/manual work can start. Failure
    # is fatal to startup rather than silently making an uncertain send eligible.
    recovered = ledger.recover_interrupted_attempts()
    if recovered:
        logger.warning("已将 %s 条中断发送转为待核对，不会自动重发", recovered)
    background_enabled = scheduler.background_tasks_enabled()
    catch_up_task = None
    catch_up_stop = threading.Event()
    if background_enabled:
        # 配置体系统一：把旧 data/email_config.json 一次性迁移到 .env / config.json
        try:
            migrate_legacy_email_config()
        except Exception as e:
            logger.warning("旧邮箱配置迁移异常（不影响启动）：%s", e)
    # 本进程可能是看门狗判定卡死后由 systemd 拉起的：清理上个进程残留的
    # running / 发送进度状态，避免前端一直显示「进行中」
    set_running(False)
    try:
        update_runtime(send_progress=None)
    except Exception:
        pass
    if background_enabled:
        try:
            scheduler.configure(
                lambda: _start_run(False),
                harvest_func=_scheduled_harvest,
                prewarm_func=automation.prewarm_browser,
                session_check_func=run_session_check,
            )
        except Exception as e:
            logger.warning("调度器启动失败: %s", e)
        _start_watchdog()
        # 独立线程检查补跑，不在 API 事件循环内执行文件检查、启动器或发信。
        catch_up_task = asyncio.create_task(_check_startup_catch_up(catch_up_stop), name="startup-catch-up")
    else:
        logger.info("本机控制台已启动：后台定时发送、采集、预启动、体检和看门狗均暂停（SPARKKEEPER_BACKGROUND_TASKS=0）。")
    restore_retry_plan()
    try:
        yield
    finally:
        # 尚未执行/仍在排队的启动检查停止启动新任务；不宣称取消已启动的发送线程。
        catch_up_stop.set()
        automation.request_send_stop("服务正在退出；未确认结束前不会启动新的发送，请等待或退出后重启。")
        try:
            # 不取消仍在运行的 to_thread：取消协程不能停底层线程，必须先等检查结束。
            if catch_up_task is not None:
                await catch_up_task
            await asyncio.to_thread(qr_credentials_router.shutdown_qr_login)
        finally:
            scheduler.shutdown()
            _clear_instance_metadata()


async def _check_startup_catch_up(stop_event: threading.Event) -> None:
    if stop_event.is_set():
        return
    try:
        await asyncio.to_thread(ensure_today_sent, stop_event=stop_event)
    except Exception:
        logger.exception("服务启动后的今日补跑检查失败")


app = FastAPI(
    title="sparkkeeper",
    lifespan=lifespan,
    # 加固：生产环境关闭公开 API 文档与 OpenAPI 描述，减少攻击侦察面
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

register_middleware(app)

app.include_router(pages_router.router)
app.include_router(auth_router.router)
app.include_router(contacts_ledger_router.router)
app.include_router(config_router.router)
app.include_router(email_router.router)
app.include_router(run_router.router)
app.include_router(credentials_router.router)
app.include_router(qr_credentials_router.router)


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")  # 加固：默认只监听本机，由 nginx 反代
    _check_local_bind(host)
    port = int(os.environ.get("PORT", "8000"))
    # URL 兼容登录可能包含敏感查询串；访问日志由 Nginx 按脱敏规则负责。
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False, proxy_headers=False)
