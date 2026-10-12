"""隔离验收反思发布验证与效能感补偿；模型、任务数据库、真实服务均使用替身。"""
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_sandbox = tempfile.TemporaryDirectory(prefix="abyss-reflection-compensation-")
os.environ["ABYSS_DATA_DIR"] = _sandbox.name
_personas = Path(_sandbox.name) / "brain" / "base" / "persona"
_personas.mkdir(parents=True)
for _name in ("common", "normal"):
    (_personas / f"{_name}.json").write_text(json.dumps({
        "persona_name": _name, "scene": "隔离测试", "kernel_prompt": "测试占位",
    }), encoding="utf-8")

from brain.amy.emotional_interface import get_emotional_interface, reset_emotional_interface
from brain.amy.feedback_state import FeedbackState
from brain.base.common_ways.tool_task import ToolTask
from brain.base.light_agent.quest_agent import QuestAgent
from brain.pfc.reflection.reflection_tools import Reflection_tool_manager
from brain.pfc.reflection.reflection_session import ReflectionSession


def _quest_result(created_tasks=None, conclusion="任务已安排"):
    return {"formatted_output": conclusion, "created_tasks": created_tasks or []}


class ReflectionCompensationAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_emotional_interface()
        self.directory = Path(tempfile.mkdtemp(dir=_sandbox.name))
        self.ei = await get_emotional_interface(self.directory)
        self.manager = Reflection_tool_manager()
        # quest_agent 入口现为 (title, context, next_occurrence, purpose_type=...)
        self.later = "2026-12-31T12:00:00"
        self.focus = SimpleNamespace(_data={}, set_activity=Mock(), manager_pfca_actions=Mock())
        self.core = SimpleNamespace(stop=AsyncMock())
        for target, replacement in (
            ("brain.pfc.reflection.reflection_tools.get_global_session_state", self.focus),
            ("brain.hc.global_session_state.get_global_session_state", self.focus),
            ("brain.pfc.reflection.reflection_core.get_reflection_instance", self.core),
        ):
            mock_patch = patch(target, return_value=replacement)
            mock_patch.start()
            self.addCleanup(mock_patch.stop)

    async def test_schema_is_reflection_only(self):
        schemas = {s["function"]["name"]: s["function"] for s in self.manager.get_tools()}
        for name in ("quest_agent", "finalize_reflection"):
            prop = schemas[name]["parameters"]["properties"]["purpose_type"]
            expected = ["普通事务", "自修正验证", "主动社交"]
            if name == "quest_agent":
                expected.append("关系建设")
            self.assertEqual(prop["enum"], expected)
            self.assertEqual(prop["default"], "普通事务")
        self.assertNotIn("purpose_type", ToolTask.get_quest_agent_tool_schema()["function"]["parameters"]["properties"])

    async def test_quest_registration_updates_state_and_tool_description(self):
        receipt = {"task_id": "test-1", "title": "择时验证新话术"}
        with patch.object(ToolTask, "_run_quest_request", AsyncMock(return_value=_quest_result([receipt]))) as run:
            result = await self.manager.quest_agent("调整了开场话术", "明天尝试", self.later, "自修正验证")
        self.assertTrue(result["verification_registered"])
        self.assertTrue(result["compensation_applied"])
        self.assertIn("效能感：80/100", result["current_emotion"])
        run.assert_awaited_once_with("调整了开场话术", "【用途：自修正验证】\n明天尝试", self.later)
        state = FeedbackState(self.directory).snapshot()
        self.assertEqual(state["competence_value"], 80)
        self.assertEqual(state["value"], 50)
        self.assertEqual(len(state["history"]), 1)
        self.assertIn("择时验证新话术", state["history"][0]["reason"])
        self.assertNotIn("最近做成", result["current_emotion"])

    async def test_claimed_registration_and_old_tool_history_do_not_pay(self):
        before = self.ei.feedback_state.snapshot()
        result = _quest_result(conclusion="我已经创建验证任务了")
        result["tools_called"] = [{"tool": "create_task", "result": {"task_id": "old"}}]
        with patch.object(ToolTask, "_run_quest_request", AsyncMock(return_value=result)):
            response = await self.manager.quest_agent("请择时验证", "请择时验证", self.later, "自修正验证")
        self.assertFalse(response["verification_registered"])
        self.assertFalse(response["compensation_applied"])
        self.assertEqual(self.ei.feedback_state.snapshot(), before)

    async def test_quest_failure_does_not_pay(self):
        before = self.ei.feedback_state.snapshot()
        with patch.object(ToolTask, "_run_quest_request", AsyncMock(side_effect=RuntimeError("执行失败"))):
            with self.assertRaises(RuntimeError):
                await self.manager.quest_agent("择时验证", "择时验证", self.later, "自修正验证")
        self.assertEqual(self.ei.feedback_state.snapshot(), before)
        self.assertFalse(self.manager._verification_compensated)

    async def test_ordinary_tools_keep_behavior_and_do_not_pay(self):
        before = self.ei.feedback_state.snapshot()
        with patch.object(ToolTask, "quest_agent", AsyncMock(return_value="普通任务已登记")) as run:
            self.assertEqual(
                await self.manager.quest_agent("安排普通事务", "安排普通事务", self.later),
                "普通任务已登记",
            )
        run.assert_awaited_once_with("安排普通事务", "安排普通事务", self.later)
        preview = await self.manager.finalize_reflection(True, "普通行动")
        self.assertTrue(preview["confirmation_required"])
        self.core.stop.assert_not_awaited()
        self.assertNotIn("pfc_handoff", self.focus._data)
        self.assertEqual(await self.manager.finalize_reflection(), "反思已结束。")
        self.assertEqual(self.focus._data["pfc_handoff"]["purpose"], "普通行动")
        self.assertEqual(self.ei.feedback_state.snapshot(), before)

    async def test_immediate_verification_clamps_and_preserves_other_emotions(self):
        self.ei.feedback_state.settle("准备边界", [("competence", "测试", 40, "测试")])
        mood_before = await self.ei.get_mood_snapshot()
        thoughts_before = self.ei.thought_cache.persistence_file.read_bytes()
        debt_before = self.ei.debt_ledger.persistence_file.read_bytes() if self.ei.debt_ledger.persistence_file.exists() else None
        before = self.ei.feedback_state.snapshot()
        preview = await self.manager.finalize_reflection(True, "立即试试调整后的表达", "自修正验证")
        self.assertTrue(preview["confirmation_required"])
        self.assertEqual(self.ei.feedback_state.snapshot(), before)
        self.assertNotIn("pfc_handoff", self.focus._data)
        self.core.stop.assert_not_awaited()
        response = await self.manager.finalize_reflection()
        self.assertTrue(response["compensation_applied"])
        self.core.stop.assert_awaited_once_with(exit_reason="reflection_done")
        self.assertEqual(self.focus._data["pfc_handoff"], {"state": "action", "purpose": "立即试试调整后的表达"})
        state = self.ei.feedback_state.snapshot()
        self.assertEqual(state["competence_value"], 100)
        self.assertEqual(state["value"], 50)
        self.assertEqual(state["history"][-1]["delta"], 30)
        self.assertEqual(state["history"][-1]["before"], 90)
        self.assertEqual(state["history"][-1]["after"], 100)
        mood_after = await self.ei.get_mood_snapshot()
        self.assertEqual((mood_before.norepinephrine, mood_before.dopamine),
                         (mood_after.norepinephrine, mood_after.dopamine))
        self.assertEqual(self.ei.thought_cache.persistence_file.read_bytes(), thoughts_before)
        self.assertEqual(self.ei.debt_ledger.persistence_file.read_bytes() if self.ei.debt_ledger.persistence_file.exists() else None, debt_before)

    async def test_both_routes_and_multiple_tasks_pay_once_per_session(self):
        receipt = [{"task_id": "1", "title": "验证一"}, {"task_id": "2", "title": "验证二"}]
        with patch.object(ToolTask, "_run_quest_request", AsyncMock(return_value=_quest_result(receipt))):
            first = await self.manager.quest_agent("择时尝试", "择时尝试", self.later, "自修正验证")
            repeated = await self.manager.quest_agent("再登记一次", "再登记一次", self.later, "自修正验证")
        await self.manager.finalize_reflection(True, "同时立即尝试", "自修正验证")
        immediate = await self.manager.finalize_reflection()
        self.assertTrue(first["compensation_applied"])
        self.assertFalse(repeated["compensation_applied"])
        self.assertFalse(immediate["compensation_applied"])
        self.assertEqual(self.ei.feedback_state.snapshot()["competence_value"], 80)
        self.assertEqual(len(self.ei.feedback_state.snapshot()["history"]), 1)
        with patch("brain.pfc.reflection.reflection_session.get_reflection_tool_manager", return_value=self.manager), \
             patch("brain.pfc.reflection.reflection_session.get_reflection_history_manager"), \
             patch("brain.pfc.reflection.reflection_session.get_reflection_input_manager"):
            session = ReflectionSession()
        self.assertIs(session.tool_manager, self.manager)
        preview = await self.manager.finalize_reflection(True, "新一轮尝试", "自修正验证")
        self.assertTrue(preview["confirmation_required"])
        self.core.stop.reset_mock()
        second = await self.manager.finalize_reflection()
        self.core.stop.assert_awaited_once()
        self.assertTrue(second["compensation_applied"])
        self.assertEqual(len(self.ei.feedback_state.snapshot()["history"]), 2)

    async def test_invalid_arguments_have_no_side_effects(self):
        with patch.object(ToolTask, "_run_quest_request", AsyncMock()) as run:
            for purpose in ("随意加分", None, []):
                with self.assertRaises(ValueError):
                    await self.manager.quest_agent("请求", "请求", self.later, purpose)
            run.assert_not_awaited()
        for enter, content, purpose in (
            (False, "", "自修正验证"), (True, "", "自修正验证"),
            ("true", "尝试", "自修正验证"), (True, "尝试", "未知"),
        ):
            with self.assertRaises(ValueError):
                await self.manager.finalize_reflection(enter, content, purpose)
        self.core.stop.assert_not_awaited()
        self.assertEqual(self.focus._data, {})
        self.assertEqual(self.ei.feedback_state.snapshot()["competence_value"], 50)

    async def test_persistence_failure_rolls_back_handoff_and_keeps_eligibility(self):
        before = self.ei.feedback_state.snapshot()
        await self.manager.finalize_reflection(True, "立即验证", "自修正验证")
        with patch.object(self.ei.feedback_state, "save_to_file", side_effect=OSError("测试写入失败")):
            with self.assertRaises(OSError):
                await self.manager.finalize_reflection()
        self.assertEqual(self.ei.feedback_state.snapshot(), before)
        self.assertNotIn("pfc_handoff", self.focus._data)
        self.core.stop.assert_not_awaited()
        self.assertFalse(self.manager._verification_compensated)
        response = await self.manager.finalize_reflection(True, "重试立即验证", "自修正验证")
        self.assertTrue(response["compensation_applied"])

    async def test_real_dispatch_returns_updated_description(self):
        response = await self.manager.handle_tool_call({
            "id": "validation-call", "type": "function", "function": {
                "name": "finalize_reflection", "arguments": json.dumps({
                    "enter_action": True, "action_purpose": "立即验证新方法",
                    "purpose_type": "自修正验证",
                }, ensure_ascii=False),
            },
        })
        self.assertEqual(response["tool_call_id"], "validation-call")
        self.assertTrue(json.loads(response["content"])["confirmation_required"])
        self.core.stop.assert_not_awaited()
        response = await self.manager.handle_tool_call({
            "id": "confirmation-call", "type": "function", "function": {
                "name": "finalize_reflection", "arguments": "{}",
            },
        })
        self.assertEqual(response["tool_call_id"], "confirmation-call")
        result = json.loads(response["content"])
        self.assertTrue(result["compensation_applied"])
        self.assertIn("效能感：80/100", result["current_emotion"])

    async def test_low_values_warn_once_and_second_call_closes_without_verification(self):
        self.ei.feedback_state.settle("失败与差评", [
            ("competence", "失败", -40, "测试"), ("recognition", "差评", -40, "测试"),
        ])
        before = self.ei.feedback_state.snapshot()
        preview = await self.manager.finalize_reflection()
        self.assertIn("本轮尚未安排自修正验证", preview["message"])
        self.assertIn("效能感 10/100（低于 20）", preview["message"])
        self.assertIn("归属感 10/100（低于 20）", preview["message"])
        self.assertIn("系统恢复空闲后会再次启动反思", preview["message"])
        self.core.stop.assert_not_awaited()
        self.assertEqual(await self.manager.finalize_reflection(), "反思已结束。")
        self.core.stop.assert_awaited_once_with(exit_reason="reflection_done")
        self.assertEqual(self.ei.feedback_state.snapshot(), before)
        self.assertNotIn("pfc_handoff", self.focus._data)

    async def test_preview_matches_threshold_and_enabled_settings(self):
        self.ei.feedback_state.settle("边界", [
            ("competence", "测试", -30, "测试"), ("recognition", "测试", -30, "测试"),
        ])
        preview = await self.manager.finalize_reflection()
        self.assertIn("当前未满足已启用的情感低值触发条件", preview["message"])
        self.manager._begin_session()
        disabled = SimpleNamespace(ENABLED=False, LOW_THRESHOLD=30)
        with patch("config.truck_settings.get_truck_config", return_value=disabled):
            preview = await self.manager.finalize_reflection()
        self.assertIn("当前未满足已启用的情感低值触发条件", preview["message"])

    async def test_second_call_can_choose_immediate_verification_after_warning(self):
        self.ei.feedback_state.settle("失败", [("competence", "失败", -40, "测试")])
        preview = await self.manager.finalize_reflection()
        self.assertIn("本轮尚未安排", preview["message"])
        result = await self.manager.finalize_reflection(True, "尝试调整后的表达", "自修正验证")
        self.assertTrue(result["compensation_applied"])
        self.assertEqual(self.ei.feedback_state.snapshot()["competence_value"], 40)
        self.core.stop.assert_awaited_once()

    async def test_second_call_can_cancel_previewed_action(self):
        before = self.ei.feedback_state.snapshot()
        await self.manager.finalize_reflection(True, "预备立即验证", "自修正验证")
        self.assertEqual(await self.manager.finalize_reflection(enter_action=False), "反思已结束。")
        self.core.stop.assert_awaited_once()
        self.assertNotIn("pfc_handoff", self.focus._data)
        self.assertEqual(self.ei.feedback_state.snapshot(), before)

    async def test_quest_validation_between_confirmations_pays_once_and_allows_close(self):
        self.ei.feedback_state.settle("失败与差评", [
            ("competence", "失败", -40, "测试"), ("recognition", "差评", -40, "测试"),
        ])
        await self.manager.finalize_reflection()
        receipt = [{"task_id": "later-1", "title": "明天验证"}]
        with patch.object(ToolTask, "_run_quest_request", AsyncMock(return_value=_quest_result(receipt))):
            result = await self.manager.quest_agent("明天验证修正", "明天验证修正", self.later, "自修正验证")
        self.assertTrue(result["compensation_applied"])
        self.assertEqual(await self.manager.finalize_reflection(), "反思已结束。")
        self.core.stop.assert_awaited_once()
        state = self.ei.feedback_state.snapshot()
        self.assertEqual((state["competence_value"], state["value"]), (40, 10))
        self.assertEqual(sum(e["label"] == "已安排自修正验证" for e in state["history"]), 1)

    async def test_registered_quest_validation_does_not_warn_as_missing(self):
        self.ei.feedback_state.settle("差评", [("recognition", "差评", -40, "测试")])
        with patch.object(ToolTask, "_run_quest_request", AsyncMock(return_value=_quest_result([
            {"task_id": "registered-1", "title": "验证"},
        ]))):
            await self.manager.quest_agent("验证修正", "验证修正", self.later, "自修正验证")
        preview = await self.manager.finalize_reflection()
        self.assertIn("本轮已经成功安排自修正验证", preview["message"])
        self.assertNotIn("本轮尚未安排自修正验证", preview["message"])
        self.assertIn("不直接恢复归属感", preview["message"])

    async def test_common_quest_tool_preserves_text_contract(self):
        agent = SimpleNamespace(run=AsyncMock(return_value=_quest_result([{"task_id": "1"}], "原结论")))
        with patch.object(ToolTask, "_get_quest_agent", return_value=agent):
            self.assertEqual(
                await ToolTask.quest_agent("普通请求", "普通请求上下文", self.later),
                "原结论",
            )
        agent.run.assert_awaited_once_with(
            title="普通请求", context="普通请求上下文", next_occurrence=self.later,
        )

    async def test_social_quest_compensation_updates_belonging_and_shows_real_reason(self):
        self.ei.feedback_state.settle("缺少回应", [("recognition", "测试", -40, "测试")])
        thoughts_before = self.ei.thought_cache.persistence_file.read_bytes()
        debt_before = self.ei.debt_ledger.persistence_file.read_bytes() if self.ei.debt_ledger.persistence_file.exists() else None
        receipt = [{"task_id": "social-1", "title": "找主人聊聊今天的见闻"}]
        with patch.object(ToolTask, "_run_quest_request", AsyncMock(return_value=_quest_result(receipt))) as run:
            result = await self.manager.quest_agent("找主人聊聊今天的见闻", "找主人聊聊今天的见闻", self.later, "主动社交")
        run.assert_awaited_once_with("找主人聊聊今天的见闻", "【用途：主动社交】\n找主人聊聊今天的见闻", self.later)
        self.assertTrue(result["social_registered"])
        self.assertTrue(result["compensation_applied"])
        state = FeedbackState(self.directory).snapshot()
        self.assertEqual((state["competence_value"], state["value"]), (50, 40))
        self.assertEqual(state["history"][-1]["axis"], "recognition")
        self.assertEqual(state["history"][-1]["label"], "已安排主动社交")
        self.assertIn("归属感：40/100。对自己与他人的联系还有些不安，心里有些空落。", result["current_emotion"])
        self.assertIn("安排主动社交 已安排主动社交 +30", result["current_emotion"])
        self.assertIn("找主人聊聊今天的见闻", state["history"][-1]["reason"])
        self.assertEqual(self.ei.thought_cache.persistence_file.read_bytes(), thoughts_before)
        self.assertEqual(self.ei.debt_ledger.persistence_file.read_bytes() if self.ei.debt_ledger.persistence_file.exists() else None, debt_before)

    async def test_social_immediate_action_previews_then_compensates_and_clamps(self):
        self.ei.feedback_state.settle("测试上限", [("recognition", "测试", 40, "测试")])
        preview = await self.manager.finalize_reflection(True, "联系主人，请他夸夸", "主动社交")
        self.assertIn("二次确认后将安排 PFCA 主动社交", preview["message"])
        self.assertEqual(self.ei.feedback_state.snapshot()["value"], 90)
        self.core.stop.assert_not_awaited()
        self.assertNotIn("pfc_handoff", self.focus._data)
        result = await self.manager.finalize_reflection()
        self.assertTrue(result["compensation_applied"])
        self.assertIn("主动社交", result["message"])
        state = self.ei.feedback_state.snapshot()
        self.assertEqual((state["competence_value"], state["value"]), (50, 100))
        self.assertEqual(state["history"][-1]["delta"], 30)
        self.core.stop.assert_awaited_once()

    async def test_social_failure_or_consultation_does_not_pay(self):
        before = self.ei.feedback_state.snapshot()
        with patch.object(ToolTask, "_run_quest_request", AsyncMock(return_value=_quest_result(conclusion="不登记"))):
            result = await self.manager.quest_agent("请安排社交", "请安排社交", self.later, "主动社交")
        self.assertFalse(result["social_registered"])
        self.assertFalse(result["compensation_applied"])
        with patch.object(ToolTask, "_run_quest_request", AsyncMock(side_effect=RuntimeError("登记失败"))):
            with self.assertRaises(RuntimeError):
                await self.manager.quest_agent("请安排社交", "请安排社交", self.later, "主动社交")
        self.assertEqual(self.ei.feedback_state.snapshot(), before)
        self.assertFalse(self.manager._social_compensated)

    async def test_social_routes_pay_once_and_axes_remain_independent(self):
        receipt = [{"task_id": "social-once", "title": "社交"}]
        with patch.object(ToolTask, "_run_quest_request", AsyncMock(return_value=_quest_result(receipt))):
            first = await self.manager.quest_agent("主动社交", "主动社交", self.later, "主动社交")
            repeated = await self.manager.quest_agent("再次社交", "再次社交", self.later, "主动社交")
            verification = await self.manager.quest_agent("验证新方法", "验证新方法", self.later, "自修正验证")
        await self.manager.finalize_reflection(True, "立即社交", "主动社交")
        immediate = await self.manager.finalize_reflection()
        self.assertTrue(first["compensation_applied"])
        self.assertFalse(repeated["compensation_applied"])
        self.assertFalse(immediate["compensation_applied"])
        self.assertTrue(verification["compensation_applied"])
        state = self.ei.feedback_state.snapshot()
        self.assertEqual((state["competence_value"], state["value"]), (80, 80))
        self.assertEqual(len(state["history"]), 2)
        self.manager._begin_session()
        self.assertFalse(self.manager._social_registered)
        with patch.object(ToolTask, "_run_quest_request", AsyncMock(return_value=_quest_result(receipt))):
            next_round = await self.manager.quest_agent("新一轮社交", "新一轮社交", self.later, "主动社交")
        self.assertTrue(next_round["compensation_applied"])

    async def test_low_belonging_preview_and_registered_social_display(self):
        self.ei.feedback_state.settle("缺少回应", [("recognition", "测试", -40, "测试")])
        preview = await self.manager.finalize_reflection()
        self.assertIn("归属感仍低且尚未安排主动社交", preview["message"])
        self.assertIn("只整理经验不会获得归属感补偿", preview["message"])
        self.manager._begin_session()
        with patch.object(ToolTask, "_run_quest_request", AsyncMock(return_value=_quest_result([
            {"task_id": "social-display", "title": "寻求联系"},
        ]))):
            await self.manager.quest_agent("寻求联系", "寻求联系", self.later, "主动社交")
        preview = await self.manager.finalize_reflection()
        self.assertIn("本轮已经成功安排主动社交", preview["message"])
        self.assertIn("不代表已经得到他人的回应或认可", preview["message"])
        self.assertNotIn("归属感仍低且尚未安排主动社交", preview["message"])

    async def test_social_persistence_failure_rolls_back_and_can_retry(self):
        before = self.ei.feedback_state.snapshot()
        await self.manager.finalize_reflection(True, "联系主人", "主动社交")
        with patch.object(self.ei.feedback_state, "save_to_file", side_effect=OSError("测试保存失败")):
            with self.assertRaises(OSError):
                await self.manager.finalize_reflection()
        self.assertEqual(self.ei.feedback_state.snapshot(), before)
        self.assertNotIn("pfc_handoff", self.focus._data)
        self.assertFalse(self.manager._social_compensated)
        self.core.stop.assert_not_awaited()
        result = await self.manager.finalize_reflection()
        self.assertTrue(result["compensation_applied"])
        self.assertEqual(self.ei.feedback_state.snapshot()["value"], 80)

    async def test_invalid_compensation_axis_and_social_exit_have_no_side_effects(self):
        before = self.ei.feedback_state.snapshot()
        with self.assertRaises(ValueError):
            await self.ei.apply_reflection_compensation("测试", "测试", axis="unknown")
        with self.assertRaises(ValueError):
            await self.manager.finalize_reflection(False, "社交", "主动社交")
        self.assertEqual(self.ei.feedback_state.snapshot(), before)
        self.core.stop.assert_not_awaited()

    async def test_real_quest_run_receipts_are_successful_and_request_scoped(self):
        agent = object.__new__(QuestAgent)
        agent._messages, agent._tools_called = [], []
        agent._final_text = ""
        agent._history = None
        agent._push = lambda message: agent._messages.append(message)
        agent.get_system_prompt = Mock(return_value="隔离提示词")
        agent._serialize_assistant_message = Mock(return_value={"role": "assistant", "content": "调用创建"})
        receipt = {
            "task_id": "new-1", "title": "验证任务",
            "context": "上下文", "next_occurrence": "2026-12-31T12:00:00",
        }
        agent.tool_functions = {"create_task": AsyncMock(return_value=receipt)}
        call = SimpleNamespace(id="create-1", function=SimpleNamespace(name="create_task", arguments="{}"))
        def reply(calls):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="完成", tool_calls=calls))])
        agent._call_llm = AsyncMock(side_effect=[reply([call]), reply([])])
        with patch("brain.base.light_agent.quest_agent.new_light_history_manager", return_value=Mock()), \
             patch("brain.hc.database.task.TaskOperations.format_active_task_overview",
                   AsyncMock(return_value="无")), \
             patch("utils.chain_context.get_current_chain_id", return_value=None):
            first = await agent.run("创建验证任务", "上下文", "2026-12-31T12:00:00")
            second = await agent.run("只咨询", "上下文", "2026-12-31T12:00:00")
        self.assertEqual(first["created_tasks"], [receipt])
        self.assertEqual(second["created_tasks"], [])
        self.assertEqual(second["tools_called"], [])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        logging.shutdown()
        _sandbox.cleanup()
