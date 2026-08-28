#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
adapt.manager
=============

ChannelManager：管理所有已注册的 Channel 的生命周期。

被 core.system_core 调用：

    async with SystemCore() as core:
        ...
        await core.start()  # 内部会 await channel_manager.start_all()

设计要点：
    - 全局单例（与 OutboundRouter 解耦，允许多 Manager 场景的测试）
    - 启动时把 MessageBus 绑定给每个 Channel
    - 启动时把每个 Channel 注册到 OutboundRouter
    - 任何一步失败，已经启动的 Channel 会被优雅地回滚关闭
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from utils import setup_logging

from .protocol import Channel
from .router import get_outbound_router

# 推迟 import 避免循环依赖
# from core.message_bus import get_message_bus  # 在 start_all() 内再 import


logger = setup_logging("adapt.manager")


class ChannelManager:
    """Channel 生命周期管理器。"""

    def __init__(self) -> None:
        self._channels: Dict[str, Channel] = {}     # name -> Channel
        self._order: List[str] = []                  # 注册顺序，便于按序启停
        self._started: List[str] = []                # 已成功 start 的 channel name
        self._lock = asyncio.Lock()

    # --------------------------------------------------------
    # 注册
    # --------------------------------------------------------

    async def add(self, channel: Channel) -> None:
        """注册一个 channel。重复注册同名会被忽略并 warning。"""
        async with self._lock:
            if channel.name in self._channels:
                logger.warning(
                    f"[ChannelManager] channel {channel.name!r} 已存在，跳过注册"
                )
                return
            self._channels[channel.name] = channel
            self._order.append(channel.name)
        logger.info(
            f"[ChannelManager] 注册 channel: {channel.name!r} (platform={channel.platform.value})"
        )

    def get(self, name: str) -> Optional[Channel]:
        return self._channels.get(name)

    def all(self) -> List[Channel]:
        return [self._channels[n] for n in self._order]

    # --------------------------------------------------------
    # 启停
    # --------------------------------------------------------

    async def start_all(self) -> None:
        """启动所有 channel。

        启动顺序：
            1. 确保 MessageBus 已 start
            2. 把 message_bus 注入到每个 channel
            3. 启动 channel
            4. 把 channel 注册到 OutboundRouter
        """
        # 局部 import 避免循环依赖
        from core.message_bus import get_message_bus

        bus = await get_message_bus()
        # bus 必须已经 start（system_core.start() 先调 bus.start 再调这里）
        # 保险：等待 ready
        # 注意：MessageBus 没有暴露 started 状态，依赖 system_core 的调用顺序

        router = await get_outbound_router()

        for name in list(self._order):
            ch = self._channels[name]
            try:
                # 绑定 message_bus（如果 channel 支持）
                if hasattr(ch, "bind_message_bus"):
                    ch.bind_message_bus(bus)
                await ch.start()
                await router.register(ch)
                self._started.append(name)
                logger.info(f"[ChannelManager] 已启动: {name!r}")
            except Exception:
                logger.exception(f"[ChannelManager] 启动 channel {name!r} 失败，将回滚已启动的")
                await self._stop_started()
                raise

    async def stop_all(self) -> None:
        await self._stop_started()

    async def _stop_started(self) -> None:
        # 逆序关闭
        router = await get_outbound_router()
        for name in reversed(self._started):
            ch = self._channels.get(name)
            if ch is None:
                continue
            try:
                await ch.stop()
                logger.info(f"[ChannelManager] 已停止: {name!r}")
            except Exception:
                logger.exception(f"[ChannelManager] 停止 channel {name!r} 失败")
            try:
                await router.unregister(ch.platform)
            except Exception:
                logger.exception(f"[ChannelManager] 注销 channel {name!r} 失败")
        self._started.clear()


# ============================================================
# 全局单例
# ============================================================

_manager_instance: Optional[ChannelManager] = None
_manager_lock = asyncio.Lock()


async def get_channel_manager() -> ChannelManager:
    """获取 ChannelManager 单例。"""
    global _manager_instance
    async with _manager_lock:
        if _manager_instance is None:
            _manager_instance = ChannelManager()
    return _manager_instance


async def reset_channel_manager() -> None:
    """重置 ChannelManager 单例，确保下次启动是干净状态"""
    global _manager_instance
    async with _manager_lock:
        _manager_instance = None
