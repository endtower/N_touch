"""PRE 复活链路验收：隔离数据、模拟 LLM，不向真实会话发送消息。"""
import asyncio
from collections import deque
import json
import logging
import os
from pathlib import Path
from workspace_layout import test_root, CACHE, BUILDS
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_sandbox = tempfile.TemporaryDirectory(prefix="pre-lifecycle-", dir=test_root())
os.environ["ABYSS_DATA_DIR"] = _sandbox.name

from brain.att.proactive.proactive_core import ProactiveCore
from brain.att.proactive.proactive_session import ProactiveSession
from brain.att.trucks.pre_truck import PreTruck
from brain.att.trucks.base_truck import BaseTruck
from brain.att.station import AttentionStation


def call(name, arguments="{}"):
    return SimpleNamespace(id="test-" + name, function=SimpleNamespace(name=name, arguments=arguments))


def response(name):
    return SimpleNamespace(content=None, reasoning_content=None, tool_calls=[call(name)])


class History:
    def __init__(self):
        self.messages = []

    def archive_context(self):
        pass

    def append(self, message):
        self.messages.append(message)

    def get_history(self):
        return self.messages


class Input:
    _queued_request = None

    def prepare_first_message(self, **kwargs):
        self._queued_request = kwargs

    async def build_first_content(self):
        request, self._queued_request = self._queued_request, None
        return request["task_draft"]["wakeup_reason"]


class PreLifecycleAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.history = History()
        self.registry = Mock()
        self.registry.handle_tool_call = AsyncMock(side_effect=lambda tool: {
            "role": "tool", "tool_call_id": tool.id,
            "content": json.dumps({"status": "queued", "task_info": "隔离验收行动"})
            if tool.function.name == "launch_pfca" else "无需主动行动",
        })
        patches = [
            patch("brain.att.proactive.proactive_session.get_proactive_history_manager", return_value=self.history),
            patch("brain.att.proactive.proactive_session.get_proactive_input_manager", side_effect=Input),
            patch("brain.att.proactive.proactive_session.get_proactive_tool_registry", return_value=self.registry),
            patch("brain.hc.global_session_state.get_global_session_state", return_value=Mock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.core = ProactiveCore()
        self.addAsyncCleanup(self.core.stop)

    def draft(self):
        return {"intent": "pre_opportunity", "wakeup_reason": "隔离验收：时间到", "chain_id": "test-pre-chain"}

    async def test_session_is_concrete_and_skip_ends_after_one_round(self):
        self.assertFalse(ProactiveSession.__abstractmethods__)
        session = ProactiveSession(self.draft())
        with patch.object(session, "_call_llm", AsyncMock(return_value=response("skip_proactive"))) as llm:
            result = await asyncio.wait_for(session.start(), 2)
        self.assertEqual(result["exit_reason"], "task_skipped")
        self.assertTrue(result["decision_result"]["skipped"])
        self.assertEqual(llm.await_count, 1)
        self.assertEqual([m["role"] for m in self.history.messages], ["user", "assistant", "tool"])
        self.assertIsInstance(session._recent_tool_sigs, deque)
        self.assertEqual(session._recent_tool_sigs.maxlen, session.repeat_threshold)

    async def test_launch_ends_after_one_round_and_returns_decision(self):
        session = ProactiveSession(self.draft())
        with patch.object(session, "_call_llm", AsyncMock(return_value=response("launch_pfca"))):
            result = await asyncio.wait_for(session.start(), 2)
        self.assertEqual(result["exit_reason"], "task_launched")
        self.assertEqual(json.loads(result["decision_result"]["tool_result"])["task_info"], "隔离验收行动")

    async def test_invalid_terminal_arguments_do_not_end_session(self):
        session = ProactiveSession(self.draft())
        self.registry.handle_tool_call.side_effect = [
            {"role": "tool", "content": "[错误] 参数无效", "tool_call_id": "test-launch_pfca"},
            {"role": "tool", "content": "本次跳过", "tool_call_id": "test-skip_proactive"},
        ]
        with patch.object(session, "_call_llm", AsyncMock(side_effect=[response("launch_pfca"), response("skip_proactive")])):
            result = await asyncio.wait_for(session.start(), 2)
        self.assertEqual(session._rounds, 2)
        self.assertTrue(result["decision_result"]["skipped"])
        self.assertEqual(sum(m["role"] == "user" for m in self.history.messages), 1)

    async def test_constructor_failure_releases_waiter_and_next_task_runs(self):
        await self.core.start()
        draft = self.draft()
        with patch("brain.att.proactive.proactive_session.ProactiveSession", side_effect=TypeError("constructor failure")):
            with self.assertRaises(RuntimeError) as caught:
                await asyncio.wait_for(self.core.feed_proactive_task(draft), 2)
        self.assertIsInstance(caught.exception.__cause__, TypeError)
        self.assertTrue(draft["_completion_event"].is_set())
        self.assertIsNone(self.core.current_session)
        await asyncio.wait_for(self.core.task_queue.join(), 2)
        session = SimpleNamespace(start=AsyncMock(), decision_result={"skipped": True})
        with patch("brain.att.proactive.proactive_session.ProactiveSession", return_value=session):
            next_draft = self.draft()
            await asyncio.wait_for(self.core.feed_proactive_task(next_draft), 2)
        self.assertTrue(next_draft["_decision_result"]["skipped"])

    async def test_worker_cancellation_releases_waiter(self):
        started = asyncio.Event()

        async def block():
            started.set()
            await asyncio.Event().wait()

        session = SimpleNamespace(start=block, stop=AsyncMock(), decision_result=None)
        await self.core.start()
        with patch("brain.att.proactive.proactive_session.ProactiveSession", return_value=session):
            feed = asyncio.create_task(self.core.feed_proactive_task(self.draft()))
            await asyncio.wait_for(started.wait(), 2)
            await self.core.stop()
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(feed, 2)
        await asyncio.wait_for(self.core.task_queue.join(), 2)

    async def test_stopped_or_dead_worker_rejects_feed(self):
        with self.assertRaises(RuntimeError):
            await self.core.feed_proactive_task(self.draft())
        await self.core.start()
        self.core._main_task.cancel()
        await asyncio.gather(self.core._main_task, return_exceptions=True)
        with self.assertRaises(RuntimeError):
            await self.core.feed_proactive_task(self.draft())

    async def test_pre_time_opportunity_runs_session_and_returns_skip(self):
        await self.core.start()
        truck = PreTruck()
        truck._pending = {"rule": "time", "sessions": []}
        with patch("brain.att.proactive.proactive_core.get_proactive_core", return_value=self.core), \
             patch.object(ProactiveSession, "_call_llm", AsyncMock(return_value=response("skip_proactive"))), \
             patch.object(truck, "_advance_shared_cursor", AsyncMock()):
            result = await asyncio.wait_for(truck.get_truck_messages(), 2)
        self.assertIsNone(result)
        self.assertIn("规则命中：时间到", self.history.messages[0]["content"])
        self.assertIsNone(self.core.current_session)

    async def test_pre_launch_returns_pfca_input_without_executing_pfca(self):
        await self.core.start()
        truck = PreTruck()
        truck._pending = {"rule": "time", "sessions": []}
        with patch("brain.att.proactive.proactive_core.get_proactive_core", return_value=self.core), \
             patch.object(ProactiveSession, "_call_llm", AsyncMock(return_value=response("launch_pfca"))), \
             patch.object(truck, "_advance_shared_cursor", AsyncMock()):
            result = await asyncio.wait_for(truck.get_truck_messages(), 2)
        self.assertIn("隔离验收行动", result["formatted_message"])
        self.assertEqual(result["decision"]["tool_name"], "launch_pfca")

    async def test_context_file_is_written_to_the_same_event_chain(self):
        from brain.att.proactive.proactive_history_manager import ProactiveHistoryManager
        history = ProactiveHistoryManager()
        await self.core.start()
        with patch("brain.att.proactive.proactive_session.get_proactive_history_manager", return_value=history), \
             patch.object(ProactiveSession, "_call_llm", AsyncMock(return_value=response("skip_proactive"))):
            await asyncio.wait_for(self.core.feed_proactive_task(self.draft()), 2)
        files = list((Path(_sandbox.name) / "memory/event_chains/test-pre-chain").glob("proactive*.json"))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertIn("隔离验收：时间到", json.dumps(data, ensure_ascii=False))
        self.assertIn("skip_proactive", json.dumps(data))
        history.archive_context()

    async def test_pre_writes_its_own_context_file_into_the_same_chain(self):
        """PRE 必须有自己的 pre*.json，而不是只在 proactive 上下文里留个影子。"""
        from utils.chain_context import set_chain_context, reset_chain_context

        await self.core.start()
        truck = PreTruck()
        truck._pending = {"rule": "time", "sessions": []}
        token = set_chain_context("test-pre-chain")
        try:
            with patch("brain.att.proactive.proactive_core.get_proactive_core", return_value=self.core), \
                 patch.object(ProactiveSession, "_call_llm", AsyncMock(return_value=response("skip_proactive"))), \
                 patch.object(truck, "_advance_shared_cursor", AsyncMock()):
                await asyncio.wait_for(truck.get_truck_messages(), 2)
        finally:
            reset_chain_context(token)

        files = list((Path(_sandbox.name) / "memory/event_chains/test-pre-chain").glob("pre*.json"))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual([m["role"] for m in data], ["system", "user", "assistant"])
        self.assertIn("规则命中: time", data[0]["content"])
        self.assertIn("规则命中：时间到", data[1]["content"])
        self.assertIn("skip_proactive", data[2]["content"])

    async def test_station_resumes_all_trucks_after_pre_skip(self):
        await self.core.start()
        truck, sibling = PreTruck(), BaseTruck("private_truck")
        truck.state, sibling.state = "paused", "running"
        truck._pending = {"rule": "time", "sessions": []}
        station = AttentionStation()
        station.running = True
        station.pre_truck, station.private_truck = truck, sibling
        with patch("brain.att.proactive.proactive_core.get_proactive_core", return_value=self.core), \
             patch.object(ProactiveSession, "_call_llm", AsyncMock(return_value=response("skip_proactive"))), \
             patch.object(truck, "_advance_shared_cursor", AsyncMock()), \
             patch("brain.att.station._force_archive_chain_contexts"):
            processing = asyncio.create_task(station._process_tasks())
            try:
                await station.task_queue.put("pre_truck")
                await asyncio.wait_for(station.task_queue.join(), 2)
                self.assertEqual(truck.state, "running")
                self.assertEqual(sibling.state, "running")
                self.assertFalse(processing.done())
            finally:
                station.running = False
                processing.cancel()
                await asyncio.gather(processing, return_exceptions=True)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        logging.shutdown()
        _sandbox.cleanup()
