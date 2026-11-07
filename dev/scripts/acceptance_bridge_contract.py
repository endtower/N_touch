"""Bridge v0 acceptance: real HTTP and SQLite, synthetic messages, no WeChat/QQ/LLM access."""
from __future__ import annotations

import asyncio
import copy
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from workspace_layout import test_root, CACHE, BUILDS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP_ROOT = (test_root() / "bridge-acceptance").resolve()
if not TEMP_ROOT.is_relative_to(ROOT.resolve()):
    raise RuntimeError("acceptance temp directory escaped workspace")
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DATA = tempfile.TemporaryDirectory(prefix="bootstrap_", dir=TEMP_ROOT)
os.environ["ABYSS_DATA_DIR"] = TEST_DATA.name
Path(TEST_DATA.name, ".env.dev").write_text("", encoding="utf-8")

from aiohttp import web
from aiohttp.test_utils import TestServer
from adapt.bridge.client import BridgeClient, BridgeHTTPError
from adapt.bridge.contract import (ContractError, REQUIRED_CAPABILITIES, SendReceipt,
                                   parse_poll)
from adapt.bridge.state import BridgeState

# Close log handles before TemporaryDirectory finalizers run on Windows.
import atexit
import logging
atexit.register(logging.shutdown)


SID = "private_wc0123456789ab"


def message(sequence=1, **changes):
    return {
        "sequence": sequence, "message_id": f"{SID}-{sequence}",
        "user_id": "wc0123456789ab", "user_name": "synthetic user",
        "timestamp": 1792108800, "timestamp_source": "observed_at",
        "content_text": "synthetic text", "direction": "inbound", "is_own": False,
        "news_source": "diff", **changes,
    }


def poll(rows=None, cursor=1, generation="cache-A", sid=SID):
    rows = [message()] if rows is None else rows
    return {
        "protocol_version": "0", "platform": "wechat", "generation": generation,
        "cursor": cursor, "arrival_confirmed": False, "mode": "patrol", "focus": [],
        "sessions": [{"session_info": {"session_id": sid, "session_name": "synthetic session"},
                      "message_list": rows}],
    }


