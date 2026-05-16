# -*- coding: utf-8 -*-
"""
Kalm FastAPI 应用工厂
=============================
生命周期：启动时开启任务清理线程，关闭时优雅停止调度器。
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.logic.yaml_config_loader import yaml_config_loader
from src.logic.task_cleanup import start_cleanup_thread
from src.core.scheduler import scheduler
from src.core.ws_manager import ws_manager
from src.logic.logger import log
from src.api.routes import system, tasks, llm, file_proxy, ws_proxy, stream_proxy


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.success("[Kalm] Starting up...")
    start_cleanup_thread()
    ws_manager.loop = asyncio.get_running_loop()
    yield
    ws_manager.close_all()
    log.info("[Kalm] Shutting down...")
    scheduler.shutdown()
    await file_proxy.close_client()
    log.success("[Kalm] Shutdown complete.")


def create_app() -> FastAPI:
    app_config = yaml_config_loader.get("app", {})
    debug = yaml_config_loader.get("app.debug", False)

    app = FastAPI(
        title=app_config.get("name", "Kalm"),
        description="纯中转控制站 — 前端与 AI 后端之间的任务调度与文件代理桥梁",
        version=app_config.get("version", "1.0.0"),
        debug=debug,
        lifespan=lifespan,
    )

    # 静态文件挂载（任务产物目录）
    task_folder = yaml_config_loader.get("paths.task_folder", "tasks")
    Path(task_folder).mkdir(parents=True, exist_ok=True)
    app.mount("/files", StaticFiles(directory=task_folder), name="files")

    # 注册路由
    app.include_router(system.router, prefix="/interface")
    app.include_router(tasks.router, prefix="/interface")
    app.include_router(llm.router, prefix="/interface")
    app.include_router(ws_proxy.router, prefix="/interface")
    app.include_router(stream_proxy.router, prefix="/interface")
    app.include_router(file_proxy.router)

    return app


app = create_app()
