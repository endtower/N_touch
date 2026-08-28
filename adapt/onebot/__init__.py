#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
adapt.onebot
============

OneBot v11 平台实现（通过 NapCat / go-cqhttp 等客户端连接）。

提供：
    - OneBotChannelConfig   配置
    - OneBotChannel         反向 WebSocket 服务端实现
    - parse_onebot_event    OneBot 事件 → NormalizedEvent（纯函数）
    - serialize_to_onebot   通用段 → OneBot 段（纯函数）
    - OB_* 常量
"""

from .config import OneBotChannelConfig
from .parser import parse_onebot_event
from .serializer import serialize_to_onebot
from .channel import OneBotChannel
from . import constants

__all__ = [
    "OneBotChannelConfig",
    "OneBotChannel",
    "parse_onebot_event",
    "serialize_to_onebot",
    "constants",
]
