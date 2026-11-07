"""事件链社交授权隔离验收：真实消息游标/链生命周期，发送打桩，不调用模型或聊天服务。"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_sandbox = tempfile.TemporaryDirectory(prefix="abyss-social-chain-")
os.environ["ABYSS_DATA_DIR"] = _sandbox.name
_personas = Path(_sandbox.name) / "brain/base/persona"
_personas.mkdir(parents=True)
for _name in ("normal", "common"):
    (_personas / f"{_name}.json").write_text(json.dumps({
        "persona_name": _name, "scene": "验收", "kernel_prompt": "测试占位",
    }), encoding="utf-8")
(Path(_sandbox.name) / "memory").mkdir()

import brain.hc.global_session_state as gmod
import brain.hc.shortlight.notepad as nmod
import brain.chat.social_chat.social_chat_tool_manager as tmod
import brain.chat.social_chat.social_chat_input_manager as imod
import memory.message_repository as rmod
from brain.hc.event_chain_permissions import EventChainPermissions
from config.settings import chat_config
from utils.error_handling import SystemError as AbyssSystemError


class SocialChainPermissionsAcceptance(unittest.IsolatedAsyncioTestCase):
    async def _fake_send_message(self, *, message, on_sent=None, **kwargs):
        if on_sent is not None:
            on_sent(message)
        return "发送成功"

    async def asyncSetUp(self):
        self.state = gmod.GlobalSessionState()
        self.state._storage_path = Path(tempfile.mkdtemp(dir=_sandbox.name))
        self.notepad = nmod.ShortlightNotepad()
        self.store = rmod.MessageRepository(tempfile.mkdtemp(dir=_sandbox.name))
        self.patches = [
            patch.object(gmod, "_global_session_state_instance", self.state),
            patch.object(nmod, "_instance", self.notepad),
            patch.object(rmod, "_message_store_instance", self.store),
            patch.object(chat_config, "SOCIAL_CHAT_SEND_MODE", "approval"),
            patch.object(chat_config, "SOCIAL_CHAT_UNRESTRICTED_NOTIFY", False),
            patch.object(tmod, "APPROVAL_TIMEOUT_S", 0.04),
            patch.object(tmod, "APPROVAL_POLL_INTERVAL_S", 0.002),
            patch.object(tmod.ToolConversation, "send_message", AsyncMock(side_effect=self._fake_send_message)),
            patch.object(tmod.ToolConversation, "send_segments_with_typing", AsyncMock()),
            patch.object(tmod.ToolConversation, "_build_image_segment", Mock(return_value={"type": "image"})),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.state.start_new_event_chain("验收", chain_id="test-chain")

    async def asyncTearDown(self):
        if self.state.is_event_chain_active():
            self.state.end_event_chain()
        await self.store.close()

    def manager(self, target="group_123", master="private_456"):
        return tmod.SocialChatToolManager(SimpleNamespace(
            master_session_id=master, working_session_id=target,
        ))

    async def reply(self, text="同意", user_id="456", master="private_456"):
        await self.store.store_message({
            "session_info": {"session_id": master, "session_name": "验收主人", "remark": ""},
            "message_list": [{"message_id": uuid.uuid4().hex, "user_id": user_id,
                              "user_name": "验收", "timestamp": int(time.time()),
                              "content_text": text, "content": [{"type": "text", "data": {"text": text}}]}],
        })

    def auto_reply(self, manager, answer="同意"):
        async def send(prompt):
            await self.reply(prompt, user_id="system", master=manager._permission_scope[0])
            if answer is not None:
                await self.reply(answer, master=manager._permission_scope[0])
        manager._send_raw_to_master = AsyncMock(side_effect=send)

    async def grant(self, manager):
        self.auto_reply(manager)
        result = await manager.set_send_mode("unrestricted")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["target_session_id"], manager._permission_scope[1])

    async def test_reentry_inherits_and_other_targets_do_not(self):
        first = self.manager()
        await self.grant(first)
        second = self.manager()
        second._request_master_approval = AsyncMock(side_effect=AssertionError("不应重复审批"))
        self.assertEqual((await second.send_to_target("再次进入"))["status"], "ok")
        self.assertEqual(self.manager("group_999").send_mode, "approval")
        self.assertEqual(self.manager(master="private_888").send_mode, "approval")
        prompt = first._send_raw_to_master.call_args.args[0]
        self.assertIn("group_123", prompt)
        self.assertIn("事件链结束即失效", prompt)

    async def test_single_message_approval_does_not_grant_free_mode(self):
        manager = self.manager()
        self.auto_reply(manager)
        self.assertEqual((await manager.send_to_target("第一条"))["status"], "ok")
        self.assertEqual(manager.send_mode, "approval")
        self.assertEqual((await manager.send_to_target("第二条"))["status"], "ok")
        self.assertEqual(manager._send_raw_to_master.await_count, 2)
        self.assertEqual(self.manager().send_mode, "approval")

    async def test_revoke_is_shared_and_survives_reentry(self):
        first, second = self.manager(), self.manager()
        await self.grant(first)
        self.assertEqual(second.send_mode, "unrestricted")
        await second.set_send_mode("approval")
        self.assertEqual(first.send_mode, "approval")
        self.assertEqual(self.manager().send_mode, "approval")

    async def test_new_chain_even_same_id_does_not_restore_grants(self):
        old = self.manager()
        await self.grant(old)
        self.state.end_event_chain()
        self.assertEqual((await old.send_to_target("失效"))["approval"], "expired")
        self.assertEqual((await old.set_send_mode("unrestricted"))["approval"], "expired")
        self.state.start_new_event_chain(chain_id="test-chain")
        self.assertEqual(self.manager().send_mode, "approval")
        self.assertEqual((await old.send_to_target("不能复活"))["status"], "rejected")

    async def test_restart_has_no_authorization_restore(self):
        await self.grant(self.manager())
        restarted = gmod.GlobalSessionState()
        self.assertFalse(restarted.is_event_chain_active())
        self.assertIsNone(restarted._event_chain_permissions)
        self.assertNotIn("permissions", self.state._build_snapshot())

    async def test_archive_failure_still_revokes_and_cleans_state(self):
        manager = self.manager()
        await self.grant(manager)
        with patch.object(self.state, "_persist_real_time", side_effect=OSError("模拟磁盘失败")):
            with self.assertRaises(OSError):
                self.state.end_event_chain()
        self.assertFalse(self.state.is_event_chain_active())
        self.assertIsNone(self.notepad._current)
        self.assertEqual((await manager.send_to_target("归档失败也不能发送"))["approval"], "expired")

    async def test_chain_closes_while_waiting_for_approval(self):
        manager = self.manager()
        async def close_then_reply(prompt):
            self.state.end_event_chain()
            self.state.start_new_event_chain(chain_id="new-chain")
            await self.reply()
        manager._send_raw_to_master = AsyncMock(side_effect=close_then_reply)
        result = await manager.set_send_mode("unrestricted")
        self.assertEqual(result["approval"], "expired")
        self.assertEqual(self.manager().send_mode, "approval")

    async def test_expired_single_approval_never_sends(self):
        manager = self.manager()
        async def close(prompt):
            self.state.end_event_chain()
            await self.reply()
        manager._send_raw_to_master = AsyncMock(side_effect=close)
        self.assertEqual((await manager.send_to_target("不能发送"))["approval"], "expired")
        tmod.ToolConversation.send_message.assert_not_awaited()

    async def test_text_and_image_share_authority(self):
        manager = self.manager()
        self.auto_reply(manager, "拒绝")
        self.assertEqual((await manager.send_image_to_target("test.png"))["status"], "rejected")
        tmod.ToolConversation.send_segments_with_typing.assert_not_awaited()
        await self.grant(manager)
        self.assertEqual((await self.manager().send_image_to_target("test.png"))["status"], "ok")
        self.assertEqual((await self.manager().send_to_target("文字"))["status"], "ok")
        tmod.ToolConversation.send_segments_with_typing.assert_awaited_once()
        tmod.ToolConversation.send_message.assert_awaited_once()

    async def test_rejection_timeout_and_old_reply_do_not_grant(self):
        manager = self.manager()
        await self.reply()  # 旧的同意必须被锚点排除。
        self.auto_reply(manager, None)
        self.assertEqual((await manager.set_send_mode("unrestricted"))["approval"], "timeout")
        self.auto_reply(manager, "拒绝")
        self.assertEqual((await manager.set_send_mode("unrestricted"))["approval"], "rejected")
        self.assertEqual(manager.send_mode, "approval")

    async def test_approval_requests_are_serialized(self):
        first, second = self.manager(), self.manager("group_999")
        started, release = asyncio.Event(), asyncio.Event()
        async def send_first(prompt):
            started.set()
            await release.wait()
            await self.reply()
        first._send_raw_to_master = AsyncMock(side_effect=send_first)
        self.auto_reply(second, None)
        a = asyncio.create_task(first.set_send_mode("unrestricted"))
        await started.wait()
        b = asyncio.create_task(second.set_send_mode("unrestricted"))
        await asyncio.sleep(0.01)
        second._send_raw_to_master.assert_not_awaited()
        release.set()
        self.assertEqual((await a)["status"], "ok")
        self.assertEqual((await b)["approval"], "timeout")
        self.assertEqual(second.send_mode, "approval")

    async def test_input_reads_same_live_permission_service(self):
        manager = self.manager()
        inputs = imod.SocialChatInputManager()
        inputs.prepare_first_message("测试任务", manager.social_chat_core)
        await self.grant(manager)
        self.assertEqual(inputs._current_send_mode(), "unrestricted")
        self.assertIn("【发送模式：自由】", imod._build_mode_block(inputs._current_send_mode()))
        await manager.set_send_mode("approval")
        self.assertIn("【发送模式：审批】", imod._build_mode_block(inputs._current_send_mode()))
        self.state.end_event_chain()
        with self.assertRaises(RuntimeError):
            inputs._current_send_mode()

    async def test_core_target_change_cannot_reuse_old_tools(self):
        manager = self.manager()
        await self.grant(manager)
        manager.social_chat_core.working_session_id = "group_999"
        with self.assertRaises(RuntimeError):
            await manager.send_to_target("不能重定向")
        tmod.ToolConversation.send_message.assert_not_awaited()

    async def test_config_default_is_chain_scoped_and_revocation_overrides_it(self):
        self.state.end_event_chain()
        with patch.object(chat_config, "SOCIAL_CHAT_SEND_MODE", "unrestricted"):
            self.state.start_new_event_chain(chain_id="configured-free")
        manager = self.manager()
        self.assertEqual(manager.send_mode, "unrestricted")
        await manager.set_send_mode("approval")
        self.assertEqual(self.manager().send_mode, "approval")
        self.assertEqual(self.manager("group_999").send_mode, "unrestricted")

    async def test_no_chain_cannot_construct_social_tools(self):
        self.state.end_event_chain()
        with self.assertRaises(AbyssSystemError):
            self.manager()

    async def test_repeated_start_does_not_reset_active_chain_grant(self):
        manager = self.manager()
        await self.grant(manager)
        permissions = manager._permissions
        self.state.start_new_event_chain(chain_id="ignored-id")
        self.assertIs(self.state.get_event_chain_permissions(), permissions)
        self.assertEqual(self.manager().send_mode, "unrestricted")

    async def test_start_failure_does_not_leave_permissions(self):
        self.state.end_event_chain()
        with patch.object(self.notepad, "begin_chain", side_effect=RuntimeError("模拟初始化失败")):
            with self.assertRaises(RuntimeError):
                self.state.start_new_event_chain()
        self.assertFalse(self.state.is_event_chain_active())
        self.assertIsNone(self.state._event_chain_permissions)

    async def test_start_hook_failure_rolls_back_chain_and_permissions(self):
        self.state.end_event_chain()
        captured = []
        def failing_hook():
            captured.append(self.state.get_event_chain_permissions())
            raise RuntimeError("模拟开始钩子失败")
        with patch.object(self.state, "_on_event_chain_start", side_effect=failing_hook):
            with self.assertRaises(RuntimeError):
                self.state.start_new_event_chain()
        self.assertFalse(captured[0].active)
        self.assertFalse(self.state.is_event_chain_active())
        self.assertIsNone(self.notepad._current)

    async def test_actual_first_input_after_reentry_shows_inherited_mode(self):
        manager = self.manager()
        await self.grant(manager)
        inputs = imod.SocialChatInputManager()
        inputs.prepare_first_message("重新进入", self.manager().social_chat_core)
        async def append_bequest(amy_effect, purpose, user_input):
            return user_input
        with patch.object(inputs, "_get_first_social_history", AsyncMock(return_value="")), \
             patch.object(inputs, "_build_persona_header", return_value=""), \
             patch.object(inputs, "_append_matched_bequest", side_effect=append_bequest), \
             patch.object(imod, "build_social_chat_environment", return_value=""), \
             patch("brain.amy.emotional_interface.get_emotional_interface", AsyncMock(return_value=SimpleNamespace(
                 get_language_description=AsyncMock(return_value="")))):
            text = await inputs._build_first_input()
        self.assertIn("【发送模式：自由】", text)
        self.assertNotIn("【发送模式：审批】", text)

    async def test_station_cancellation_closes_chain(self):
        import brain.att.station as smod
        import brain.pfc.core as pmod
        self.state.end_event_chain()
        station = smod.AttentionStation()
        station.running = True
        entered = asyncio.Event()
        async def processing(*args):
            entered.set()
            await asyncio.Future()
        truck = SimpleNamespace(truck_id="test", state="paused", start=Mock(),
                                get_truck_messages=AsyncMock(return_value={"formatted_message": "验收"}))
        station.task_queue = asyncio.Queue()
        await station.task_queue.put("test")
        with patch.object(station, "_get_truck", return_value=truck), \
             patch.object(station, "_all_running_trucks", return_value=[]), \
             patch.object(pmod, "async_process_from_att_factory", side_effect=processing):
            task = asyncio.create_task(station._process_tasks())
            await asyncio.wait_for(entered.wait(), 2)
            manager = self.manager()
            await self.grant(manager)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.state.is_event_chain_active())
        self.assertEqual((await manager.send_to_target("取消后不能发送"))["approval"], "expired")
        await asyncio.wait_for(station.task_queue.join(), 1)
        truck.start.assert_called_once()


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        unittest.main(verbosity=2)
    finally:
        logging.shutdown()
        _sandbox.cleanup()
