# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends
from src.core.response import success
from src.core.security import verify_token
from src.core.service_controller import service_controller

# router = APIRouter(tags=["System"], dependencies=[Depends(verify_token)])
router = APIRouter(tags=["System"])


@router.get("/ping")
async def ping():
    """
    健康检查：返回中转站自身状态 + 所有下游服务连通性。
    """
    services_status = service_controller.status_all()
    all_running = all(s["running"] for s in services_status)

    return success(data={
        "ping": "pong",
        "service": "Kalm",
        "downstream_services": services_status,
        "all_healthy": all_running,
    })
