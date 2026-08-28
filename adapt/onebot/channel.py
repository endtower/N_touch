#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
adapt.onebot.channel
====================

OneBotChannel：OneBot v11 反向 WebSocket 服务端实现。

数据流：
    NapCat (WS client)
        |
        |  ws://host:port/onebot/v11/ws
        v
    OneBotChannel._on_connection (aiohttp.web)
        |
        |  json.loads(msg.data)
        v
    OneBotChannel._dispatch(raw)
        |
        +--[post_type in (message, message_sent)]--> parse_onebot_event --> bus.publish
        |
        +--[echo in payload]------------------------> future.set_result
        |
        +--[meta_event]-----------------------------> 日志，不发到 bus

发送：
    OneBotChannel.send(target, payload)
        |
        |  构造 {action, params, echo} -> send_json
        v
    NapCat 处理 -> 回 {retcode, data, echo} -> 唤醒 future -> return message_id
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Dict, Optional

from aiohttp import WSCloseCode, WSMsgType, web

from utils import setup_logging, NetworkError

from adapt.protocol import (
    NormalizedEvent,
    OutboundPayload,
    OutboundTarget,
    Platform,
    SessionScope,
)

from .config import OneBotChannelConfig
from . import constants as C
from .parser import parse_onebot_event
from .recorder import RawEventRecorder
from .serializer import serialize_to_onebot
from .media_downloader import MediaDownloader, build_media_config_from_adapt


logger = setup_logging("adapt.onebot")


