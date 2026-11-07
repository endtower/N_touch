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
    |  直达消息处理器 message_handler.process_message 落库
    |  echo 用户消息回 WS（让前端立刻看到自己的消息）
    v
  message_handler -> chat_history.db -> attention/trucks 轮询 -> brain -> LLM -> tool
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

API 文档：docs/api/web_api.md —— 修改本包对外接口（HTTP 路由 / WS action / 推送）
后必须同步更新该文档（校验：python scripts/check_api_docs.py）。
"""

from .config import WebChannelConfig
from .channel import WebChannel
from .models import WebSession
from .db_store import DBStore

__all__ = ["WebChannel", "WebChannelConfig", "WebSession", "DBStore"]
