"""Asynchronous bridge client. No automatic sends, resend loops or desktop operations."""
from __future__ import annotations

import asyncio
from urllib.parse import quote, urlsplit

import aiohttp

from .contract import (ContractError, SendReceipt, nonempty, parse_poll,
                       validate_health, valid_connection_id)
from .state import BridgeState


class BridgeHTTPError(RuntimeError):
    def __init__(self, status: int):
        # Do not include peer response bodies, URLs, tokens or message contents in errors.
        self.status = status
        super().__init__(f"bridge HTTP {status}")


class BridgeClient:
    def __init__(self, connection_id: str, platform: str, base_url: str,
                 token: str, state: BridgeState, timeout: float = 10):
        self.connection_id = valid_connection_id(connection_id)
        self.platform = nonempty(platform, "platform")
        url = urlsplit(base_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password \
                or url.query or url.fragment:
            raise ContractError("base_url must be HTTP(S), without credentials/query/fragment")
        self._base = base_url.rstrip("/")
        self._token = nonempty(token, "token")
        self._timeout = timeout
        self.state = state
        self.state.bind(connection_id, platform)
        self._http = None
        self._receive_lock = asyncio.Lock()

    async def __aenter__(self):
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._timeout))
        try:
            await self.health()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc):
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def _request(self, method: str, path: str, **kwargs):
        if self._http is None:
            raise RuntimeError("bridge client is not open")
        async with self._http.request(method, self._base + path,
                                      headers={"Authorization": f"Bearer {self._token}"},
                                      allow_redirects=False, **kwargs) as response:
            if response.status not in (200, 202):
                raise BridgeHTTPError(response.status)
            return await response.json()

    async def health(self):
        return validate_health(await self._request("GET", "/health"), self.platform)

    async def poll_once(self):
        async with self._receive_lock:
            expected = await asyncio.to_thread(self.state.checkpoint, self.connection_id)
            generation, cursor = expected
            body = await self._request("GET", "/poll", params={"after": cursor})
            if isinstance(body, dict) and generation and body.get("generation") != generation:
                # Discard the old-cursor page, even if it happened to contain records.
                new_generation = body.get("generation")
                body = await self._request("GET", "/poll", params={"after": 0})
                if not isinstance(body, dict) or body.get("generation") != new_generation:
                    raise ContractError("generation changed during reset; no progress committed")
                cursor = 0
            batch = parse_poll(body, cursor, self.platform)
            await asyncio.to_thread(self.state.commit_poll, self.connection_id, batch, expected)
            return batch

    def _own_send(self, request_id: str) -> dict:
        row = self.state.send_record(request_id)
        if row["connection_id"] != self.connection_id:
            raise ContractError("request belongs to another connection")
        return row

    def _local_receipt(self, request_id: str) -> SendReceipt:
        row = self._own_send(request_id)
        return SendReceipt(request_id, row["status"], row["send_id"], row["message_id"])

    async def submit_once(self, request_id: str) -> SendReceipt:
        row = await asyncio.to_thread(self._own_send, request_id)
        if row["status"] != "prepared":
            # Interrupted POSTs are resolved by GET, never a second POST.
            return await self.reconcile_send(request_id)
        health = await self.health()
        if health["generation"] != row["generation"]:
            raise ContractError("generation changed since the send was prepared")
        if not await asyncio.to_thread(self.state.claim_send, self.connection_id, request_id):
            return await self.reconcile_send(request_id)
        try:
            body = await self._request("POST", "/send", json={
                "request_id": request_id, "conversation": row["conversation"], "text": row["text"],
                "expected_generation": row["generation"],
            })
            receipt = SendReceipt.parse(body, request_id)
            await asyncio.to_thread(self.state.record_receipt, self.connection_id, receipt)
            return await asyncio.to_thread(self._local_receipt, request_id)
        except BaseException:
            # Includes task cancellation and a lost response after the peer accepted the POST.
            await asyncio.shield(asyncio.to_thread(self.state.mark_unknown,
                                                  self.connection_id, request_id))
            raise

    async def reconcile_send(self, request_id: str) -> SendReceipt:
        row = await asyncio.to_thread(self._own_send, request_id)
        if row["status"] == "prepared":
            return SendReceipt(request_id, "prepared")
        if row["status"] in ("observed", "rejected", "failed"):
            return SendReceipt(request_id, row["status"], row["send_id"], row["message_id"])
        try:
            body = await self._request("GET", "/send/by-request/" + quote(request_id, safe=""))
            receipt = SendReceipt.parse(body, request_id)
            await asyncio.to_thread(self.state.record_receipt, self.connection_id, receipt)
            return await asyncio.to_thread(self._local_receipt, request_id)
        except BridgeHTTPError as exc:
            if exc.status not in (404, 410):
                raise
            await asyncio.to_thread(self.state.mark_unknown, self.connection_id, request_id)
            return SendReceipt(request_id, "unknown", row["send_id"])
