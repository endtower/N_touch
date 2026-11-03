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


def tool(name, args=None, call_id="call-1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args or {})))


def startup_note(basis="地址已确认", action="先给老师发消息确认收货时间"):
    """构造符合固定五段格式的启动单（目的 / 要做 / 对象 / 依据 / 兜底）。"""
    return (
        "目的：办妥这条待接续事项\n"
        f"要做：{action}\n"
        "对象：会话 private_test\n"
        f"依据：{basis}\n"
        "兜底：到点无回应就换个渠道再问一次"
    )


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
        return await QuestAgent._create_task_impl(title, startup_note(), self.later)

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

    async def test_reschedule_only_preserves_full_context_and_same_record(self):
        original = "已确认的进展。" * 30 + "\n关键线索：private_keep，材料编号 last-123"
        task_id = await self.ops.create_task("同一事项改期", original, self.later)
        earlier = (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")
        result = await QuestAgent._update_task_impl(task_id, next_occurrence=earlier)
        self.assertEqual(result["task_id"], task_id)
        self.assertEqual(result["context"], original)
        self.assertEqual(result["next_occurrence"], earlier)
        later = (datetime.now() + timedelta(days=5)).isoformat(timespec="seconds")
        await QuestAgent._update_task_impl(task_id, next_occurrence=later)
        self.assertEqual((await self.ops.get_task_by_id(task_id))["next_occurrence"], later)
        self.assertEqual(await self.ops.get_active_tasks(), [task_id])

    async def test_reschedule_expired_or_invalid_time_does_not_write(self):
        receipt = await self.create()
        original = await self.ops.get_task_by_id(receipt["task_id"])
        for args in ({}, {"next_occurrence": "明天再说"}, {"next_occurrence": "不要尽快"}, {"context": " "}):
            with self.assertRaises(ValueError):
                await QuestAgent._update_task_impl(receipt["task_id"], **args)
            self.assertEqual(await self.ops.get_task_by_id(receipt["task_id"]), original)
        await self.ops.expire_task(receipt["task_id"])
        with self.assertRaises(ValueError):
            await QuestAgent._update_task_impl(receipt["task_id"], next_occurrence=self.later)
        self.assertEqual((await self.ops.get_task_by_id(receipt["task_id"]))["status"], TASK_STATUS_EXPIRED)

    async def test_quest_reschedules_duplicate_and_has_untruncated_context(self):
        original = "原始进展。" * 40 + "重要尾段：private_keep"
        task_id = await self.ops.create_task("改期测试", original, self.later)
        when = (datetime.now() + timedelta(days=6)).isoformat(timespec="seconds")
        agent = QuestAgent()
        agent._call_llm = AsyncMock(side_effect=[
            reply(content="", calls=[tool("update_task", {"task_id": task_id, "next_occurrence": when})]),
            reply(content="", calls=[tool("finish", {"summary": "已改期"}, call_id="call-2")]),
        ])
        result = await agent.run("改期测试", "同一事项改到新的时间", when)
        self.assertEqual(result["created_tasks"], [])
        self.assertEqual(result["updated_tasks"][0]["task_id"], task_id)
        self.assertEqual(result["updated_tasks"][0]["context"], original)
        self.assertIn("已更新", result["formatted_output"])
        self.assertIn(when, result["formatted_output"])
        self.assertIn(original, agent._messages[0]["content"])
        self.assertIn(self.later, agent._messages[0]["content"])
        self.assertEqual(await self.ops.get_active_tasks(), [task_id])

    async def test_failed_reschedule_never_claims_success(self):
        agent = QuestAgent()
        agent._call_llm = AsyncMock(side_effect=[
            reply(content="改期成功", calls=[tool("update_task", {
                "task_id": "missing", "next_occurrence": self.later,
            })]),
            reply(content="", calls=[tool("finish", call_id="call-2")]),
        ])
        result = await agent.run("不存在的安排", "请求改期", self.later)
        self.assertEqual(result["updated_tasks"], [])
        self.assertIn("未成功", result["formatted_output"])
        self.assertNotIn("改期成功", result["formatted_output"])

    async def test_reschedule_replaces_due_time_for_truck(self):
        task_id = await self.ops.create_task("延后待办", "保持原事项", (datetime.now() - timedelta(minutes=2)).isoformat())
        self.assertIn(task_id, [r["task_id"] for r in await TaskTruck()._find_due_tasks()])
        await QuestAgent._update_task_impl(task_id, next_occurrence=self.later)
        self.assertNotIn(task_id, [r["task_id"] for r in await TaskTruck()._find_due_tasks()])

    async def test_queued_old_deadline_does_not_wake_after_postponement(self):
        task_id = await self.ops.create_task("已排队后改期", "保留进展", (datetime.now() - timedelta(minutes=2)).isoformat())
        truck = TaskTruck()
        truck._triggered_tasks = await truck._find_due_tasks()
        await QuestAgent._update_task_impl(task_id, next_occurrence=self.later)
        self.assertIsNone(await truck.get_truck_messages())
        self.assertEqual((await self.ops.get_task_by_id(task_id))["status"], TASK_STATUS_ACTIVE)

    async def test_concurrent_reschedule_between_scan_and_delivery_is_not_expired(self):
        task_id = await self.ops.create_task("交付前改期", "原始进展", (datetime.now() - timedelta(minutes=2)).isoformat())
        truck = TaskTruck()
        scanned = await truck._find_due_tasks()
        await QuestAgent._update_task_impl(task_id, next_occurrence=self.later)
        with patch.object(truck, "_find_due_tasks", AsyncMock(return_value=scanned)):
            self.assertIsNone(await truck.get_truck_messages())
        self.assertEqual((await self.ops.get_task_by_id(task_id))["status"], TASK_STATUS_ACTIVE)

    async def test_due_task_is_delivered_once_and_then_cannot_reschedule(self):
        task_id = await self.ops.create_task("仅交付一次", "准确上下文", (datetime.now() - timedelta(minutes=2)).isoformat())
        truck = TaskTruck()
        self.assertIn("准确上下文", (await truck.get_truck_messages())["formatted_message"])
        self.assertIsNone(await truck.get_truck_messages())
        with self.assertRaises(ValueError):
            await QuestAgent._update_task_impl(task_id, next_occurrence=self.later)

    async def test_create_task_rejects_sixth_and_accepts_after_merge_or_complete(self):
        for i in range(5):
            await self.create(title=f"事项{i}")
        self.assertEqual(await self.ops.count_active_tasks(), 5)
        with self.assertRaises(ValueError) as ctx:
            await self.create(title="第6条")
        self.assertIn("上限 5 条", str(ctx.exception))
        self.assertEqual(await self.ops.count_active_tasks(), 5)
        self.assertIsNone(await self.ops.get_task_id_by_title("第6条"))

        first = await self.ops.get_task_id_by_title("事项0")
        await QuestAgent._complete_task_impl(first)
        self.assertEqual(await self.ops.count_active_tasks(), 4)
        await self.create(title="第6条")
        self.assertEqual(await self.ops.count_active_tasks(), 5)

        second = await self.ops.get_task_id_by_title("事项1")
        await QuestAgent._update_task_impl(second, context=startup_note(basis="合并后的进展"))
        self.assertEqual(await self.ops.count_active_tasks(), 5)

    async def test_quest_reports_capacity_error_without_writing(self):
        for i in range(5):
            await self.create(title=f"事项{i}")
        agent = QuestAgent()
        args = {"title": "溢出事项", "context": startup_note(basis="private_overflow"), "next_occurrence": self.later}
        agent._call_llm = AsyncMock(side_effect=[
            reply(calls=[tool("create_task", args)]),
            reply(content="", calls=[tool("finish", call_id="call-2")]),
        ])
        result = await agent.run("溢出事项", "上下文 private_overflow", self.later)
        self.assertEqual(result["created_tasks"], [])
        self.assertIn("上限 5 条", result["formatted_output"])
        self.assertEqual(await self.ops.count_active_tasks(), 5)

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

    async def test_quest_agent_multiround_registers(self):
        agent = QuestAgent()
        args = {"title": "择时验证", "context": startup_note(basis="验证经验 private_verify"), "next_occurrence": self.later}
        agent._call_llm = AsyncMock(side_effect=[
            reply(calls=[tool("create_task", args)]),
            reply(content="", calls=[tool("finish", {"summary": "已登记"}, call_id="call-2")]),
        ])
        result = await agent.run("择时验证", "验证经验 private_verify", self.later)
        self.assertEqual(len(result["created_tasks"]), 1)
        self.assertEqual(result["created_tasks"][0]["next_occurrence"], self.later)
        self.assertEqual(agent._call_llm.await_count, 2)
        self.assertEqual(result["formatted_output"].splitlines()[0], "已登记待接续事项：择时验证（"
                         + result["created_tasks"][0]["task_id"] + "）；下次唤醒：" + self.later)

    async def test_missing_finish_aborts(self):
        agent = QuestAgent()
        agent._call_llm = AsyncMock(return_value=reply(content="已处理"))
        with self.assertRaises(RuntimeError):
            await agent.run("重复事项", "上下文", self.later)
        self.assertEqual(agent._call_llm.await_count, QuestAgent.MAX_ROUNDS)

    async def test_quest_agent_duplicate_reply_registers_nothing(self):
        agent = QuestAgent()
        agent._call_llm = AsyncMock(side_effect=[
            reply(content="已存在同一事项的待接续安排，不重复登记"),
            reply(content="", calls=[tool("finish", {"summary": "已存在，不重复登记"}, call_id="call-2")]),
        ])
        result = await agent.run("重复事项", "上下文", self.later)
        self.assertEqual(result["created_tasks"], [])
        self.assertEqual([c["tool"] for c in result["tools_called"]], ["finish"])
        self.assertEqual(agent._call_llm.await_count, 2)

    async def test_oversized_context_is_rejected_and_creates_nothing(self):
        from brain.hc.database.task import TASK_CONTEXT_MAX_CHARS
        with self.assertRaises(ValueError) as ctx:
            await QuestAgent._create_task_impl("超长上下文", "过程记录。" * 400, self.later)
        self.assertIn(str(TASK_CONTEXT_MAX_CHARS), str(ctx.exception))
        self.assertEqual(await self.ops.get_active_tasks(), [])

    async def test_update_rejects_oversized_context_without_overwriting(self):
        receipt = await self.create()
        original = (await self.ops.get_task_by_id(receipt["task_id"]))["context"]
        with self.assertRaises(ValueError):
            await QuestAgent._update_task_impl(receipt["task_id"], context="追加。" * 400)
        self.assertEqual((await self.ops.get_task_by_id(receipt["task_id"]))["context"], original)

    async def test_context_without_purpose_or_action_is_rejected(self):
        for text in ("老师下周方便收；地址已确认。", "目的：把枣送到老师手上", "要做：先发消息问收货时间"):
            with self.assertRaises(ValueError) as ctx:
                await QuestAgent._create_task_impl("缺段事项", text, self.later)
            self.assertIn("目的", str(ctx.exception))
            self.assertIn("要做", str(ctx.exception))
        self.assertEqual(await self.ops.get_active_tasks(), [])

    async def test_context_with_task_id_bookkeeping_is_rejected(self):
        text = startup_note() + "\nTask_20261011123456 与 Task_20261012123456 合并"
        with self.assertRaises(ValueError) as ctx:
            await QuestAgent._create_task_impl("编号事项", text, self.later)
        self.assertIn("编号", str(ctx.exception))
        self.assertEqual(await self.ops.get_active_tasks(), [])

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
        agent._call_llm = AsyncMock(return_value=reply(content="", calls=[tool("finish")]))
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
        self.assertEqual(sorted(required), ["context", "title"])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        logging.shutdown()
        _sandbox.cleanup()
