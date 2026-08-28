# -*- coding: utf-8 -*-
"""social_chat 会话循环。

与 conversation_session 的关键差异：
- 无 strategy_check_agent（social_chat 由 social_agent 驱动，不使用 PFCA 策略机）
- 无 emotion 持久化（不写 emotion_results）
- 由 social_chat_core 主动调用 request_stop() 终止循环

继承重型 Session 基类（HeavySession），复用固定骨架 process_cycle() / stop()。
start() 保留原注入顺序（先取 user 输入，再判断是否继续）。
"""

import time
import asyncio
from typing import List, Dict, Optional

from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from utils.llm_client_manager import call_llm

from brain.base import HeavySession
from brain.chat.chat_history_manager import get_history_manager
from brain.chat.social_chat.social_chat_input_manager import get_social_chat_input_manager
from brain.chat.social_chat.social_chat_prompt import get_social_chat_system_prompt
from brain.chat.social_chat.social_chat_tool_manager import SocialChatToolManager

logger = get_logger('SocialChatSession', "DEBUG")


class SocialChatSession(HeavySession):
    PREFIX = 'social_chat'
    TYPING_SPEED_CHARS_PER_MINUTE = 100
    TYPING_DELAY_PER_CHAR = 60.0 / TYPING_SPEED_CHARS_PER_MINUTE

    def __init__(
        self,
        social_chat_core,
        preset_messages: list,
        enforcement_level: str,
        max_self_loops: int = 30,
        max_no_tool_call: int = 4,
        min_response_interval: float = 0.0,
    ):
        """构造会话循环。

        master / working 会话 ID 一律从 social_chat_core 读，调用方不传。
        social_chat_core 必须非 None——未注入就直接崩溃（符合 user_rules）。
        """
        if social_chat_core is None:
            raise ValueError("SocialChatSession 缺少 social_chat_core 引用")
        # 顺手校验 core 已 start()；如未启动直接抛错阻断
        _ = social_chat_core.master_session_id
        _ = social_chat_core.working_session_id

        self.social_chat_core = social_chat_core
        self.enforcement_level = enforcement_level

        self.history_manager = get_history_manager(prefix=self.PREFIX)
        self.tool_manager = SocialChatToolManager(
            social_chat_core=social_chat_core,
            preset_messages=preset_messages,
            enforcement_level=enforcement_level,
        )
        self._input_manager = get_social_chat_input_manager()

        self.self_loop_count = 0
        self.no_tool_call_count = 0
        self.max_self_loops = max_self_loops
        self.max_no_tool_call = max_no_tool_call
        self.min_response_interval = min_response_interval
        self.last_response_time = 0.0
        self.running = True

        self._exit_reason: Optional[str] = None
        self._exit_content: Optional[str] = None
        # SocialChat 不使用工具参数错误上限（max_tool_error=None 表示不启用）
        self.tool_error_count = 0
        self.max_tool_error = None
        self._rounds = 0

    def get_tools(self) -> List[Dict]:
        return self.tool_manager.get_tools()

    async def _handle_tool_call(self, tool_call):
        return await self.tool_manager.handle_tool_call(tool_call)

    async def _check_response_interval(self) -> None:
        if self.min_response_interval <= 0:
            return
        now = time.time()
        elapsed = now - self.last_response_time
        if elapsed < self.min_response_interval:
            await asyncio.sleep(self.min_response_interval - elapsed)

    @async_handle_errors()
    async def should_continue(self) -> bool:
        if not self.running:
            return False
        if self.social_chat_core.is_stop_requested():
            logger.info("social_chat 收到 core 的停止请求")
            return False
        if self.self_loop_count >= self.max_self_loops:
            self._exit_reason = f"达到最大自我循环次数 ({self.max_self_loops})"
            return False
        await self._check_response_interval()
        return True

    async def stop(self) -> None:
        self.running = False
        logger.debug("SocialChatSession 已停止")

    async def start(self) -> None:
        """启动会话循环（保留原注入顺序：先取 user 输入，再判断是否继续）。"""
        logger.debug("social_chat 启动会话循环")
        self.self_loop_count = 0
        self.no_tool_call_count = 0
        self.last_response_time = 0.0
        self.running = True
        self.history_manager.archive_context()

        while self.running and self.self_loop_count < self.max_self_loops:
            user_input = await self._user_input()
            if user_input:
                self.self_loop_count = 0
                self.history_manager.append({"role": "user", "content": user_input})

            if not await self.should_continue():
                logger.info("social_chat 会话结束条件满足，退出循环")
                break

            await self.process_cycle()

        if self.self_loop_count >= self.max_self_loops:
            self._exit_reason = f"达到最大自我循环次数 ({self.max_self_loops})"
            logger.error(self._exit_reason)

    async def _user_input(self) -> str:
        result = await self._input_manager.get_user_input()
        return result or ""

    async def _inject_single_user_message(self) -> bool:
        """用户输入已在 start() 循环顶部注入，此处不再重复注入。"""
        return False

    async def _on_cycle_start(self) -> None:
        """每轮开始：记录响应时间戳（供响应间隔检查使用）。"""
        self.last_response_time = time.time()

    async def _call_llm(self):
        tools = self.get_tools()
        messages = self.history_manager.get_history()
        llm_messages = [
            {"role": "system", "content": get_social_chat_system_prompt()}
        ] + messages
        return await call_llm(
            mode="basic-reason",
            messages=llm_messages,
            tools=tools,
        )
