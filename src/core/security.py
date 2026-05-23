# -*- coding: utf-8 -*-
"""
API 安全认证
============
使用 FastAPI 原生 Security + Depends 机制验证 Bearer Token，
这样 /docs 页面会自动显示 Authorize 按钮。

同时保留 WebSocket 中间件认证（WebSocket 不支持 Depends）。

配置：config.yaml 中 api_server.tokens
- 留空则跳过认证
- HTTP 请求通过 Authorization: Bearer <token> 头验证
- WebSocket 请求通过 query 参数 ?token=<token> 或 Sec-WebSocket-Protocol 头验证
"""

from fastapi import Depends, HTTPException, Security, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.websockets import WebSocket
from starlette.responses import JSONResponse
from src.logic.yaml_config_loader import yaml_config_loader

# FastAPI 原生的 Bearer token 提取器
security_scheme = HTTPBearer(auto_error=True)


async def require_token(
    credentials: HTTPAuthorizationCredentials = Security(security_scheme)
):
    """
    全局认证依赖。使用 Security(security_scheme) 作为默认参数，
    这样 FastAPI / Swagger UI 会自动在 docs 页面显示 Authorize 按钮。

    关键：必须使用 Security() 而非 Depends() 作为参数默认值，
    Swagger UI 才能识别这个安全方案并显示认证输入框。
    """
    tokens = yaml_config_loader.get("api_server.tokens", [])
    if not tokens:
        # 未配置 token，跳过认证
        return True

    if credentials.credentials not in tokens:
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    return True


class WebSocketAuthMiddleware:
    """WebSocket 专用认证中间件 — 只处理 websocket 类型请求"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return

        ws = WebSocket(scope, receive=receive, send=send)
        if not self._verify_ws_token(ws):
            await ws.close(code=4001, reason="Missing or invalid authentication token")
            return

        await self.app(scope, receive, send)

    def _verify_ws_token(self, ws) -> bool:
        tokens = yaml_config_loader.get("api_server.tokens", [])
        if not tokens:
            return True

        # 方式1: 从 query 参数获取 token (?token=xxx)
        query_token = ws.query_params.get("token", "")
        if query_token and query_token in tokens:
            return True

        # 方式2: 从 Subprotocols 获取 token
        subprotocols = ws.headers.get("sec-websocket-protocol", "")
        if subprotocols:
            for proto in subprotocols.split(","):
                proto = proto.strip()
                parts = proto.split()
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    if parts[1] in tokens:
                        return True
                elif proto in tokens:
                    return True

        return False
