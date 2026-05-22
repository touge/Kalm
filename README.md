# Kalm

纯 AI 任务中转控制站。前端 ↔ Kalm ↔ 后端服务（ComfyUI / TTS / Ollama）。

核心原则：**透明代理 + FIFO 排队**。后端用什么协议，前端就用什么协议，Kalm 只做排队和透传，不做协议转换。

## 快速开始

```powershell
pip install -r requirements.txt
python main.py
```

服务默认运行在 `http://localhost:7000`。

## 四种代理通知模式

| 模式 | 端点 | 适用任务 | 说明 |
|------|------|---------|------|
| 队列广播 | `WS /interface/queue/ws` | 所有任务 | 全局广播：入队/开始/完成通知 |
| 任务 WS | `WS /interface/tasks/{id}/ws` | 所有任务 | 实时进度（ws 透传 / poll 转发） |
| multipart 上传 | `POST /interface/tasks/submit` (multipart/form-data) | `subtitle` | 上传音频文件 + 文稿 |
| HTTP 流式 | `POST /interface/llm/generate-stream` | `llm_streaming` | NDJSON 逐 token 推送 |

## API 端点

### 任务提交

```http
POST /interface/tasks/submit
Content-Type: application/json

{
  "task_type": "comfyui",       // comfyui | llm | tts | subtitle
  "payload": { ... }            // 透传给后端，不同任务类型不同
}
```

返回：
```json
{
  "task_id": "xxx",
  "status": "queued",
  "track_mode": "ws",
  "hint": "Connect to ws://host:port/interface/tasks/xxx/ws for real-time progress"
}
```
`track_mode` 标记告诉前端用什么方式收结果：`ws` → WebSocket、`poll` → 轮询、`stream` → NDJSON 流。

### 任务状态查询

```http
GET /interface/tasks/{task_id}/status
```

### 队列广播 WebSocket（推荐）

**一次连接，全程通知，无需轮询。**

```
ws://localhost:7000/interface/queue/ws
```

收到三种消息：`task_enqueued`（入队）、`task_started`（开始执行）、`task_completed`（完成，含下一任务信息）。客户端按 `task_id` 过滤自己的任务。

```javascript
const queueWs = new WebSocket("ws://localhost:7000/interface/queue/ws");
queueWs.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.task_id !== myTaskId) return; // 不是我的，忽略
  // msg.type: "task_enqueued" | "task_started" | "task_completed"
};
// 每 30 秒发 ping 保活
setInterval(() => { queueWs.send("ping"); }, 30000);
```

### 任务 WebSocket 进度

ws 和 poll 模式统一：收到 `task_started` 后连接任务 WS 获取实时进度。

```javascript
const taskWs = new WebSocket(`ws://localhost:7000/interface/tasks/${taskId}/ws`);
taskWs.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  // ws 模式(ComfyUI): 透传原生协议 {type: "executing", ...}
  // poll 模式(TTS): Kalm 轮询转发的进度 {status: "running", ...}
};
```

> 详细说明见 `docs/Kalm-队列WebSocket通知接口文档.md`

### LLM 流式生成

```http
POST /interface/llm/generate-stream
Content-Type: application/json

{ "model": "qwen3:14b", "prompt": "你好" }
```

返回 `application/x-ndjson` 流，每行一个 JSON，与 Ollama 原生 `/api/generate (stream=true)` 协议一致。

### LLM 非流式生成

```http
POST /interface/llm/generate
Content-Type: application/json

{ "model": "qwen3:14b", "prompt": "你好" }
```

### 模型列表

```http
GET /interface/llm/models
```

### TTS 语音 + 字幕（一条龙）

```http
POST /interface/tasks/submit
Content-Type: application/json

{ "task_type": "tts", "payload": { "path": "/v1.5/generate", "text": "...", "speaker": "...", "generate_subtitle": true } }
```

完成后通过静态端点下载产物：
```http
GET /file/tts/static/{backend_task_id}/{backend_task_id}.wav   # 音频
GET /file/tts/static/{backend_task_id}/{backend_task_id}.srt   # 字幕
```

### 字幕独立生成（已有音频）

```http
POST /interface/tasks/submit
Content-Type: multipart/form-data

task_type: subtitle
text: 文稿内容
audio_file: <音频文件>
```

产物下载：`GET /file/tts/static/{backend_task_id}/subtitle.srt`

### 文件访问

```http
GET /file/{service_name}/{file_path}
```

## 后端服务

| 服务 | 端口 | 协议 |
|------|------|------|
| ComfyUI | 7001 | HTTP + WebSocket |
| Ollama | 11434 | HTTP |
| TTS | 8001 | HTTP |

## 测试

```powershell
# 独立字幕
python test/test_subtitle.py
# TTS + 字幕一条龙
python test/test_tts_subtitle.py
# 交叉并发测试
python test/test_cross.py
```

产物输出到 `test/output/` 下对应子目录。

## 配置

- `config.yaml` — API 配置、任务映射、LLM 本地模型
- `services.yaml` — 后端子进程定义
- 配置值支持 `${ENV_VAR}` 语法引用环境变量

## 资源释放与批量提交

Kalm 每个任务完成后会检查队列，决定是否释放 GPU 资源：

| 队列状态 | 行为 |
|---|---|
| 有同类型任务排队 | 不释放，模型常驻复用 |
| 有不同类型任务 | 释放，为下一任务腾显存 |
| 队列为空 | 释放，回收显存 |

**重要**：释放决策只看队列里**已入队**的任务。如果前端串行提交（等上一个完成才提交下一个），每次完成时队列都是空的，必然触发释放 → 重新加载模型。

要避免频繁释放重载，**批量提交同类型任务**，让它们在队列中排队：
```
A入队 → B入队 → C入队  （A完成后 B 已在队列，跳过释放，直接复用）
```

而不是：
```
A入队 → 等A完成 → B入队 → 等B完成 → C入队  （每次完成队列都空，每次都释放+重载）
```

## 项目结构

```
src/
├── api/
│   ├── main.py              # FastAPI 应用工厂 + 生命周期
│   └── routes/
│       ├── tasks.py         # 任务提交 + 状态查询
│       ├── ws_proxy.py      # WebSocket 进度透传（含兜底轮询）
│       ├── queue_ws.py      # 队列广播 WebSocket
│       ├── stream_proxy.py  # HTTP 流式透传 (NDJSON)
│       ├── llm.py           # LLM 端点 (模型列表/普通生成)
│       ├── file_proxy.py    # 通用文件代理
│       └── system.py        # 健康检查
├── core/
│   ├── scheduler.py         # FIFO 队列调度器 + 智能资源释放
│   ├── task_manager.py      # 内存任务状态存储
│   ├── ws_manager.py        # WS 连接管理 (per-task + 队列广播)
│   ├── service_controller.py # 后端服务生命周期管理
│   ├── response.py          # API 响应辅助
│   ├── security.py          # Token 认证
│   └── executors/
│       ├── image.py         # ComfyUI 图像生成
│       ├── llm.py           # LLM 生成 (普通 + 流式)
│       ├── tts.py           # TTS 语音生成（支持 generate_subtitle）
│       └── subtitle.py      # 字幕生成（文件上传）
└── logic/
    ├── logger.py            # 日志
    ├── yaml_config_loader.py # YAML 配置加载
    └── task_cleanup.py      # 定期清理过期任务
```
