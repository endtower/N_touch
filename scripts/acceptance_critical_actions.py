"""只检查重写后的四状态登记、failure_reason 分批与独立经验统计，不运行旧维护流程。"""
import asyncio
import json
import logging
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_sandbox = tempfile.TemporaryDirectory(prefix="abyss-critical-")
os.environ["ABYSS_DATA_DIR"] = _sandbox.name
personas = Path(_sandbox.name) / "brain/base/persona"
personas.mkdir(parents=True)
for name in ("common", "normal"):
    (personas / (name + ".json")).write_text(json.dumps({"persona_name": name, "scene": "检查", "kernel_prompt": "占位"}), encoding="utf-8")

import brain.hc.database.core as dbmod
import brain.amy.feedback_state as feedbackmod
import brain.hc.critical_actions as actionmod
import brain.amy.failure_reasons as reasonsmod
from brain.amy.constants import EvaluationAct, FinalEvaluationAct, TaskResultAct
from brain.amy.emotional_interface import EmotionalInterface, reset_emotional_interface
from brain.hc.database.bequest import BequestOperations
from brain.hc.maintenance import MaintenanceService
from brain.base.common_ways.tool_task import ToolTask
from brain.pfc.pfca.pfca_tools import PFCA_tool_manager
from brain.pfc.reflection.reflection_tools import Reflection_tool_manager
from brain.chat.conversation.conversation_tool_manager import ConversationToolManager
from brain.chat.social_chat.social_chat_tool_manager import SocialChatToolManager


class CriticalActionChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_emotional_interface()
        self.root = Path(tempfile.mkdtemp(dir=_sandbox.name))
        self.db = dbmod.MemoryDatabase(str(self.root / "memory.db"))
        self.feedback = feedbackmod.FeedbackState(self.root)
        self.patches = [patch.object(dbmod, "_memory_db_instance", self.db),
                        patch.object(feedbackmod, "_feedback_state", self.feedback),
                        patch.object(actionmod, "_action_lock", asyncio.Lock()),
                        patch.object(reasonsmod, "failure_reason_lock", asyncio.Lock()),
                        patch.object(actionmod, "failure_reason_lock", reasonsmod.failure_reason_lock)]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        self.conn = await self.db._ensure_connection()
        self.service = actionmod.CriticalActions()

    async def asyncTearDown(self):
        await self.db.close()

    def values(self, competence, recognition):
        self.feedback._competence_value = competence
        self.feedback._value = recognition

    def args(self, action):
        return dict(action=action, title="本轮行动", reason="明确的实际原因", plan="针对问题的实施打算",
                    next_occurrence="2030-01-01T12:00:00", notify_master="私聊说明问题与维护时间",
                    verification="观察新方式的实际结果", event="引发不满的事件", target="具体对象", expectation="改善相处方式")

    async def quest(self, title, context, when):
        await self.conn.execute("INSERT INTO tasks(task_id,title,context,next_occurrence) VALUES(?,?,?,?)",
                                ("new-task", title, context, when))
        await self.conn.commit()
        return {"created_tasks": [{"task_id": "new-task", "title": title}]}

    async def test_four_states_only_reset_corresponding_axis_and_register_real_tasks(self):
        for action, values, expected in (
            ("申请维护", (100, 55), (50, 55)), ("尝试自修复", (0, 55), (50, 55)),
            ("情绪宣泄", (65, 0), (65, 50)), ("发布关系改进任务", (65, 100), (65, 50)),
        ):
            with self.subTest(action=action):
                self.values(*values)
                await self.conn.execute("DELETE FROM tasks")
                await self.conn.commit()
                with patch.object(ToolTask, "_run_quest_request", AsyncMock(side_effect=self.quest)) as quest:
                    result = await self.service.register(**self.args(action))
                    self.assertEqual(result["status"], "registered")
                    self.assertEqual(quest.await_count, int(action != "申请维护"))
                state = self.feedback.snapshot()
                self.assertEqual((state["competence_value"], state["value"]), expected)
                self.assertEqual(state["history"][-1]["label"], "临界行动已登记")

    async def test_boundaries_and_failed_task_do_not_reset(self):
        self.values(20, 99)
        for action in actionmod.ACTION_STATES:
            with self.assertRaises(ValueError):
                await self.service.register(**self.args(action))
        self.values(0, 55)
        with patch.object(ToolTask, "_run_quest_request", AsyncMock(return_value={"created_tasks": []})):
            result = await self.service.register(**self.args("尝试自修复"))
        self.assertEqual(result["status"], "not_registered")
        self.assertEqual(self.feedback.snapshot()["competence_value"], 0)

    async def test_reason_batch_moves_into_request_then_new_reasons_stay_pending(self):
        for source in ("conversation", "social_chat"):
            await reasonsmod.cache_failure_reason("策略受挫/陷入被动", source + "的具体失败原因", source=source, title="事件")
        await reasonsmod.cache_failure_reason("策略有效/推进顺利", "正面总结", source="conversation", title="良好事件")
        self.values(100, 50)
        first = await self.service.register(**self.args("申请维护"))
        self.assertEqual(len(first["failure_reasons"]), 2)
        self.assertEqual(await reasonsmod.pending_failure_reasons(), [])
        await reasonsmod.cache_failure_reason("表现平淡/未达预期", "新一批的原因", source="conversation", title="后续事件")
        stored = await MaintenanceService().query()
        self.assertEqual(len(stored["requests"][0]["failure_reasons"]), 2)
        self.assertEqual(len(stored["failure_reasons"]), 1)
        self.values(100, 50)
        second = await self.service.register(**self.args("申请维护"))
        self.assertEqual([r["failure_reason"] for r in second["failure_reasons"]], ["新一批的原因"])
        self.assertEqual(await reasonsmod.pending_failure_reasons(), [])

    async def test_cache_uses_declared_partition_and_keeps_verbatim_reason(self):
        reason = "具体原因" * 40
        with patch.object(EvaluationAct, "failure_items", [{"name": "新分区词条", "da_delta": 5}]):
            await reasonsmod.cache_failure_reason("新分区词条", reason, source="conversation", title="原文")
        self.assertEqual((await reasonsmod.pending_failure_reasons())[0]["failure_reason"], reason)
        for act in (TaskResultAct.UNCERTAIN, "无评价", "策略有效/推进顺利"):
            await reasonsmod.cache_failure_reason(act, "不应收入负面缓存", source="pfca", title="不入缓存")
        self.assertEqual(len(await reasonsmod.pending_failure_reasons()), 1)

    async def test_conversation_and_social_endings_collect_their_failure_reason(self):
        conv = object.__new__(ConversationToolManager)
        conv.conversation_core = SimpleNamespace(conversation_id="private_test", stop_session=AsyncMock(return_value="ended"))
        conv.emotional_judgment = AsyncMock()
        await conv.transfer_task_to_PFCA_agent("对话结束", "严重失败/全盘搞砸", "没有核实就下了结论")
        social = object.__new__(SocialChatToolManager)
        social.social_chat_core = SimpleNamespace(working_session_id="group_test", set_transfer_summary=Mock(), request_stop=AsyncMock())
        social.emotional_judgment = AsyncMock()
        await social.transfer_back("社交结束", "表现平淡/未达预期", "没有回应对方的问题")
        self.assertEqual([r["source"] for r in await reasonsmod.pending_failure_reasons()], ["conversation", "social_chat"])

    async def test_pfca_uses_title_for_both_changes_and_only_one_failure_reason(self):
        ei = EmotionalInterface(self.root)
        await ei.apply_final_evaluation("含蓄坏评价", "查证不足", TaskResultAct.HARMFUL, failure_reason="未核实造成返工")
        history = self.feedback.snapshot()["history"]
        self.assertEqual([r["reason"] for r in history], ["查证不足", "查证不足"])
        self.assertEqual(len(await reasonsmod.pending_failure_reasons()), 1)
        schema = next(t["function"]["parameters"] for t in PFCA_tool_manager.get_tools() if t["function"]["name"] == "terminate")
        self.assertIn("failure_reason", schema["properties"])
        self.assertNotIn("task_reason", schema["properties"])
        self.assertNotIn("evaluation_reason", schema["properties"])

    async def test_reset_recovery_is_idempotent_and_old_compensation_is_absent(self):
        self.values(100, 50)
        result = await self.service.register(**self.args("申请维护"))
        self.values(42, 50)
        await self.conn.execute("UPDATE critical_actions SET reset_applied=0 WHERE action_id=?", (result["action_id"],))
        await self.conn.commit()
        await self.service.recover_resets()
        self.assertEqual(self.feedback.snapshot()["competence_value"], 42)
        self.assertFalse(hasattr(EmotionalInterface, "apply_reflection_compensation"))
        for tool in Reflection_tool_manager.get_tools():
            self.assertNotIn(tool["function"]["name"], ("request_maintenance", "query_maintenance"))

    async def test_retrieval_and_human_maintenance_counts_are_independent(self):
        await self.conn.execute("INSERT INTO bequests(bequest_id,title) VALUES('one','经验')")
        await self.conn.commit()
        await BequestOperations().record_retrievals(["one", "one"])
        await BequestOperations().record_retrievals(["one"])
        await MaintenanceService().review("one")
        item = (await MaintenanceService().metadata())[0]
        self.assertEqual((item["retrieval_count"], item["manual_maintenance_count"]), (2, 1))
        self.assertTrue(item["last_maintained_at"])
        self.assertEqual(self.feedback.snapshot()["competence_value"], 50)

    async def test_pfca_preview_does_not_cache_or_settle_and_confirmation_does_once(self):
        import brain.hc.database.task as tasksmod
        import brain.hc.temporary_memory as memorymod
        import brain.hc.shortlight as notesmod
        import brain.pfc.pfca.pfca_core as coremod
        ei = EmotionalInterface(self.root)
        focus = SimpleNamespace(_data={})
        args = dict(summary="未核实信息造成返工", title="查证不足", task_result=TaskResultAct.HARMFUL,
                    final_evaluation="含蓄坏评价", failure_reason="使用未核实的信息")
        with patch("brain.hc.global_session_state.get_global_session_state", return_value=focus), \
             patch.object(tasksmod, "TaskOperations", return_value=SimpleNamespace(get_active_tasks=AsyncMock(return_value=[]))), \
             patch.object(memorymod, "get_temporary_memory", return_value=SimpleNamespace(add=Mock())), \
             patch.object(notesmod, "get_shortlight_notepad", return_value=SimpleNamespace(finalize_chain=AsyncMock())), \
             patch.object(coremod, "get_pfca_instance", return_value=SimpleNamespace(stop=AsyncMock())), \
             patch("brain.amy.emotional_interface.get_emotional_interface", AsyncMock(return_value=ei)):
            tool = PFCA_tool_manager()
            await tool.terminate(**args)
            self.assertEqual(await reasonsmod.pending_failure_reasons(), [])
            self.assertEqual(self.feedback.snapshot()["history"], [])
            await tool.terminate(**args)
        self.assertEqual(len(await reasonsmod.pending_failure_reasons()), 1)
        self.assertEqual(len(self.feedback.snapshot()["history"]), 2)

    async def test_old_database_statistics_migrate_once_without_old_reset_effects(self):
        path = self.root / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
          CREATE TABLE bequests(bequest_id TEXT PRIMARY KEY,title TEXT,summary TEXT,category TEXT,persona TEXT,
            title_vector BLOB,is_vectorized INTEGER,start_time TEXT,end_time TEXT,updated_at TEXT);
          INSERT INTO bequests VALUES('old','旧经验','','speech','',NULL,0,'','','');
          CREATE TABLE memory_usage_events(kind TEXT,item_id TEXT,event_type TEXT);
          INSERT INTO memory_usage_events VALUES('experience','old','vector_returned'),('experience','old','injected');
          CREATE TABLE maintenance_reviews(kind TEXT,item_id TEXT,reviewed_at REAL);
          INSERT INTO maintenance_reviews VALUES('experience','old',123.0),('experience','old',456.0);
          CREATE TABLE maintenance_requests(request_id TEXT);
          CREATE TABLE growth_actions(action_id TEXT,effect_applied INTEGER);
          INSERT INTO growth_actions VALUES('old-unapplied',0);
        """)
        conn.close()
        db = dbmod.MemoryDatabase(str(path))
        try:
            connection = await db._ensure_connection()
            row = await (await connection.execute("SELECT * FROM bequests WHERE bequest_id='old'")).fetchone()
            self.assertEqual((row["retrieval_count"], row["manual_maintenance_count"], row["last_maintained_at"]), (1, 2, 456))
            await db._migrate_bequests_table()
            row = await (await connection.execute("SELECT * FROM bequests WHERE bequest_id='old'")).fetchone()
            self.assertEqual(row["manual_maintenance_count"], 2)
            tables = {r["name"] for r in await (await connection.execute("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
            self.assertIn("retired_growth_actions", tables)
            self.assertNotIn("growth_actions", tables)
            self.assertEqual(self.feedback.snapshot()["competence_value"], 50)
        finally:
            await db.close()


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        logging.shutdown()
        _sandbox.cleanup()
