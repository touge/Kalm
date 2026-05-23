# Kalm API 认证指南

## 概述

Kalm 后端 API 采用 **Bearer Token** 认证机制，所有 API 请求（包括 HTTP 和 WebSocket）都需要携带有效的 Token。

## 配置说明

Token 在服务器端 `config.yaml` 中配置：

```yaml
api_server:
  tokens:
    - "your-token-here"
```

如需关闭认证（仅开发环境），将 tokens 留空：
```yaml
api_server:
  tokens: []
```

---

## HTTP 请求认证

所有 HTTP 请求需在 Header 中携带 `Authorization` 字段：

```
Authorization: Bearer <your-token>
```

### 示例

#### cURL
```bash
curl -H "Authorization: Bearer your-token-here" \
  http://localhost:7000/interface/system/services
```

#### JavaScript (Fetch)
```javascript
const response = await fetch('http://localhost:7000/interface/system/services', {
  method: 'GET',
  headers: {
    'Authorization': 'Bearer your-token-here',
    'Content-Type': 'application/json'
  }
});
```

#### JavaScript (Axios)
```javascript
import axios from 'axios';

const api = axios.create({
  baseURL: 'http://localhost:7000/interface',
  headers: {
    'Authorization': 'Bearer your-token-here'
  }
});

const services = await api.get('/system/services');
```

#### Python (Requests)
```python
import requests

headers = {'Authorization': 'Bearer your-token-here'}
response = requests.get('http://localhost:7000/interface/system/services', headers=headers)
```

---

## WebSocket 连接认证

WebSocket 连接支持两种 Token 传递方式。

### 方式一：URL 查询参数（推荐）

```javascript
const token = 'your-token-here';
const taskId = 'task-123';

const ws = new WebSocket(
  `ws://localhost:7000/interface/tasks/${taskId}/ws?token=${token}`
);

ws.onopen = () => console.log('Connected');
ws.onmessage = (event) => console.log('Received:', event.data);
```

### 方式二：Subprotocol 头

```javascript
const token = 'your-token-here';
const taskId = 'task-123';

const ws = new WebSocket(
  `ws://localhost:7000/interface/tasks/${taskId}/ws`,
  [`Bearer ${token}`]
);
```

---

## 前端封装建议

### 统一 API 客户端

```javascript
// api.js
const API_BASE = 'http://localhost:7000/interface';
const TOKEN = 'your-token-here';  // 从环境变量或配置获取

// HTTP 请求封装
export async function apiRequest(endpoint, options = {}) {
  const response = await fetch(`${API_BASE}${endpoint}`, {
    ...options,
    headers: {
      'Authorization': `Bearer ${TOKEN}`,
      'Content-Type': 'application/json',
      ...options.headers
    }
  });

  if (response.status === 401) {
    throw new Error('Token 无效或已过期，请检查配置');
  }

  return response.json();
}

// WebSocket 连接封装
export function createWebSocket(path) {
  return new WebSocket(`${API_BASE}${path}?token=${TOKEN}`);
}
```

### 使用示例

```javascript
import { apiRequest, createWebSocket } from './api';

// HTTP 请求
const services = await apiRequest('/system/services');

// 创建任务
const task = await apiRequest('/tasks/create', {
  method: 'POST',
  body: JSON.stringify({ service: 'comfyui', params: { ... } })
});

// WebSocket 进度监听
const ws = createWebSocket(`/tasks/${task.id}/ws`);
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Task progress:', data);
};
```

---

## 错误响应

### 401 Unauthorized

当 Token 缺失或无效时，返回：

```json
{
  "detail": "Missing or invalid authentication token"
}
```

**WebSocket 连接会被直接关闭**，关闭码：`4001`

---

## 常见问题

**Q: Token 从哪里获取？**

A: Token 由服务端管理员在 `config.yaml` 中配置，请联系管理员获取。

**Q: 可以配置多个 Token 吗？**

A: 可以，`api_server.tokens` 支持配置多个 Token，任一有效即可。

**Q: 本地开发需要 Token 吗？**

A: 如果服务端将 `tokens` 配置为空数组 `[]`，则无需 Token。

**Q: WebSocket 重连时 Token 会失效吗？**

A: 不会，Token 本身无状态，每次连接独立验证。