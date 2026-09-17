# -*- coding: utf-8 -*-
from typing import List, Dict
from utils.logger import get_logger
from brain.base import HeavySession
from brain.chat.chat_history_manager import get_history_manager
from brain.chat.conversation.conversation_input_manager import get_conversation_input_manager
from brain.chat.conversation.conversation_prompt import get_full_system_prompt
from brain.chat.conversation.conversation_tool_manager import ConversationToolManager
from config.settings import llm_config
from utils.llm_client_manager import call_llm

logger = get_logger('ConversationSession', "DEBUG")


class ConversationSession(HeavySession):

    PREFIX = 'conversation'
    TYPING_SPEED_CHARS_PER_MINUTE = 150
    TYPING_DELAY_PER_CHAR = 60.0 / TYPING_SPEED_CHARS_PER_MINUTE

    # 单会话累计 token 预算（熔断值，参照 token_stats.json 校准）
    TOKEN_BUDGET = 500_000
    # wait_and_observe 的语义就是"参数相同地反复等待"，豁免重复调用检测
    repeat_exempt_tools = frozenset({"wait_and_observe"})

    def __init__(self, conversation_core, max_messages: int = 0):
        
        self.conversation_core = conversation_core
        self.prefix = self.PREFIX
        self.history_manager = get_history_manager(prefix=self.prefix)
        self.running = True
        self._exit_reason = None
        self._exit_content = None
        self.tool_manager = ConversationToolManager(conversation_core)
        self._input_manager = get_conversation_input_manager()
        # 注：旧的"基于上一次发送时间戳"的打字延迟已迁移到
        # conversation_tool_manager.send_message 内部（从方法入口起算），
        # 这里不再保留 _last_send_time / _last_send_char_count 状态。
        self.no_tool_call_count = 0
        self.max_no_tool_call = llm_config.MAX_NO_TOOL_CALLS
        # Conversation 不使用工具参数错误上限（max_tool_error=None 表示不启用）
        self.tool_error_count = 0
        self.max_tool_error = None
        # 循环上限（父类 should_continue 默认基于 max_rounds 判断）
        self.max_rounds = llm_config.MAX_SELF_LOOPS
        self._rounds = 0
        # 发送轮次计数（软限制：一次发送工具调用计 1，与拆条后的消息条数无关；
        # 由 conversation_tool_manager 在发送成功后递增，input_manager 读取）
        self._max_messages = max_messages if max_messages > 0 else 0
        self._sent_message_count = 0
        self._initialized = True

    def get_tools(self) -> List[Dict]:
        return self.tool_manager.get_tools()

    async def _handle_tool_call(self, tool_call):
        return await self.tool_manager.handle_tool_call(tool_call)

    def increment_sent_message(self) -> None:
        """增加已发送轮次计数（一次 send_message / send_image 工具调用计 1）"""
        self._sent_message_count += 1
        logger.debug(f"已发送轮次计数: {self._sent_message_count}/{self._max_messages}")
        if self._max_messages > 0 and self._sent_message_count >= self._max_messages:
            logger.info(f"已达到本轮策略中计划发送的轮次数量限制 ({self._max_messages})")

    def get_max_messages(self) -> int:
        """获取当前设置的最大发送轮次数量"""
        return self._max_messages

    def get_sent_message_count(self) -> int:
        """获取已发送轮次数量"""
        return self._sent_message_count

    async def _user_input(self) -> str:
        return await self._input_manager.get_user_input()
    
    async def _inject_first_user_message(self) -> bool:
        """注入首条 user 消息（唯一注入点）。"""
        user_input = await self._user_input()
        if user_input:
            self.history_manager.append({"role": "user", "content": user_input})
            return True
        return False
    
    async def _inject_incremental_user_message(self) -> bool:
        """注入后续增量 user 消息（唯一注入点）。"""
        user_input = await self._user_input()
        if user_input:
            self.history_manager.append({"role": "user", "content": user_input})
            return True
        return False

    async def _call_llm(self):
        tools = self.get_tools()
        messages = self.history_manager.get_history()
        
        llm_messages = [
            {"role": "system", "content": get_full_system_prompt()}
        ] + messages
        response = await call_llm(
            level='flash',
            messages=llm_messages,
            tools=tools,
        )
        return response.choices[0].message
