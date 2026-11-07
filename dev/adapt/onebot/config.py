#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
adapt.onebot.config
===================

OneBot v11 反向 WebSocket 连接配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class OneBotChannelConfig:
    """OneBotChannel 配置。

    字段：
        host                   WebSocket 服务端绑定地址（"127.0.0.1" / "0.0.0.0"）
        port                   WebSocket 服务端监听端口
        path                   URL 路径，必须以 "/" 开头
        access_token           访问令牌（None = 不校验）
        name                   Channel 名称（用于日志和注册表）
        message_post_format    NapCat 上报格式（"array" / "string"），本实现仅支持 "array"
        send_timeout           发送超时（秒）
        fail_fast_on_disconnect  NapCat 离线时调用 send() 立即抛 SendAbortedError（不缓冲、不等）
        record_raw_events      是否把每个原始事件落盘（用于事后分析与生成 fixture）
        raw_events_dir         录制目录（当天事件合并为单个 JSONL 文件）
        max_raw_events_per_day 单日最大录制数（防止磁盘爆掉）
    """
    host: str = "127.0.0.1"
    port: int = 5001
    path: str = "/onebot/v11/ws"
    access_token: Optional[str] = None
    name: str = "onebot"
    message_post_format: str = "array"
    send_timeout: float = 15.0
    fail_fast_on_disconnect: bool = True
    record_raw_events: bool = False
    raw_events_dir: str = "logs/onebot_raw_events"
    max_raw_events_per_day: int = 5000

    # 媒体下载（图片 / 表情包自动从 QQ CDN 下载到本地）
    media_download_enabled: bool = True
    media_base_dir: str = "memory/chat_history/media"
    media_download_timeout: float = 30.0
    media_download_max_size_mb: int = 50
    media_download_retries: int = 2

    def __post_init__(self) -> None:
        if not self.path.startswith("/"):
            self.path = "/" + self.path
        if self.message_post_format != "array":
            # OneBotChannel 的 parser 假设 array 格式（message 是 list[seg]）
            raise ValueError(
                f"OneBotChannel 仅支持 message_post_format='array'，"
                f"当前: {self.message_post_format!r}。"
                f"请在 NapCat 的 onebot.json 里把 messagePostFormat 改成 'array'。"
            )

    @classmethod
    def from_platform_config(cls, cfg) -> "OneBotChannelConfig":
        """从 OneBotConfig 平台配置构造通道配置，消除逐字段拷贝"""
        return cls(
            host=cfg.WS_HOST,
            port=cfg.WS_PORT,
            path=cfg.WS_PATH,
            access_token=cfg.ACCESS_TOKEN,
            name="onebot",
            fail_fast_on_disconnect=cfg.FAIL_FAST_ON_DISCONNECT,
            record_raw_events=cfg.RECORD_RAW_EVENTS,
            raw_events_dir=cfg.RAW_EVENTS_DIR,
            max_raw_events_per_day=cfg.MAX_RAW_EVENTS_PER_DAY,
            media_download_enabled=cfg.MEDIA_DOWNLOAD_ENABLED,
            media_base_dir=cfg.MEDIA_BASE_DIR,
            media_download_timeout=cfg.MEDIA_DOWNLOAD_TIMEOUT,
            media_download_max_size_mb=cfg.MEDIA_DOWNLOAD_MAX_SIZE_MB,
            media_download_retries=cfg.MEDIA_DOWNLOAD_RETRIES,
        )
