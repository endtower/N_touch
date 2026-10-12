"""人机共治隔离验收：临时 SQLite/人格/AMY，模拟模型和外部发送。"""
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
_sandbox = tempfile.TemporaryDirectory(prefix="abyss-maintenance-")
os.environ["ABYSS_DATA_DIR"] = _sandbox.name
_personas = Path(_sandbox.name) / "brain/base/persona"
_personas.mkdir(parents=True)
for name in ("common", "normal"):
    (_personas / f"{name}.json").write_text(json.dumps({"persona_name": name, "scene": "验收", "kernel_prompt": "初始正文"}), encoding="utf-8")

import brain.hc.maintenance as mmod
import brain.hc.database.core as dbmod
import brain.amy.feedback_state as fmod
import brain.base.persona.persona_registry as pmod
from brain.hc.database.decision_kit import DecisionKitOperations
from brain.hc.database.bequest import BequestOperations
from brain.pfc.reflection.reflection_tools import Reflection_tool_manager, PURPOSE_SOCIAL_GROWTH
from brain.base.common_ways.tool_maintenance import ToolMaintenance
from brain.att.trucks.feedback_reflection_truck import ReflectionEfficacyTruck, ReflectionBelongingTruck
from config import maintenance_settings as settings
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from adapt.web import maintenance_api as api
from adapt.web import decision_memory_api as kits_api
from utils.chain_context import set_chain_context, reset_chain_context


class MaintenanceAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(tempfile.mkdtemp(dir=_sandbox.name))
        self.db = dbmod.MemoryDatabase(str(self.root / "memory.db"))
        self.feedback = fmod.FeedbackState(self.root)
        personas = {name: pmod.PersonaDef(name, "验收", "初始正文") for name in ("common", "normal")}
        for persona in personas.values():
            pmod._write_persona_file(persona)
        self.patches = [patch.object(dbmod, "_memory_db_instance", self.db),
                        patch.object(fmod, "_feedback_state", self.feedback),
                        patch.object(pmod, "_PERSONAS", personas),
                        patch.object(mmod, "_write_lock", asyncio.Lock())]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.service = mmod.MaintenanceService()
        self.conn = await self.db._ensure_connection()
        await self.conn.execute("INSERT INTO bequests(bequest_id,title,summary,category) VALUES('exp1','说明求助背景','提供当前资源与缺口','speech')")
        await self.conn.commit()
        self.kit = await DecisionKitOperations().create({"advice": "先查手头资源", "dilemma": "不知如何联系", "category": "求助"})

    async def asyncTearDown(self):
        await self.db.close()

    def high_values(self):
        for n, result in enumerate(("成功", "失败", "成功")):
            self.feedback.settle(f"任务{n}", [("competence", result, 20 if result == "成功" else -20, "真实任务结果")], task_result=result)
        self.feedback.settle("达到高值", [("competence", "状态", 100, "测试"), ("recognition", "状态", 100, "测试")])

    async def request(self):
        return await self.service.request("如何说明求助背景", "两次成功依靠补足资源信息，希望主人校准适用范围", [{"kind": "experience", "item_id": "exp1"}])

    async def task(self, task_id="task1"):
        await self.conn.execute("INSERT INTO tasks(task_id,title,status) VALUES(?,?,'active')", (task_id, "跟进共同话题"))
        await self.conn.commit()
        return {"task_id": task_id, "title": "跟进共同话题"}

    async def test_human_review_tracks_exact_version_and_no_change_check(self):
        item = await self.service.item("experience", "exp1")
        await self.service.review("experience", "exp1", item["revision"])
        self.assertTrue((await self.service.metadata("experience"))[0]["maintenance"]["human_confirmed"])
        await self.conn.execute("UPDATE bequests SET summary='后续修改' WHERE bequest_id='exp1'")
        await self.conn.commit()
        meta = (await self.service.metadata("experience"))[0]["maintenance"]
        self.assertFalse(meta["human_confirmed"])
        self.assertTrue(meta["changed_since_review"])
        with self.assertRaises(ValueError):
            await self.service.review("experience", "exp1", item["revision"])

    async def test_usage_is_deduplicated_per_chain_version_and_type(self):
        token = set_chain_context("test-chain")
        try:
            for _ in range(2):
                await self.service.record_usage("experience", ["exp1"], "vector_returned")
            await self.service.record_usage("experience", ["exp1"], "injected")
        finally:
            reset_chain_context(token)
        item = (await self.service.metadata("experience"))[0]
        self.assertEqual(item["usage"]["vector_returned"]["count"], 1)
        self.assertEqual(item["usage"]["injected"]["count"], 1)
        await self.service.record_usage("experience", ["exp1"], "vector_returned", scope="another-chain")
        self.assertEqual((await self.service.metadata("experience"))[0]["usage"]["vector_returned"]["count"], 2)
        with self.assertRaises(ValueError):
            await self.service.record_usage("kit", [self.kit["kit_id"]], "vector_returned")

    async def test_exact_100_and_real_task_results_gate_maintenance(self):
        self.feedback.settle("补偿", [("competence", "已安排验证", 50, "补偿不是任务成功")])
        self.assertFalse((await self.service.eligibility())["maintenance"]["allowed"])
        self.high_values()
        self.feedback.settle("不足100", [("competence", "变化", -1, "测试")])
        self.assertFalse((await self.service.eligibility())["maintenance"]["allowed"])
        self.feedback.settle("达到100", [("competence", "变化", 1, "测试")])
        self.assertTrue((await self.service.eligibility())["maintenance"]["allowed"])

    async def test_maintenance_resets_only_competence_and_preserves_result_history(self):
        self.high_values()
        result = await self.request()
        self.assertEqual(result["status"], "registered")
        state = self.feedback.snapshot()
        self.assertEqual((state["competence_value"], state["value"]), (50, 100))
        self.assertEqual(len(state["task_results"]), 3)
        self.assertEqual(state["history"][-1]["delta"], -50)
        self.assertEqual((await self.request())["status"], "blocked")

    async def test_pending_request_blocks_even_after_values_recover(self):
        self.high_values(); await self.request()
        self.feedback.settle("再次达到100", [("competence", "成功", 50, "测试")])
        self.assertIn("已有", (await self.service.eligibility())["maintenance"]["reason"])

    async def test_cooldown_and_owner_deferral_survive_restart(self):
        self.high_values(); request = await self.request()
        await self.service.handle_request(request["request_id"], "deferred", "下周再看", defer_hours=168)
        self.feedback.settle("再次达到100", [("competence", "成功", 50, "测试")])
        self.assertFalse((await mmod.MaintenanceService().query())["eligibility"]["maintenance"]["allowed"])
        await self.service.handle_request(request["request_id"], "rejected", "先不维护")
        self.assertIn("间隔", (await self.service.eligibility())["maintenance"]["reason"])

    async def test_owner_unchanged_confirms_content_and_is_in_tool_query(self):
        self.high_values(); request = await self.request()
        await self.service.handle_request(request["request_id"], "unchanged", "现有建议已经够用")
        result = await ToolMaintenance.query_maintenance()
        self.assertEqual(result["requests"][0]["conclusion"], "现有建议已经够用")
        item = next(i for i in result["items"] if i["item_id"] == "exp1")
        self.assertTrue(item["maintenance"]["human_confirmed"])

    async def test_invalid_items_do_not_register_or_reset(self):
        self.high_values()
        with self.assertRaises(ValueError):
            await self.service.request("求助", "测试", [{"kind": "kit", "item_id": "missing"}])
        self.assertEqual(self.feedback.snapshot()["competence_value"], 100)
        self.assertEqual((await self.conn.execute_fetchall("SELECT * FROM maintenance_requests")), [])

    async def test_concurrent_requests_register_only_once(self):
        self.high_values()
        results = await asyncio.gather(self.request(), self.request())
        self.assertEqual(sorted(r["status"] for r in results), ["blocked", "registered"])
        self.assertEqual(len(self.feedback.snapshot()["applied_growth_actions"]), 1)

    async def test_old_pending_request_stays_visible_and_changed_content_is_not_confirmed(self):
        self.high_values()
        request = await self.request()
        await self.conn.execute("UPDATE maintenance_requests SET created_at=0 WHERE request_id=?", (request["request_id"],))
        await self.conn.executemany("INSERT INTO maintenance_requests VALUES (?,?,?,?,?,?,?,?,?)",
                                   [(f"done{i}", "旧专题", "已处理", "[]", "completed", i + 1, i + 1, 0, "") for i in range(60)])
        await self.conn.execute("UPDATE bequests SET summary='后续变化' WHERE bequest_id='exp1'")
        await self.conn.commit()
        self.assertIn(request["request_id"], [r["request_id"] for r in (await self.service.query())["requests"]])
        result = await self.service.handle_request(request["request_id"], "unchanged")
        self.assertEqual(result["unconfirmed_items"][0]["item_id"], "exp1")
        self.assertFalse((await self.service.metadata("experience"))[0]["maintenance"]["human_confirmed"])

    async def test_real_vector_and_prompt_hooks_count_without_changing_ranking(self):
        from brain.hc.database.core import serialize_vector
        from brain.base.common_ways.bequest_helpers import activate_bequests_for_input
        from brain.base.common_ways.tool_jinnang import ToolJinnang
        await self.conn.execute("UPDATE bequests SET title_vector=?,is_vectorized=1 WHERE bequest_id='exp1'", (serialize_vector([1.0, 0.0]),))
        await self.conn.commit()
        token = set_chain_context("usage-integration")
        try:
            with patch("brain.hc.database.bequest.get_vector_store", return_value=SimpleNamespace(generate_vector=AsyncMock(return_value=[1.0, 0.0]))):
                internal = await BequestOperations().search_bequest_by_vector("情境")
                self.assertEqual((await self.service.metadata("experience"))[0]["usage"], {})
                activated = await activate_bequests_for_input("情境")
                self.assertEqual([i["id"] for i in activated], [i[0] for i in internal])
                await activate_bequests_for_input("情境")
            usage = (await self.service.metadata("experience"))[0]["usage"]
            self.assertEqual(usage["vector_returned"]["count"], 1)
            self.assertEqual(usage["injected"]["count"], 1)
            with patch("brain.base.common_ways.tool_jinnang.search_observations", AsyncMock(return_value={"status": "disabled", "count": 0, "observations": [], "hint": "停用"})):
                kits = await ToolJinnang.query_jinnang("情境")
            self.assertEqual(kits["kits"][0]["kit_id"], self.kit["kit_id"])
            self.assertEqual((await self.service.metadata("kit"))[0]["usage"]["returned"]["count"], 1)
        finally:
            reset_chain_context(token)

    async def test_failed_reset_is_recovered_and_not_applied_twice(self):
        self.high_values()
        with patch.object(self.feedback, "save_to_file", side_effect=OSError("模拟磁盘失败")):
            with self.assertRaises(OSError):
                await self.request()
        self.assertEqual(self.feedback.snapshot()["competence_value"], 100)
        await self.service.query()
        self.assertEqual(self.feedback.snapshot()["competence_value"], 50)
        self.feedback.settle("新结果", [("competence", "成功", 20, "新任务结果")], task_result="成功")
        # 模拟重置已保存、数据库尚未标记完成时崩溃。
        await self.conn.execute("UPDATE growth_actions SET effect_applied=0")
        await self.conn.commit()
        await self.service.query()
        self.assertEqual(self.feedback.snapshot()["competence_value"], 70)
        reloaded = fmod.FeedbackState(self.root)
        self.assertEqual(reloaded.snapshot()["applied_growth_actions"], self.feedback.snapshot()["applied_growth_actions"])

    async def test_social_growth_checks_real_tasks_and_resets_only_recognition(self):
        self.high_values()
        self.assertFalse((await self.service.register_social_growth([], "没有登记"))["registered"])
        with self.assertRaises(ValueError):
            await self.service.register_social_growth([{"task_id": "missing"}], "测试")
        task = await self.task()
        result = await self.service.register_social_growth([task], "择时跟进共同话题")
        self.assertTrue(result["registered"])
        self.assertEqual((self.feedback.snapshot()["competence_value"], self.feedback.snapshot()["value"]), (100, 50))
        self.feedback.settle("新评价", [("recognition", "好评价", 20, "测试")])
        self.assertTrue((await self.service.register_social_growth([task], "重复"))["duplicate"])
        self.assertEqual(self.feedback.snapshot()["value"], 70)

    async def test_reflection_growth_has_no_plus30_and_failed_quest_has_no_reset(self):
        self.high_values()
        manager = Reflection_tool_manager()
        from brain.base.common_ways.tool_task import ToolTask
        later = "2026-12-31T12:00:00"
        with patch.object(ToolTask, "_run_quest_request", AsyncMock(return_value={"created_tasks": [], "formatted_output": "未登记"})):
            self.assertFalse((await manager.quest_agent("择时联系", "择时联系共同话题", later, purpose_type=PURPOSE_SOCIAL_GROWTH))["registered"])
        self.assertEqual(self.feedback.snapshot()["value"], 100)
        task = await self.task()
        with patch.object(ToolTask, "_run_quest_request", AsyncMock(return_value={"created_tasks": [task], "formatted_output": "已登记"})):
            self.assertTrue((await manager.quest_agent("择时联系", "择时联系共同话题", later, purpose_type=PURPOSE_SOCIAL_GROWTH))["registered"])
        self.assertEqual(self.feedback.snapshot()["value"], 50)
        self.assertNotIn("已安排主动社交", [e["label"] for e in self.feedback.snapshot()["history"]])
        with self.assertRaises(ValueError):
            await manager.finalize_reflection(enter_action=True, action_purpose="测试", purpose_type=PURPOSE_SOCIAL_GROWTH)

    async def test_high_trucks_recheck_gates_without_changing_values(self):
        config = SimpleNamespace(ENABLED=True, LOW_THRESHOLD=20)
        efficacy, belonging = ReflectionEfficacyTruck(), ReflectionBelongingTruck()
        self.high_values()
        self.assertIn("达到100", await efficacy._check_trigger(config, force=True))
        self.assertIn("达到100", await belonging._check_trigger(config, force=True))
        self.assertEqual(self.feedback.snapshot()["value"], 100)
        await self.request()
        self.assertEqual(await efficacy._check_trigger(config, force=True), "")

    async def test_human_persona_edit_keeps_llm_freeze_and_snapshot(self):
        changed = await pmod.update_persona_from_human("normal", {"kernel_prompt": "人工新正文"})
        self.assertEqual(changed.kernel_prompt, "人工新正文")
        self.assertTrue(list(pmod.PERSONA_HISTORY_DIR.glob("normal_*.json")))
        with self.assertRaises(ValueError):
            await pmod.set_persona_kernel_prompt("normal", "LLM改动")
        with patch.object(pmod, "persist_persona", AsyncMock(side_effect=OSError("模拟失败"))):
            with self.assertRaises(OSError):
                await pmod.update_persona_from_human("normal", {"kernel_prompt": "不应生效"})
        self.assertEqual(pmod.get_persona("normal").kernel_prompt, "人工新正文")

    async def test_http_human_edit_and_review_and_request_controls(self):
        app = web.Application()
        app.router.add_post('/memory/kits', kits_api.handle_create_kit)
        app.router.add_patch('/memory/personas/{persona_name}', api.handle_update_persona)
        app.router.add_post('/memory/maintenance/reviews', api.handle_review)
        app.router.add_patch('/memory/maintenance/requests/{request_id}', api.handle_maintenance_request)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post('/memory/kits', json={"advice": "建议", "dilemma": "困境", "category": "其他"})
            self.assertEqual(response.status, 201)
            created = (await response.json())["kit"]
            self.assertTrue(next(i for i in await self.service.metadata("kit") if i["item_id"] == created["kit_id"])["maintenance"]["human_confirmed"])
            response = await client.patch('/memory/personas/normal', json={"kernel_prompt": "网页人工修改"})
            self.assertEqual(response.status, 200)
            self.assertTrue((await self.service.metadata("persona"))[1]["maintenance"]["human_confirmed"])
            response = await client.post('/memory/maintenance/reviews', json={"kind": "experience", "item_id": "exp1", "revision": "outdated"})
            self.assertEqual(response.status, 400)
            self.high_values(); request = await self.request()
            response = await client.patch(f'/memory/maintenance/requests/{request["request_id"]}', json={"status": "deferred", "defer_hours": 12, "conclusion": "稍后"})
            self.assertEqual(response.status, 200)
        finally:
            await client.close()


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        unittest.main(verbosity=2)
    finally:
        logging.shutdown()
        _sandbox.cleanup()
