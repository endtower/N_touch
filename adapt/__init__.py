#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
adapt
=====

平台无关的"外部 IM 适配层"。

职责：
    1. 把不同平台（QQ / 微信 / ...）的事件归一化为统一数据形状（NormalizedEvent）
    2. 把归一化事件发布到 core.message_bus
    3. 提供 OutboundRouter，把出站消息按 platform 路由到对应 Channel
    4. 通过 ChannelManager 统一管理所有 Channel 的生命周期

设计原则：
    - Core 不直接接触任何平台协议（OneBot / Gewechat / ...）
    - 每一种平台 = 一个子包 + 一个 Channel 实现
    - 平台无关层（protocol / router / manager）只依赖 abc / dataclass / asyncio
    - 平台实现层（onebot / wechat）只依赖 aiohttp / httpx / 该平台 SDK
    - 解析 / 序列化 与 网络 I/O 分离（parser / serializer 是纯函数）

公共 API：
    - Platform, SessionScope, MessageSegment
    - NormalizedEvent, OutboundTarget, OutboundPayload
    - Channel
    - OutboundRouter, get_outbound_router
    - ChannelManager, get_channel_manager
"""

from .protocol import (
    Platform,
    SessionScope,
    MessageSegment,
    NormalizedEvent,
    OutboundTarget,
    OutboundPayload,
    Channel,
)
from .router import OutboundRouter, get_outbound_router
from .manager import ChannelManager, get_channel_manager, reset_channel_manager

__all__ = [
    # 基础类型
    "Platform",
    "SessionScope",
    "MessageSegment",
    "NormalizedEvent",
    "OutboundTarget",
    "OutboundPayload",
    # 协议
    "Channel",
    # 单例
    "OutboundRouter",
    "get_outbound_router",
    "ChannelManager",
    "get_channel_manager",
    "reset_channel_manager",
]
