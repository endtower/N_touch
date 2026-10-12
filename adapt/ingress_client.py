"""Core 中的 OneBot 代理；停止代理不会停止独立接入进程。"""
from __future__ import annotations

import asyncio
import aiohttp

from adapt.protocol import Platform
from ingress_config import api_port, service_token
from paths import data_root
from utils import NetworkError


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
