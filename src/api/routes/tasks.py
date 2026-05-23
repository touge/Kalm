# -*- coding: utf-8 -*-
"""
任务端点
========
POST /tasks/submit   — 提交任务到 FIFO 队列（支持 JSON 和 multipart/form-data）
GET  /tasks/{id}/status — 查询任务状态
"""

import json
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Request

from src.core.response import success, error
from src.core.task_manager import TaskManager
from src.core.scheduler import scheduler
from src.logic.yaml_config_loader import yaml_config_loader
from src.logic.logger import log

router = APIRouter(tags=["Tasks"])

TASK_FOLDER = Path(yaml_config_loader.get("paths.task_folder", "tasks"))
TYPE_ALIASES = {}


@router.post("/tasks/submit")
async def submit_task(request: Request):
    """统一任务提交。支持 JSON 和 multipart/form-data 两种请求格式。"""
    content_type = request.headers.get("content-type", "")

    # ---- multipart/form-data（文件上传类任务，如 subtitle）----
    if "multipart/form-data" in content_type:
        form = await request.form()
        task_type = form.get("task_type")
        if not task_type:
            return error("Missing 'task_type' in form data", 400)

        # 收集所有非文件字段为 payload
        payload: Dict[str, Any] = {}
        saved_files: Dict[str, str] = {}  # field_name → local_path

        for field_name, value in form.items():
            if field_name == "task_type":
                continue
            if hasattr(value, "filename"):
                # 文件字段：保存到任务目录
                filename = value.filename or f"{field_name}.bin"
                ext = Path(filename).suffix or ".bin"
                # 先创建临时目录，用 task_id 命名（稍后分配）
                saved_files[field_name] = {
                    "content": await value.read(),
                    "filename": filename,
                    "ext": ext,
                }
            else:
                payload[field_name] = value

        task_id = TaskManager.create_task(task_type)

        # 将上传文件保存到任务目录
        task_dir = TASK_FOLDER / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        for field_name, finfo in saved_files.items():
            file_path = task_dir / f"{field_name}{finfo['ext']}"
            file_path.write_bytes(finfo["content"])
            payload[f"{field_name}_path"] = str(file_path)
            log.info(f"[API] Saved uploaded file: {file_path} ({len(finfo['content'])} bytes)")

    # ---- JSON（普通任务）----
    else:
        try:
            body = await request.json()
        except Exception:
            return error("Request body must be valid JSON, or use multipart/form-data for file uploads", 400)

        task_type = body.get("task_type", "")
        payload = body.get("payload", {})
        log.info(f"[API] 客户端原始请求: task_type='{task_type}', payload keys={list(payload.keys())}")

        if not task_type:
            return error("Missing 'task_type' in request body", 400)

        task_id = TaskManager.create_task(task_type)

    # ---- 提交到调度器 ----
    internal_type = TYPE_ALIASES.get(task_type, task_type)
    known_types = list(scheduler.task_executors.keys())
    if internal_type not in known_types:
        return error(f"Unknown task type: '{task_type}'. Known types: {known_types}", 400)

    try:
        log.info(f"[API] >>> submit_task 调用前: type={internal_type}, task_id={task_id}")
        scheduler.submit_task(internal_type, task_id, payload)
        log.info(f"[API] >>> submit_task 返回后: type={internal_type}, task_id={task_id}")
        TaskManager.update_task(task_id, TaskManager.STATUS_QUEUED)
        log.info(f"[API] Task {task_id} submitted: type={internal_type}")

        tasks_config = yaml_config_loader.get("tasks", {})
        track_mode = tasks_config.get(internal_type, {}).get("track_mode", "poll")

        hint = _track_hint(track_mode, task_id)

        return success(data={
            "task_id": task_id,
            "status": TaskManager.STATUS_QUEUED,
            "track_mode": track_mode,
            "hint": hint,
        })
    except Exception as e:
        log.error(f"[API] Failed to submit task: {e}")
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})
        return error(f"Failed to submit task: {str(e)}", 500)


def _track_hint(mode: str, task_id: str) -> str:
    if mode == "ws":
        return f"Connect to ws://host:port/interface/tasks/{task_id}/ws for real-time progress"
    elif mode == "poll":
        return f"Poll GET /interface/tasks/{task_id}/status for results"
    elif mode == "stream":
        return "Streaming response is already in progress via NDJSON"
    return "Unknown track mode"


@router.get("/tasks")
async def list_tasks():
    """返回当前所有活跃任务状态。前端轮询这一个端点即可。"""
    return success(data={"tasks": TaskManager.list_tasks()})


@router.get("/tasks/{task_id}/status")
async def get_task_status(task_id: str):
    task = TaskManager.get_task(task_id)
    if task.get("status") == "not_found":
        return error(f"Task '{task_id}' not found", 404)

    return success(data=task)
