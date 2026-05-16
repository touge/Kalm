# -*- coding: utf-8 -*-
"""
TTS 任务执行器
==============
纯透传模式：接收前端 payload → 提交到 TTS 后端 → 轮询状态 → 原样透传结果。
文件访问由前端自行通过 /file 端点完成。
"""

import requests
import time
from src.core.service_controller import service_controller
from src.core.task_manager import TaskManager
from src.logic.logger import log

RESOURCE_NAME = "TTS"
POLL_INTERVAL = 2
MAX_POLL_SECONDS = 600


def execute(task_id: str, **payload):
    """
    TTS 生成任务入口。由调度器动态调用。

    payload 期望字段:
      - path: str — 后端生成接口路径，如 "/v1.5/generate"
      - 其余字段原样 POST 给后端
    """
    path = payload.pop("path", "/v1.5/generate")

    svc = service_controller.get_service_config(RESOURCE_NAME)
    if not svc:
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
            {"message": f"Service '{RESOURCE_NAME}' not configured"})
        return

    base_url = f"{svc['host']}:{svc['port']}"
    token = svc.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    submit_url = f"{base_url}{path}"

    log.info(f"[TTS Executor] Task {task_id}: submit to {submit_url}")

    try:
        TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING,
            {"message": "Submitting to TTS service..."})

        # 阶段一：提交
        resp = requests.post(submit_url, json=payload, headers=headers, timeout=(5, 10))
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:200]
            TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
                {"message": f"TTS rejected: {resp.status_code}", "detail": detail})
            return

        result = resp.json()
        svc_task_id = result.get("data", {}).get("task_id")
        if not svc_task_id:
            TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
                {"message": "TTS returned no task_id"})
            return

        log.info(f"[TTS Executor] Task {task_id} -> {svc_task_id}")

        # 阶段二：轮询
        elapsed = 0
        while elapsed < MAX_POLL_SECONDS:
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            try:
                sr = requests.get(
                    f"{base_url}/status/{svc_task_id}",
                    headers=headers, timeout=(5, 10),
                )
                if sr.status_code != 200:
                    continue

                status_data = sr.json()
                svc_status = status_data.get("status")

                TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, result=status_data)

                if svc_status == "failed":
                    TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, result=status_data)
                    return

                if svc_status == "completed":
                    log.info(f"[TTS Executor] Task {task_id} completed.")
                    TaskManager.update_task(task_id, TaskManager.STATUS_SUCCESS, result=status_data)
                    return

            except requests.RequestException as e:
                log.warning(f"[TTS Executor] Task {task_id}: poll error: {e}")

        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
            {"message": f"Polling timeout after {MAX_POLL_SECONDS}s"})

    except Exception as e:
        log.error(f"[TTS Executor] Task {task_id} failed: {e}", exc_info=True)
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})
