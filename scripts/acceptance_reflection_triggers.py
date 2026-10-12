"""验证两个低值 truck 的单向触发与空闲调度；全部使用临时数据和模型替身。"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_sandbox = tempfile.TemporaryDirectory(prefix="abyss-reflection-trigger-")
os.environ["ABYSS_DATA_DIR"] = _sandbox.name
_personas = Path(_sandbox.name) / "brain" / "base" / "persona"
_personas.mkdir(parents=True)
for _name in ("common", "normal"):
    (_personas / f"{_name}.json").write_text(json.dumps({
        "persona_name": _name, "scene": "隔离测试", "kernel_prompt": "测试占位",
    }), encoding="utf-8")

from brain.amy.emotional_interface import reset_emotional_interface
from brain.amy.feedback_state import get_feedback_state
from brain.att.trucks.feedback_reflection_truck import ReflectionEfficacyTruck, ReflectionBelongingTruck
from brain.att.station import AttentionStation
from config.truck_settings import get_all_truck_configs, get_enabled_trucks


class ReflectionTriggerAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_emotional_interface()
        self.directory = Path(tempfile.mkdtemp(dir=_sandbox.name))
        self.feedback = get_feedback_state(self.directory)
        self.efficacy = ReflectionEfficacyTruck()
        self.belonging = ReflectionBelongingTruck()
        self.models = AsyncMock()
        notes = SimpleNamespace(get_all=AsyncMock(return_value=[]),
                                count_unprocessed_chars=AsyncMock(return_value=0))
        self.patches = [patch("brain.pfc.core.run_pfc_cycle", self.models),
                        patch("brain.hc.shortlight.notepad.get_shortlight_notepad", return_value=notes)]
        for mock_patch in self.patches:
            mock_patch.start()
            self.addCleanup(mock_patch.stop)

    async def test_strict_boundary_and_independent_axes(self):
        config = self.efficacy._get_config()
        self.assertEqual(config.LOW_THRESHOLD, 20)
        self.feedback.settle("测试边界", [("competence", "失败", -30, "测试")])
        self.assertEqual(await self.efficacy._check_trigger(config), "")
        self.feedback.settle("进入低值", [("competence", "失败", -1, "测试")])
        self.assertIn("效能感 19/100", await self.efficacy._check_trigger(config))
        self.assertEqual(await self.belonging._check_trigger(self.belonging._get_config()), "")
        await self.efficacy.get_truck_messages()
        self.models.assert_awaited_once()
        self.feedback.settle("差评", [("recognition", "明确坏评价", -31, "测试")])
        self.assertIn("归属感 19/100", await self.belonging._check_trigger(self.belonging._get_config()))

    async def test_low_value_remains_eligible_after_reflection(self):
        self.feedback.settle("失败", [("competence", "失败", -40, "测试")])
        before = self.feedback.snapshot()
        await self.efficacy.get_truck_messages()
        self.assertTrue(await self.efficacy._check_trigger(self.efficacy._get_config()))
        await self.efficacy.get_truck_messages()
        self.assertEqual(self.models.await_count, 2)
        self.assertEqual(self.feedback.snapshot(), before)
        self.assertFalse((self.directory / "reflection_efficacy_truck.json").exists())

    async def test_recovered_value_skips_queued_request(self):
        self.feedback.settle("失败", [("competence", "失败", -40, "测试")])
        self.assertTrue(await self.efficacy._check_trigger(self.efficacy._get_config()))
        self.feedback.settle("已成功", [("competence", "成功", 20, "测试")])
        await self.efficacy.get_truck_messages()
        self.models.assert_not_awaited()
        self.assertFalse((self.directory / "reflection_efficacy_truck.json").exists())

    async def test_disabled_request_skips(self):
        self.feedback.settle("失败", [("competence", "失败", -40, "测试")])
        config = SimpleNamespace(ENABLED=False, LOW_THRESHOLD=20)
        with patch.object(self.efficacy, "_get_config", return_value=config):
            await self.efficacy.get_truck_messages()
        self.models.assert_not_awaited()

    async def test_station_registration_pause_resume_and_web_controls(self):
        from brain.att.trucks import get_available_trucks
        from adapt.web.truck_control import get_state_snapshot
        station = AttentionStation()
        station.reflection_efficacy_truck = self.efficacy
        station.reflection_belonging_truck = self.belonging
        for truck in (self.efficacy, self.belonging):
            self.assertIn(truck.truck_id, get_available_trucks())
            self.assertIn(truck.truck_id, get_all_truck_configs())
            self.assertIn(truck.truck_id, get_enabled_trucks())
            self.assertIs(station._get_truck(truck.truck_id), truck)
            truck.state = "running"
        self.assertEqual(len(station._all_running_trucks()), 2)
        self.efficacy.pause()
        self.assertEqual(station._all_running_trucks(), [self.belonging])
        self.assertTrue(station.set_truck_enabled(self.efficacy.truck_id, True))
        self.assertEqual(self.efficacy.state, "paused")  # 开关操作不能绕过忙碌暂停
        self.efficacy.start()  # station 收尾恢复时使用同一路径
        self.assertEqual(self.efficacy.state, "running")
        self.assertIsNone(self.efficacy._task)  # 恢复既有 truck，不创建第二个循环
        ids = {entry["truck_id"] for entry in get_state_snapshot()}
        self.assertIn(self.efficacy.truck_id, ids)
        self.assertIn(self.belonging.truck_id, ids)
        station.stop()
        self.assertEqual(self.efficacy.state, "stopped")
        self.assertEqual(self.belonging.state, "stopped")

    async def test_busy_pause_blocks_check_until_resume(self):
        self.feedback.settle("失败", [("competence", "失败", -40, "测试")])
        config = SimpleNamespace(ENABLED=True, LOW_THRESHOLD=20, CHECK_CYCLE_TIME=0.01)
        notification = AsyncMock()
        with patch.object(self.efficacy, "_get_config", return_value=config), \
             patch.object(self.efficacy, "_check_trigger", wraps=self.efficacy._check_trigger) as check, \
             patch("brain.att.station.station_input", notification):
            self.efficacy.start()
            self.efficacy.pause()
            try:
                await asyncio.sleep(0.03)
                check.assert_not_awaited()
                notification.assert_not_awaited()
                self.efficacy.start()
                for _ in range(20):
                    if notification.await_count:
                        break
                    await asyncio.sleep(0.01)
                notification.assert_awaited_once()
                check.assert_awaited_once()
                self.models.assert_not_awaited()  # 仅入队，模型运行归 station 调度
            finally:
                self.efficacy.stop()
                await asyncio.gather(self.efficacy._task, return_exceptions=True)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        logging.shutdown()
        _sandbox.cleanup()
