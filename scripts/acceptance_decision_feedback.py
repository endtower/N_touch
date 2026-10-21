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
        self.assertEqual(TaskResultAct.deltas, {"对方明确说自己有用/产生了实际价值/明确使对方收益": 10,
                                               "可能产生收益": 0, "结果不满": -5,
                                               "任务延期": -10, "产生负面收益": -20, "帮倒忙": -30})
        for old_label in ("成功", "失败", "纯闲聊或结果不明", "产生实际效益", "未产生实际效益",
                          "对主人产生了实际效益", "对自己带来了帮助、收获或体验改善", "结果未明"):
            with self.assertRaises(ValueError):
                TaskResultAct.validate(old_label)
        schema = next(tool["function"]["parameters"] for tool in PFCA_tool_manager.get_tools()
                      if tool["function"]["name"] == "terminate")
        self.assertIn("task_result", schema["required"])
        self.assertNotIn("task_reason", schema["properties"])
        self.assertEqual(schema["properties"]["task_result"]["enum"], TaskResultAct.names())
        self.assertEqual(schema["properties"]["final_evaluation"]["enum"], FinalEvaluationAct.names())

    async def test_nonfinite_data_is_rejected_without_overwrite(self):
        document = self.directory / "feedback_state.json"
        document.write_text('{"value": NaN}', encoding="utf-8")
        with self.assertRaises(ValueError):
            FeedbackState(self.directory)
        self.assertEqual(document.read_text(encoding="utf-8"), '{"value": NaN}')



    async def test_omitted_benefit_defaults_to_zero_and_keeps_competence(self):
        ei = EmotionalInterface(self.directory)
        await ei.apply_final_evaluation("无评价", "普通闲聊")
        self.assertEqual((ei.feedback_state.snapshot()["competence_value"], ei.feedback_state.read_value()), (50, 45))
        self.assertEqual(len(ei.feedback_state.snapshot()["history"]), 1)
        self.assertEqual(ei.feedback_state.snapshot()["task_results"][-1]["result"], TaskResultAct.DEFAULT)


    async def test_all_six_outcomes_settle_independently_and_reload(self):
        for result, delta in TaskResultAct.deltas.items():
            with self.subTest(result=result):
                await reset_emotional_interface()
                ei = EmotionalInterface(Path(tempfile.mkdtemp(dir=_sandbox.name)))
                await ei.apply_final_evaluation("无评价", "六档验收", result, failure_reason="已观察到的具体原因")
                state = FeedbackState(ei.feedback_state.persistence_file.parent).snapshot()
                self.assertEqual((state["competence_value"], state["value"]), (50 + delta, 45))
                self.assertEqual(state["task_results"][0]["result"], result)
                if delta:
                    self.assertEqual(state["history"][0]["delta"], delta)
                else:
                    # 0 档只记评价结果，不产生变动词条
                    self.assertEqual([e["axis"] for e in state["history"]], ["recognition"])


    async def test_later_benefit_does_not_retract_prior_unknown_or_clear_score_with_thought(self):
        ei = EmotionalInterface(self.directory)
        await ei.apply_final_evaluation("无评价", "同一事项", TaskResultAct.DISSATISFIED, failure_reason="结果不满")
        await ei.apply_final_evaluation("无评价", "同一事项", TaskResultAct.BENEFIT)
        state = ei.feedback_state.snapshot()
        self.assertEqual(state["competence_value"], 55)
        self.assertEqual([r["result"] for r in state["task_results"]], [TaskResultAct.DISSATISFIED, TaskResultAct.BENEFIT])
        ei.thought_cache.remove_thoughts_by_act("无评价")
        self.assertEqual(ei.feedback_state.snapshot(), state)
        for i in range(6):
            ei.feedback_state.settle(f"后续{i}", [("competence", TaskResultAct.DISSATISFIED, -5, "结果不满")], task_result=TaskResultAct.DISSATISFIED)
        self.assertEqual(ei.feedback_state.snapshot()["competence_value"], 25)
        self.assertEqual(len(ei.feedback_state.snapshot()["history"]), 7)

    async def test_legacy_benefit_history_is_retained_without_reclassification(self):
        original = {"value": 60, "competence_value": 70, "history": [
            {"axis": "competence", "title": "旧收益", "label": "产生实际效益", "delta": 20,
             "before": 50, "after": 70, "reason": "旧依据", "time": 1}]}
        (self.directory / "feedback_state.json").write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
        state = FeedbackState(self.directory)
        state.save_to_file()
        loaded = FeedbackState(self.directory).snapshot()
        self.assertEqual((loaded["competence_value"], loaded["value"]), (70, 60))
        self.assertEqual(loaded["history"], original["history"])
        self.assertEqual(loaded["task_results"][0]["result"], "产生实际效益")

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



if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        logging.shutdown()
        _sandbox.cleanup()
