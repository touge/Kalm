# Kalm 任务通知接口文档

## 概述

Kalm 提供两种机制供下游客户端接收任务进度和结果：

| 通道 | 地址 | 用途 |
|------|------|------|
| 队列 WS | `ws://<host>:7000/interface/queue/ws` | 全局广播：任务入队、开始执行、完成 |
| 任务 WS | `ws://<host>:7000/interface/tasks/{task_id}/ws` | 单任务进度：执行中的实时数据 |

客户端只需维持**一个**队列 WS 长连接，即可获知所有任务的生命周期。任务开始后，再按需连接任务 WS 收取内容。

**适用版本**：Kalm ≥ 1.1.0

---

## 一、队列 WS — 生命周期通知

### 连接

```
ws://<kalm-host>:7000/interface/queue/ws
```

- 无需认证
- 建议在提交任务**之前**建立连接
- 断线后自动重连，重连期间错过消息无影响（可补查 status 接口）

### 三种消息

#### 1. task_enqueued — 已入队

提交任务后**立即**广播。任务已进入 FIFO 队列，等待执行。

```json
{
  "type": "task_enqueued",
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "task_type": "comfyui",
  "track_mode": "ws"
}
```

#### 2. task_started — 开始执行

任务从队列取出、开始执行时广播。

```json
{
  "type": "task_started",
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "task_type": "comfyui",
  "track_mode": "ws"
}
```

#### 3. task_completed — 执行完毕

任务执行完成时广播。`next_task_id` 告知队列中下一个任务，`null` 表示队列已空。

```json
{
  "type": "task_completed",
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "next_task_id": "660e8400-e29b-41d4-a716-446655440001",
  "next_task_type": "tts",
  "next_track_mode": "poll"
}
```

### 字段一览

| 字段 | 出现于 | 类型 | 说明 |
|------|------|------|------|
| `type` | 全部 | string | `task_enqueued` / `task_started` / `task_completed` |
| `task_id` | 全部 | string | 任务唯一标识（UUID v4） |
| `task_type` | enqueued / started | string | 任务种类，见下方枚举 |
| `track_mode` | enqueued / started | string | 跟踪方式，见下方枚举 |
| `next_task_id` | completed | string\|null | 队列中下一任务 ID，空为 `null` |
| `next_task_type` | completed | string\|null | 下一任务种类，空为 `null` |
| `next_track_mode` | completed | string\|null | 下一任务跟踪方式，空为 `null` |

### 枚举

**task_type**

| 值 | 含义 | 后端 |
|------|------|------|
| `comfyui` | AI 图像生成 | ComfyUI |
| `tts` | 文本转语音 | IndexTTS |
| `subtitle` | 字幕生成 | TTS Whisper |
| `llm` | 大模型对话 | Ollama |

**track_mode**

| 值 | 含义 | 收到 task_started 后的动作 |
|------|------|------|
| `ws` | 后端原生 WS 推送 | 连 `/tasks/{task_id}/ws` 收实时进度 |
| `poll` | 后端 HTTP 轮询 | 连 `/tasks/{task_id}/ws` 收 Kalm 轮询转发的进度 |

> 对客户端而言，`ws` 和 `poll` **行为完全一致**：都是连任务 WS 收数据。区别仅在 Kalm 内部实现。

---

## 二、任务 WS — 执行进度与结果

### 连接

```
ws://<kalm-host>:7000/interface/tasks/{task_id}/ws
```

在收到队列 WS 的 `task_started` 之后连接。任务完成后 Kalm 自动关闭连接。

### 消息格式

内容由后端服务决定。以 ComfyUI 为例：

```json
{"type": "executing", "data": {"node": "15", "prompt_id": "..."}}
{"type": "progress", "data": {"value": 24, "max": 40}}
{"type": "task_complete", "status": "success", "result": {...}}
{"type": "task_failed", "status": "failed", "result": {"message": "..."}}
```

两种模式都会以 `task_complete` 或 `task_failed` 结束。客户端可据此关闭任务 WS 连接。

### 完成后的结果获取

收到 `task_completed`（队列 WS 或任务 WS 均可）后，拉取最终结果：

```
GET /interface/tasks/{task_id}/status
```

返回示例：

```json
{
  "task_id": "xxx",
  "status": "success",
  "task_type": "tts",
  "result": {
    "audio_url": "/file/tts/static/abc/abc.wav",
    "subtitle_url": "/file/tts/static/abc/abc.srt"
  }
}
```

---

## 三、健壮性说明

### 任务 WS 的双保险机制

```
主线：  executor → ws_manager.send() → 实时推送 → 前端 WS
                                           ↘ 推送失败？
兜底：  ws_proxy 内部轮询 TaskManager（每 2 秒）→ 检测完成 → 前端 WS
```

