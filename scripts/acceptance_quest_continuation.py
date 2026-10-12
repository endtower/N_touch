"""接续事项验收：临时 SQLite、模型替身，不联系真实会话或调用远程 LLM。

覆盖 2026-10-11 收敛后的语义：字段 title / context / next_occurrence，
status 仅 active / expired，任务一次性（到点交付后自动过期，续办靠重新登记）。
"""
import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_sandbox = tempfile.TemporaryDirectory(prefix="abyss-quest-continuation-")
os.environ["ABYSS_DATA_DIR"] = _sandbox.name
_personas = Path(_sandbox.name) / "brain" / "base" / "persona"
_personas.mkdir(parents=True)
for _name in ("common", "normal"):
    (_personas / f"{_name}.json").write_text(json.dumps({
        "persona_name": _name, "scene": "隔离验收", "kernel_prompt": "占位",
    }), encoding="utf-8")

from brain.hc.database.core import MemoryDatabase
from brain.hc.database.task import TaskOperations, TASK_STATUS_ACTIVE, TASK_STATUS_EXPIRED
from brain.base.light_agent.quest_agent import QuestAgent
from brain.base.common_ways.tool_task import ToolTask
from brain.base.common_ways.tool_persistent import ToolPersistent
from brain.att.trucks.task_truck import TaskTruck
from brain.base.light_agent.light_history_manager import new_light_history_manager


def reply(content="已处理", calls=None):
    message = SimpleNamespace(content=content, tool_calls=calls or [], reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def tool(name, args, call_id="call-1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


class QuestContinuationAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = Path(tempfile.mkdtemp(dir=_sandbox.name))
        self.db = MemoryDatabase(str(directory / "memory.db"))
        patcher = patch("brain.hc.database.task.get_memory_db", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ops = TaskOperations()
        self.history = Mock()
        patcher = patch("brain.base.light_agent.quest_agent.new_light_history_manager", return_value=self.history)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.later = (datetime.now() + timedelta(days=3)).isoformat(timespec="seconds")

    async def asyncTearDown(self):
        await self.db.close()

    async def create(self, title="给老师送枣"):
        return await QuestAgent._create_task_impl(
            title, "老师下周方便收；会话 private_test；地址已确认。", self.later,
        )

    async def test_create_task_keeps_context(self):
        receipt = await self.create()
        record = await self.ops.get_task_by_id(receipt["task_id"])
        self.assertEqual(record["status"], TASK_STATUS_ACTIVE)
        self.assertEqual(record["title"], "给老师送枣")
        self.assertIn("private_test", record["context"])
        self.assertEqual(record["next_occurrence"], self.later)

    async def test_missing_time_is_rejected_and_creates_nothing(self):
        with self.assertRaises(ValueError):
            await QuestAgent._create_task_impl("无时间", "待确认", "")
        self.assertEqual(await self.ops.get_active_tasks(), [])

    async def test_expire_task_marks_expired_and_leaves_active_set(self):
        receipt = await self.create()
        self.assertTrue(await self.ops.expire_task(receipt["task_id"]))
        record = await self.ops.get_task_by_id(receipt["task_id"])
        self.assertEqual(record["status"], TASK_STATUS_EXPIRED)
        self.assertTrue(record["end_time"])
        self.assertEqual(await self.ops.get_active_tasks(), [])

    async def test_directory_is_bounded_and_carries_context(self):
        for i in range(7):
            await self.ops.create_task(
                f"事项{i}", f"上下文{i}", next_occurrence=(datetime.now() + timedelta(days=7 - i)).isoformat(),
            )
        overview = await self.ops.format_active_task_overview(limit=3)
        self.assertIn("共 7 条", overview)
        self.assertEqual(overview.count("· "), 3)
        self.assertLess(overview.index("事项6"), overview.index("事项5"))
        self.assertIn("另有 4 条", overview)
        self.assertIn("上下文6", overview)

    async def test_quest_agent_single_round_registers(self):
        agent = QuestAgent()
        args = {"title": "择时验证", "context": "验证经验 private_verify", "next_occurrence": self.later}
        agent._call_llm = AsyncMock(side_effect=[reply(calls=[tool("create_task", args)])])
        result = await agent.run("择时验证", "验证经验 private_verify", self.later)
        self.assertEqual(len(result["created_tasks"]), 1)
        self.assertEqual(result["created_tasks"][0]["next_occurrence"], self.later)

    async def test_quest_agent_duplicate_reply_registers_nothing(self):
        agent = QuestAgent()
        agent._call_llm = AsyncMock(return_value=reply(content="已存在同一事项的待接续安排，不重复登记"))
        result = await agent.run("重复事项", "上下文", self.later)
        self.assertEqual(result["created_tasks"], [])
        self.assertEqual(result["tools_called"], [])
        self.assertEqual(len(agent._messages), 2)

    async def test_wake_block_carries_title_and_context(self):
        record = {"title": "寄枣", "context": "运单 test；会话 private_test；检查收货"}
        text = TaskTruck._wake_block(record)
        self.assertIn("寄枣", text)
        self.assertIn("运单 test", text)
        self.assertIn("重新登记", text)

    async def test_due_detection_uses_next_occurrence_only(self):
        receipt = await self.create()
        self.assertEqual(await TaskTruck()._find_due_tasks(), [])
        utc_past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        await self.ops.create_task("已到期", "上下文", utc_past)
        due = await TaskTruck()._find_due_tasks()
        self.assertEqual([r["title"] for r in due], ["已到期"])
        self.assertEqual([r["task_id"] for r in due], [await self.ops.get_task_id_by_title("已到期")])
        # 到期任务仍为 active（过期由 truck 交付时执行）
        self.assertEqual((await self.ops.get_task_by_id(receipt["task_id"]))["status"], TASK_STATUS_ACTIVE)

    async def test_real_logs_are_separate_and_old_dialogue_is_not_replayed(self):
        agent = QuestAgent()
        agent._call_llm = AsyncMock(return_value=reply())
        with patch("brain.base.light_agent.quest_agent.new_light_history_manager", new_light_history_manager), \
             patch("utils.chain_context.get_current_chain_id", return_value="event_chain_quest_acceptance"):
            await agent.run("第一份请求，仅保留在第一份日志", "上下文", self.later)
            first_path = agent._history._session_file
            await agent.run("第二份请求", "上下文", self.later)
            second_path = agent._history._session_file
            agent.reset()
        self.assertNotEqual(first_path, second_path)
        first = json.loads(first_path.read_text(encoding="utf-8"))
        second = json.loads(second_path.read_text(encoding="utf-8"))
        self.assertIn("第一份请求", json.dumps(first, ensure_ascii=False))
        self.assertNotIn("第一份请求", json.dumps(second, ensure_ascii=False))
        self.assertEqual(first[0]["role"], "system")
        self.assertEqual(second[0]["role"], "system")

    async def test_primary_and_legacy_entry_have_same_role_and_fresh_instances(self):
        self.assertIsNot(ToolTask._get_quest_agent(), ToolTask._get_quest_agent())
        self.assertIsNot(ToolPersistent._get_quest_agent(), ToolPersistent._get_quest_agent())
        primary = ToolTask.get_quest_agent_tool_schema()["function"]
        legacy = ToolPersistent.get_quest_agent_tool_schema()["function"]
        self.assertEqual(primary["description"], legacy["description"])
        required = QuestAgent.get_create_task_tool_schema()["function"]["parameters"]["required"]
        self.assertEqual(sorted(required), ["context", "next_occurrence", "title"])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        logging.shutdown()
        _sandbox.cleanup()
