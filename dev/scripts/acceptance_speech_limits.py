"""发送分段/滚动字数/同轮重复调用验收，不调用聊天平台或模型。"""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brain.base.common_ways.tool_conversation import ToolConversation
from brain.base.heavy_agent.heavy_session import HeavySession
from memory.message_repository import MessageRepository


class SpeechLimits(unittest.IsolatedAsyncioTestCase):
    async def test_segments_and_thresholds(self):
        for chars, replied, maximum in [(0, False, 3), (149, False, 3),
                                        (150, False, 2), (299, False, 2),
                                        (300, False, 1), (500, True, 3)]:
            store = SimpleNamespace(get_speech_pressure=AsyncMock(
                return_value={"characters": chars, "user_replied": replied}))
            send = AsyncMock()
            with patch('memory.message_repository.get_message_store', return_value=store), \
                 patch.object(ToolConversation, 'send_segments_with_typing', send):
                result = await ToolConversation.send_message('private_test', '甲\n乙\n丙\n丁')
            self.assertEqual(len(send.call_args.args[2]), maximum)
            self.assertIn('未发送', result)
            self.assertIn('丁', result)
            self.assertEqual('发言量提醒' in result, chars >= 150 and not replied)

    async def test_normal_and_long_message(self):
        store = SimpleNamespace(get_speech_pressure=AsyncMock(side_effect=[
            {"characters": 0, "user_replied": True},
            {"characters": 350, "user_replied": False}]))
        send = AsyncMock()
        with patch('memory.message_repository.get_message_store', return_value=store), \
             patch.object(ToolConversation, 'send_segments_with_typing', send):
            result = await ToolConversation.send_message('private_test', '长' * 350)
        self.assertEqual(len(send.call_args.args[2][0].to_text()), 350)
        self.assertIn('最多发送1条', result)
        store.get_speech_pressure = AsyncMock(return_value={"characters": 10, "user_replied": False})
        with patch('memory.message_repository.get_message_store', return_value=store), \
             patch.object(ToolConversation, 'send_segments_with_typing', AsyncMock()):
            result = await ToolConversation.send_message('private_test', '短句')
        self.assertNotIn('提醒', result)
        self.assertNotIn('警告', result)

    async def test_repository_window_and_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = MessageRepository(directory)
            conn = await repo._ensure_connection()
            async def insert(user, text, timestamp):
                await conn.execute(
                    'INSERT INTO messages(session_id,message_id,user_id,timestamp,content_text) VALUES(?,?,?,?,?)',
                    ('private_test', str(time.time_ns()), user, timestamp, text))
                await conn.commit()
            await insert('system', '旧' * 500, time.time() - 61)
            await insert('system', 'A 中\n！', time.time())
            self.assertEqual(await repo.get_speech_pressure('private_test'),
                             {"characters": 3, "user_replied": False})
            await insert('user', '收到', time.time())
            self.assertTrue((await repo.get_speech_pressure('private_test'))['user_replied'])
            await conn.close()

    async def test_one_send_per_llm_round(self):
        class Session(HeavySession):
            def get_tools(self): return []
            async def _handle_tool_call(self, call):
                self.executed.append(call.function.name)
                return {'role': 'tool', 'tool_call_id': call.id, 'content': 'ok'}
            async def _call_llm(self): return self.response
            async def _inject_first_user_message(self): return False
            async def _inject_incremental_user_message(self): return False
            def _register_tool_call(self, call): return 'ok'
        s = Session()
        s.executed = []
        s.history_manager = []
        s._rounds = s.no_tool_call_count = s.tool_error_count = 0
        s.max_no_tool_call = s.max_tool_error = None
        s.running = True
        calls = [SimpleNamespace(id=str(i), function=SimpleNamespace(
            name=name, arguments=json.dumps({'message': '未发内容'})))
            for i, name in enumerate(['send_message', 'send_to_target', 'query_note'])]
        s.response = SimpleNamespace(content='', tool_calls=calls)
        await s.process_cycle()
        self.assertEqual(s.executed, ['send_message', 'query_note'])
        self.assertIn('全部内容未发送', s.history_manager[2]['content'])
        await s.process_cycle()
        self.assertEqual(s.executed.count('send_message'), 2)


if __name__ == '__main__':
    unittest.main()
