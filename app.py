"""sparkkeeper 单账号自动服务入口（FastAPI）。

业务逻辑已拆分到各服务模块与 routers/ 路由包，本文件只负责应用装配与生命周期。
"""

from __future__ import annotations

import os
import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app_common import PID_PATH, STATIC_DIR, logger
from auth_service import _check_auth_config
from core import automation, scheduler
from core.runtime import set_running, update_runtime
from email_service import migrate_legacy_email_config
from instance_lock import _acquire_instance_lock
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
from tasks_service import _scheduled_harvest, _start_run
from watchdog_service import _start_watchdog


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _check_auth_config()
    _acquire_instance_lock()
    background_enabled = scheduler.background_tasks_enabled()
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
    else:
        logger.info("本机控制台已启动：后台定时发送、采集、预启动、体检和看门狗均暂停（SPARKKEEPER_BACKGROUND_TASKS=0）。")
    try:
        yield
    finally:
        try:
            await asyncio.to_thread(qr_credentials_router.shutdown_qr_login)
        finally:
            scheduler.shutdown()
            try:
                PID_PATH.unlink(missing_ok=True)
            except Exception:
                pass


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
    port = int(os.environ.get("PORT", "8000"))
    # URL 兼容登录可能包含敏感查询串；访问日志由 Nginx 按脱敏规则负责。
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
