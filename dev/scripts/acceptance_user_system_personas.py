"""人工系统人格验收：临时设置、真实 HTTP、实际提示词，不调用模型。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = tempfile.TemporaryDirectory(prefix="abyss-system-personas-")
os.environ["ABYSS_DATA_DIR"] = DATA.name
Path(DATA.name, ".env.dev").touch()

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import user_prompt_settings as settings
from adapt.web.maintenance_api import (
    handle_create_persona, handle_delete_persona, handle_personas, handle_update_persona,
)
from brain.base.persona import persona_registry
from brain.base.common_ways.tool_persona import ToolPersona
from brain.pfc.pfca.pfca_prompt import get_full_system_prompt as pfca_prompt
from brain.pfc.pfca.pfca_tools import PFCA_tool_manager
from brain.pfc.reflection.reflection_prompt import get_full_system_prompt as reflection_prompt


class SystemPersonaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=DATA.name)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"ABYSS_DATA_DIR": str(self.root)})
        self.env.start()
        app = web.Application()
        app.router.add_get("/memory/personas", handle_personas)
        app.router.add_post("/memory/personas", handle_create_persona)
        app.router.add_patch("/memory/personas/{persona_name}", handle_update_persona)
        app.router.add_delete("/memory/personas/{persona_name}", handle_delete_persona)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.env.stop()
        self.tmp.cleanup()

    async def update(self, target, text):
        response = await self.client.patch(f"/memory/personas/system%3A{target}", json={"kernel_prompt": text})
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())["persona"]

    async def test_human_list_shows_two_empty_entries_without_seeding_persona_files(self):
        response = await self.client.get("/memory/personas")
        entries = [p for p in (await response.json())["personas"] if p.get("user_only")]
        self.assertEqual([p["display_name"] for p in entries], ["PFCA", "反思系统"])
        self.assertEqual([p["persona_name"] for p in entries], ["system:pfca", "system:reflection"])
        self.assertTrue(all(p["kernel_prompt"] == "" for p in entries))
        self.assertFalse(settings._path().exists())
        self.assertFalse((self.root / "brain/base/persona").exists())

    async def test_each_setting_is_appended_last_and_only_to_its_own_prompt(self):
        base_pfca, base_reflection = pfca_prompt(), reflection_prompt()
        await self.update("pfca", "PFCA 私有设定：保持独立判断。\n使用具体行动。")
        self.assertEqual(pfca_prompt(), base_pfca.rstrip() + "\n\nPFCA 私有设定：保持独立判断。\n使用具体行动。")
        self.assertEqual(reflection_prompt(), base_reflection)
        await self.update("reflection", "反思私有设定：先核实观察。")
        self.assertTrue(reflection_prompt().endswith("\n\n反思私有设定：先核实观察。"))
        self.assertNotIn("反思私有设定", pfca_prompt())
        self.assertNotIn("PFCA 私有设定", reflection_prompt())
        self.assertNotIn("system:pfca", pfca_prompt())
        self.assertNotIn("仅用户可编辑", pfca_prompt())

    async def test_edit_is_visible_to_next_call_without_reload_and_can_be_cleared(self):
        base = pfca_prompt()
        await self.update("pfca", "第一版")
        self.assertTrue(pfca_prompt().endswith("第一版"))
        await self.update("pfca", "第二版")
        self.assertTrue(pfca_prompt().endswith("第二版"))
        self.assertNotIn("第一版", pfca_prompt())
        result = await self.update("pfca", "  \n ")
        self.assertEqual(result["kernel_prompt"], "")
        self.assertEqual(pfca_prompt(), base)
        stored = json.loads(settings._path().read_text(encoding="utf-8"))
        self.assertEqual(stored["pfca"], "")
        self.assertGreaterEqual(len(list(settings._path().parent.joinpath("history").glob("*.json"))), 2)

    async def test_settings_are_not_registered_or_exposed_to_llm_tools(self):
        before = [p.to_dict() for p in persona_registry.list_personas()]
        await self.update("pfca", "人工保护内容")
        await self.update("reflection", "另一份人工保护内容")
        self.assertEqual(before, [p.to_dict() for p in persona_registry.list_personas()])
        roster = persona_registry.render_persona_roster()
        schemas = json.dumps(ToolPersona.get_persona_tool_schemas(), ensure_ascii=False)
        for name in ("system:pfca", "system:reflection"):
            self.assertFalse(persona_registry.persona_exists(name))
            self.assertNotIn(name, roster)
            self.assertNotIn(name, schemas)
            with self.assertRaises(KeyError):
                persona_registry.get_persona(name)
            with self.assertRaises(ValueError):
                await persona_registry.set_persona_kernel_prompt(name, "模型试图修改")
        self.assertEqual(set(ToolPersona.get_tool_functions()), {"query_common_persona", "update_common_persona"})
        self.assertNotIn("人工保护内容", str(ToolPersona.query_common_persona()))
        self.assertNotIn("人工保护内容", roster)
        self.assertTrue(pfca_prompt().endswith("人工保护内容"))

    async def test_inner_persona_lifecycle_updates_candidate_and_fallback(self):
        path = "/memory/personas"
        record = path + "/normal"
        fields = {"persona_name": "normal", "scene": "日常", "kernel_prompt": "保持简洁"}
        for invalid in ("../escape", "CON", "system:pfca", "Common"):
            response = await self.client.post(path, json={**fields, "persona_name": invalid})
            self.assertEqual(response.status, 400)
        response = await self.client.post(path, json=fields)
        self.assertEqual(response.status, 201, await response.text())
        try:
            response = await self.client.post(path, json=fields)
            self.assertEqual(response.status, 400)
            self.assertEqual(PFCA_tool_manager._resolve_persona_name(""), "normal")
            self.assertIn("normal：日常", persona_registry.render_persona_roster())
            response = await self.client.patch(record, json={"kernel_prompt": "改用短句"})
            self.assertEqual(response.status, 200)
            self.assertEqual(persona_registry.get_session_persona("normal").kernel_prompt, "改用短句")
        finally:
            response = await self.client.delete(record)
            self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(PFCA_tool_manager._resolve_persona_name(""), "common")
        self.assertEqual(persona_registry.get_session_persona("normal").persona_name, "common")
        self.assertNotIn("normal", persona_registry.render_persona_roster())
        self.assertIsNone((await PFCA_tool_manager.summon_personas(None))["suggested"])
        self.assertFalse((persona_registry.PERSONA_DIR / "normal.json").exists())
        archived = list(persona_registry.PERSONA_HISTORY_DIR.glob("normal_*.json"))
        self.assertTrue(any("改用短句" in file.read_text(encoding="utf-8") for file in archived))
        for protected in ("common", "system%3Apfca"):
            response = await self.client.delete(f"{path}/{protected}")
            self.assertEqual(response.status, 400)

    async def test_invalid_updates_are_rejected_without_changing_saved_content(self):
        await self.update("pfca", "保留正文")
        before = settings._path().read_bytes()
        for target, body in (("pfca", {"scene": "模型入口"}), ("pfca", {"kernel_prompt": None}),
                             ("pfca", {"kernel_prompt": 1}), ("pfca", {}), ("pfca", []),
                             ("pfca", {"kernel_prompt": "x" * 50001}),
                             ("unknown", {"kernel_prompt": "注入"}),
                             ("pfca", {"kernel_prompt": "新内容", "model_level": "pro"})):
            response = await self.client.patch(f"/memory/personas/system%3A{target}", json=body)
            self.assertEqual(response.status, 400, (target, body))
        self.assertEqual(settings._path().read_bytes(), before)

    async def test_concurrent_edits_preserve_both_settings_and_unknown_fields(self):
        path = settings._path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"future_option": {"keep": True}}), encoding="utf-8")
        await asyncio.gather(self.update("pfca", "调度设定"), self.update("reflection", "反思设定"))
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored, {"future_option": {"keep": True}, "pfca": "调度设定", "reflection": "反思设定"})

    async def test_failed_atomic_write_preserves_content_and_removes_temporary_file(self):
        await self.update("pfca", "保留旧版")
        path = settings._path()
        before = path.read_bytes()
        with patch.object(settings.os, "replace", side_effect=OSError("模拟替换失败")):
            with self.assertRaises(OSError):
                await settings.update_user_system_persona("system:pfca", {"kernel_prompt": "不能落盘的新版"})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])
        self.assertTrue(pfca_prompt().endswith("保留旧版"))

    async def test_malformed_saved_settings_fail_instead_of_silently_disappearing(self):
        path = settings._path()
        path.parent.mkdir(parents=True)
        for content in ("{broken", "[]", '{"pfca": 42}'):
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                pfca_prompt()
            with self.assertRaises(ValueError):
                reflection_prompt()
            with self.assertRaises(ValueError):
                await settings.update_user_system_persona("system:pfca", {"kernel_prompt": "不能覆盖坏文件"})
            self.assertEqual(path.read_text(encoding="utf-8"), content)


if __name__ == "__main__":
    try:
        result = unittest.main(exit=False).result
    finally:
        logging.shutdown()
        DATA.cleanup()
    sys.exit(0 if result.wasSuccessful() else 1)
