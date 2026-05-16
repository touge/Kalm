# -*- coding: utf-8 -*-
"""
通用文件代理端点
=================
GET /file/{service_name}/{*file_path}
前端通过此端点访问后端服务生成的文件（图片、音频等），无需知道后端实际地址。
做好纯字节流中转，不关心文件内容和类型。
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import requests as sync_requests
from fastapi import APIRouter, Request
from fastapi.responses import Response

from src.core.service_controller import service_controller
from src.core.response import error
from src.logic.logger import log

router = APIRouter(tags=["Proxy"])

_executor = ThreadPoolExecutor(max_workers=4)


def _fetch_sync(url: str, headers: dict, timeout: int = 30) -> tuple:
    """同步拉取文件（线程池中运行，默认模式直接读 content，与 GUI 项目行为一致）"""
    resp = sync_requests.get(url, headers=headers, timeout=timeout)
    return resp.status_code, dict(resp.headers), resp.content


def _post_sync(url: str, headers: dict, body: bytes, timeout: int = 30) -> tuple:
    """同步 POST 请求，透传 raw body"""
    safe_headers = {k: v for k, v in headers.items() if k.lower() not in ("host", "content-length")}
    resp = sync_requests.post(url, headers=safe_headers, data=body, timeout=timeout)
    return resp.status_code, dict(resp.headers), resp.content

async def close_client():
    _executor.shutdown(wait=False)


@router.api_route("/file/{service_name}/{file_path:path}", methods=["GET", "HEAD", "POST"])
async def proxy_file(service_name: str, file_path: str, request: Request):
    """
    通用文件代理：从指定后端服务拉取文件并返回给前端，或向其发送文件。

    - service_name: 对应 services.yaml 中的服务名（如 ComfyUI, TTS）
    - file_path: 文件在后端服务上的路径
    """
    service_url = service_controller.get_service_url(service_name)
    if not service_url:
        return error(f"Service '{service_name}' not found", 404)

    backend_url = f"{service_url}/{file_path}"
    if request.url.query:
        backend_url += f"?{request.url.query}"

    headers = dict(request.headers)

    svc = service_controller.get_service_config(service_name)
    if svc:
        token = svc.get("token", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"

    try:
        service_controller.download_begin(service_name)

        loop = asyncio.get_event_loop()
        
        if request.method == "POST":
            body = await request.body()
            status_code, resp_headers, content = await loop.run_in_executor(
                _executor, _post_sync, backend_url, headers, body
            )
        else:
            status_code, resp_headers, content = await loop.run_in_executor(
                _executor, _fetch_sync, backend_url, headers
            )

        if status_code >= 400:
            log.warning(f"[FileProxy] Backend returned {status_code}: {backend_url}")
            return error(f"Backend returned {status_code}", status_code)

        log.info(f"[FileProxy] {service_name}:{file_path} -> {len(content)} bytes")

        response_headers = {}
        for key in ("content-type", "content-length", "content-disposition", "etag", "cache-control"):
            if key in resp_headers:
                response_headers[key] = resp_headers[key]

        return Response(
            content=content,
            status_code=status_code,
            headers=response_headers,
            media_type=response_headers.get("content-type"),
        )

    except sync_requests.ConnectionError:
        return error(f"Backend service '{service_name}' is not reachable", 502)
    except Exception as e:
        log.error(f"[FileProxy] Error proxying {backend_url}: {e}")
        return error(f"Proxy error: {str(e)}", 500)
    finally:
        service_controller.download_end(service_name)
