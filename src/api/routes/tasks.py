# -*- coding: utf-8 -*-
"""
任务端点
========
POST /tasks/submit   — 提交任务到 FIFO 队列
GET  /tasks/{id}/status — 查询任务状态
"""

from typing import Any, Dict
from fastapi import APIRouter
from pydantic import BaseModel

from src.core.response import success, error
from src.core.task_manager import TaskManager
from src.core.scheduler import scheduler
from src.logic.yaml_config_loader import yaml_config_loader
from src.logic.logger import log

router = APIRouter(tags=["Tasks"])


class TaskSubmitRequest(BaseModel):
    task_type: str
    payload: Dict[str, Any]


TYPE_ALIASES = {}


@router.post("/tasks/submit")
async def submit_task(req: TaskSubmitRequest):
    internal_type = TYPE_ALIASES.get(req.task_type, req.task_type)

    known_types = list(scheduler.task_executors.keys())
    if internal_type not in known_types:
        return error(f"Unknown task type: '{req.task_type}'. Known types: {known_types}", 400)

    task_id = TaskManager.create_task(internal_type)

    try:
        scheduler.submit_task(internal_type, task_id, req.payload)
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
