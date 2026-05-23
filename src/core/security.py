# -*- coding: utf-8 -*-
"""
API 安全认证
============
使用中间件方式验证 Bearer Token，同时支持 HTTP 和 WebSocket 连接。

配置：config.yaml 中 api_server.tokens
- 留空则跳过认证
- HTTP 请求通过 Authorization: Bearer <token> 头验证
- WebSocket 请求通过 query 参数 ?token=<token> 或 Sec-WebSocket-Protocol 头验证
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.websockets import WebSocket
from starlette.responses import JSONResponse
from src.logic.yaml_config_loader import yaml_config_loader


class TokenAuthMiddleware:
    """同时支持 HTTP 和 WebSocket 的 Token 认证中间件"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from starlette.requests import Request
        from starlette.websockets import WebSocket as StarletteWebSocket
        from starlette.routing import NoMatchFound

        if scope["type"] == "http":
            request = Request(scope)
            if not self._verify_http_token(request):
                response = JSONResponse(
                    status_code=401,
                    content={"detail": "Missing or invalid authentication token"},
                )
                await response(scope, receive, send)
                return
        elif scope["type"] == "websocket":
            ws = StarletteWebSocket(scope)
            if not self._verify_ws_token(ws):
                await ws.close(code=4001, reason="Missing or invalid authentication token")
                return

        await self.app(scope, receive, send)

    def _verify_http_token(self, request) -> bool:
        tokens = yaml_config_loader.get("api_server.tokens", [])
        if not tokens:
            return True

        authorization = request.headers.get("authorization", "")
        if not authorization:
            return False

        # 支持 "Bearer <token>" 格式
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1] in tokens

        # 也直接支持 token 字符串
        return authorization in tokens

    def _verify_ws_token(self, ws) -> bool:
        tokens = yaml_config_loader.get("api_server.tokens", [])
        if not tokens:
            return True

        # 方式1: 从 query 参数获取 token (?token=xxx)
        query_token = ws.query_params.get("token", "")
        if query_token and query_token in tokens:
            return True

        # 方式2: 从 Subprotocols 获取 token
        # 客户端可以发送: Sec-WebSocket-Protocol: Bearer <token>
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