- **主线推送失败不影响最终结果**。兜底轮询最多 2 秒延迟内补发完成通知。
- **竞态已处理**：先注册 WS 连接再检查任务状态，避免检查和注册之间任务正好完成而漏掉。

### 队列 WS 断连

队列 WS 断连期间，客户端可通过以下方式恢复状态：

1. 重连队列 WS
2. 对仍在关注的任务，GET `/interface/tasks/{task_id}/status` 补查当前状态
3. 若状态为 `queued`，继续等 `task_started`
4. 若状态为 `running`，连任务 WS
5. 若状态为 `success`/`failed`，直接取结果

### 三种提交方式的稳定性

| 模式 | 完成可靠性 | 进度实时性 | 故障影响 |
|------|------|------|------|
| ws（ComfyUI 透传）| 双保险，不会丢 | 毫秒级 | WS 断连需重连 |
| poll（TTS 轮询转发）| 双保险，不会丢 | 2 秒内 | 中间进度可能丢，完成不丢 |

---

## 四、客户端接入

### 完整示例

```javascript
// 我的任务集合
const myTasks = new Map(); // taskId → { taskType, trackMode }

// 建立队列 WS（提交任务前）
const queueWs = new WebSocket("ws://localhost:7000/interface/queue/ws");

queueWs.onmessage = (event) => {
  const msg = JSON.parse(event.data);

  // 入队通知：记录是我的就认领
  if (msg.type === "task_enqueued") {
    if (myTasks.has(msg.task_id)) {
      console.log(`任务 ${msg.task_id} 已排队`);
    }
    return;
  }

  // 不是我的任务，忽略
  if (!myTasks.has(msg.task_id)) return;

  if (msg.type === "task_started") {
    console.log(`任务 ${msg.task_id} 开始执行`);
    // ws 和 poll 统一：连接任务 WS 收进度
    connectTaskWs(msg.task_id);
    return;
  }

  if (msg.type === "task_completed") {
    console.log(`任务 ${msg.task_id} 完成`);
    fetchResult(msg.task_id);
    myTasks.delete(msg.task_id);
    return;
  }
};

queueWs.onclose = () => {
  // 指数退避重连：1s → 2s → 4s → 上限 30s
  setTimeout(reconnect, nextBackoff());
};

// 连接任务 WS
function connectTaskWs(taskId) {
  const taskWs = new WebSocket(
    `ws://localhost:7000/interface/tasks/${taskId}/ws`
  );
  taskWs.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "task_complete" || msg.type === "task_failed") {
      taskWs.close();
      // 处理 msg.result 中的业务数据
    }
    // 其他消息为执行中的进度数据，按需处理
  };
}

// 提交任务
async function submitTask(taskType, payload) {
  const res = await fetch("http://localhost:7000/interface/tasks/submit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_type: taskType, payload }),
  });
  const data = await res.json();
  // 记录我的任务
  myTasks.set(data.task_id, {
    taskType: data.task_type,
    trackMode: data.track_mode,
  });
  return data.task_id;
}

// 拉取结果
async function fetchResult(taskId) {
  const res = await fetch(`/interface/tasks/${taskId}/status`);
  const data = await res.json();
  if (data.status === "success") {
    // 处理 data.result
  }
}
```

### 完整生命周期（以 tts 任务为例）

```
1. 提交 POST /interface/tasks/submit { task_type: "tts", payload: {...} }
   ← 返回 { task_id: "xxx", track_mode: "poll" }
   
2. 队列 WS 收到 task_enqueued(id=xxx, track_mode=poll)
   → "我的 tts 任务已排队"

3. 队列 WS 收到 task_started(id=xxx, track_mode=poll)
   → 连接 /tasks/xxx/ws

4. 任务 WS 收到进度消息（Kalm 轮询 TTS 后端转发的中间状态）
   → { status: "running", ... }

5. 任务 WS 收到 task_complete
   或队列 WS 收到 task_completed(id=xxx)
   → GET /tasks/xxx/status 拿最终结果
   → 下载产物：GET /file/tts/static/...

6. 后续再无通知，任务结束
```

---

## 五、批量提交建议

Kalm 在连续同类型任务时跳过 GPU 资源释放，模型复用避免重载。

| 提交方式 | 释放行为 | 建议 |
|------|------|------|
| 批量提交同类型 | 连续执行，不释放 | ✅ 推荐 |
| 逐个等完成再提交 | 每次完成队列空，释放重载 | ❌ 避免 |

---

## 六、错误处理

- **队列 WS 断连**：指数退避重连（1s → 2s → 4s → 上限 30s），重连后补查 status 接口恢复状态
- **任务 WS 断连**：重连即可，Kalm 无状态，重连后继续收后续消息
- **任务失败**：`task_completed` 或 `task_failed` 中 `status` 为 `failed`，取 `result.message` 获取错误原因
