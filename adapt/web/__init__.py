# -*- coding: utf-8 -*-
"""
adapt.web
=========

WebChannel：浏览器前端作为消息通道，替代 NapCat/QQ。

数据流（与 OneBotChannel 几乎对称）：

  浏览器
    |
    |  WebSocket ws://host:port/web/ws
    |  发送 {"type": "message", "text": "...", "images": [data:...]}
    v
  WebChannel._on_text
    |
    |  保存图片到 <MEDIA_BASE_DIR>/images/<date>/web_<uuid>.<ext>
    |  构造 NormalizedEvent (platform=WEB, scope=PRIVATE, session_id="private_web_user")
    |  publish 到 message_bus 主题 "user_message"
    |  echo 用户消息回 WS（让前端立刻看到自己的消息）
    v
  message_bus -> SystemCore -> attention -> brain -> LLM -> tool
    |
    |  OutboundRouter.send(platform="web", target, payload)
    v
  WebChannel.send
    |
    |  序列化 MessageSegment 列表为 JSON
    |  广播给所有已连接的 WS 客户端
    v
  浏览器（显示 LLM 回复）

文件：
- adapt/web/__init__.py
- adapt/web/config.py   - WebChannelConfig
- adapt/web/channel.py  - WebChannel 实现
"""

from .config import WebChannelConfig
from .channel import WebChannel
from .models import WebSession
from .db_store import DBStore

__all__ = ["WebChannel", "WebChannelConfig", "WebSession", "DBStore"]
