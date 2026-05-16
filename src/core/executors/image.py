# -*- coding: utf-8 -*-
"""
图像生成任务执行器
===================
纯透传模式：接收 workflow → 提交 ComfyUI → WebSocket 跟踪进度 → 原样返回结果 JSON。
不做任何下载、URL 改写等业务操作。文件访问走通用 /file 端点。
"""

import time
import urllib.request
import urllib.error
import urllib.parse
import json
import websocket

from src.core.service_controller import service_controller
from src.core.task_manager import TaskManager
from src.core.ws_manager import ws_manager
from src.logic.logger import log


def execute(task_id: str, **payload):
    """
    图像生成任务入口。由调度器通过 importlib 动态调用。

    payload 期望字段:
      - workflow: dict — ComfyUI 工作流 JSON
      - final_output_node_id: str — 输出节点 ID
      - client_id: str (可选) — WebSocket 客户端标识
    """
    workflow = payload.get("workflow")
    final_output_node_id = payload.get("final_output_node_id")
    client_id = payload.get("client_id", task_id[:8])

    if not workflow or not final_output_node_id:
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
            {"message": "Missing 'workflow' or 'final_output_node_id' in payload"})
        ws_manager.send(task_id, {"type": "task_failed",
            "message": "Missing 'workflow' or 'final_output_node_id' in payload"})
        return

    service_url = service_controller.get_service_url("ComfyUI")
    if not service_url:
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
            {"message": "ComfyUI service not available"})
        ws_manager.send(task_id, {"type": "task_failed",
            "message": "ComfyUI service not available"})
        return

    if "://" not in service_url:
        service_url = f"http://{service_url}"
    parsed = urllib.parse.urlparse(service_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 7001

    client = _ComfyUIClient(host, port)

    def progress_callback(raw_message: dict):
        """将 ComfyUI WebSocket 进度消息透传到 TaskManager 和前端 WS"""
        TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, result=raw_message)
        ws_manager.send(task_id, raw_message)

    try:
        TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, {"message": "Submitting to ComfyUI..."})
        final_outputs = client.run_workflow(workflow, client_id, progress_callback)

        TaskManager.update_task(task_id, TaskManager.STATUS_SUCCESS, result={
            "message": "Image generation completed",
            "outputs": final_outputs,
        })
        ws_manager.send(task_id, {"type": "task_complete", "status": "success",
            "message": "Image generation completed", "outputs": final_outputs})
    except Exception as e:
        log.error(f"[Image Executor] Task {task_id} failed: {e}", exc_info=True)
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})
        ws_manager.send(task_id, {"type": "task_failed", "message": str(e)})


class _ComfyUIClient:
    """轻量 ComfyUI 客户端：提交 Prompt + WebSocket 进度 + 获取输出信息"""

    def __init__(self, server_address, port):
        self.server_address = server_address
        self.port = port
        self.base_url = f"http://{server_address}:{port}"
        self.ws_url = f"ws://{server_address}:{port}/ws"

    def run_workflow(self, workflow: dict, client_id: str, message_callback: callable) -> dict:
        prompt_id = self._queue_prompt(workflow, client_id)
        message_callback({"status": "queued", "prompt_id": prompt_id})

        self._track_progress(prompt_id, client_id, message_callback)

        history = self._get_history(prompt_id)
        outputs = history.get(prompt_id, {}).get("outputs", {})
        message_callback({"status": "completed", "prompt_id": prompt_id, "output_nodes": list(outputs.keys())})
        return outputs

    def _queue_prompt(self, prompt: dict, client_id: str) -> str:
        data = json.dumps({"prompt": prompt, "client_id": client_id}).encode("utf-8")
        req = urllib.request.Request(f"{self.base_url}/prompt", data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())["prompt_id"]

    def _get_history(self, prompt_id: str) -> dict:
        with urllib.request.urlopen(f"{self.base_url}/history/{prompt_id}") as resp:
            return json.loads(resp.read().decode())

    def _track_progress(self, prompt_id: str, client_id: str, message_callback: callable):
        ws = websocket.create_connection(f"{self.ws_url}?clientId={client_id}", timeout=30)
        ws.settimeout(30)
        try:
            last_msg_time = time.time()
            max_idle_seconds = 300  # 5 分钟无新消息视为超时
            while True:
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    if time.time() - last_msg_time > max_idle_seconds:
                        raise RuntimeError(f"WebSocket idle timeout after {max_idle_seconds}s")
                    continue
                if not msg:
                    break
                last_msg_time = time.time()
                data = json.loads(msg)
                msg_type = data.get("type", "")
                if msg_type in ("executing", "progress", "execution_start", "execution_cached", "execution_error"):
                    message_callback(data)
                if msg_type == "executed" and data.get("data", {}).get("prompt_id") == prompt_id:
                    break
        finally:
            ws.close()
