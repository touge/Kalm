# -*- coding: utf-8 -*-
"""
字幕生成任务执行器
==================
接收前端上传的本地音频文件 + 文稿 → multipart 提交到 TTS 后端 /subtitle/generate → 轮询 → 透传结果。
与 TTS 语音生成共享同一 TTS 服务进程，调度器自动复用，无需重启。
"""
import time
from pathlib import Path

import requests

from src.core.service_controller import service_controller
from src.core.task_manager import TaskManager
from src.logic.logger import log

RESOURCE_NAME = "TTS"
POLL_INTERVAL = 2
MAX_POLL_SECONDS = 600


def execute(task_id: str, **payload):
    """
    字幕生成任务入口。由调度器动态调用。

    payload 期望字段:
      - text: str — 字幕对齐文本
      - audio_file_path: str — 本地音频文件绝对路径（前端上传后由 Kalm 保存）
      - output_filename: str (可选) — 输出字幕文件名
    """
    text = payload.get("text", "")
    audio_file_path = payload.get("audio_file_path", "")
    output_filename = payload.get("output_filename")

    if not text:
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
                                {"message": "Missing 'text' in payload"})
        return

    if not audio_file_path or not Path(audio_file_path).is_file():
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
                                {"message": "Missing or invalid 'audio_file_path' — "
                                 "upload audio_file via multipart/form-data"})
        return

    svc = service_controller.get_service_config(RESOURCE_NAME)
    if not svc:
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
                                {"message": f"Service '{RESOURCE_NAME}' not configured"})
        return

    base_url = f"{svc['host']}:{svc['port']}"
    token = svc.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        # 读取本地音频文件
        audio_bytes = Path(audio_file_path).read_bytes()
        audio_name = Path(audio_file_path).name
        log.info(f"[Subtitle Executor] Task {task_id}: read local audio {audio_name} ({len(audio_bytes)} bytes)")

        # multipart 提交到 TTS 后端字幕端点
        TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING,
                                {"message": "Submitting to subtitle endpoint..."})

        files = {"audio_file": (audio_name, audio_bytes, "audio/wav")}
        data = {"text": text}
        if output_filename:
            data["output_filename"] = output_filename

        submit_url = f"{base_url}/subtitle/generate"
        log.info(f"[Subtitle Executor] Task {task_id}: POST {submit_url}")

        resp = requests.post(submit_url, headers=headers, files=files, data=data, timeout=(5, 30))
        resp.raise_for_status()

        svc_task_id = resp.json().get("task_id")
        if not svc_task_id:
            TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
                                    {"message": "Subtitle endpoint returned no task_id"})
            return

        log.info(f"[Subtitle Executor] Task {task_id} -> {svc_task_id}")

        # 轮询等待完成
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
                    log.info(f"[Subtitle Executor] Task {task_id} completed.")
                    TaskManager.update_task(task_id, TaskManager.STATUS_SUCCESS, result=status_data)
                    return

            except requests.RequestException as e:
                log.warning(f"[Subtitle Executor] Task {task_id}: poll error: {e}")

        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
                                {"message": f"Polling timeout after {MAX_POLL_SECONDS}s"})

    except Exception as e:
        log.error(f"[Subtitle Executor] Task {task_id} failed: {e}", exc_info=True)
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})
