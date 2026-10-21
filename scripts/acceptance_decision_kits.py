"""锦囊与见闻录隔离验收：临时 SQLite/见闻文件，模拟向量，不调用真实模型。"""
import asyncio
import hashlib
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
_sandbox = tempfile.TemporaryDirectory(prefix="abyss-kits-")
os.environ["ABYSS_DATA_DIR"] = _sandbox.name
personas = Path(_sandbox.name) / "brain/base/persona"
personas.mkdir(parents=True)
for name in ("normal", "common"):
    (personas / f"{name}.json").write_text(json.dumps({"persona_name": name, "kernel_prompt": "测试", "scene": "测试"}), encoding="utf-8")

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import brain.hc.database.core as dbmod
import brain.hc.shortlight.notepad as nmod
import brain.hc.shortlight.observation_search as searchmod
from brain.hc.database.core import MemoryDatabase
from brain.hc.database.decision_kit import DecisionKitOperations
from brain.hc.shortlight.notepad import ShortlightNotepad, SLOT_EVENT
from brain.base.common_ways.tool_jinnang import ToolJinnang
from brain.pfc.pfca.pfca_tools import PFCA_tool_manager
from adapt.web.decision_memory_api import (
    handle_get_kits, handle_create_kit, handle_update_kit, handle_delete_kit,
    handle_get_observations, handle_search_observations,
    handle_create_observation, handle_update_observation, handle_delete_observation,
)
from adapt.web.memory_api import handle_create_social_memory, handle_create_experience, handle_get_social_memory, handle_get_experiences

DATA = {"advice": "先查询项目通讯录与历史记录，确认人物和可用渠道。", "dilemma": "缺少人物信息，不知道如何联系。", "category": "信息缺口"}


class DecisionKitAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(tempfile.mkdtemp(dir=_sandbox.name))
        self.db = MemoryDatabase(str(self.root / "memory.db"))
        for target, value in (("NOTES_DIR", self.root / "notes"),):
            mock = patch.object(nmod, target, value); mock.start(); self.addCleanup(mock.stop)
        self.notepad = ShortlightNotepad()
        for target, attribute, value in ((dbmod, "_memory_db_instance", self.db), (nmod, "_instance", self.notepad),
                                         (searchmod, "_search_lock", asyncio.Lock())):
            mock = patch.object(target, attribute, value); mock.start(); self.addCleanup(mock.stop)
        await self.db.ensure_ready()
        self.ops = DecisionKitOperations()
        app = web.Application()
        app.router.add_get("/memory/kits", handle_get_kits)
        app.router.add_post("/memory/kits", handle_create_kit)
        app.router.add_patch("/memory/kits/{kit_id}", handle_update_kit)
        app.router.add_delete("/memory/kits/{kit_id}", handle_delete_kit)
        app.router.add_get("/memory/observations", handle_get_observations)
        app.router.add_get("/memory/observations/search", handle_search_observations)
        app.router.add_post("/memory/observations", handle_create_observation)
        app.router.add_patch("/memory/observations/{note_id}", handle_update_observation)
        app.router.add_delete("/memory/observations/{note_id}", handle_delete_observation)
        app.router.add_get("/memory/social", handle_get_social_memory)
        app.router.add_post("/memory/social/{entity_type}", handle_create_social_memory)
        app.router.add_get("/memory/experiences", handle_get_experiences)
        app.router.add_post("/memory/experiences", handle_create_experience)
        self.client = TestClient(TestServer(app)); await self.client.start_server()
        self.vector = SimpleNamespace(
            generate_vector=AsyncMock(side_effect=self.embed),
            generate_vectors=AsyncMock(side_effect=self.embed_many),
        )
        self.embedded = []  # 已请求嵌入的文本（按条计，批量接口内部逐条记录）
        for module in ("character", "group", "bequest"):
            mock = patch(f"brain.hc.database.{module}.get_vector_store", return_value=self.vector)
            mock.start(); self.addCleanup(mock.stop)
        mock = patch.object(searchmod, "get_vector_store", return_value=self.vector)
        mock.start(); self.addCleanup(mock.stop)

    async def asyncTearDown(self):
        await self.client.close()
        await self.db.close()

    async def embed(self, text, target="bequest"):
        self.embedded.append(text)
        return [1.0, float(text.rsplit("-", 1)[-1])] if text.startswith("见闻-") else [1.0, 0.0]

    async def embed_many(self, texts, target="bequest"):
        return [await self.embed(text, target) for text in texts]

    async def seed_notes(self, count):
        for index in range(count):
            await self.notepad.record_observation(f"见闻-{index}")

    async def test_http_crud_and_persistence(self):
        created = await self.client.post("/memory/kits", json=DATA)
        self.assertEqual(created.status, 201)
        item = (await created.json())["kit"]
        self.assertEqual({key: item[key] for key in DATA}, DATA)
        response = await self.client.patch("/memory/kits/" + item["kit_id"], json={"advice": "修改后的建议"})
        self.assertEqual(response.status, 200)
        updated = (await response.json())["kit"]
        self.assertEqual(updated["advice"], "修改后的建议")
        self.assertEqual(updated["dilemma"], DATA["dilemma"])
        await self.db.close()
        self.db = MemoryDatabase(str(self.root / "memory.db"))
        dbmod._memory_db_instance = self.db
        response = await self.client.get("/memory/kits")
        self.assertEqual((await response.json())["kits"][0], updated)
        self.assertEqual((await self.client.delete("/memory/kits/" + item["kit_id"])).status, 200)
        self.assertEqual((await self.client.delete("/memory/kits/" + item["kit_id"])).status, 404)
        self.assertEqual((await (await self.client.get("/memory/kits")).json())["total"], 0)

    async def test_invalid_input_and_nonexistent_update(self):
        for data in ({}, {**DATA, "advice": " "}, {**DATA, "category": 42}, {**DATA, "extra": "字段"}, []):
            self.assertEqual((await self.client.post("/memory/kits", json=data)).status, 400)
        self.assertEqual((await self.client.patch("/memory/kits/missing", json={"advice": "内容"})).status, 404)
        self.assertEqual(await self.ops.list_all(), [])

    async def test_concurrent_entries_have_unique_ids(self):
        kits = await asyncio.gather(*(self.ops.create(DATA) for _ in range(12)))
        self.assertEqual(len({kit["kit_id"] for kit in kits}), 12)
        self.assertEqual(len(await self.ops.list_all()), 12)

    async def test_llm_returns_every_kit_and_all_ranked_observations(self):
        for index in range(7): await self.ops.create({**DATA, "category": f"分类-{index}"})
        await self.seed_notes(8)
        before = hashlib.sha256(self.notepad._slot_path(SLOT_EVENT).read_bytes()).hexdigest()
        result = await ToolJinnang.query_jinnang("当前困境")
        self.assertEqual(result["count"], 7)
        self.assertEqual(result["observation_count"], 8)
        self.assertEqual(result["observations"][0]["text"], "见闻-0")
        scores = [note["similarity"] for note in result["observations"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(hashlib.sha256(self.notepad._slot_path(SLOT_EVENT).read_bytes()).hexdigest(), before)

    async def test_cache_reuses_vectors_and_invalidates_text_and_model(self):
        await self.seed_notes(6)
        await ToolJinnang.query_jinnang("问题")
        self.assertEqual(len(self.embedded), 7)
        await ToolJinnang.query_jinnang("另一个问题")
        self.assertEqual(len(self.embedded), 8)
        file = self.notepad._slot_path(SLOT_EVENT)
        payload = json.loads(file.read_text(encoding="utf-8")); payload["notes"][0]["text"] = "见闻-9"
        file.write_text(json.dumps(payload), encoding="utf-8")
        await ToolJinnang.query_jinnang("问题")
        self.assertEqual(len(self.embedded), 10)
        with patch.object(searchmod.vector_config, "MODEL_NAME", "test-model-changed"):
            await ToolJinnang.query_jinnang("问题")
        self.assertEqual(len(self.embedded), 17)

    async def test_small_inventory_empty_kits_and_empty_observations(self):
        result = await ToolJinnang.query_jinnang("问题")
        self.assertEqual(result["count"], 0); self.assertEqual(result["observation_status"], "empty")
        await self.seed_notes(3)
        result = await ToolJinnang.query_jinnang("问题")
        self.assertEqual(result["observation_count"], 3)
        self.assertIn("不足5条", result["hint"])

    async def test_disabled_or_failed_vectors_do_not_hide_kits(self):
        await self.ops.create(DATA); await self.seed_notes(6)
        self.vector.generate_vector.side_effect = None; self.vector.generate_vector.return_value = None
        result = await ToolJinnang.query_jinnang("问题")
        self.assertEqual(result["count"], 1); self.assertEqual(result["observation_status"], "disabled")
        self.vector.generate_vector.side_effect = RuntimeError("模拟嵌入服务断线")
        result = await ToolJinnang.query_jinnang("问题")
        self.assertEqual(result["count"], 1); self.assertEqual(result["observation_status"], "error")
        self.assertEqual(result["observations"], [])

    async def test_view_ledger_and_search_via_http(self):
        await self.seed_notes(7)
        response = await self.client.get("/memory/observations")
        self.assertEqual((await response.json())["total"], 7)
        response = await self.client.get("/memory/observations/search", params={"query": "问题"})
        self.assertEqual((await response.json())["count"], 7)
        self.assertEqual((await self.client.get("/memory/observations/search")).status, 400)

    async def test_pfca_read_only_tool_is_registered_and_dispatches(self):
        manager = PFCA_tool_manager()
        names = [schema["function"]["name"] for schema in manager.get_tools()]
        self.assertIn("query_jinnang", names)
        self.assertNotIn("create_kit", names); self.assertNotIn("update_kit", names)
        focus = SimpleNamespace(set_activity=Mock())
        with patch("brain.pfc.pfca.pfca_tools.get_global_session_state", return_value=focus):
            response = await manager.handle_tool_call({"id": "kit-test", "type": "function",
                "function": {"name": "query_jinnang", "arguments": json.dumps({"query": "困难"})}})
        self.assertEqual(json.loads(response["content"])["count"], 0)
        with self.assertRaises(ValueError): await ToolJinnang.query_jinnang(" ")

    async def test_create_social_and_experience_with_existing_operations(self):
        character = await self.client.post("/memory/social/character", json={"name": "测试角色", "impression": "可信", "session_id": "private_123"})
        self.assertEqual(character.status, 201)
        node = (await character.json())["node"]
        self.assertEqual(node["contacts"][0]["session_id"], "private_123")
        self.assertEqual(node["impressions"][0]["content"], "可信")
        group = await self.client.post("/memory/social/group", json={"name": "测试群组", "session_id": "group_456"})
        self.assertEqual(group.status, 201)
        fuzzy = await self.client.post("/memory/social/fuzzy_role", json={"name": "老师", "content": "测试描述", "source_session_id": "private_123"})
        self.assertEqual(fuzzy.status, 201)
        self.assertEqual((await fuzzy.json())["node"]["source_entity_uuid"], node["uuid"])
        duplicate = await self.client.post("/memory/social/fuzzy_role", json={"name": "老师", "content": "不应该追加", "source_session_id": "private_123"})
        self.assertEqual(duplicate.status, 409)
        snapshot = await (await self.client.get("/memory/social")).json()
        self.assertEqual(snapshot["fuzzy_role_count"], 1)
        self.assertEqual(next(n for n in snapshot["nodes"] if n["type"] == "FuzzyRole")["content"], "测试描述")
        for category in ("speech", "decision"):
            response = await self.client.post("/memory/experiences", json={"title": "测试经验", "summary": "正文", "category": category})
            self.assertEqual(response.status, 201)
        response = await self.client.post("/memory/experiences", json={"title": "测试经验", "summary": "正文", "category": "speech"})
        self.assertEqual(response.status, 409)
        self.assertEqual((await (await self.client.get("/memory/experiences")).json())["total"], 2)

    async def test_create_validation_and_failed_vector_leaves_no_partial_entity(self):
        for kind, data in (("character", {"name": ""}), ("group", {"name": "群", "session_id": "private_123"}),
                           ("fuzzy_role", {"name": "老师", "content": "正文", "source_session_id": "private_missing"})):
            self.assertEqual((await self.client.post("/memory/social/" + kind, json=data)).status, 400)
        self.assertEqual((await self.client.post("/memory/experiences", json={"title": "经验", "summary": "正文", "category": "other"})).status, 400)
        self.vector.generate_vector.side_effect = RuntimeError("模拟嵌入失败")
        self.assertEqual((await self.client.post("/memory/social/character", json={"name": "失败角色"})).status, 500)
        self.assertEqual((await (await self.client.get("/memory/social")).json())["character_count"], 0)
        self.assertEqual((await self.client.post("/memory/experiences", json={"title": "失败经验", "summary": "正文", "category": "speech"})).status, 500)
        self.assertEqual((await (await self.client.get("/memory/experiences")).json())["total"], 0)

    async def test_observation_crud_preserves_other_slots_and_invalidates_vectors(self):
        self.notepad._append_slot_note_sync(nmod.SLOT_TASK, {"note_id": "task", "text": "任务笔记"})
        response = await self.client.post("/memory/observations", json={"text": "人工观察\n根据实际发言整理。"})
        self.assertEqual(response.status, 201)
        note = (await response.json())["observation"]
        self.assertEqual(note["source"], "human")
        await ToolJinnang.query_jinnang("问题")
        before = len(self.embedded)
        response = await self.client.patch("/memory/observations/" + note["note_id"], json={"text": "修订后的观察"})
        edited = (await response.json())["observation"]
        self.assertEqual(edited["record_time"], note["record_time"])
        self.assertEqual(edited["source"], "human")
        await ToolJinnang.query_jinnang("问题")
        self.assertEqual(len(self.embedded), before + 2)
        self.assertEqual((await self.client.delete("/memory/observations/" + note["note_id"])).status, 200)
        self.assertEqual((await self.client.delete("/memory/observations/" + note["note_id"])).status, 404)
        conn = await self.db._ensure_connection()
        self.assertEqual((await (await conn.execute("SELECT COUNT(*) FROM observation_vectors")).fetchone())[0], 0)
        self.assertEqual(len(await self.notepad.get_slot_notes(nmod.SLOT_TASK)), 1)
        self.assertEqual((await ToolJinnang.query_jinnang("问题"))["observation_status"], "empty")
        for data in ({"text": " "}, {"text": 3}, {"text": "正文", "source": "reflection"}):
            self.assertEqual((await self.client.post("/memory/observations", json=data)).status, 400)

    async def test_concurrent_reflection_and_human_observations_are_preserved(self):
        ids = await asyncio.gather(*(self.notepad.record_observation(f"并发见闻-{i}", source="human" if i % 2 else "reflection") for i in range(12)))
        self.assertEqual(len(set(ids)), 12)
        self.assertEqual(len(await self.notepad.get_slot_notes(SLOT_EVENT)), 12)


if __name__ == "__main__":
    try: unittest.main(verbosity=2)
    finally:
        logging.shutdown(); _sandbox.cleanup()