class BridgeFixture:
    def __init__(self):
        self.generation = "cache-A"
        self.capabilities = sorted(REQUIRED_CAPABILITIES)
        self.rows = [message()]
        self.cursor = 1
        self.poll_queries = []
        self.posts = []
        self.receipts = {}
        self.lookups = 0
        self.lose_response = False
        self.poll_hook = None
        self.app = web.Application(middlewares=[self.auth])
        self.app.router.add_get("/health", self.health)
        self.app.router.add_get("/poll", self.poll)
        self.app.router.add_post("/send", self.send)
        self.app.router.add_get("/send/by-request/{request_id}", self.lookup)

    @web.middleware
    async def auth(self, request, handler):
        if request.headers.get("Authorization") != "Bearer synthetic-test-token":
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def health(self, request):
        return web.json_response({
            "protocol_version": "0", "platform": "wechat", "generation": self.generation,
            "ready": True, "capabilities": self.capabilities,
        })

    async def poll(self, request):
        after = int(request.query["after"])
        self.poll_queries.append(after)
        if self.poll_hook:
            return web.json_response(self.poll_hook(after))
        rows = [row for row in self.rows if row["sequence"] > after]
        return web.json_response(poll(rows, self.cursor, self.generation))

    async def send(self, request):
        body = await request.json()
        self.posts.append(body)
        rid = body["request_id"]
        if body["expected_generation"] != self.generation:
            return web.json_response({"error": "generation_mismatch"}, status=409)
        # Synthetic peer only. No desktop calls or external network operations.
        self.receipts[rid] = {"request_id": rid, "send_id": f"receipt-{len(self.posts)}",
                              "status": "queued", "message_id": None}
        if self.lose_response:
            return web.json_response({"error": "simulated lost acknowledgement"}, status=504)
        await asyncio.sleep(0.02)
        return web.json_response(self.receipts[rid], status=202)

    async def lookup(self, request):
        self.lookups += 1
        receipt = self.receipts.get(request.match_info["request_id"])
        if receipt is None:
            return web.json_response({"error": "record_missing"}, status=404)
        return web.json_response(receipt)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="case_", dir=TEMP_ROOT)
        self.path = Path(self.tmp.name, "bridge.db")
        self.state = BridgeState(self.path)
        self.peer = BridgeFixture()
        self.server = TestServer(self.peer.app)
        await self.server.start_server()
        self.client = self.make_client()
        await self.client.__aenter__()

    def make_client(self, token="synthetic-test-token"):
        return BridgeClient("bridge-1", "wechat", str(self.server.make_url("/")), token, self.state)

    async def asyncTearDown(self):
        await self.client.__aexit__()
        await self.server.close()
        self.tmp.cleanup()

    async def prepare(self):
        await self.client.poll_once()
        return self.state.prepare_send("bridge-1", "operation-1", SID, "synthetic outbound")

    async def test_progress_and_messages_survive_restart_and_acknowledgement(self):
        await self.client.poll_once()
        reopened = BridgeState(self.path)
        self.assertEqual(reopened.checkpoint("bridge-1"), ("cache-A", 1))
        rows = reopened.inbox("bridge-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["session_info"]["platform"], "wechat")
        await self.client.poll_once()
        self.assertEqual(len(reopened.inbox("bridge-1")), 1)
        reopened.acknowledge("bridge-1", "cache-A", SID, message()["message_id"])
        self.assertEqual(reopened.inbox("bridge-1"), [])

    async def test_empty_page_can_advance_over_non_message_rows(self):
        self.peer.rows = []
        self.peer.cursor = 4
        await self.client.poll_once()
        self.assertEqual(self.state.checkpoint("bridge-1"), ("cache-A", 4))
        self.assertEqual(self.state.inbox("bridge-1"), [])

    async def test_new_generation_resets_cursor_and_does_not_collide_with_old_message_id(self):
        self.peer.cursor = 10
        await self.client.poll_once()
        self.peer.generation = "cache-B"
        self.peer.cursor = 1
        await self.client.poll_once()
        self.assertEqual(self.peer.poll_queries, [0, 10, 0])
        self.assertEqual(self.state.checkpoint("bridge-1"), ("cache-B", 1))
        self.assertEqual({r["generation"] for r in self.state.inbox("bridge-1")}, {"cache-A", "cache-B"})

    async def test_generation_changes_again_during_reset_do_not_commit(self):
        await self.client.poll_once()
        self.peer.generation = "cache-B"
        self.peer.poll_hook = lambda after: poll([], 0, "cache-B" if after else "cache-C")
        with self.assertRaises(ContractError):
            await self.client.poll_once()
        self.assertEqual(self.state.checkpoint("bridge-1"), ("cache-A", 1))

    async def test_bad_page_is_not_partially_staged_and_cursor_does_not_advance(self):
        self.peer.rows = [message(), message(2, timestamp="2026-10-16T12:00:00+08:00")]
        self.peer.cursor = 2
        with self.assertRaises(ContractError):
            await self.client.poll_once()
        self.assertEqual(self.state.checkpoint("bridge-1"), ("", 0))
        self.assertEqual(self.state.inbox("bridge-1"), [])

    async def test_sqlite_failure_rolls_back_cursor_and_all_rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("CREATE TRIGGER reject_inbox BEFORE INSERT ON inbox "
                           "BEGIN SELECT RAISE(ABORT, 'synthetic disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.client.poll_once()
        self.assertEqual(self.state.checkpoint("bridge-1"), ("", 0))

    async def test_unknown_direction_and_rebuilt_screens_are_quarantined(self):
        self.peer.rows = [message(direction="unknown"), message(2, news_source="baseline"),
                          message(3, news_source="unquantified")]
        self.peer.cursor = 3
        await self.client.poll_once()
        self.assertEqual(self.state.inbox("bridge-1"), [])
        self.assertEqual(len(self.state.inbox("bridge-1", "quarantined")), 3)
        self.assertEqual(self.state.checkpoint("bridge-1"), ("cache-A", 3))

    async def test_outbound_is_system_before_downstream_and_repeated_text_is_not_dropped(self):
        self.peer.rows = [message(direction="outbound", is_own=True, user_id="self"), message(2), message(3)]
        self.peer.cursor = 3
        await self.client.poll_once()
        rows = self.state.inbox("bridge-1")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["payload"]["message_list"][0]["user_id"], "system")

    async def test_stale_concurrent_poll_cannot_overwrite_progress(self):
        await self.client.poll_once()
        with self.assertRaises(ContractError):
            self.state.commit_poll("bridge-1", parse_poll(poll([], 2), 1, "wechat"), ("", 0))
        self.assertEqual(self.state.checkpoint("bridge-1"), ("cache-A", 1))

    async def test_immutable_message_conflict_rolls_back_the_whole_page(self):
        await self.client.poll_once()
        changed = message(2, message_id=message()["message_id"], direction="outbound",
                          is_own=True, user_id="self")
        batch = parse_poll(poll([changed], 2), 1, "wechat")
        with self.assertRaises(ContractError):
            self.state.commit_poll("bridge-1", batch, ("cache-A", 1))
        self.assertEqual(self.state.checkpoint("bridge-1"), ("cache-A", 1))
        self.assertEqual(len(self.state.inbox("bridge-1")), 1)

    async def test_request_id_survives_restart_and_logical_payload_cannot_change(self):
        row = await self.prepare()
        reopened = BridgeState(self.path)
        again = reopened.prepare_send("bridge-1", "operation-1", SID, "synthetic outbound")
        self.assertEqual(row["request_id"], again["request_id"])
        with self.assertRaises(ContractError):
            reopened.prepare_send("bridge-1", "operation-1", SID, "different text")
        with self.assertRaises(ContractError):
            reopened.prepare_send("bridge-1", "other", "unknown_wc0123456789ab", "text")

    async def test_two_stage_receipt_never_fabricates_message_id(self):
        row = await self.prepare()
        rid = row["request_id"]
        queued = await self.client.submit_once(rid)
        self.assertEqual(queued.status, "queued")
        self.assertIsNone(queued.message_id)
        self.peer.receipts[rid].update(status="actioned", report={"clicked": True})
        actioned = await self.client.reconcile_send(rid)
        self.assertEqual(actioned.status, "actioned")
        self.assertIsNone(actioned.message_id)
        self.peer.receipts[rid].update(status="queued")
        stale = await self.client.reconcile_send(rid)
        self.assertEqual(stale.status, "actioned")
        self.peer.receipts[rid].update(status="observed", message_id=f"{SID}-2")
        observed = await self.client.reconcile_send(rid)
        self.assertEqual(observed.message_id, f"{SID}-2")
        self.assertEqual(len(self.peer.posts), 1)

    async def test_lost_ack_and_restart_only_query_never_post_again(self):
        row = await self.prepare()
        self.peer.lose_response = True
        with self.assertRaises(BridgeHTTPError):
            await self.client.submit_once(row["request_id"])
        self.assertEqual(self.state.send_record(row["request_id"])["status"], "unknown")
        reopened = self.make_client()
        async with reopened:
            receipt = await reopened.submit_once(row["request_id"])
        self.assertEqual(receipt.status, "queued")
        self.assertEqual(len(self.peer.posts), 1)
        self.assertEqual(self.peer.lookups, 1)

    async def test_missing_receipt_after_interrupted_post_is_not_reexecuted(self):
        row = await self.prepare()
        self.state.claim_send("bridge-1", row["request_id"])
        receipt = await self.client.submit_once(row["request_id"])
        self.assertEqual(receipt.status, "unknown")
        await self.client.submit_once(row["request_id"])
        self.assertEqual(self.peer.posts, [])

    async def test_concurrent_submits_only_one_post(self):
        row = await self.prepare()
        await asyncio.gather(*(self.client.submit_once(row["request_id"]) for _ in range(4)))
        self.assertEqual(len(self.peer.posts), 1)

    async def test_generation_change_prevents_prepared_send(self):
        row = await self.prepare()
        self.peer.generation = "cache-B"
        with self.assertRaises(ContractError):
            await self.client.submit_once(row["request_id"])
        self.assertEqual(self.peer.posts, [])

    async def test_cross_connection_send_and_terminal_receipt_rewrite_are_rejected(self):
        row = await self.prepare()
        self.state.bind("other", "wechat")
        self.assertFalse(self.state.claim_send("other", row["request_id"]))
        receipt = SendReceipt(row["request_id"], "observed", "receipt-one", "message-one")
        with self.assertRaises(ContractError):
            self.state.record_receipt("other", receipt)
        self.state.record_receipt("bridge-1", receipt)
        with self.assertRaises(ContractError):
            self.state.record_receipt("bridge-1", SendReceipt(
                row["request_id"], "observed", "receipt-one", "message-two"))
        self.assertEqual(self.state.send_record(row["request_id"])["message_id"], "message-one")

    async def test_auth_and_old_capabilities_are_rejected(self):
        with self.assertRaises(BridgeHTTPError):
            async with self.make_client(token="wrong"):
                self.fail("wrong token accepted")
        self.peer.capabilities = ["text.receive", "text.send"]
        with self.assertRaises(ContractError):
            async with self.make_client():
                self.fail("legacy peer accepted")
        self.assertEqual(self.peer.posts, [])


class PureContractTests(unittest.TestCase):
    def test_unknown_scope_is_quarantined(self):
        batch = parse_poll(poll(sid="unknown_wc0123456789ab"), 0, "wechat")
        self.assertIn("unknown_scope", batch.messages[0].quarantine_reason)

    def test_invalid_directions_boolean_timestamps_and_out_of_page_rows_rejected(self):
        variants = [message(timestamp=True), message(is_own=True), message(user_id="self"),
                    message(direction="invalid"), message(2), message(sequence=0)]
        for row in variants:
            with self.subTest(row=row), self.assertRaises(ContractError):
                parse_poll(poll([row]), 0, "wechat")

    def test_receipt_ids_are_not_platform_message_ids(self):
        base = {"request_id": "req", "send_id": "bridge-id", "status": "queued"}
        for changes in ({"message_id": "not-observed"}, {"status": "observed"},
                        {"status": "actioned"}, {"request_id": "other"}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                SendReceipt.parse({**base, **changes}, "req")

    def test_parser_does_not_mutate_wire_rows(self):
        original = poll([message(direction="outbound", is_own=True, user_id="self")])
        saved = copy.deepcopy(original)
        parse_poll(original, 0, "wechat")
        self.assertEqual(original, saved)


if __name__ == "__main__":
    try:
        result = unittest.main(verbosity=2, exit=False).result
    finally:
        import logging
        logging.shutdown()
        TEST_DATA.cleanup()
    sys.exit(0 if result.wasSuccessful() else 1)
