"""Ingress-owned bridge receiver and sender; the desktop driver remains external."""
from __future__ import annotations

import asyncio
import copy

import aiohttp

from paths import data_root
from utils import NetworkError, setup_logging
from .client import BridgeClient, BridgeHTTPError
from .contract import ContractError
from .state import BridgeState

logger = setup_logging("adapt.bridge")


def state_path():
    return data_root() / "memory" / "chat_history" / "bridge_state.db"


class BridgeRuntime:
    def __init__(self, config, repository, write_lock):
        self.config = config
        self.repo = repository
        self.write_lock = write_lock
        self.state = None
        self.client = None
        self.task = None
        self.ready = False
        self.blocked = False
        self.last_error = ""
        self.peer_status = {}
        self._send_lock = asyncio.Lock()

    async def start(self):
        self.state = await asyncio.to_thread(BridgeState, state_path())
        await asyncio.to_thread(self.state.bind, self.config.connection_id, self.config.platform)
        self.task = asyncio.create_task(self._run(), name="bridge-receive")

    async def stop(self):
        self.ready = False
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _run(self):
        while True:
            try:
                # Locally staged messages remain deliverable even if the bridge is offline.
                await self._deliver()
                async with BridgeClient(self.config.connection_id, self.config.platform,
                                        self.config.base_url, self.config.token, self.state) as client:
                    self.client = client
                    while True:
                        self.peer_status = await client.health()
                        await client.poll_once()
                        await self._deliver()
                        self.ready = True
                        self.last_error = ""
                        await self._reconcile()
                        await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except (ContractError, BridgeHTTPError) as exc:
                self.last_error = str(exc)
                self.ready = False
                if isinstance(exc, ContractError) or exc.status in (401, 403):
                    self.blocked = True
                    logger.error("桥接契约或鉴权失败；请修正配置后手动重启接入服务")
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                self.last_error = "桥接服务不可达；等待恢复，不重发任何消息"
                self.ready = False
            except Exception:
                self.last_error = "桥接处理失败；请查接入日志并手动重启"
                self.ready = False
                self.blocked = True
                logger.exception("桥接处理已停止，保留待处理记录")
                return
            finally:
                self.client = None
            await asyncio.sleep(2.0)

    async def _deliver(self):
        while True:
            rows = await asyncio.to_thread(self.state.inbox, self.config.connection_id)
            if not rows:
                return
            for row in rows:
                payload = copy.deepcopy(row["payload"])
                sid = await self.repo.bridge_session_id(
                    self.config.connection_id, row["generation"], row["session_id"])
                payload["session_info"].update(
                    session_id=sid, platform=self.config.platform,
                    connection_id=self.config.connection_id,
                    remote_session_id=row["session_id"], generation=row["generation"],
                )
                async with self.write_lock:
                    result = await self.repo.store_message(payload)
                    if not isinstance(result, dict) or result.get("status") != "success":
                        raise RuntimeError("bridge message storage failed")
                await asyncio.to_thread(self.state.acknowledge, self.config.connection_id,
                                        row["generation"], row["session_id"], row["message_id"])

    async def _reconcile(self):
        rows = await asyncio.to_thread(self.state.pending_sends, self.config.connection_id)
        for row in rows:
            # A new database/account must not lend its receipt namespace to an old send.
            if row["generation"] != self.peer_status.get("generation"):
                await asyncio.to_thread(self.state.mark_unknown, self.config.connection_id, row["request_id"])
                continue
            await self.client.reconcile_send(row["request_id"])

    async def submit(self, request_id: str):
        async with self._send_lock:
            if not self.ready or self.client is None:
                raise NetworkError("桥接尚未就绪，请检查平台接入状态；不要重复提交发送")
            row = await asyncio.to_thread(self.state.send_record, request_id)
            if row["connection_id"] != self.config.connection_id:
                raise ValueError("请求不属于当前连接")
            client = self.client
            try:
                return await client.submit_once(request_id)
            except (aiohttp.ClientError, asyncio.TimeoutError, BridgeHTTPError, ContractError):
                # The durable journal carries the actual outcome; never convert this to 'sent'.
                return await asyncio.to_thread(client._local_receipt, request_id)

    async def status(self):
        result = {"enabled": True, "connection_id": self.config.connection_id,
                  "platform": self.config.platform, "ready": self.ready, "blocked": self.blocked,
                  "last_error": self.last_error,
                  "mode": self.peer_status.get("mode"), "focus": self.peer_status.get("focus", []),
                  "pinned_missing": self.peer_status.get("pinned_missing", []),
                  "unclaimed_sends": self.peer_status.get("unclaimed_sends", 0)}
        if self.state:
            result.update(await asyncio.to_thread(self.state.summary, self.config.connection_id))
        return result
