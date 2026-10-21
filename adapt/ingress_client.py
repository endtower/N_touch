"""Core 中的 OneBot 代理；停止代理不会停止独立接入进程。"""
from __future__ import annotations

import asyncio
import uuid
import aiohttp

from adapt.protocol import Platform
from ingress_config import api_port, service_token
from paths import data_root
from utils import NetworkError
from adapt.bridge.contract import SendReceipt


class IngressChannel:
    name = "onebot"
    platform = Platform.QQ
    persists_outbound = True

    def __init__(self):
        self._client = None
        self._base = f"http://127.0.0.1:{api_port()}"

    async def start(self):
        self._client = aiohttp.ClientSession(
            headers={"X-Manager-Token": service_token()},
            timeout=aiohttp.ClientTimeout(total=25),
        )
        try:
            state = await self._request("GET", "/v1/status")
            if state.get("service") != "abyss-ingress" or state.get("instance") != str(data_root()):
                raise NetworkError("接入服务身份或数据目录不匹配")
            if not state.get("ready") or not state.get("onebot_enabled"):
                raise NetworkError("消息接入服务未就绪或 OneBot 已禁用")
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _request(self, method, path, body=None):
        if self._client is None:
            raise NetworkError("接入服务代理未启动")
        try:
            async with self._client.request(method, self._base + path, json=body) as response:
                result = await response.json()
                if response.status != 200:
                    raise NetworkError(result.get("error", f"接入服务 HTTP {response.status}"))
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise NetworkError(f"消息接入服务不可用: {exc}") from exc

    async def send(self, target, payload):
        result = await self._request("POST", "/v1/send", {
            "scope": target.scope.value, "target_id": target.target_id,
            "segments": [segment.as_dict() for segment in payload.segments],
        })
        return result["message_id"]

    async def call_api(self, action, params):
        return await self._request("POST", "/v1/onebot-api", {"action": action, "params": params})

    async def get_msg(self, message_id):
        response = await self.call_api("get_msg", {"message_id": message_id})
        if not isinstance(response.get("data"), dict) or not response["data"]:
            raise NetworkError("OneBot get_msg 返回空数据")
        return response["data"]


class BridgeIngressChannel(IngressChannel):
    """Core proxy: requests are journalled locally before invoking ingress's single POST."""
    name = "bridge"
    platform = Platform.WECHAT
    persists_outbound = True

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.state = None

    async def start(self):
        from adapt.bridge.state import BridgeState
        from adapt.bridge.runtime import state_path
        self._client = aiohttp.ClientSession(
            headers={"X-Manager-Token": service_token()},
            timeout=aiohttp.ClientTimeout(total=25),
        )
        try:
            state = await self._request("GET", "/v1/status")
            if state.get("service") != "abyss-ingress" or state.get("instance") != str(data_root()):
                raise NetworkError("接入服务身份或数据目录不匹配")
            bridge = state.get("bridge", {})
            if not state.get("ready") or not bridge.get("enabled") \
                    or bridge.get("connection_id") != self.config.connection_id:
                raise NetworkError("桥接配置与接入服务不一致；请先重启接入服务，再重启 Core")
            self.state = await asyncio.to_thread(BridgeState, state_path())
        except BaseException:
            await self.stop()
            raise

    async def send(self, target, payload):
        from memory.message_repository import get_message_store
        if not payload.segments or any(segment.type != "text" for segment in payload.segments):
            raise ValueError("通用桥接第一阶段仅支持纯文字")
        if not target.session_id:
            raise ValueError("桥接发送必须提供主程序会话身份")
        route = await get_message_store().session_route(target.session_id)
        if not route or route["platform"] != self.platform.value \
                or route["connection_id"] != self.config.connection_id:
            raise ValueError("桥接目标来源不匹配")
        generation, _ = await asyncio.to_thread(self.state.checkpoint, self.config.connection_id)
        if generation != route["generation"]:
            raise ValueError("这是旧缓存或旧账号的会话，请从当前会话列表重新选择")
        row = await asyncio.to_thread(self.state.prepare_send, self.config.connection_id,
                                      uuid.uuid4().hex, route["remote_session_id"],
                                      "".join(segment.to_text() for segment in payload.segments))
        try:
            result = await self._request("POST", "/v1/bridge/send", {"request_id": row["request_id"]})
            if result.get("request_id") != row["request_id"]:
                raise NetworkError("桥接回执身份不匹配")
        except NetworkError:
            current = await asyncio.to_thread(self.state.send_record, row["request_id"])
            if current["status"] in ("prepared", "submitting"):
                await asyncio.to_thread(self.state.mark_unknown, self.config.connection_id, row["request_id"])
        saved = await asyncio.to_thread(self.state.send_record, row["request_id"])
        return SendReceipt(saved["request_id"], saved["status"], saved["send_id"], saved["message_id"])
