# CLAUDE.md

## 项目定位

Kalm 是纯 AI 任务中转控制站。前端 ↔ Kalm ↔ 后端服务（ComfyUI / TTS / Ollama）。

核心原则：**透明代理 + FIFO 排队**。后端用什么协议，Kalm 对前端就暴露什么协议。只做排队和透传，不做协议转换。

## 架构概要

```
POST /interface/tasks/submit      → scheduler (FIFO 队列) → executor → 后端服务
GET  /interface/tasks/{id}/status → TaskManager (内存状态)
WS   /interface/tasks/{id}/ws     → ws_manager (实时推送，ComfyUI 原生 WS 协议)
WS   /interface/queue/ws          → ws_manager 队列广播 (任务生命周期通知)
POST /interface/llm/generate-stream → 排队 → StreamingResponse (NDJSON 透传)
GET  /file/{service}/{path}      → httpx → 后端 → 流式返回前端
```

- 调度器：`src/core/scheduler.py`，单例单线程，`queue.Queue` + 服务生命周期。支持普通任务和流式任务（带 `output_queue`/`started_event`）。支持智能资源释放（详见下方"资源释放策略"）。
- 执行器：`src/core/executors/*.py`，每个只做 提交→透传。普通执行器签名 `def execute(task_id, **payload)`，流式执行器签名 `def execute_stream(task_id, output_queue, **payload)`。
- 文件代理：`src/api/routes/file_proxy.py`，通用端点。`service_name` 可以是 `task_type`（如 `comfyui`），通过 `config.yaml` 的 `tasks.{task_type}.service` 映射到实际服务名（如 `ComfyUI_Windows`）。使用 requests 库同步拉取 + ThreadPoolExecutor。
- 任务提交支持 JSON 和 multipart/form-data 两种 Content-Type（字幕任务需文件上传）。
- 服务管理：`src/core/service_controller.py`，引用计数 + 子进程启停。`_find_service_key` 大小写不敏感，精确匹配失败后模糊匹配兜底（`ComfyUI` → `ComfyUI_Windows`）。支持 `auto_start` 常驻模式（Kalm 启动时自动拉起，常驻不回收）和启动时端口强杀。
- 任务状态：`src/core/task_manager.py`，内存字典 + threading.Lock + TTL 自动清理。
- WS 管理：`src/core/ws_manager.py`，桥接同步执行器线程 → 异步 WS 推送（`run_coroutine_threadsafe`）。支持 per-task 推送和全局队列广播（`broadcast_queue()`）。
- WS 端点：`src/api/routes/ws_proxy.py`，前端连 `/tasks/{id}/ws` 获取实时进度。内部有兜底轮询（`poll_fallback`，每 2 秒检查 TaskManager），不依赖特定后端协议的 WS 也能检测完成。
- 队列广播端点：`src/api/routes/queue_ws.py`，前端连 `/queue/ws` 接收任务生命周期通知（`task_enqueued` / `task_started` / `task_completed`）。所有连接客户端收到相同广播，按 `task_id` 自行过滤。
- 流式端点：`src/api/routes/stream_proxy.py`，前端 POST `/llm/generate-stream` 获取 NDJSON 流式响应。

## 安全认证

双层认证架构，`config.yaml` 中 `api_server.tokens` 配置驱动：

| 层 | 机制 | 适用范围 |
|---|---|---|
| HTTP 依赖 | `require_token` + `OptionalHTTPBearer` + `Security()` | 所有 HTTP 路由，Swagger UI 自动显示 Authorize 按钮 |
| WS 中间件 | `WebSocketAuthMiddleware` | 所有 WebSocket 连接，支持 `Authorization: Bearer` 和 `x-token` 两种 Header |

`OptionalHTTPBearer` 是 `HTTPBearer` 的子类，`request` 参数可选：WebSocket 路由无 `Request` 对象时走默认值 `None` 跳过 HTTP 认证层，交由中间件处理。

**WebSocket 认证失败反馈**：先 `accept()` 完成握手，再 `close(code=4001, reason="...")`。客户端通过关闭码 4001 区分认证失败与其他错误。

**服务名解析**：`_find_service_key` 二级查找 — 精确匹配（大小写不敏感）→ 模糊匹配兜底（`ComfyUI` 匹配 `ComfyUI_Windows`）。

详细使用指南：`API_AUTH.md`

## 协议透传原则

| 后端 | 后端原生协议 | Kalm 对前端协议 | 实现 |
|---|---|---|---|
| ComfyUI | HTTP POST + WS 进度 | HTTP POST + WS 进度 | `image.py` + `ws_proxy.py` |
| Ollama 流式 | HTTP NDJSON 流 | HTTP NDJSON 流 | `stream_proxy.py` |
| Ollama 非流式 | HTTP 一次性 | HTTP 一次性 | `llm.py` |
| TTS | HTTP 轮询 | HTTP 轮询 | `tts.py` + `tasks.py`（支持 `generate_subtitle: True` 同步生成字幕） |
| 字幕 | multipart POST + HTTP 轮询 | multipart POST + HTTP 轮询 | `subtitle.py` + `tasks.py` |
| 文件 | HTTP GET | HTTP GET | `file_proxy.py` |

