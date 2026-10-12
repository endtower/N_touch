"""隔离验收 Reflection 见闻录：真实文件读写，临时数据，无模型调用。"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_sandbox = tempfile.TemporaryDirectory(prefix="abyss-observations-")
os.environ["ABYSS_DATA_DIR"] = _sandbox.name
_personas = Path(_sandbox.name) / "brain/base/persona"
_personas.mkdir(parents=True)
for _name in ("common", "normal"):
    (_personas / f"{_name}.json").write_text(json.dumps({
        "persona_name": _name, "scene": "隔离测试", "kernel_prompt": "测试占位",
    }), encoding="utf-8")

import brain.hc.shortlight.notepad as nmod
from brain.hc.shortlight.notepad import ShortlightNotepad, SLOT_EVENT, SLOT_TASK
from brain.base.common_ways.tool_hc import ToolHC
from brain.pfc.reflection.reflection_tools import Reflection_tool_manager
from brain.pfc.pfca.pfca_tools import PFCA_tool_manager
from brain.chat.conversation.conversation_tool_manager import ConversationToolManager
from brain.chat.social_chat.social_chat_tool_manager import SocialChatToolManager
from brain.hc.event_chain_permissions import EventChainPermissions


class ReflectionObservationsAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = Path(tempfile.mkdtemp(dir=_sandbox.name))
        self.root_patch = patch.object(nmod, "NOTES_DIR", self.directory)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.notepad = ShortlightNotepad()
        self.instance_patch = patch.object(nmod, "_instance", self.notepad)
        self.instance_patch.start()
        self.addCleanup(self.instance_patch.stop)
        self.manager = Reflection_tool_manager()

    async def test_removed_event_category_is_rejected_and_other_notes_work(self):
        self.notepad.begin_chain("test-event")
        with self.assertRaises(ValueError):
            await self.notepad.append_note("群友说要等更强AI", "pfca", "事件见闻")
        await self.notepad.append_note("主人指出我概括范围过大", "pfca", "自我修正")
        await self.notepad.append_note("明天继续确认时间", "conversation", "任务进度相关")
        await self.notepad.finalize_chain("讨论")
        docs = await self.notepad.get_all()
        self.assertEqual(len(docs[0]["notes"]), 2)
        self.assertEqual(await self.notepad.get_slot_notes(SLOT_EVENT), [])
        self.assertEqual(len(await self.notepad.get_slot_notes(SLOT_TASK)), 1)

    async def test_reflection_writes_without_chain_or_note_truncation(self):
        text = "本次群聊中，部分成员反复谈到等待更强的AI，却较少讨论当下怎么推进。" * 6
        self.assertGreater(len(text), 150)
        result = await self.manager.record_observation("  " + text + "  ")
        notes = await self.notepad.get_slot_notes(SLOT_EVENT)
        self.assertIn(notes[0]["note_id"], result)
        self.assertEqual(notes[0]["text"], text)
        self.assertEqual(notes[0]["source"], "reflection")
        self.assertTrue(notes[0]["record_time"])
        self.assertEqual(await self.notepad.get_all(), [])
        self.assertIsNone(self.notepad._current)

    async def test_legacy_records_preserved_and_queryable(self):
        old = {"note_id": "legacy-1", "text": "旧见闻", "source": "pfca", "record_time": "2026-10-07"}
        file = self.notepad._slot_path(SLOT_EVENT)
        file.write_text(json.dumps({"slot": SLOT_EVENT, "notes": [old]}, ensure_ascii=False), encoding="utf-8")
        await self.manager.record_observation("部分群友多次谈到等更强AI，可能对当前能力边界缺乏把握。")
        notes = await self.notepad.get_slot_notes(SLOT_EVENT)
        self.assertEqual(notes[0], old)
        self.assertEqual(len(notes), 2)
        self.assertIn("旧见闻", await ToolHC.list_notes(SLOT_EVENT))
        self.assertIn(notes[1]["text"], await ToolHC.list_notes(SLOT_EVENT))

    async def test_archive_does_not_move_observations(self):
        await self.manager.record_observation("本次交互中，对方先试探身份，再询问关系；可能在确认交流边界。")
        before = self.notepad._slot_path(SLOT_EVENT).read_bytes()
        self.notepad.begin_chain("archive-test")
        await self.notepad.append_note("对方纠正了我的身份说明", "conversation", "自我修正")
        await self.notepad.finalize_chain("身份讨论")
        archived = await self.notepad.archive_all()
        self.assertEqual(len(archived), 1)
        self.assertEqual(await self.notepad.get_all(), [])
        self.assertEqual(self.notepad._slot_path(SLOT_EVENT).read_bytes(), before)

    async def test_invalid_arguments_do_not_write(self):
        for invalid in ("", "  ", None, 123):
            with self.assertRaises(ValueError):
                await self.manager.record_observation(invalid)
        self.assertFalse(self.notepad._slot_path(SLOT_EVENT).exists())

    async def test_concurrent_appends_do_not_lose_entries(self):
        await asyncio.gather(*(self.manager.record_observation(f"观察{i}，依据是对应交互。") for i in range(12)))
        notes = await self.notepad.get_slot_notes(SLOT_EVENT)
        self.assertEqual(len(notes), 12)
        self.assertEqual(len({note["text"] for note in notes}), 12)

    async def test_tool_exposure_is_reflection_only(self):
        names = lambda manager: {s["function"]["name"] for s in manager.get_tools()}
        self.assertIn("record_observation", names(self.manager))
        # 此项只验工具暴露面；发送权限单独由事件链验收覆盖。
        state = SimpleNamespace(get_event_chain_permissions=lambda: EventChainPermissions("schema-test", "approval"))
        with patch("brain.hc.global_session_state.get_global_session_state", return_value=state):
            social = SocialChatToolManager(SimpleNamespace(master_session_id="private_test", working_session_id="group_test"))
        for manager in (PFCA_tool_manager, ConversationToolManager, social):
            self.assertNotIn("record_observation", names(manager))
            record = next(s["function"] for s in manager.get_tools() if s["function"]["name"] == "record_to_notepad")
            self.assertEqual(record["parameters"]["properties"]["category"]["enum"],
                             ["任务进度相关", "自我修正", "可借鉴的对方发言"])
        schema = next(s["function"] for s in self.manager.get_tools() if s["function"]["name"] == "record_observation")
        self.assertEqual(set(schema["parameters"]["properties"]), {"text"})

    async def test_legacy_ordinary_event_notes_still_readable(self):
        (self.directory / "legacy.json").write_text(json.dumps({
            "title": "旧事件", "notes": [{"text": "旧事件素材", "category": "事件见闻"}],
        }, ensure_ascii=False), encoding="utf-8")
        result = await ToolHC.query_note("旧事件")
        self.assertIn("【事件见闻】旧事件素材", result)

    async def test_real_tool_dispatch(self):
        focus = SimpleNamespace(set_activity=Mock())
        with patch("brain.pfc.reflection.reflection_tools.get_global_session_state", return_value=focus):
            result = await self.manager.handle_tool_call({
                "id": "observation-1", "type": "function",
                "function": {"name": "record_observation", "arguments": json.dumps({"text": "本次群聊中有人等待新模型，也有人正在动手。"})},
            })
        self.assertEqual(result["role"], "tool")
        self.assertEqual(result["tool_call_id"], "observation-1")
        self.assertIn("见闻已记录", result["content"])
        self.assertEqual(len(await self.notepad.get_slot_notes(SLOT_EVENT)), 1)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        logging.shutdown()
        _sandbox.cleanup()
