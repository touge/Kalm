# -*- coding: utf-8 -*-
"""
LLM 端点
========
GET /llm/models — 列出可用本地模型
POST /llm/generate — 提交 LLM 生成任务（等同于 POST /tasks/submit with task_type=llm）
"""

from typing import Any, Dict, Optional
from fastapi import APIRouter
from pydantic import BaseModel

from src.core.response import success, error
from src.core.task_manager import TaskManager
from src.core.scheduler import scheduler
from src.logic.yaml_config_loader import yaml_config_loader
from src.logic.logger import log

router = APIRouter(tags=["LLM"])


class GenerateRequest(BaseModel):
    model: str
    prompt: str
    options: Optional[Dict[str, Any]] = {}


@router.get("/llm/models")
async def list_models():
    llm_config = yaml_config_loader.get("llm_config", {})
    return success(data={
        "models": llm_config.get("local_models", []),
    })


@router.post("/llm/generate")
async def generate(req: GenerateRequest):
    task_type = "llm"
    task_id = TaskManager.create_task(task_type)
    payload = {"model": req.model, "prompt": req.prompt, "options": req.options}

    try:
        scheduler.submit_task(task_type, task_id, payload)
        TaskManager.update_task(task_id, TaskManager.STATUS_QUEUED)
        log.info(f"[LLM API] Task {task_id}: model={req.model}")
        return success(data={
            "task_id": task_id,
            "status": TaskManager.STATUS_QUEUED,
            "message": "LLM task queued. Poll GET /tasks/{task_id}/status for results.",
        })
    except Exception as e:
        log.error(f"[LLM API] Failed to submit: {e}")
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})
        return error(str(e), 500)
