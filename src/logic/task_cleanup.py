import os
import time
import shutil
import threading
from pathlib import Path
from src.logic.yaml_config_loader import yaml_config_loader
from src.logic.logger import log


def cleanup_old_tasks():
    task_folder = yaml_config_loader.get("paths.task_folder", "tasks")
    max_age_hours = yaml_config_loader.get("auto_cleanup_tasks_time", 4)
    if not os.path.exists(task_folder):
        return

    now = time.time()
    max_age_seconds = max_age_hours * 3600

    for entry in os.listdir(task_folder):
        entry_path = os.path.join(task_folder, entry)
        if os.path.isdir(entry_path):
            mtime = os.path.getmtime(entry_path)
            if now - mtime > max_age_seconds:
                try:
                    shutil.rmtree(entry_path)
                    log.info(f"[Cleanup] Removed expired task directory: {entry}")
                except Exception as e:
                    log.error(f"[Cleanup] Failed to remove {entry}: {e}")


def scheduled_cleanup():
    from src.core.task_manager import TaskManager
    interval_hours = yaml_config_loader.get("auto_cleanup_interval_hours", 0.5)
    while True:
        time.sleep(interval_hours * 3600)
        cleanup_old_tasks()
        TaskManager.cleanup_expired()


def start_cleanup_thread():
    t = threading.Thread(target=scheduled_cleanup, daemon=True, name="TaskCleanup")
    t.start()
    log.info("[Cleanup] Task cleanup thread started.")
