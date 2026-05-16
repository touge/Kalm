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


def main():
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