## 代码约定

- 所有模块通过 `importlib.import_module` 动态加载，由 `config.yaml` 驱动。**不要静态导入 executor。**
- 添加新任务类型：在 `src/core/executors/` 新建文件 → 在 `config.yaml` 注册（含 `track_mode`） → 重启，不改调度器代码。
- 普通 executor 函数签名：`def execute(task_id: str, **payload):`，结果通过 `TaskManager.update_task()` 写回。
- 流式 executor 函数签名：`def execute_stream(task_id: str, output_queue: queue.Queue, **payload):`，逐行写入 `output_queue`，结束写 `None`。
- 支持 WS 推送的执行器：额外调用 `ws_manager.send(task_id, message)` 推送进度，完成时发 `{"type": "task_complete"}`，失败发 `{"type": "task_failed"}`。
- executor 绝不做：下载文件、改写 URL、处理音频/图片、判断代理/下载模式。
- executor 获取服务名：**统一从 `payload["_service_name"]` 获取**（调度器注入，来源于 `config.yaml` 的 `tasks.{task_type}.service`），不要硬编码服务名。默认值仅作 fallback。
- 所有 API 响应走 `src/core/response.py` 的 `success()` / `error()` 辅助函数。
- 环境变量用 `${VAR}` 语法在 `config.yaml` 中引用。
- 日志用 `src/logic/logger.py` 的 `log` 实例，支持 `log.success()`。

## 配置

- `config.yaml` — API、任务映射、LLM 本地模型
- `services.yaml` — 后端子进程定义。新增 `auto_start` 字段（true=Kalm 启动时自动拉起并常驻），与 `manage_lifecycle` 互斥。`startup_timeout` 自定义启动超时。新增 `free_api` 字段（可选）定义资源释放接口，未配置的服务跳过释放。

## 资源释放策略

调度器在**每个任务完成后**决策是否释放后端服务的 GPU 资源（调用 `services.yaml` 中配置的 `free_api` 接口）。决策逻辑：

| 下一任务类型 | 行为 | 说明 |
|---|---|---|
| 同类型（已在队列中） | 跳过释放 | 模型常驻，避免重复加载 |
| 不同类型 | 释放 | 切换服务前释放旧模型 |
| 队列为空（暂无下一任务） | 释放 | 确认无后续任务，回收显存 |

**关键限制**：释放决策基于队列中**已入队**的任务。如果下游串行提交（等上一个完成才提交下一个），上一个完成时队列为空，必然触发释放 → 重载。要利用"同类型跳过释放"特性，必须**批量提交，让任务在队列中排队**。

只有 `services.yaml` 中配置了 `free_api` 的服务才会被调用释放接口（目前仅 ComfyUI）。TTS、Ollama 等未配置，自动跳过。

`auto_start` 仅保证服务**进程**常驻不回收，模型仍可能被 `free_api` 释放。两者独立。

## 队列 WS 广播机制

调度器在三个关键节点通过 `ws_manager.broadcast_queue()` 广播任务生命周期事件：

| 触发点 | 消息类型 | 携带数据 |
|---|---|---|
| `submit_task()` 入队后 | `task_enqueued` | task_id, task_type, track_mode |
| `_scheduler_loop` 取到任务 | `task_started` | task_id, task_type, track_mode |
| `_scheduler_loop` 执行完毕 | `task_completed` | task_id, next_task_id/type/track_mode |

**客户端行为**：
- 连上 `/interface/queue/ws` 后收到所有广播，按 `task_id` 过滤自己的任务
- `task_started` 后，ws 和 poll 模式统一连接 `/tasks/{id}/ws` 获取执行进度
- `task_completed` 后 GET `/tasks/{id}/status` 拉取最终结果

**任务 WS 双保险**：
- 主线：executor 通过 `ws_manager.send()` 实时推送进度
- 兜底：`ws_proxy.poll_fallback` 每 2 秒轮询 TaskManager，完成时补发通知
- 主线推送失败不影响最终结果，兜底最多 2 秒延迟

**心跳保活**：客户端每 30 秒发 `ping`，Kalm 回 `pong`，防止中间网络层断开空闲连接。

## 测试脚本

| 脚本 | 说明 |
|------|------| 
| `test/test_cross.py` | 交叉测试：多类型任务并发提交 + 自适应轮询 |
| `test/test_serial.py` | 串行测试：逐个提交，等完成后下一个 |
| `test/test_comfyui.py` | ComfyUI 图像生成端到端 |
| `test/test_tts.py` | TTS 语音生成 + 可选字幕下载 |
| `test/test_subtitle.py` | 独立字幕：上传本地音频 + 文稿 → SRT |
| `test/test_tts_subtitle.py` | TTS 一条龙：`generate_subtitle: True` 同时产出音频+字幕 |
| `test/_diag_subtitle.py` | 诊断脚本：快速验证 TTS+字幕链路 |

产物统一输出到 `test/output/` 子目录。

## 运行

```powershell
pip install -r requirements.txt
python main.py
```
