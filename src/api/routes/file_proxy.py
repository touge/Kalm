# -*- coding: utf-8 -*-
"""
通用文件代理端点
=================
GET /file/{service_name}/{*file_path}
前端通过此端点访问后端服务生成的文件（图片、音频等），无需知道后端实际地址。
做好纯字节流中转，不关心文件内容和类型。
"""

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from src.core.service_controller import service_controller
from src.core.response import error
from src.logic.logger import log

router = APIRouter(tags=["Proxy"])

# 使用 httpx 异步客户端复用连接
_client: httpx.AsyncClient = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    return _client


async def close_client():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


@router.api_route("/file/{service_name}/{file_path:path}", methods=["GET", "HEAD"])
async def proxy_file(service_name: str, file_path: str, request: Request):
    """
    通用文件代理：从指定后端服务拉取文件并流式返回给前端。

    - service_name: 对应 services.yaml 中的服务名（如 ComfyUI, TTS）
    - file_path: 文件在后端服务上的路径
    """
    service_url = service_controller.get_service_url(service_name)
    if not service_url:
        return error(f"Service '{service_name}' not found", 404)

    backend_url = f"{service_url}/{file_path}"
    if request.url.query:
        backend_url += f"?{request.url.query}"

    try:
        client = _get_client()
        headers = {}

        # 透传前端的 Range / If-None-Match 等请求头
        if "range" in request.headers:
            headers["range"] = request.headers["range"]

        # 如果后端服务配置了 token，自动带上认证头
        svc = service_controller.get_service_config(service_name)
        if svc:
            token = svc.get("token", "")
            if token:
                headers["Authorization"] = f"Bearer {token}"

        backend_resp = await client.send(
            client.build_request("GET", backend_url, headers=headers),
            stream=True,
        )

        if backend_resp.status_code >= 400:
            log.warning(f"[FileProxy] Backend returned {backend_resp.status_code}: {backend_url}")
            return error(
                f"Backend returned {backend_resp.status_code}",
                backend_resp.status_code,
            )

        # 透传 Content-Type 和其他关键响应头
        response_headers = {}
        for key in ("content-type", "content-length", "content-disposition", "etag", "cache-control"):
            if key in backend_resp.headers:
                response_headers[key] = backend_resp.headers[key]

        service_controller.download_begin(service_name)

        async def stream_with_tracking():
            try:
                async for chunk in backend_resp.aiter_bytes():
                    yield chunk
            finally:
                service_controller.download_end(service_name)

        return StreamingResponse(
            stream_with_tracking(),
            status_code=backend_resp.status_code,
            headers=response_headers,
        )

    except httpx.ConnectError:
        return error(f"Backend service '{service_name}' is not reachable", 502)
    except Exception as e:
        log.error(f"[FileProxy] Error proxying {backend_url}: {e}")
        return error(f"Proxy error: {str(e)}", 500)
