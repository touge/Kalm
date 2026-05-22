# -*- coding: utf-8 -*-
"""
队列 WebSocket 广播端点
========================
前端连 ws://host:port/interface/queue/ws 即可收到任务生命周期通知：
  - task_enqueued : 新任务入队
  - task_started  : 任务开始执行
  - task_completed: 任务完成（含下一任务信息）

所有连上的客户端收到相同的广播消息，客户端按 task_id 自行过滤。
"""

import uuid
from fastapi import APIRouter, WebSocket

from src.core.ws_manager import ws_manager
from src.logic.logger import log

router = APIRouter(tags=["Queue Broadcast"])


@router.websocket("/queue/ws")
async def queue_websocket(websocket: WebSocket):
    await websocket.accept()
    ws_id = str(uuid.uuid4())
    ws_manager.subscribe_queue(ws_id, websocket)

    try:
        while True:
            # 保持连接，等待客户端消息（客户端发 ping，我们回 pong）
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except Exception:
        pass
    finally:
        ws_manager.unsubscribe_queue(ws_id)
        log.info(f"[QueueWS] Subscriber '{ws_id}' disconnected")
