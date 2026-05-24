# -*- coding: utf-8 -*-
"""
服务控制器 — Kalm 后端子进程的启停、就绪检测、生命周期管理。

核心机制：
  - 引用计数：每个任务使用服务时 +1，完成时 -1，归零后可回收。
  - 就绪检测：对于 Kalm 管理的服务，读取 stdout 匹配 ready_keyword；
    对于外部管理的服务，检测端口占用。
  - 优雅停止：支持 stop_command 预处理（如卸载模型），再 terminate/kill 进程树。
  - 下载等待：停止前等待客户端完成文件下载，避免服务先关导致下载中断。

使用：模块级单例 service_controller = _ServiceController()，其它模块直接 import 使用。
"""

import subprocess
import psutil
import yaml
import time
import threading
import os
import re
from pathlib import Path
from collections import defaultdict
from src.logic.logger import log
from src.logic.yaml_config_loader import yaml_config_loader


# ANSI 转义序列正则表达式 — 移除子进程输出中的颜色码
_ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\([a-zA-Z]')


def _strip_ansi(text: str) -> str:
    """移除字符串中的 ANSI 转义序列（颜色码、格式控制等）。"""
    return _ANSI_ESCAPE.sub('', text)


class _ServiceController:
    """服务控制器单例，管理 services.yaml 中定义的所有后端子进程。"""

    def __init__(self, config_path="services.yaml"):
        # ---------- 加载服务定义 ----------
        with open(config_path, encoding="utf-8") as f:
            self.services = yaml.safe_load(f)

        # ---------- 从 config.yaml 读取 auto_start 服务列表 ----------
        # 服务的自动启动由 config.yaml 中的 auto_start_services 配置控制
        self.auto_start_services = yaml_config_loader.get("auto_start_services", [])

        # 项目根目录（main.py 的父目录）
        self.script_root = Path(__file__).resolve().parent.parent

        # ---------- 运行时状态 ----------
        # 子进程映射：{ 服务键名: subprocess.Popen }
        self.processes = {}

        # 引用计数：{ 服务键名: 当前使用该服务的任务数 }
        self._service_users = defaultdict(int)

        # 活跃下载计数：{ 服务键名: 正在下载的客户端数 }
        self._active_downloads = defaultdict(int)

        # 待下载标记：{ 服务键名: True/False }，任务完成后置 True，客户端开始下载后置 False
        self._pending_outputs: dict = {}

        # 线程锁（保护引用计数等共享状态，注意：不在锁内做阻塞等待，避免死锁）
        self._lock = threading.Lock()

    # =========================================================================
    # 工具方法
    # =========================================================================

    def _find_service_key(self, name: str) -> str | None:
        """大小写不敏感查找服务键名。Ollama / ollama / OLLAMA 均匹配。"""
        name_lower = name.lower()
        # 第一步：精确匹配（大小写不敏感）
        for key in self.services:
            if key.lower() == name_lower:
                return key
        # 第二步：模糊匹配（如 ComfyUI → ComfyUI_Windows, TTS → TTS_Windows）
        for key in self.services:
            if name_lower in key.lower():
                return key
        return None

    def has_service(self, name: str) -> bool:
        """判断服务是否在配置文件中定义。"""
        return self._find_service_key(name) is not None

    def is_auto_start(self, service_name: str) -> bool:
        """判断服务是否在 config.yaml 的 auto_start_services 列表中。"""
        key = self._find_service_key(service_name)
        return key in self.auto_start_services if key else False

    def get_service_config(self, service_name: str) -> dict | None:
        """获取服务的完整配置字典。"""
        key = self._find_service_key(service_name)
        return self.services.get(key) if key else None

    def get_service_url(self, service_name: str) -> str | None:
        """获取服务的完整 URL，如 http://127.0.0.1:8001。"""
        key = self._find_service_key(service_name)
        svc = self.services.get(key) if key else None
        if svc and "host" in svc and "port" in svc:
            return f"{svc['host']}:{svc['port']}"
        return None

    # =========================================================================
    # 安全启动 + 就绪检测（对外主入口）
    # =========================================================================

    def safe_start(self, service_name: str, timeout: int = 30, interval: float = 0.5):
        """
        安全启动服务：启动 → 等就绪 → 失败自动停止。
        调度器（scheduler）调用此方法而非直接调用 start()。
        timeout: 等待就绪的最大秒数。
        interval: stdout 轮询间隔。
        auto_start 服务已在 Kalm 启动时运行就绪，仅验证端口即可，不重复等待 stdout。
        """
        key = self._find_service_key(service_name)
        if not key:
            raise ValueError(f"Service '{service_name}' not defined in configuration.")
        svc = self.services[key]

        # ---- auto_start：已在 Kalm 启动时运行就绪，仅验证端口 ----
        if self.is_auto_start(key):
            port = svc.get("port")
            if port and self._get_pid_by_port(port):
                log.info(f"Service '{key}' is auto-started and already running on port {port}.")
                return
            # 端口不在 → 服务可能挂了，尝试重新拉起
            log.warning(f"Auto-started service '{key}' is not running, re-launching...")

        keyword = svc.get("ready_keyword")
        if not keyword:
            raise ValueError(f"Service '{service_name}' in config is missing 'ready_keyword'.")

        self.start(key)
        try:
            self.wait_until_ready(key, keyword=keyword, timeout=timeout, interval=interval)
        except Exception as e:
            # 启动失败，回滚：停止服务，避免残留僵尸进程
            self.stop(key)
            raise e

    def wait_until_ready(self, service_name, keyword="started", timeout=30, interval=0.1):
        """
        阻塞等待服务就绪。
        - 外部管理（manage_lifecycle=false）：轮询端口占用。
        - Kalm 管理：读取子进程 stdout，匹配 ready_keyword。
        """
        key = self._find_service_key(service_name)
        svc = self.services.get(key, {}) if key else {}
        # auto_start 隐含 Kalm 管理（忽略 manage_lifecycle 配置）
        is_managed = self.is_auto_start(key) or svc.get("manage_lifecycle", True)
        port = svc.get("port")
        start_time = time.time()

        # ---- 外部管理服务：端口轮询 ----
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

        # ---- Kalm 管理服务：读取 stdout ----
        proc = self.processes.get(key)
        if not proc:
            # 可能已经被外部启动了（例如 auto_start 服务端口已占用，Kalm 未启动它）
            if port and self._get_pid_by_port(port):
                log.success(f"Service '{key}' was already running and is ready.")
                return
            raise RuntimeError(f"Managed service '{key}' was not started.")

        # 进程已退出 → 可能是外部进程占用了同端口
        if proc.poll() is not None:
            if port and self._get_pid_by_port(port):
                log.success(f"Service '{key}' process exited but port {port} is occupied, assuming ready.")
                del self.processes[key]  # 清理死进程记录
                return
            raise RuntimeError(
                f"Service '{key}' exited prematurely with code {proc.poll()}."
            )

        # 重新配置 stdout 编码
        proc.stdout.reconfigure(encoding='utf-8', errors='ignore')
        output_buffer = ""

        while time.time() - start_time < timeout:
            # 进程提前退出 → 启动失败
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Service '{key}' exited prematurely with code {proc.poll()}. "
                    f"Output: {output_buffer}"
                )

            line = proc.stdout.readline()
            if line:
                clean_line = _strip_ansi(line).strip()
                log.info(f"[{key}] {clean_line}")
                output_buffer += line
                if keyword in line:
                    log.success(f"Service '{key}' is ready (keyword '{keyword}' found).")
                    # 就绪后挂后台线程持续输出日志
                    self.attach_log_stream(key)
                    return
            time.sleep(interval)

        raise TimeoutError(
            f"Service '{key}' did not show readiness keyword '{keyword}' within {timeout}s."
        )

    # =========================================================================
    # 启动自动启动服务（Kalm boot 时调用）
    # =========================================================================

    def start_auto_services(self):
        """
        Kalm 启动时自动启动 config.yaml 中 auto_start_services 列出的服务。
        在 main.py 中 uvicorn.run 之前调用，确保服务在 API 就绪前启动完毕。
        auto_start 服务启动后常驻运行，不受引用计数回收影响。
        如果端口被外部进程占用，直接强杀后启动，确保是 Kalm 管理的实例。
        """
        if not self.auto_start_services:
            log.info("No auto_start_services configured in config.yaml.")
            return

        for key in self.auto_start_services:
            real_key = self._find_service_key(key)
            if not real_key:
                log.warning(f"Auto-start service '{key}' not found in services.yaml, skipping.")
                continue
            svc = self.services[real_key]
            port = svc.get("port")
            # 端口被占 → 强杀后重启，确保是 Kalm 管理的实例
            if port and self._get_pid_by_port(port):
                self._kill_process_on_port(port)
                time.sleep(0.5)  # 等端口释放
            log.info(f"Auto-starting service '{real_key}'...")
            try:
                self._launch_process(real_key, svc)
                self.wait_until_ready(
                    real_key,
                    keyword=svc.get("ready_keyword", "started"),
                    timeout=svc.get("startup_timeout", 30),
                )
            except Exception as e:
                # 启动失败时清理残留进程和记录，避免影响后续任务
                self._cleanup_process(real_key)
                log.error(f"Failed to auto-start service '{real_key}': {e}")

    # =========================================================================
    # 启停核心
    # =========================================================================

    def _launch_process(self, key, svc):
        """
        纯进程启动（不涉及引用计数）。
        根据 type 构建启动命令，启动子进程并记录到 self.processes。
        启动前检测端口占用，避免重复启动冲突。
        auto_start 服务和普通 managed 服务共用此方法。
        """
        port = svc.get("port")
        # 端口已被占用 → 跳过，避免端口冲突
        if port and self._get_pid_by_port(port):
            log.info(f"Service '{key}' is already running on port {port}, skipping launch.")
            return None

        if svc["type"] == "ps1":
            # PowerShell 脚本方式：传 -Port 参数
            ps1_path = str(Path(svc["path"]))
            cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", ps1_path, "-Port", str(port)]
        elif svc["type"] == "sh":
            # Linux bash 脚本方式：直接拉起以支持脚本内部加载环境（依靠 Shebang 解析）
            sh_path = os.path.abspath(svc["path"])
            
            try:
                import stat
                st = os.stat(sh_path)
                os.chmod(sh_path, st.st_mode | stat.S_IEXEC)
            except Exception as e:
                log.warning(f"Failed to chmod +x for {sh_path}: {e}")

            cmd = [sh_path, "--port", str(port)]
        elif svc["type"] == "cmd":
            # 直接命令方式
            cmd = ["powershell", "-Command", svc["command"]]
        else:
            log.error(f"Unknown service type: {svc['type']}")
            return None

        # 合并配置文件中的环境变量
        env = os.environ.copy()
        env['PYTHONUTF8'] = '1'  # 确保子进程输出 UTF-8
        # 支持配置中定义的自定义环境变量
        if "env" in svc and isinstance(svc["env"], dict):
            for env_key, env_value in svc["env"].items():
                env[str(env_key)] = str(env_value)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # stderr 合并到 stdout，统一读取
            text=True,
            encoding='utf-8',
            errors='ignore',
            env=env,
        )
        self.processes[key] = proc
        log.success(f"Started service '{key}' with PID {proc.pid}")
        return proc

    def _cleanup_process(self, key):
        """清理残留的进程记录。启动失败时调用，防止死进程条目影响后续 wait_until_ready。"""
        if key in self.processes:
            del self.processes[key]

    def start(self, key):
        """
        启动服务（引用计数 +1）。
        - auto_start 服务：已在 Kalm 启动时运行，无需任何操作，直接返回。
        - 外部管理（manage_lifecycle=false）：只增加计数，不启动进程。
        - Kalm 管理：计数从 0→1 时真正启动进程，>1 时跳过（已运行）。
        - 自动检测端口占用，避免重复启动。
        """
        with self._lock:
            real_key = self._find_service_key(key)
            if not real_key:
                log.warning(f"Service '{key}' is not defined in configuration.")
                return
            svc = self.services[real_key]
            key = real_key

            # ---- auto_start：常驻服务，已在 Kalm 启动时运行，无需操作 ----
            # 但如果端口不在（服务挂了），需要重新拉起
            if self.is_auto_start(key):
                port = svc.get("port")
                if port and self._get_pid_by_port(port):
                    log.info(f"Service '{key}' is auto-started, already running on port {port}.")
                    return
                log.warning(f"Auto-started service '{key}' is not running, re-launching...")

            # ---- 外部管理：只维护引用计数，不碰进程 ----
            if svc.get("manage_lifecycle") is False:
                self._service_users[key] += 1
                log.info(f"Service '{key}' is externally managed. Skipping start. "
                         f"User count: {self._service_users[key]}.")
                return

            # ---- 引用计数 +1 ----
            self._service_users[key] += 1
            log.info(f"Service '{key}' user count incremented to {self._service_users[key]}.")

            # 已有其他任务在使用，无需重复启动
            if self._service_users[key] > 1:
                log.info(f"Service '{key}' already in use by other tasks. Skipping start.")
                return

            # 端口已被占用（可能是上次异常退出后残留），不重复启动
            port = svc.get("port")
            if port and self._get_pid_by_port(port):
                log.info(f"Service '{key}' already running on port {port}.")
                return

            # ---- 真正启动子进程 ----
            self._launch_process(key, svc)

    def stop(self, key):
        """
        停止服务（引用计数 -1）。
        - auto_start 服务：常驻不停止，直接返回。
        - 外部管理：只减少计数，不碰进程。
        - Kalm 管理：计数 >1 时只减不关，归零时真正停止。
        - 停止前等待客户端完成下载（_wait_downloads）。
        - 先执行 stop_command（优雅关闭），再终止进程树。
        - 最后轮询确认端口释放。
        """
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

            # ---- auto_start：常驻服务，永不停止 ----
            if self.is_auto_start(key):
                log.info(f"Service '{key}' is auto-started, skipping stop (always running).")
                return

            # ---- 外部管理：只维护引用计数 ----
            if svc.get("manage_lifecycle") is False:
                if self._service_users.get(key, 0) > 0:
                    self._service_users[key] -= 1
                log.info(f"Service '{key}' is externally managed. Skipping stop. "
                         f"User count: {self._service_users[key]}.")
                return

            # ---- 防止负数计数 ----
            if self._service_users[key] <= 0:
                log.warning(f"Stop called for service '{key}' with zero or negative users. Ignoring.")
                return

            # ---- 引用计数 -1 ----
            self._service_users[key] -= 1
            log.info(f"Service '{key}' user count decremented to {self._service_users[key]}.")

            # 仍有其他任务在使用，不停止
            if self._service_users[key] > 0:
                log.info(f"Service '{key}' still in use by other tasks. Skipping stop.")
                return

            # 计数归零，执行停止
            should_stop = True
            port = svc.get("port")
            stop_cmd = svc.get("stop_command")

        # ---- 锁外等待下载完成，避免死锁 ----
        if should_stop:
            self._wait_downloads(key)

        if should_stop:
            if not port:
                return

            pid = self._get_pid_by_port(port)
            if pid:
                # ---- 优雅关闭：执行 stop_command（如卸载 Ollama 模型） ----
                if stop_cmd:
                    log.info(f"Running stop command for service '{key}'...")
                    try:
                        subprocess.run(stop_cmd, shell=True, timeout=10)
                    except Exception as e:
                        log.warning(f"Stop command for '{key}' failed: {e}")

                # ---- 终止进程树：子进程先关，父进程后关 ----
                try:
                    parent = psutil.Process(pid)
                    children = parent.children(recursive=True)
                    for child in children:
                        child.terminate()
                    gone, alive = psutil.wait_procs(children, timeout=3)
                    # 僵死进程强制 kill
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

            # ---- 等待端口释放 ----
            timeout = 10
            start_time = time.time()
            while self._get_pid_by_port(port) and time.time() - start_time < timeout:
                time.sleep(0.5)
            if not self._get_pid_by_port(port):
                log.success(f"Port {port} released.")
            else:
                log.warning(f"Port {port} was not released within {timeout}s.")

    # =========================================================================
    # 状态查询
    # =========================================================================

    def status(self, key: str) -> dict:
        """查询单个服务的运行状态。返回 {name, port, pid, running}。"""
        real_key = self._find_service_key(key)
        if not real_key:
            return {"name": key, "running": False, "reason": "Service not found"}
        service = self.services[real_key]
        port = service.get("port")
        pid = self._get_pid_by_port(port)
        return {"name": key, "port": port, "pid": pid, "running": bool(pid)}

    def status_all(self) -> list[dict]:
        """查询所有服务的运行状态。"""
        return [self.status(key) for key in self.services.keys()]

    # =========================================================================
    # 端口检测
    # =========================================================================

    def _get_pid_by_port(self, port: int) -> int | None:
        """
        通过端口号查找监听进程的 PID。
        使用 psutil.net_connections 遍历所有网络连接，
        匹配 LISTEN 状态且端口一致的第一条记录。
        """
        if port is None:
            return None
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                return conn.pid
        return None

    def _kill_process_on_port(self, port: int):
        """
        强杀占用指定端口的进程及其子进程树。
        auto_start 服务启动时如果端口被外部进程占用，先 kill 再启动。
        """
        pid = self._get_pid_by_port(port)
        if not pid:
            return
        try:
            proc = psutil.Process(pid)
            name = proc.name()
            log.warning(f"Killing process '{name}' (PID {pid}) occupying port {port}...")
            children = proc.children(recursive=True)
            for child in children:
                child.kill()
            psutil.wait_procs(children, timeout=3)
            proc.kill()
            proc.wait(timeout=5)
            log.success(f"Killed process '{name}' (PID {pid}), port {port} released.")
        except psutil.NoSuchProcess:
            log.info(f"Process on port {port} already gone.")
        except Exception as e:
            log.error(f"Failed to kill process on port {port}: {e}")

    # =========================================================================
    # 下载协调（防止服务先于下载被关闭）
    # =========================================================================

    def download_begin(self, service_name: str):
        """
        标记客户端开始从该服务下载文件。
        递增活跃下载计数，供 _wait_downloads 判断是否可安全停止。
        """
        key = self._find_service_key(service_name)
        if not key:
            return
        with self._lock:
            self._pending_outputs[key] = False  # 开始下载，清除待下载标记
            self._active_downloads[key] += 1

    def download_end(self, service_name: str):
        """
        标记客户端下载完成。
        递减活跃下载计数。
        """
        key = self._find_service_key(service_name)
        if not key:
            return
        with self._lock:
            if self._active_downloads[key] > 0:
                self._active_downloads[key] -= 1

    def mark_has_outputs(self, service_name: str):
        """
        任务完成时标记该服务有产出文件待下载。
        服务停止前会等待客户端下载这些文件。
        """
        key = self._find_service_key(service_name)
        if key:
            with self._lock:
                self._pending_outputs[key] = True

    def _wait_downloads(self, key: str, timeout: int = 10):
        """
        阻塞等待客户端的文件下载完成（带超时）。
        停止服务前调用，避免下载中途服务被关。

        等待逻辑：
          - 如果无待下载标记且无活跃下载 → 立即放行。
          - 如果有待下载但在超时内无客户端开始下载 → 放弃等待。
          - 如果有活跃下载 → 等待完成。
          - 日志限流：每 5 秒输出一条等待状态。
        """
        deadline = time.time() + timeout
        pending_start = time.time()
        last_log = 0
        while time.time() < deadline:
            with self._lock:
                pending = self._pending_outputs.get(key, False)
                active = self._active_downloads[key]
            if not pending and active <= 0:
                return True
            # 有产出但超时内无人下载 → 放弃
            if pending and active <= 0 and time.time() - pending_start > timeout:
                self._pending_outputs[key] = False
                log.warning(f"No download started for service '{key}' in {timeout}s, giving up.")
                return True
            # 日志限流（避免疯狂刷屏）
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

    # =========================================================================
    # 日志流（后台线程持续输出子进程日志）
    # =========================================================================

    def attach_log_stream(self, service_name: str):
        """
        将子进程 stdout 接入 Kalm 日志系统。
        服务就绪后调用，启动一个 daemon 线程持续读取子进程输出。
        进程退出后线程自动结束。
        """
        proc = self.processes.get(service_name)
        if not proc:
            return

        def stream():
            while proc.poll() is None:
                line = proc.stdout.readline()
                if line:
                    clean_line = _strip_ansi(line).strip()
                    log.info(f"[{service_name}] {clean_line}")
            log.info(f"Service '{service_name}' has exited.")

        threading.Thread(target=stream, daemon=True).start()


# =============================================================================
# 模块级单例 — 全局唯一服务控制器实例
# =============================================================================
service_controller = _ServiceController()
