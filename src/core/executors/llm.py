# -*- coding: utf-8 -*-
"""
LLM 文本生成任务执行器
======================
纯本地模式：通过 Ollama 透传。不做任何云端调用。
"""

import queue
import requests
import json
from src.core.service_controller import service_controller
from src.core.task_manager import TaskManager
from src.core.ws_manager import ws_manager
from src.logic.logger import log


def execute(task_id: str, **payload):
    """
    LLM 生成任务入口。由调度器动态调用。

    payload 期望字段:
      - model: str — 模型名称
      - prompt: str — 提示词
      - options: dict (可选) — temperature, max_tokens 等
    """
    model = payload.get("model", "")
    prompt = payload.get("prompt", "")
    options = payload.get("options", {})

    service_name = payload.get("_service_name", "Ollama")

    if not model or not prompt:
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
            {"message": "Missing 'model' or 'prompt' in payload"})
        return

    try:
        TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING,
            {"message": "Sending to Ollama..."})
        result = _call_ollama(service_name, model, prompt, options)
        TaskManager.update_task(task_id, TaskManager.STATUS_SUCCESS, result=result)
    except Exception as e:
        log.error(f"[LLM Executor] Task {task_id} failed: {e}", exc_info=True)
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})


def _call_ollama(service_name: str, model: str, prompt: str, options: dict) -> dict:
    base_url = service_controller.get_service_url(service_name)
    if not base_url:
        raise RuntimeError("Ollama service not available")

    api_url = f"{base_url}/api/generate"
    req_data = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {k: v for k, v in options.items() if v is not None},
    }

    # 推理模型跳过 thinking 输出
    if any(tag in model for tag in ("qwen3", "deepseek", "qwq")):
        req_data["think"] = False

    resp = requests.post(api_url, json=req_data, timeout=1200)
    resp.raise_for_status()
    data = resp.json()

    return {
        "text": data.get("response", ""),
        "model": model,
        "total_duration": data.get("total_duration"),
    }


def execute_stream(task_id: str, output_queue: queue.Queue, **payload):
    """
    流式 LLM 执行器。由调度器调用。

    将 Ollama NDJSON 流逐行写入 output_queue，前端通过 StreamingResponse 读取。
    """
    model = payload.get("model", "")
    prompt = payload.get("prompt", "")
    options = payload.get("options", {})

    service_name = payload.get("_service_name", "Ollama")

    if not model or not prompt:
        output_queue.put(json.dumps({"error": "Missing 'model' or 'prompt' in payload"}))
        output_queue.put(None)
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
            {"message": "Missing 'model' or 'prompt' in payload"})
        ws_manager.send(task_id, {"type": "task_failed",
            "message": "Missing 'model' or 'prompt' in payload"})
        return

    base_url = service_controller.get_service_url(service_name)
    if not base_url:
        output_queue.put(json.dumps({"error": "Ollama service not available"}))
        output_queue.put(None)
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
            {"message": "Ollama service not available"})
        ws_manager.send(task_id, {"type": "task_failed",
            "message": "Ollama service not available"})
        return

    api_url = f"{base_url}/api/generate"
    req_data = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": {k: v for k, v in options.items() if v is not None},
    }

    if any(tag in model for tag in ("qwen3", "deepseek", "qwq")):
        req_data["think"] = False

    full_text = ""

    try:
        TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING,
            {"message": "LLM streaming started"})

        resp = requests.post(api_url, json=req_data, stream=True, timeout=1200)
        resp.raise_for_status()

        for raw_line in resp.iter_lines():
            if raw_line:
                output_queue.put(raw_line)
                try:
                    data = json.loads(raw_line)
                    full_text += data.get("response", "")
                    if data.get("done"):
                        break
                except json.JSONDecodeError:
                    pass

        output_queue.put(None)

        TaskManager.update_task(task_id, TaskManager.STATUS_SUCCESS, result={
            "text": full_text, "model": model,
        })
        ws_manager.send(task_id, {"type": "task_complete", "status": "success",
            "text": full_text, "model": model})
    except Exception as e:
        log.error(f"[LLM Stream] Task {task_id} failed: {e}", exc_info=True)
        output_queue.put(json.dumps({"error": str(e)}))
        output_queue.put(None)
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})
        ws_manager.send(task_id, {"type": "task_failed", "message": str(e)})