class OneBotChannel:
    """OneBot v11 反向 WebSocket 服务端 Channel。"""

    # 协议要求的固定字段
    name: str
    platform: Platform = Platform.QQ

    def __init__(self, config: OneBotChannelConfig):
        self.config = config
        self.name = config.name

        # 网络层
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        # 当前活跃连接（NapCat 通常是 1 个）
        self._ws: Optional[web.WebSocketResponse] = None
        self._connected_event = asyncio.Event()

        # API 调用的 echo -> future
        self._send_futures: Dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()

        # 接收：MessageBus（外部注入）
        self._message_bus: Optional[Any] = None   # 避免 import 循环

        # 原始事件录制器（可选）
        self._recorder: Optional[RawEventRecorder] = None
        if self.config.record_raw_events:
            self._recorder = RawEventRecorder(
                base_dir=self.config.raw_events_dir,
                max_per_day=self.config.max_raw_events_per_day,
            )
            logger.info(
                f"[OneBot:{self.name}] raw event recording enabled → "
                f"{self.config.raw_events_dir}/ (max {self.config.max_raw_events_per_day}/day)"
            )

        # 媒体下载器（图片 / 表情包，自动从 QQ CDN 下载到本地）
        self._media_downloader: Optional[MediaDownloader] = None
        if self.config.media_download_enabled:
            media_cfg = build_media_config_from_adapt(
                media_enabled=self.config.media_download_enabled,
                media_base_dir=self.config.media_base_dir,
                media_download_timeout=self.config.media_download_timeout,
                media_download_max_size_mb=self.config.media_download_max_size_mb,
                media_download_retries=self.config.media_download_retries,
            )
            self._media_downloader = MediaDownloader(media_cfg)
            logger.info(
                f"[OneBot:{self.name}] media downloader enabled → "
                f"{self.config.media_base_dir}/{{stickers,images}}/<date>/"
            )

    # --------------------------------------------------------
    # 注入
    # --------------------------------------------------------

    def bind_message_bus(self, message_bus: Any) -> None:
        """由 ChannelManager 在 start 前注入 message_bus。"""
        self._message_bus = message_bus

    # --------------------------------------------------------
    # 状态查询（供调用方在 send 之前检查）
    # --------------------------------------------------------

    def is_connected(self) -> bool:
        """当前是否有活跃的 WebSocket 连接。"""
        return (
            self._connected_event.is_set()
            and self._ws is not None
            and not self._ws.closed
        )

    # --------------------------------------------------------
    # 生命周期
    # --------------------------------------------------------

    async def start(self) -> None:
        if self._site is not None:
            return
        self._app = web.Application()
        self._app.router.add_get(self.config.path, self._handle_connection)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.config.host, self.config.port)
        await self._site.start()
        logger.info(
            f"[OneBot:{self.name}] WS server listening on "
            f"ws://{self.config.host}:{self.config.port}{self.config.path}"
        )

    async def stop(self) -> None:
        # 关闭媒体下载器
        if self._media_downloader is not None:
            try:
                await self._media_downloader.close()
            except Exception:
                logger.debug("[OneBot] media_downloader.close 失败", exc_info=True)

        # 唤醒所有挂起的 send future
        async with self._send_lock:
            for fut in list(self._send_futures.values()):
                if not fut.done():
                    fut.set_exception(NetworkError("OneBot channel closed"))
            self._send_futures.clear()

        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close(code=WSCloseCode.GOING_AWAY)
            except Exception:
                logger.debug("[OneBot] ws.close 失败", exc_info=True)

        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                logger.debug("[OneBot] site.stop 失败", exc_info=True)
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                logger.debug("[OneBot] runner.cleanup 失败", exc_info=True)
            self._runner = None
            self._app = None

        self._connected_event.clear()
        logger.info(f"[OneBot:{self.name}] stopped")

    # --------------------------------------------------------
    # 入站：WS 接入
    # --------------------------------------------------------

    async def _handle_connection(self, request: web.Request) -> web.WebSocketResponse:
        # 1) 鉴权
        if not self._check_auth(request):
            logger.warning(f"[OneBot] 鉴权失败 from {request.remote}")
            return web.Response(status=401, text="unauthorized")

        # 2) 升级
        ws = web.WebSocketResponse(heartbeat=30.0)
        try:
            await ws.prepare(request)
        except Exception:
            logger.exception("[OneBot] ws.prepare 失败")
            return ws

        # 3) 替换活跃连接
        old_ws = self._ws
        self._ws = ws
        self._connected_event.set()
        logger.info(f"[OneBot:{self.name}] NapCat connected from {request.remote}")
        if old_ws is not None and not old_ws.closed:
            try:
                await old_ws.close(code=WSCloseCode.GOING_AWAY)
            except Exception:
                pass

        # 4) 读循环
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_text(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    logger.warning("[OneBot] 收到 binary 帧，忽略")
                elif msg.type == WSMsgType.ERROR:
                    logger.warning(f"[OneBot] WS error: {ws.exception()}")
                    break
        except Exception:
            logger.exception("[OneBot] 读循环异常")
        finally:
            if self._ws is ws:
                self._ws = None
                self._connected_event.clear()
            # 唤醒所有挂起的 send future（让它们别永远 await send_timeout）
            lost = 0
            async with self._send_lock:
                for fut in list(self._send_futures.values()):
                    if not fut.done():
                        fut.set_exception(NetworkError(
                            f"[OneBot:{self.name}] send aborted: NapCat disconnected"
                        ))
                        lost += 1
                self._send_futures.clear()
            if lost:
                logger.error(
                    f"[OneBot:{self.name}] disconnected with {lost} pending send(s), all aborted"
                )
            logger.warning(f"[OneBot:{self.name}] NapCat disconnected")

        return ws

    def _check_auth(self, request: web.Request) -> bool:
        """支持 Bearer header + ?access_token= query 两种方式（NapCat 两种都发）。"""
        token = self.config.access_token
        if not token:
            return True

        # Header
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            if auth[len("Bearer "):].strip() == token:
                return True
        # Query
        if request.query.get("access_token") == token:
            return True
        return False

    # --------------------------------------------------------
    # 入站：分发
    # --------------------------------------------------------

    async def _on_text(self, raw_text: str) -> None:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning(f"[OneBot] 无效 JSON: {raw_text[:200]}")
            return

        if not isinstance(data, dict):
            return

        # API 响应（有 echo 字段）
        if "echo" in data:
            echo = str(data["echo"])
            async with self._send_lock:
                fut = self._send_futures.pop(echo, None)
            if fut is not None and not fut.done():
                fut.set_result(data)
            if self._recorder is not None:
                self._recorder.record(
                    category="api_response",
                    raw=data,
                    parsed=None,
                    metadata={"echo": echo, "retcode": data.get("retcode")},
                )
            return

        # meta_event：仅记录
        if data.get("post_type") == C.OB_POST_META:
            meta_type = data.get("meta_event_type")
            if meta_type == C.OB_META_LIFECYCLE:
                logger.info(f"[OneBot] lifecycle: {data.get('sub_type')}")
            elif meta_type == C.OB_META_HEARTBEAT:
                # 心跳过于频繁，DEBUG 级别
                logger.debug("[OneBot] heartbeat received")
            if self._recorder is not None:
                self._recorder.record(
                    category="meta_event",
                    raw=data,
                    parsed=None,
                    metadata={
                        "meta_event_type": meta_type,
                        "sub_type": data.get("sub_type"),
                        "self_id": data.get("self_id"),
                    },
                )
            return

        # message / message_sent
        if data.get("post_type") in (C.OB_POST_MESSAGE, C.OB_POST_MESSAGE_SENT):
            ev = parse_onebot_event(data)
            # 媒体下载（图片 / 表情包 → 本地）
            if ev is not None and self._media_downloader is not None:
                try:
                    await self._media_downloader.enrich(ev)
                except Exception:
                    logger.exception("[OneBot] media enrich 失败，继续处理消息")
            if self._recorder is not None:
                self._recorder.record(
                    category="message",
                    raw=data,
                    parsed=ev.to_system_message_dict() if ev is not None else None,
                    metadata={
                        "self_id": data.get("self_id"),
                        "message_id": str(data.get("message_id") or ""),
                        "session_id": ev.session_id if ev is not None else None,
                        "scope": ev.scope.value if ev is not None else None,
                        "parse_ok": ev is not None,
                    },
                )
            if ev is None:
                return
            await self._dispatch_to_bus(ev)
            return

        # 其它类型（notice / request）：当前忽略
        if self._recorder is not None:
            self._recorder.record(
                category=data.get("post_type", "unknown"),
                raw=data,
                parsed=None,
                metadata={
                    "self_id": data.get("self_id"),
                    "notice_type": data.get("notice_type"),
                    "request_type": data.get("request_type"),
                },
            )

    async def _dispatch_to_bus(self, ev: NormalizedEvent) -> None:
        if self._message_bus is None:
            logger.error("[OneBot] message_bus 未绑定，事件将被丢弃")
            return
        try:
            payload = ev.to_system_message_dict()
            await self._message_bus.publish("user_message", payload)
            logger.debug(
                f"[OneBot] published: {ev.platform.value} "
                f"{ev.scope.value} user={ev.user_id} text={ev.content_text[:60]!r}"
            )
        except Exception:
            logger.exception("[OneBot] publish 到 message_bus 失败")

    # --------------------------------------------------------
    # 出站
    # --------------------------------------------------------

    async def send(self, target: OutboundTarget, payload: OutboundPayload) -> str:
        # fail-fast：连接不在线就立即抛错，不缓冲不等
        if not self.is_connected():
            raise NetworkError(
                f"[OneBot:{self.name}] send aborted: NapCat not connected (fail-fast)"
            )

        ws = self._ws
        assert ws is not None and not ws.closed   # is_connected 已保证

        # 构造 action
        segments = payload.segments
        message = serialize_to_onebot(segments)

        if target.scope == SessionScope.PRIVATE:
            action = C.OB_API_SEND_PRIVATE_MSG
            params_key = "user_id"
        elif target.scope == SessionScope.GROUP:
            action = C.OB_API_SEND_GROUP_MSG
            params_key = "group_id"
        else:
            raise ValueError(f"[OneBot] 不支持的作用域: {target.scope}")

        params: Dict[str, Any] = {params_key: target.target_id, "message": message}

        echo = f"ob-{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        async with self._send_lock:
            self._send_futures[echo] = fut

        body = {"action": action, "params": params, "echo": echo}
        try:
            await ws.send_json(body)
        except Exception as e:
            async with self._send_lock:
                self._send_futures.pop(echo, None)
            self._connected_event.clear()
            raise NetworkError(
                f"[OneBot] send aborted: connection lost during send: {e}"
            ) from e

        try:
            resp = await asyncio.wait_for(fut, timeout=self.config.send_timeout)
        except asyncio.TimeoutError:
            async with self._send_lock:
                self._send_futures.pop(echo, None)
            raise NetworkError(f"[OneBot] send 超时（>{self.config.send_timeout}s）")

        retcode = resp.get("retcode", -1)
        if retcode != C.OB_RETCODE_OK:
            msg = resp.get("msg") or resp.get("wording") or "unknown"
            raise NetworkError(f"[OneBot] send 失败 retcode={retcode}: {msg}")

        data = resp.get("data") or {}
        message_id = str(data.get("message_id", ""))
        return message_id
