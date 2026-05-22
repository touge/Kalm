# -*- coding: utf-8 -*-
"""
WebSocket 连接管理器
====================
管理前端 WS 连接，桥接同步执行器线程 → 异步 WS 推送。

执行器线程调用 send() → run_coroutine_threadsafe → 推送到前端 WS。
"""

import asyncio
import threading
from typing import Dict, Optional
from fastapi import WebSocket

from src.logic.logger import log


class WebSocketManager:
    _instance: Optional["WebSocketManager"] = None
    _init_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._connections: Dict[str, WebSocket] = {}
        self._events: Dict[str, asyncio.Event] = {}
        self._queue_subscribers: Dict[str, WebSocket] = {}  # 队列广播订阅者
        self._lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._initialized = True

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def register(self, task_id: str, websocket: WebSocket) -> asyncio.Event:
        """注册前端 WS 连接。返回一个 asyncio.Event，任务完成时会被 set。"""
        with self._lock:
            self._connections[task_id] = websocket
        if self._loop:
            event = asyncio.Event()
            self._events[task_id] = event
            return event
        return asyncio.Event()

    def send(self, task_id: str, message: dict):
        """从同步线程推送消息到前端 WS。"""
        if not self._loop:
            return
        with self._lock:
            ws = self._connections.get(task_id)
        if ws:
            asyncio.run_coroutine_threadsafe(
                self._send_async(task_id, ws, message), self._loop
            )

    async def _send_async(self, task_id: str, ws: WebSocket, message: dict):
        try:
            await ws.send_json(message)
            msg_type = message.get("type", "")
            if msg_type in ("task_complete", "task_failed"):
                event = self._events.get(task_id)
                if event:
                    event.set()
        except Exception:
            log.warning(f"[WSManager] Failed to send to task {task_id}, disconnecting")
            await self._close(task_id, ws)

    def unregister(self, task_id: str):
        """移除连接。从同步线程调用。"""
        with self._lock:
            ws = self._connections.pop(task_id, None)
        if ws and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._close(task_id, ws), self._loop
            )

    async def _close(self, task_id: str, ws: WebSocket):
        """异步关闭 WS 连接"""
        self._events.pop(task_id, None)
        try:
            await ws.close()
        except Exception:
            pass

    def close_all(self):
        """关闭所有连接（shutdown 时调用）"""
        with self._lock:
            task_ids = list(self._connections.keys())
        for task_id in task_ids:
            self.unregister(task_id)
        with self._queue_lock:
            subs = list(self._queue_subscribers.keys())
        for sub_id in subs:
            self.unsubscribe_queue(sub_id)

    # =========================================================================
    # 队列广播（全局通知频道）
    # =========================================================================

    def subscribe_queue(self, ws_id: str, websocket: WebSocket):
        """订阅队列广播。ws_id 为连接标识，用于取消订阅。"""
        with self._queue_lock:
            self._queue_subscribers[ws_id] = websocket
        log.info(f"[WSManager] Queue subscriber '{ws_id}' connected ({len(self._queue_subscribers)} total)")

    def unsubscribe_queue(self, ws_id: str):
        """取消队列广播订阅。"""
        ws = None
        with self._queue_lock:
            ws = self._queue_subscribers.pop(ws_id, None)
        if ws and self._loop:
            asyncio.run_coroutine_threadsafe(self._close_queue_ws(ws_id, ws), self._loop)

    async def _close_queue_ws(self, ws_id: str, ws: WebSocket):
        try:
            await ws.close()
        except Exception:
            pass

    def broadcast_queue(self, message: dict):
        """从同步线程向所有队列订阅者广播消息。"""
        if not self._loop:
            return
        # 快照订阅者列表（锁内），发送在锁外，避免阻塞
        with self._queue_lock:
            snapshot = list(self._queue_subscribers.items())
        dead_ids = []
        for ws_id, ws in snapshot:
            future = asyncio.run_coroutine_threadsafe(
                self._send_queue_async(ws_id, ws, message), self._loop
            )
            try:
                future.result(timeout=2)
            except Exception:
                dead_ids.append(ws_id)
        for ws_id in dead_ids:
            self.unsubscribe_queue(ws_id)

    async def _send_queue_async(self, ws_id: str, ws: WebSocket, message: dict):
        try:
            await ws.send_json(message)
        except Exception:
            pass  # 连接已断，由 broadcast_queue 统一清理


ws_manager = WebSocketManager()
