# -*- coding: utf-8 -*-
"""
Kalm 全局任务调度器 (TaskScheduler)
==========================================
纯中转控制站的核心：FIFO 队列 + 动态执行器加载 + 服务生命周期管理。

设计原则：
  - 单线程串行消费，适配单 GPU 场景（4090 + 64GB）。
  - 服务智能启停：任务到达时自动启动，空闲超时后回收。
  - 执行器由 config.yaml 驱动，通过 importlib 动态加载。
  - 不包含任何业务逻辑——只做排队、分发、透传。
"""

import threading
import queue
import time
import importlib
from typing import Dict, Any, Optional

from src.logic.logger import log
from src.core.service_controller import service_controller
from src.core.task_manager import TaskManager
from src.logic.yaml_config_loader import yaml_config_loader

DEFAULT_SERVICE_IDLE_TIMEOUT = 3


class TaskScheduler:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super(TaskScheduler, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.task_queue: queue.Queue = queue.Queue()
        self.current_service_name: Optional[str] = None
        self.is_running = False
        self.worker_thread = None

        self.idle_timeout = yaml_config_loader.get("scheduler.idle_timeout", DEFAULT_SERVICE_IDLE_TIMEOUT)
        self.startup_timeout = yaml_config_loader.get("scheduler.startup_timeout", 300)
        self.download_idle_timeout = yaml_config_loader.get("scheduler.download_idle_timeout", 10)

        tasks_config = yaml_config_loader.get("tasks", {})
        if not tasks_config:
            log.warning("[Scheduler] 'tasks' configuration is empty or missing.")

        self.task_service_map: Dict[str, Optional[str]] = {}
        self.task_executors: Dict[str, callable] = {}

        for task_type, task_config in tasks_config.items():
            service_name = task_config.get("service")
            self.task_service_map[task_type] = service_name

            executor_config = task_config.get("executor", {})
            module_path = executor_config.get("module")
            function_name = executor_config.get("function")

            if module_path and function_name:
                try:
                    module = importlib.import_module(module_path)
                    executor_func = getattr(module, function_name)
                    self.task_executors[task_type] = executor_func
                    log.info(f"[Scheduler] Registered '{task_type}': service={service_name}")
                except Exception as e:
                    log.error(f"[Scheduler] Failed to load executor for '{task_type}': {e}", exc_info=True)
            else:
                log.warning(f"[Scheduler] Task '{task_type}' has incomplete executor config")

        self._start_worker()
        self._initialized = True

    def _start_worker(self):
        if self.is_running:
            return
        self.is_running = True
        self.worker_thread = threading.Thread(
            target=self._scheduler_loop, daemon=True, name="TaskSchedulerWorker"
        )
        self.worker_thread.start()
        log.info(f"[Scheduler] Worker thread started. Idle timeout: {self.idle_timeout}s")

    def submit_task(self, task_type: str, task_id: str, payload: Dict[str, Any],
                    output_queue=None, started_event=None):
        task_item = {
            "type": task_type, "id": task_id, "payload": payload,
            "output_queue": output_queue, "started_event": started_event,
        }
        self.task_queue.put(task_item)
        log.info(f"[Scheduler] Task {task_id} ({task_type}) enqueued. Queue size: {self.task_queue.qsize()}")

    def _scheduler_loop(self):
        while self.is_running:
            try:
                timeout = self.idle_timeout if self.current_service_name else None

                try:
                    task = self.task_queue.get(timeout=timeout)
                except queue.Empty:
                    if self.current_service_name:
                        log.info(f"[Scheduler] Service '{self.current_service_name}' idle for {self.idle_timeout}s, stopping.")
                        self._stop_current_service()
                    continue

                if task is None:
                    break

                task_type = task["type"]
                task_id = task["id"]
                payload = task["payload"]
                output_queue = task.get("output_queue")
                started_event = task.get("started_event")

                log.info(f"[Scheduler] Processing task {task_id} ({task_type})")

                # 通知流式端点：任务已开始执行
                if started_event:
                    started_event.set()

                # 服务管理
                required_service = self.task_service_map.get(task_type)
                if required_service:
                    if self.current_service_name != required_service:
                        if self.current_service_name:
                            log.info(f"[Scheduler] Switching service: '{self.current_service_name}' -> '{required_service}'")
                            self._stop_current_service()
                        self._start_service(required_service)
                    else:
                        log.info(f"[Scheduler] Reusing active service '{self.current_service_name}'")

                # 执行任务
                self._execute_task_logic(task_type, task_id, payload, output_queue)
                self.task_queue.task_done()

                # 标记产出并等待客户端下载完成，才处理下一个任务
                if required_service:
                    task_info = TaskManager.get_task(task_id)
                    if task_info and task_info.get("status") == TaskManager.STATUS_FAILED:
                        log.info(f"[Scheduler] Task {task_id} failed, skipping download wait.")
                    else:
                        service_controller.mark_has_outputs(required_service)
                        service_controller._wait_downloads(required_service, timeout=self.download_idle_timeout)

            except Exception as e:
                log.error(f"[Scheduler] Critical error in scheduler loop: {e}", exc_info=True)
                time.sleep(1)

    def _start_service(self, service_name: str):
        log.info(f"[Scheduler] Starting service '{service_name}'...")
        try:
            service_controller.safe_start(service_name, timeout=self.startup_timeout)
            self.current_service_name = service_name
            log.info(f"[Scheduler] Service '{service_name}' is ready.")
        except Exception as e:
            log.error(f"[Scheduler] Failed to start service '{service_name}': {e}")
            self.current_service_name = None
            raise e

    def _stop_current_service(self):
        if not self.current_service_name:
            return
        log.info(f"[Scheduler] Stopping service '{self.current_service_name}'...")
        try:
            service_controller.stop(self.current_service_name)
        except Exception as e:
            log.error(f"[Scheduler] Error stopping service '{self.current_service_name}': {e}")
        finally:
            self.current_service_name = None

    def _execute_task_logic(self, task_type: str, task_id: str, payload: Dict[str, Any],
                            output_queue=None):
        try:
            executor = self.task_executors.get(task_type)
            if executor:
                if output_queue is not None:
                    executor(task_id, output_queue, **payload)
                else:
                    executor(task_id, **payload)
            else:
                log.error(f"[Scheduler] Unknown task type: '{task_type}'. No executor registered.")
                TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {
                    "message": f"Unknown task type: {task_type}"
                })
                if output_queue:
                    output_queue.put(None)
        except Exception as e:
            error_msg = f"Task execution failed: {str(e)}"
            log.error(f"[Scheduler] {error_msg}", exc_info=True)
            try:
                TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": error_msg})
            except Exception as update_error:
                log.error(f"[Scheduler] Failed to update task status for {task_id}: {update_error}")
            if output_queue:
                output_queue.put(None)

    def shutdown(self, wait: bool = True):
        if not self.is_running:
            return
        log.info("[Scheduler] Shutting down...")
        self.is_running = False
        try:
            self.task_queue.put(None)
        except Exception as e:
            log.warning(f"[Scheduler] Failed to enqueue poison pill: {e}")

        if wait and self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=5)
            log.info("[Scheduler] Worker thread terminated.")

        while not self.task_queue.empty():
            try:
                task = self.task_queue.get_nowait()
                if task and task is not None:
                    task_id = task.get("id")
                    if task_id:
                        log.warning(f"[Scheduler] Cancelling pending task {task_id}")
                        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
                            {"message": "Service shutting down, task cancelled."})
                self.task_queue.task_done()
            except queue.Empty:
                break

        if self.current_service_name:
            self._stop_current_service()
        log.info("[Scheduler] Shutdown complete.")


scheduler = TaskScheduler()
