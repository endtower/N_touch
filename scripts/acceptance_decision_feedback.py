"""隔离验收决策情感；不调用模型、不访问实际数据库。直接用项目 Python 运行。"""
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
_sandbox = tempfile.TemporaryDirectory(prefix="abyss-decision-feedback-")
os.environ["ABYSS_DATA_DIR"] = _sandbox.name
_persona_dir = Path(_sandbox.name) / "brain" / "base" / "persona"
_persona_dir.mkdir(parents=True)
for _name in ("common", "normal"):
    (_persona_dir / f"{_name}.json").write_text(json.dumps({
        "persona_name": _name, "scene": "隔离测试", "kernel_prompt": "测试占位",
    }), encoding="utf-8")

from brain.amy.constants import FinalEvaluationAct, TaskResultAct
from brain.amy.feedback_state import FeedbackState
from brain.amy.emotional_interface import EmotionalInterface, reset_emotional_interface
from brain.amy.state_api import AmyState
from brain.pfc.pfca.pfca_tools import PFCA_tool_manager


class FeedbackAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_emotional_interface()
        self.directory = Path(tempfile.mkdtemp(dir=_sandbox.name))

    async def test_legacy_migration_and_reload(self):
        (self.directory / "feedback_state.json").write_text('{"value": 70}', encoding="utf-8")
        state = FeedbackState(self.directory)
        self.assertEqual(state.snapshot()["competence_value"], 50)
        self.assertEqual(state.read_value(), 70)
        state.settle("测试事件", [("competence", "成功", 20, "已达成承接目标")])
        again = FeedbackState(self.directory).snapshot()
        self.assertEqual((again["value"], again["competence_value"]), (70, 70))
        self.assertEqual(again["history"][0]["reason"], "已达成承接目标")

    async def test_clamp_history_and_independence(self):
        state = FeedbackState(self.directory)
        for i in range(6):
            state.settle(str(i), [("recognition", "无评价", -5, "没有可观察的评价")])
        snapshot = state.snapshot()
        self.assertEqual(len(snapshot["history"]), 5)
        self.assertEqual(snapshot["history"][0]["title"], "1")
        self.assertEqual(snapshot["competence_value"], 50)
        snapshot["history"].clear()
        self.assertEqual(len(state.snapshot()["history"]), 5)
        state.settle("大量成功", [("competence", "成功", 200, "测试上限")])
        state.settle("大量差评", [("recognition", "明确坏评价", -200, "测试下限")])
        self.assertEqual((state.snapshot()["competence_value"], state.read_value()), (100, 0))
        state.settle("上限不变", [("competence", "成功", 20, "测试")])
        self.assertEqual(state.snapshot()["competence_value"], 100)
        self.assertEqual(state.snapshot()["history"][-1]["title"], "上限不变")

    async def test_atomic_write_failure_rolls_back(self):
        state = FeedbackState(self.directory)
        state.save_to_file()
        original = state.persistence_file.read_bytes()
        with patch("brain.amy.feedback_state.os.replace", side_effect=OSError("测试写入失败")):
            with self.assertRaises(OSError):
                state.settle("失败写入", [("competence", "成功", 20, "测试")])
        self.assertEqual(state.snapshot()["competence_value"], 50)
        self.assertEqual(state.snapshot()["history"], [])
        self.assertEqual(state.persistence_file.read_bytes(), original)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    async def test_labels_and_aliases(self):
        self.assertEqual([FinalEvaluationAct.feedback_delta(name) for name in FinalEvaluationAct.names()],
                         [20, 10, -10, -20, -5])
        self.assertEqual(FinalEvaluationAct.feedback_delta("未得到他人评价与反馈"), -5)
        self.assertTrue(FinalEvaluationAct.contains("被他人评价欠佳/不认可"))
        self.assertEqual(TaskResultAct.deltas, {"成功": 20, "失败": -20, "纯闲聊或结果不明": 0})
        schema = next(tool["function"]["parameters"] for tool in PFCA_tool_manager.get_tools()
                      if tool["function"]["name"] == "terminate")
        self.assertIn("task_result", schema["required"])
        self.assertEqual(schema["properties"]["final_evaluation"]["enum"], FinalEvaluationAct.names())

    async def test_nonfinite_data_is_rejected_without_overwrite(self):
        document = self.directory / "feedback_state.json"
        document.write_text('{"value": NaN}', encoding="utf-8")
        with self.assertRaises(ValueError):
            FeedbackState(self.directory)
        self.assertEqual(document.read_text(encoding="utf-8"), '{"value": NaN}')

    async def test_web_state_and_failure_validation(self):
        import adapt.web.amy_api as api
        ei = EmotionalInterface(self.directory)
        with patch.object(api, "_try_get_ei", AsyncMock(return_value=ei)):
            response = await api.handle_get_amy_state(None)
            self.assertEqual(response.status, 200)
            payload = json.loads(response.body)
            self.assertIn("presence", payload)
            self.assertEqual(payload["decision_feedback"]["competence_value"], 50)
            request = SimpleNamespace(json=AsyncMock(return_value={
                "title": "失败测试", "final_evaluation": "明确坏评价", "task_result": "失败",
            }))
            rejected = await api.handle_apply_amy_final_evaluation(request)
            self.assertEqual(rejected.status, 400)
            self.assertEqual(ei.feedback_state.snapshot()["history"], [])
            request.json.return_value["task_reason"] = "可能缺少经验，证据不足"
            applied = await api.handle_apply_amy_final_evaluation(request)
            self.assertEqual(applied.status, 200)
            self.assertEqual((ei.feedback_state.snapshot()["competence_value"], ei.feedback_state.read_value()), (30, 30))

    async def test_real_interface_isolates_expression_and_keeps_causes(self):
        ei = EmotionalInterface(self.directory)
        language_before = await ei.get_language_description()
        await ei.apply_final_evaluation("含蓄好评价", "邀请未成", "失败", "策略时机可能不合适", "对方感谢关心，但拒绝邀请")
        self.assertEqual((ei.feedback_state.snapshot()["competence_value"], ei.feedback_state.read_value()), (30, 60))
        prompt = await ei.get_decision_description()
        self.assertIn("效能感", prompt)
        self.assertIn("归属感", prompt)
        self.assertIn("邀请未成", prompt)
        self.assertEqual(await ei.get_reflection_description(), prompt)
        self.assertEqual(await ei.get_language_description(), language_before)
        self.assertTrue(any(FinalEvaluationAct.contains(event.get("act", ""))
                            for event in await ei.get_unread_events("reflection")))
        before = ei.feedback_state.snapshot()
        with self.assertRaises(ValueError):
            await ei.apply_final_evaluation("明确坏评价", "测试", "失败")
        self.assertEqual(ei.feedback_state.snapshot(), before)

    async def test_neutral_result_still_settles_social_feedback(self):
        ei = EmotionalInterface(self.directory)
        await ei.apply_final_evaluation("无评价", "普通闲聊")
        self.assertEqual((ei.feedback_state.snapshot()["competence_value"], ei.feedback_state.read_value()), (50, 45))
        self.assertEqual(len(ei.feedback_state.snapshot()["history"]), 1)

    async def test_snapshot_roundtrip_and_old_preset(self):
        state = FeedbackState(self.directory)
        state.settle("成功", [("competence", "成功", 20, "测试")])
        captured = await AmyState.capture_from(self.directory)
        document = self.directory / "preset.json"
        captured.to_json_file(document)
        loaded = await AmyState.from_json_file(document)
        target = self.directory / "restored"
        await loaded.restore(target)
        self.assertEqual(FeedbackState(target).snapshot(), state.snapshot())
        await reset_emotional_interface()
        document.write_text(json.dumps({"module": "amy", "state": {"thoughts": {}}}), encoding="utf-8")
        legacy = await AmyState.from_json_file(document)
        await legacy.restore(self.directory / "legacy")
        self.assertEqual(FeedbackState(self.directory / "legacy").snapshot()["competence_value"], 50)

    async def test_terminate_preview_then_single_settlement(self):
        import brain.hc.database.task as task_module
        import brain.hc.temporary_memory as memory_module
        import brain.hc.shortlight as notes_module
        import brain.pfc.pfca.pfca_core as core_module
        ei = EmotionalInterface(self.directory)
        focus = SimpleNamespace(_data={})
        tasks = SimpleNamespace(get_active_tasks=AsyncMock(return_value=[]))
        memory = SimpleNamespace(add=Mock())
        notes = SimpleNamespace(finalize_chain=AsyncMock())
        core = SimpleNamespace(stop=AsyncMock())
        tool = PFCA_tool_manager()
        arguments = dict(summary="尝试邀请未成功，对方仍感谢。时机可能不合适。", title="邀请测试",
                         final_evaluation="含蓄好评价", task_result="失败",
                         task_reason="可能是邀请时机不合适，尚未确认", evaluation_reason="对方感谢但拒绝邀请")
        with patch("brain.hc.global_session_state.get_global_session_state", return_value=focus), \
             patch.object(task_module, "TaskOperations", return_value=tasks), \
             patch.object(memory_module, "get_temporary_memory", return_value=memory), \
             patch.object(notes_module, "get_shortlight_notepad", return_value=notes), \
             patch.object(core_module, "get_pfca_instance", return_value=core), \
             patch("brain.amy.emotional_interface.get_emotional_interface", AsyncMock(return_value=ei)):
            invalid = dict(arguments, task_reason="")
            with self.assertRaises(ValueError):
                await tool.terminate(**invalid)
            self.assertEqual(focus._data, {})
            preview = await tool.terminate(**arguments)
            self.assertIn("效能感：50/100 → 30/100", preview)
            self.assertIn("归属感：50/100 → 60/100", preview)
            self.assertEqual(ei.feedback_state.snapshot()["history"], [])
            memory.add.assert_not_called()
            await tool.terminate(**arguments)
            self.assertEqual((ei.feedback_state.snapshot()["competence_value"], ei.feedback_state.read_value()), (30, 60))
            self.assertEqual(len(ei.feedback_state.snapshot()["history"]), 2)
            memory.add.assert_called_once()
            notes.finalize_chain.assert_awaited_once()
            core.stop.assert_awaited_once()
            self.assertNotIn("pfc_handoff", focus._data)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        logging.shutdown()
        _sandbox.cleanup()
