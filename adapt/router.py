#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
adapt.router
============

出站消息路由（OutboundRouter）。

调用方（ChatAgent / AttentionCore / 任何需要"发消息"的模块）只调：

    await outbound_router.send(
        platform="qq",
        target=OutboundTarget(scope=SessionScope.GROUP, target_id="198431299"),
        payload=OutboundPayload.text("hello"),
    )

OutboundRouter 内部维护 {platform: Channel} 映射，找到对应 channel
后调 channel.send(target, payload)。

设计要点：
    - 全局单例
    - 平台不存在时给出明确错误
    - 线程/协程安全（asyncio 单线程模型下不需要锁）
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional

from utils import setup_logging, NetworkError, async_handle_errors

from .protocol import (
    Channel,
    MessageSegment,
    OutboundPayload,
    OutboundTarget,
    Platform,
)


logger = setup_logging("adapt.router")


class OutboundRouter:
    """出站消息路由。"""

    def __init__(self) -> None:
        self._channels: Dict[Platform, Channel] = {}
        self._lock = asyncio.Lock()

    # --------------------------------------------------------
    # 注册 / 注销
    # --------------------------------------------------------

    async def register(self, channel: Channel) -> None:
        """注册一个 channel。

        同平台后注册的会覆盖先注册的（便于热替换/重连场景）。
        """
        async with self._lock:
            old = self._channels.get(channel.platform)
            self._channels[channel.platform] = channel
        if old is not None and old is not channel:
            logger.warning(
                f"[OutboundRouter] 平台 {channel.platform.value} 已有 channel "
                f"{old.name!r}，将被 {channel.name!r} 覆盖"
            )
        logger.info(f"[OutboundRouter] 注册 channel: {channel.name!r} -> {channel.platform.value}")

    async def unregister(self, platform: Platform) -> Optional[Channel]:
        async with self._lock:
            return self._channels.pop(platform, None)

    def get(self, platform: str | Platform) -> Optional[Channel]:
        p = platform if isinstance(platform, Platform) else Platform.parse(platform)
        return self._channels.get(p)

    def platforms(self) -> list[Platform]:
        return list(self._channels.keys())

    # --------------------------------------------------------
    # 发送
    # --------------------------------------------------------

    @async_handle_errors(error_types=(NetworkError,))
    async def send(
        self,
        platform: str | Platform,
        target: OutboundTarget,
        payload: OutboundPayload,
    ) -> str:
        """发送一条消息，返回平台消息 ID。

        管线（精简版）:
        1. SIMULATED_SEND=True  → 推到 web 通道 (无 web 客户端则跳过整个流程)
        2. SIMULATED_SEND=False → 推到 OneBot/NapCat
        3. 落库 (无论哪种模式都做, OneBot v11 段格式与 NapCat 入站一致)
        """
        p = platform if isinstance(platform, Platform) else Platform.parse(platform)
        ch = self._channels.get(p)
        if ch is None:
            raise NetworkError(
                f"未找到平台 {p.value!r} 的 channel（已注册: "
                f"{[x.value for x in self._channels.keys()]}）"
            )
        if not payload.segments and not payload.raw_text:
            raise ValueError("OutboundPayload 不能为空（segments 和 raw_text 均为空）")

        # 兼容性：如果只给了 raw_text，构造一个 text 段
        if not payload.segments and payload.raw_text is not None:
            from .message_segments import text_segment
            payload.segments = [text_segment(payload.raw_text)]

        segments = payload.segments

        # === 分支点: 模拟模式 vs 真实模式 ===
        from config.settings import safety_config
        if safety_config.SIMULATED_SEND:
            message_id = await self._send_via_web(target, payload)
            if not message_id:
                # 模拟模式下 web 通道无客户端 = 无实际投递对象，
                # 但消息仍须落库（设计决策：跳过实际发送，仅落库 + 推 MessageBus；
                # 此处直接 return 会导致 AI 出站消息从历史记录中丢失）
                logger.info(
                    f"[SIMULATED_SEND] web 通道无客户端, 跳过投递仅落库: "
                    f"{target.scope.value}:{target.target_id}"
                )
                import uuid
                message_id = uuid.uuid4().hex
        else:
            logger.info(
                f"[OutboundRouter] -> {p.value} {target.scope.value}:{target.target_id} "
                f"({len(segments)} segments)"
            )
            message_id = await ch.send(target, payload)
            if not message_id:
                return message_id

        # 落库 (两模式通用, OneBot v11 段格式)
        # 独立接入服务已在发送回执后落库；模拟发送仍由本地记录。
        if safety_config.SIMULATED_SEND or not getattr(ch, "persists_outbound", False):
            await self._persist_outbound(target, segments, message_id)
        return message_id

    async def _send_via_web(
        self,
        target: OutboundTarget,
        payload: OutboundPayload,
    ) -> str:
        """模拟模式专用: 把消息推到 web 通道 (WebChannel.send 内部会广播 llm_message).

        返回值:
          - 非空 message_id: web 通道存在且至少一个客户端接收成功 (uuid4().hex)
          - 空串 "":         web 通道未注册 / 没有连接的浏览器 (由 WebChannel.send 返回)
        """
        from adapt.manager import get_channel_manager
        mgr = await get_channel_manager()
        web_ch = mgr.get("web")
        if web_ch is None:
            return ""
        return await web_ch.send(target, payload)

    async def _persist_outbound(
        self,
        target: OutboundTarget,
        segments: List[MessageSegment],
        message_id: str,
    ) -> None:
        """落库 (写入 chat_history 文件), OneBot v11 段格式与 NapCat 入站完全一致."""
        from config.role_settings import role_config
        session_id = f"{target.scope.value}_{target.target_id}"
        content_text = "".join(seg.to_text() for seg in segments)
        content_array = [seg.as_dict() for seg in segments]

        message_data = {
            "session_info": {
                "session_id": session_id,
                "session_name": "",
                "remark": "",
            },
            "message_list": [{
                "message_id": message_id,
                "user_id": "system",
                "user_name": role_config.ASSISTANT_NAME,
                "timestamp": int(time.time()),
                "content_text": content_text,
                "content": content_array,
            }]
        }

        from memory.message_handler import get_message_handler
        message_handler = get_message_handler()
        await message_handler.process_message(message_data)

    async def send_text(
        self,
        platform: str | Platform,
        target: OutboundTarget,
        text: str,
    ) -> str:
        """便利方法：发送纯文本。"""
        return await self.send(platform, target, OutboundPayload.text(text))


# ============================================================
# 全局单例
# ============================================================

_router_instance: Optional[OutboundRouter] = None
_router_lock = asyncio.Lock()


async def get_outbound_router() -> OutboundRouter:
    """获取 OutboundRouter 单例（必须在已运行的 asyncio loop 内调用）。"""
    global _router_instance
    async with _router_lock:
        if _router_instance is None:
            _router_instance = OutboundRouter()
    return _router_instance
