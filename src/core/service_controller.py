# -*- coding: utf-8 -*-
import subprocess
import psutil
import yaml
import time
import threading
import os
from pathlib import Path
from collections import defaultdict
from src.logic.logger import log


class _ServiceController:
    def __init__(self, config_path="services.yaml"):
        with open(config_path, encoding="utf-8") as f:
            self.services = yaml.safe_load(f)

        self.script_root = Path(__file__).resolve().parent.parent
        self.processes = {}
        self._service_users = defaultdict(int)
        self._active_downloads = defaultdict(int)
        self._pending_outputs: dict = {}  # 任务完成后标记有产出待下载
        self._lock = threading.Lock()

    def _find_service_key(self, name: str) -> str | None:
        """大小写不敏感查找服务键名"""
        name_lower = name.lower()
        for key in self.services:
            if key.lower() == name_lower:
                return key
        return None

    def has_service(self, name: str) -> bool:
        return self._find_service_key(name) is not None

    def get_service_config(self, service_name: str) -> dict | None:
        key = self._find_service_key(service_name)
        return self.services.get(key) if key else None

    def get_service_url(self, service_name: str) -> str | None:
        key = self._find_service_key(service_name)
        svc = self.services.get(key) if key else None
        if svc and "host" in svc and "port" in svc:
            return f"{svc['host']}:{svc['port']}"
        return None

    def safe_start(self, service_name: str, timeout: int = 30, interval: float = 0.5):
        key = self._find_service_key(service_name)
        if not key:
            raise ValueError(f"Service '{service_name}' not defined in configuration.")
        svc = self.services[key]

        keyword = svc.get("ready_keyword")
        if not keyword:
            raise ValueError(f"Service '{service_name}' in config is missing 'ready_keyword'.")

        self.start(key)
        try:
            self.wait_until_ready(key, keyword=keyword, timeout=timeout, interval=interval)
        except Exception as e:
            self.stop(key)
            raise e

    def wait_until_ready(self, service_name, keyword="started", timeout=30, interval=0.1):
        key = self._find_service_key(service_name)
        svc = self.services.get(key, {}) if key else {}
        is_managed = svc.get("manage_lifecycle", True)
        port = svc.get("port")
        start_time = time.time()

        if not is_managed:
            if not port:
                log.warning(f"Service '{service_name}' is not managed and has no port defined.")
                return
            log.info(f"Checking for externally managed service '{service_name}' on port {port}...")
            while time.time() - start_time < timeout:
                if self._get_pid_by_port(port):
                    log.success(f"Externally managed service '{service_name}' is active on port {port}.")
                    return
                time.sleep(interval)
            raise TimeoutError(f"Service '{service_name}' did not become active on port {port} within {timeout}s.")

        proc = self.processes.get(key)
        if not proc:
            if port and self._get_pid_by_port(port):
                log.success(f"Service '{key}' was already running and is ready.")
                return
            raise RuntimeError(f"Managed service '{key}' was not started.")

        proc.stdout.reconfigure(encoding='utf-8', errors='ignore')
        output_buffer = ""

        while time.time() - start_time < timeout:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Service '{key}' exited prematurely with code {proc.poll()}. "
                    f"Output: {output_buffer}"
                )

            line = proc.stdout.readline()
            if line:
                log.info(f"[{key}] {line.strip()}")
                output_buffer += line
                if keyword in line:
                    log.success(f"Service '{key}' is ready (keyword '{keyword}' found).")
                    self.attach_log_stream(key)
                    return
            time.sleep(interval)

        raise TimeoutError(
            f"Service '{key}' did not show readiness keyword '{keyword}' within {timeout}s."
        )

    def start(self, key):
        with self._lock:
            real_key = self._find_service_key(key)
            if not real_key:
                log.warning(f"Service '{key}' is not defined in configuration.")
                return
            svc = self.services[real_key]
            key = real_key

            if svc.get("manage_lifecycle") is False:
                self._service_users[key] += 1
                log.info(f"Service '{key}' is externally managed. Skipping start. "
                         f"User count: {self._service_users[key]}.")
                return

            self._service_users[key] += 1
            log.info(f"Service '{key}' user count incremented to {self._service_users[key]}.")

            if self._service_users[key] > 1:
                log.info(f"Service '{key}' already in use by other tasks. Skipping start.")
                return

            port = svc.get("port")
            if port and self._get_pid_by_port(port):
                log.info(f"Service '{key}' already running on port {port}.")
                return

            if svc["type"] == "ps1":
                ps1_path = str(Path(svc["path"]))
                cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", ps1_path, "-Port", str(port)]
            elif svc["type"] == "cmd":
                cmd = ["powershell", "-Command", svc["command"]]
            else:
                log.error(f"Unknown service type: {svc['type']}")
                return

            env = os.environ.copy()
            env['PYTHONUTF8'] = '1'

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='ignore',
                env=env,
            )
            self.processes[key] = proc
            log.success(f"Started service '{key}' with PID {proc.pid}")

    def stop(self, key):
        should_stop = False
        port = None
        stop_cmd = None

        with self._lock:
            real_key = self._find_service_key(key)
            if not real_key:
                log.warning(f"Service '{key}' is not defined in configuration.")
                return
            svc = self.services[real_key]
            key = real_key

            if svc.get("manage_lifecycle") is False:
                if self._service_users.get(key, 0) > 0:
                    self._service_users[key] -= 1
                log.info(f"Service '{key}' is externally managed. Skipping stop. "
                         f"User count: {self._service_users[key]}.")
                return

            if self._service_users[key] <= 0:
                log.warning(f"Stop called for service '{key}' with zero or negative users. Ignoring.")
                return

            self._service_users[key] -= 1
            log.info(f"Service '{key}' user count decremented to {self._service_users[key]}.")

            if self._service_users[key] > 0:
                log.info(f"Service '{key}' still in use by other tasks. Skipping stop.")
                return

            should_stop = True
            port = svc.get("port")
            stop_cmd = svc.get("stop_command")

        # 锁外等待下载完成，避免死锁
        if should_stop:
            self._wait_downloads(key)

        if should_stop:
            if not port:
                return

            pid = self._get_pid_by_port(port)
            if pid:
                if stop_cmd:
                    log.info(f"Running stop command for service '{key}'...")
                    try:
                        subprocess.run(stop_cmd, shell=True, timeout=10)
                    except Exception as e:
                        log.warning(f"Stop command for '{key}' failed: {e}")

                try:
                    parent = psutil.Process(pid)
                    children = parent.children(recursive=True)
                    for child in children:
                        child.terminate()
                    gone, alive = psutil.wait_procs(children, timeout=3)
                    for p in alive:
                        p.kill()
                    parent.terminate()
                    parent.wait(timeout=5)
                    log.success(f"Stopped service '{key}' with PID {pid} terminated.")
                except psutil.NoSuchProcess:
                    log.info(f"Process with PID {pid} no longer exists for '{key}'.")
                except Exception as e:
                    log.error(f"Failed to terminate process for '{key}': {e}")
                finally:
                    if key in self.processes:
                        del self.processes[key]
            else:
                log.info(f"No process found for service '{key}' on port {port}")

            timeout = 10
            start_time = time.time()
            while self._get_pid_by_port(port) and time.time() - start_time < timeout:
                time.sleep(0.5)
            if not self._get_pid_by_port(port):
                log.success(f"Port {port} released.")
            else:
                log.warning(f"Port {port} was not released within {timeout}s.")

    def status(self, key: str) -> dict:
        real_key = self._find_service_key(key)
        if not real_key:
            return {"name": key, "running": False, "reason": "Service not found"}
        service = self.services[real_key]
        port = service.get("port")
        pid = self._get_pid_by_port(port)
        return {"name": key, "port": port, "pid": pid, "running": bool(pid)}

    def status_all(self) -> list[dict]:
        return [self.status(key) for key in self.services.keys()]

    def _get_pid_by_port(self, port: int) -> int | None:
        if port is None:
            return None
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                return conn.pid
        return None

    def download_begin(self, service_name: str):
        """标记开始从该服务下载文件"""
        key = self._find_service_key(service_name)
        if not key:
            return
        with self._lock:
            self._pending_outputs[key] = False  # 客户端已经开始取，清除待下载标记
            self._active_downloads[key] += 1

    def download_end(self, service_name: str):
        """标记下载完成"""
        key = self._find_service_key(service_name)
        if not key:
            return
        with self._lock:
            if self._active_downloads[key] > 0:
                self._active_downloads[key] -= 1

    def mark_has_outputs(self, service_name: str):
        """任务完成时标记该服务有产出文件待下载"""
        key = self._find_service_key(service_name)
        if key:
            with self._lock:
                self._pending_outputs[key] = True

    def _wait_downloads(self, key: str, timeout: int = 10):
        """等待客户端下载完毕。超时后放弃。"""
        deadline = time.time() + timeout
        pending_start = time.time()
        last_log = 0
        while time.time() < deadline:
            with self._lock:
                pending = self._pending_outputs.get(key, False)
                active = self._active_downloads[key]
            if not pending and active <= 0:
                return True
            # 超时放弃
            if pending and active <= 0 and time.time() - pending_start > timeout:
                self._pending_outputs[key] = False
                log.warning(f"No download started for service '{key}' in {timeout}s, giving up.")
                return True
            # 日志限流：5 秒一条
            if time.time() - last_log > 5:
                if active > 0:
                    log.info(f"Waiting for {active} download(s) on service '{key}'...")
                elif pending:
                    remain = timeout - int(time.time() - pending_start)
                    log.info(f"Service '{key}' has pending outputs, waiting for download (will timeout in {remain}s)...")
                last_log = time.time()
            time.sleep(0.5)
        log.warning(f"Download wait timeout for service '{key}'")
        return False

    def attach_log_stream(self, service_name: str):
        proc = self.processes.get(service_name)
        if not proc:
            return

        def stream():
            while proc.poll() is None:
                line = proc.stdout.readline()
                if line:
                    log.info(f"[{service_name}] {line.strip()}")
            log.info(f"Service '{service_name}' has exited.")

        threading.Thread(target=stream, daemon=True).start()


service_controller = _ServiceController()
