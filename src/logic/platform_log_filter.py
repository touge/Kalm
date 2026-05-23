# -*- coding: utf-8 -*-
"""
平台监控日志过滤器
==================
屏蔽部署平台（如 AutoDL）探活请求产生的 uvicorn 日志，
避免 /api/jobs 和 /ws 的日志刷屏。
"""

import logging


class PlatformNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        
        # 1. 拦截特定的 HTTP 路径
        if "/api/jobs" in msg:
            return False
            
        # 2. 拦截特定的 WebSocket 握手请求（排除你自己的 /interface 路径）
        if "WebSocket /ws" in msg and "/interface" not in msg:
            return False
            
        # 3. 拦截由 uvicorn.error 产生的 WebSocket 连接状态提示
        if "connection open" in msg or "connection closed" in msg:
            return False
            
        return True


def setup_platform_log_filter():
    # 同时获取访问日志和错误/状态日志两个通道
    loggers = [
        logging.getLogger("uvicorn.access"),
        logging.getLogger("uvicorn.error")
    ]
    
    for logger in loggers:
        # 移除旧实例，防止重复添加
        for f in logger.filters[:]:
            if isinstance(f, PlatformNoiseFilter):
                logger.removeFilter(f)
        # 注入更新后的过滤器
        logger.addFilter(PlatformNoiseFilter())