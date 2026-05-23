# Kalm API 认证使用指南

## 概述

Kalm 所有 API 接口均受 Bearer Token 保护，包括 HTTP 请求和 WebSocket 连接。

---

## 配置

在 `config.yaml` 中配置允许的 Token 列表：

```yaml
api_server:
  tokens:
    - "your-secret-token-1"
    - "your-secret-token-2"
```

- **留空或不配置**：跳过认证（仅建议本地开发使用）
- **配置多个 token**：任意一个均可通过验证

---

## HTTP 请求认证

所有 HTTP API 请求需在 Header 中携带 `Authorization` 字段：

```
Authorization: Bearer your-secret-token-1
```

### 示例

```bash
# curl 示例
curl -H "Authorization: Bearer your-secret-token-1" http://127.0.0.1:7000/interface/status

# Python 示例
import requests
headers = {"Authorization": "Bearer your-secret-token-1"}
response = requests.get("http://127.0.0.1:7000/interface/status", headers=headers)
```

### 错误响应

- **401 Unauthorized**：Token 无效或未提供
```json
{"detail": "Invalid authentication token"}
```

---

## WebSocket 连接认证

WebSocket 连接支持两种认证方式：

### 方式 1：Query 参数（推荐）

在 WebSocket URL 中添加 `?token=xxx` 参数：

```javascript
// JavaScript 示例
const ws = new WebSocket("ws://127.0.0.1:7000/interface/ws?token=your-secret-token-1");
```

```python
# Python websockets 示例
import websockets
uri = "ws://127.0.0.1:7000/interface/ws?token=your-secret-token-1"
async with websockets.connect(uri) as ws:
    # ...
```

### 方式 2：Subprotocol Header

在 `Sec-WebSocket-Protocol` 头中携带 Token：

```javascript
// JavaScript 示例
const ws = new WebSocket("ws://127.0.0.1:7000/interface/ws", {
  protocols: "Bearer your-secret-token-1"
});
```

### 错误响应

- **4001 关闭码**：Token 无效或未提供
- 关闭原因：`"Missing or invalid authentication token"`

---

## Swagger UI 测试

FastAPI 内置的 `/docs` 页面支持直接在 UI 中输入 Token：

1. 访问 `http://127.0.0.1:7000/docs`
2. 点击右上角 **Authorize** 按钮
3. 输入 Bearer Token（只需输入 token 值，不要加 "Bearer " 前缀）
4. 点击 **Authorize**
5. 点击 **Close**
6. 现在可以直接在页面上测试所有 API，请求会自动携带 Token

---

## 前端集成示例

### JavaScript (fetch)

```javascript
const TOKEN = "your-secret-token-1";

async function apiCall(endpoint) {
  const response = await fetch(endpoint, {
    headers: {
      "Authorization": `Bearer ${TOKEN}`
    }
  });
  return response.json();
}

// 使用
apiCall("http://127.0.0.1:7000/interface/status").then(console.log);
```

### JavaScript (WebSocket)

```javascript
const TOKEN = "your-secret-token-1";
const ws = new WebSocket(`ws://127.0.0.1:7000/interface/ws?token=${TOKEN}`);

ws.onopen = () => console.log("Connected");
ws.onmessage = (e) => console.log("Message:", e.data);
ws.onclose = (e) => console.log("Closed:", e.code, e.reason);
```

### Python (requests)

```python
import requests

TOKEN = "your-secret-token-1"
BASE_URL = "http://127.0.0.1:7000"
headers = {"Authorization": f"Bearer {TOKEN}"}

# 获取状态
response = requests.get(f"{BASE_URL}/interface/status", headers=headers)
print(response.json())
```

### Python (websockets)

```python
import asyncio
import websockets

TOKEN = "your-secret-token-1"
uri = f"ws://127.0.0.1:7000/interface/ws?token={TOKEN}"

async def main():
    async with websockets.connect(uri) as ws:
        await ws.send("test message")
        response = await ws.recv()
        print(response)

asyncio.run(main())
```

---

## 安全建议

1. **生产环境务必配置 Token**：不要留空
2. **使用强随机 Token**：建议 32 位以上随机字符串
3. **定期轮换 Token**：修改 `config.yaml` 后重启服务即可生效
4. **限制网络暴露**：建议只绑定 `127.0.0.1` 或通过反向代理暴露
5. **HTTPS/WSS**：生产环境建议配合 Nginx 等使用 HTTPS

---

## 常见问题

### Q: 忘记 Token 怎么办？
查看 `config.yaml` 文件中 `api_server.tokens` 配置的值。

### Q: 如何临时关闭认证？
将 `api_server.tokens` 设为空列表 `[]` 或删除该配置，重启服务。

### Q: 401 错误怎么解决？
1. 确认 Token 值正确（无多余空格）
2. 确认 Header 格式为 `Authorization: Bearer <token>`
3. WebSocket 确认 URL 包含 `?token=<token>`

### Q: 多个客户端用不同 Token 可以吗？
可以，`tokens` 列表中的任何一个都能通过验证。