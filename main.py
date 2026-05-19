# -*- coding: utf-8 -*-
"""
Kalm 入口
=================
启动 FastAPI 服务，监听配置指定的 host:port。
"""

import sys
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn
from src.logic.yaml_config_loader import yaml_config_loader
from src.core.service_controller import service_controller


def main():
    # Kalm 启动时自动拉起 auto_start: true 的服务（常驻，不受引用计数回收）
    service_controller.start_auto_services()

    host = yaml_config_loader.get("api_server.host", "0.0.0.0")
    port = yaml_config_loader.get("api_server.port", 7000)

    uvicorn.run(
        "src.api.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
