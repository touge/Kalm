# -*- coding: utf-8 -*-
"""
WebSocket 进度透传端点
=======================
前端连 ws://host:port/interface/tasks/{task_id}/ws 即可收到后端实时推送。
- ComfyUI 等原生 WS 后端：executor 通过 ws_manager 实时推送进度
- TTS 等 HTTP 轮询后端：ws_proxy 内部轮询 TaskManager 兜底，任务完成时通知
"""

import asyncio
from fastapi import APIRouter, WebSocket

from src.core.task_manager import TaskManager
from src.core.ws_manager import ws_manager
from src.logic.logger import log

router = APIRouter(tags=["WebSocket"])

POLL_INTERVAL = 2  # 兜底轮询间隔


@router.websocket("/tasks/{task_id}/ws")
async def task_websocket(websocket: WebSocket, task_id: str):
    await websocket.accept()

    # 先注册，再检查状态，防止 executor 在检查和注册之间完成任务的竞态
    done_event = ws_manager.register(task_id, websocket)

    task = TaskManager.get_task(task_id)
    status = task.get("status", "not_found")

    if status == "not_found":
        await websocket.send_json({"type": "error", "message": "Task not found"})
        await websocket.close()
        ws_manager.unregister(task_id)
        return

    if status == TaskManager.STATUS_SUCCESS:
        await websocket.send_json({
            "type": "task_complete",
            "status": "success",
            "result": task.get("result"),
        })
        await websocket.close()
        ws_manager.unregister(task_id)
        return

    if status == TaskManager.STATUS_FAILED:
        await websocket.send_json({
            "type": "task_complete",
            "status": "failed",
            "result": task.get("result"),
        })
        await websocket.close()
        ws_manager.unregister(task_id)
        return

    log.info(f"[WS] Task {task_id} connected, status={status}")

    async def poll_fallback():
        """兜底：对不支持原生 WS 的任务类型，轮询 TaskManager 检测完成"""
        while not done_event.is_set():
            await asyncio.sleep(POLL_INTERVAL)
            current = TaskManager.get_task(task_id)
            current_status = current.get("status", "")
            if current_status in (TaskManager.STATUS_SUCCESS, TaskManager.STATUS_FAILED):
                msg_type = "task_complete" if current_status == TaskManager.STATUS_SUCCESS else "task_failed"
                try:
                    await websocket.send_json({
                        "type": msg_type,
                        "status": current_status,
                        "result": current.get("result"),
                    })
                except Exception:
                    pass
                done_event.set()
                return

    poll_task = asyncio.create_task(poll_fallback())

    try:
        await done_event.wait()
    except Exception:
        pass
    finally:
        poll_task.cancel()
        ws_manager.unregister(task_id)
