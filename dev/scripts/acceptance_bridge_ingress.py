"""Integrated bridge -> ingress -> main history -> routed send, using synthetic HTTP only."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from acceptance_bridge_contract import BridgeFixture, TEMP_ROOT, TEST_DATA, SID, message

os.environ["ONEBOT_ENABLED"] = "false"
from aiohttp.test_utils import TestClient, TestServer
from adapt.bridge.config import BridgeConfig
from adapt.bridge.contract import SendReceipt
from adapt.ingress_runtime import IngressRuntime, build_app
from adapt.ingress_client import BridgeIngressChannel
from adapt.protocol import OutboundPayload, OutboundTarget, SessionScope
from adapt.router import OutboundRouter
from adapt.session_identity import parse_session_id, session_scope
from memory.message_repository import MessageRepository


async def until(predicate, timeout=8):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.03)
    raise AssertionError("synthetic bridge condition timed out")


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="integration_", dir=TEMP_ROOT)
        self.env = patch.dict(os.environ, {"ABYSS_DATA_DIR": self.tmp.name, "ONEBOT_ENABLED": "false"})
        self.env.start()
        Path(self.tmp.name, ".env.dev").write_text("ONEBOT_ENABLED=false\n", encoding="utf-8")
        self.peer = BridgeFixture()
        self.server = TestServer(self.peer.app)
        await self.server.start_server()
        self.config = BridgeConfig(True, "bridge-1", "wechat", str(self.server.make_url("/")), "synthetic-test-token")
        self.repo = MessageRepository()
        self.runtime = IngressRuntime(repository=self.repo, bridge_config=self.config)
        self.http = TestClient(TestServer(build_app(self.runtime)))
        await self.http.start_server()
        self.proxy = BridgeIngressChannel(self.config)
        self.proxy._base = str(self.http.make_url("")).rstrip("/")
        await self.proxy.start()
        self.local = await self.repo.bridge_session_id("bridge-1", "cache-A", SID)
        self.store_patch = patch("memory.message_repository.get_message_store", return_value=self.repo)
        self.store_patch.start()
        await until(self.ready)

    async def ready(self):
        return self.runtime.bridge.ready or self.runtime.bridge.blocked

    async def asyncTearDown(self):
        await self.proxy.stop()
        await self.http.close()
        await self.server.close()
        self.store_patch.stop()
        self.env.stop()
        logging.shutdown()
        self.tmp.cleanup()

    async def test_received_text_has_platform_prefix_and_persisted_return_route(self):
        self.assertRegex(self.local, r"^wechat_private_[0-9]+$")
        self.assertTrue(self.runtime.bridge.ready, self.runtime.bridge.last_error)
        rows = (await self.repo.read_session_messages(self.local))["message_list"]
        self.assertEqual(len(rows), 1)
        route = await self.repo.session_route(self.local)
        self.assertEqual((route["platform"], route["connection_id"], route["remote_session_id"], route["generation"]),
                         ("wechat", "bridge-1", SID, "cache-A"))
        self.assertEqual(session_scope(self.local), "private")
        unread = await self.repo.get_unread_for(self.local, "conversation")
        self.assertEqual(len(unread), 1)

    async def test_router_sends_to_actual_bridge_target_without_fabricating_message(self):
        router = OutboundRouter()
        await router.register(self.proxy)
        router._persist_outbound = AsyncMock()
        with patch("config.settings.safety_config.SIMULATED_SEND", False):
            receipt = await router.send_to_session(self.local, OutboundPayload.text("synthetic reply"))
        self.assertIsInstance(receipt, SendReceipt)
        self.assertEqual(receipt.status, "queued")
        self.assertIsNone(receipt.message_id)
        self.assertEqual(self.peer.posts[0]["conversation"], SID)
        self.assertEqual(self.peer.posts[0]["expected_generation"], "cache-A")
        router._persist_outbound.assert_not_awaited()
        self.assertEqual(len((await self.repo.read_session_messages(self.local))["message_list"]), 1)

    async def test_unknown_session_and_nontext_do_not_send(self):
        router = OutboundRouter()
        await router.register(self.proxy)
        with self.assertRaises(ValueError):
            await router.send_to_session("wechat_private_unmapped", OutboundPayload.text("do not send"))
        from adapt.protocol import MessageSegment
        with self.assertRaises(ValueError):
            await self.proxy.send(OutboundTarget(SessionScope.PRIVATE, "unused", self.local),
                                  OutboundPayload([MessageSegment("image", {"file": "synthetic.png"})]))
        self.assertEqual(self.peer.posts, [])

    async def test_unknown_direction_is_preserved_but_not_exposed_as_user_input(self):
        self.peer.rows.append(message(2, direction="unknown"))
        self.peer.cursor = 2
        async def quarantined():
            return (await self.runtime.bridge.status()).get("inbox", {}).get("quarantined") == 1
        await until(quarantined)
        self.assertEqual(len((await self.repo.read_session_messages(self.local))["message_list"]), 1)

    async def test_own_row_is_system_and_does_not_increase_user_unread(self):
        self.peer.rows.append(message(2, direction="outbound", is_own=True, user_id="self"))
        self.peer.cursor = 2
        async def own_stored():
            return len((await self.repo.read_session_messages(self.local))["message_list"]) == 2
        await until(own_stored)
        self.assertEqual(await self.repo.count_unread_user_for(self.local, "conversation"), 1)

    async def test_queued_receipt_is_reconciled_without_a_second_post(self):
        receipt = await self.proxy.send(OutboundTarget(SessionScope.PRIVATE, "unused", self.local),
                                        OutboundPayload.text("synthetic reply"))
        self.peer.receipts[receipt.request_id].update(status="actioned", report={"clicked": True})
        async def actioned():
            return self.runtime.bridge.state.send_record(receipt.request_id)["status"] == "actioned"
        await until(actioned)
        self.assertEqual(len(self.peer.posts), 1)

    async def test_lost_core_ack_preserves_receipt_already_committed_by_ingress(self):
        from utils import NetworkError
        original = self.proxy._request
        async def lost_ack(method, path, body=None):
            result = await original(method, path, body)
            if path == "/v1/bridge/send":
                raise NetworkError("synthetic Core acknowledgement loss")
            return result
        with patch.object(self.proxy, "_request", side_effect=lost_ack):
            receipt = await self.proxy.send(OutboundTarget(SessionScope.PRIVATE, "unused", self.local),
                                            OutboundPayload.text("synthetic reply"))
        self.assertEqual(receipt.status, "queued")
        self.assertEqual(len(self.peer.posts), 1)

    async def test_new_generation_changes_local_identity_and_old_target_is_rejected(self):
        self.peer.generation = "cache-B"
        self.peer.cursor = 1
        new_local = await self.repo.bridge_session_id("bridge-1", "cache-B", SID)
        async def new_route():
            return await self.repo.session_route(new_local) is not None
        await until(new_route)
        self.assertNotEqual(self.local, new_local)
        with self.assertRaises(ValueError):
            await self.proxy.send(OutboundTarget(SessionScope.PRIVATE, "unused", self.local),
                                  OutboundPayload.text("old target must not send"))
        self.assertEqual(self.peer.posts, [])

    async def test_bridge_auth_failure_does_not_stop_ingress_or_other_inbound(self):
        self.peer.capabilities = []
        async def blocked():
            return self.runtime.bridge.blocked
        await until(blocked)
        self.assertTrue(self.runtime._healthy())
        await self.runtime.capture({"post_type": "message", "message_type": "private", "user_id": 123,
                                    "message_id": "synthetic-qq", "time": 100,
                                    "message": [{"type": "text", "data": {"text": "QQ still works"}}]})
        async def qq_stored():
            return bool(await self.repo.session_route("private_123"))
        await until(qq_stored)


class IdentityTests(unittest.TestCase):
    def test_prefixed_and_legacy_ids_share_one_scope_parser(self):
        for sid, platform, scope, identity in [("qq_private_123", "qq", "private", "123"),
                                               ("wechat_group_wc_abc", "wechat", "group", "wc_abc"),
                                               ("private_123", None, "private", "123")]:
            parsed = parse_session_id(sid)
            self.assertEqual((parsed.platform, parsed.scope, parsed.identity), (platform, scope, identity))


class ToolReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_tool_result_preserves_session_and_does_not_claim_success(self):
        from brain.base.common_ways.tool_conversation import ToolConversation
        store = SimpleNamespace(get_speech_pressure=AsyncMock(return_value={"characters": 0, "user_replied": True}))
        callback = Mock()
        receipt = SendReceipt("synthetic-request", "queued", "synthetic-send")
        send = AsyncMock(return_value=[receipt])
        with patch("memory.message_repository.get_message_store", return_value=store), \
                patch.object(ToolConversation, "send_segments_with_typing", send):
            result = await ToolConversation.send_message("wechat_private_example", "synthetic text", on_sent=callback)
        self.assertEqual(send.call_args.kwargs["session_id"], "wechat_private_example")
        self.assertIn("已受理", result)
        self.assertNotIn("发送成功", result)
        callback.assert_not_called()

    async def test_uncertain_send_stops_remaining_parts(self):
        from brain.base.common_ways.tool_conversation import ToolConversation
        from adapt.message_segments import text_segment
        router = SimpleNamespace(send_to_session=AsyncMock(return_value=SendReceipt("synthetic-request", "unknown")))
        with patch("brain.base.common_ways.tool_conversation.get_outbound_router", AsyncMock(return_value=router)):
            result = await ToolConversation.send_segments_with_typing(
                SessionScope.PRIVATE, "example", [text_segment("one"), text_segment("two")],
                typing_delay_per_char=0, session_id="wechat_private_example")
        self.assertEqual(len(result), 1)
        self.assertEqual(router.send_to_session.await_count, 1)


if __name__ == "__main__":
    try:
        result = unittest.main(verbosity=2, exit=False).result
    finally:
        logging.shutdown()
        TEST_DATA.cleanup()
    sys.exit(0 if result.wasSuccessful() else 1)
