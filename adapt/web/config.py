# -*- coding: utf-8 -*-
"""
adapt.web.config
================

WebChannel 配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class WebChannelConfig:
    """Web 通道配置。

    跟 OneBotChannelConfig 字段尽量对齐，方便上层统一处理。
    """

    # 通道标识
    name: str = "web"

    # 反向 WebSocket 服务端（WebChannel 起在 host:port，前端来连）
    host: str = "127.0.0.1"
    port: int = 5002
    path: str = "/web/ws"

    # 鉴权（前端 JS 直连时用，生产环境建议打开）
    access_token: Optional[str] = None   # None = 不鉴权

    # 会话配置（单会话模式，浏览器侧无登录概念）
    user_id: str = "web_user"             # 归一化事件里的 user_id
    user_name: str = "Web User"           # 显示名
    session_id: str = "private_web_user"  # 跟 OneBot 私聊保持同构

    # 媒体（用户从前端发的图，存到本地让 SystemCore 引用）
    media_base_dir: Path = Path("memory/chat_history/media")

    # 限制（防止前端一不小心发个几百 MB 的图把后端打爆）
    max_image_bytes: int = 8 * 1024 * 1024   # 单张 8MB
    max_images_per_message: int = 9          # 跟 QQ 一次最多 9 张图对齐

    @classmethod
    def from_platform_config(cls, cfg) -> "WebChannelConfig":
        """从 WebConfig 平台配置构造通道配置，消除逐字段拷贝"""
        return cls(
            name="web",
            host=cfg.WS_HOST,
            port=cfg.WS_PORT,
            path=cfg.WS_PATH,
            access_token=cfg.ACCESS_TOKEN,
            user_id=cfg.USER_ID,
            user_name=cfg.USER_NAME,
            session_id=cfg.SESSION_ID,
            media_base_dir=Path(cfg.MEDIA_BASE_DIR),
            max_image_bytes=cfg.MAX_IMAGE_BYTES,
            max_images_per_message=cfg.MAX_IMAGES_PER_MESSAGE,
        )
