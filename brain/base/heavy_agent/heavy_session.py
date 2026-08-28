# -*- coding: utf-8 -*-
"""重型 Session 基类：有状态、多轮循环、持久化上下文的会话执行单元。

设计文档：docs/heavyweight_session_base_design.md

本基类抽象出 4 个重型 Session（PFCASession / ConversationSession /
SocialChatSession / ProactiveSession）的公共骨架：
- 固定骨架：start() / process_cycle() / stop() 由基类实现，子类不重写。
- 可变点：消息注入（首条 / 增量）、循环终止、自我保护上限、LLM mode
  由子类通过抽象方法实现。

设计原则：
- 唯一注入点：每轮最多一条 user 消息（首条 > 增量 优先级）。
- 自我保护：no_tool_call_count / tool_error_count 上限，超限抛 AbyssCoreError 崩溃。
- 统一返回值：start() 返回 self._build_result()（默认 _exit_content，str）。
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional

from utils.logger import get_logger
from utils.error_handling import AbyssCoreError

logger = get_logger('HeavySession', "DEBUG")


class HeavySession(ABC):
    """重型 Session 基类：有状态、多轮循环、持久化上下文的会话执行单元"""

    # ============ 子类必须实现的抽象方法 ============

    @abstractmethod
    def get_tools(self) -> List[Dict]:
        """获取工具 schema 列表（委托给子类的 *ToolManager）"""

    @abstractmethod
    async def _handle_tool_call(self, tool_call) -> Dict[str, Any]:
        """处理单个工具调用（委托给子类的 *ToolManager）"""

    @abstractmethod
    async def _call_llm(self):
        """调用 LLM（组装 system prompt + history）"""

    @abstractmethod
    async def _inject_first_user_message(self) -> bool:
        """注入首条 user 消息（唯一注入点）。返回是否成功注入。"""

    @abstractmethod
    async def _inject_incremental_user_message(self) -> bool:
        """注入后续增量 user 消息（唯一注入点）。返回是否成功注入。"""

    # ============ 可重写的钩子（默认实现） ============

    async def should_continue(self) -> bool:
        """判断是否继续循环。默认：running 且未达 max_rounds。"""
        return self.running and self._rounds < self.max_rounds

    def _inject_no_tool_call_warning(self) -> None:
        """无工具调用时注入警告 user 消息（持久化上下文，与正常上下文注入逻辑一致）。
        子类可重写以自定义警告文案。"""
        self.history_manager.append({
            "role": "user",
            "content": "【系统警告】未使用工具，请使用工具回复。",
        })

    def _should_append_tool_response(self, tool_response: Any) -> bool:
        """判断工具响应是否写入 history。默认 True。"""
        return True

    def _build_result(self) -> str:
        """构造返回值。默认返回 self._exit_content（str）。"""
        return self._exit_content

    # ============ 固定骨架（基类实现，子类不重写） ============

    async def start(self) -> str:
        """启动会话循环（固定骨架），返回 self._build_result()。"""
        self.running = True
        self._exit_reason = None
        self._exit_content = None
        self.no_tool_call_count = 0
        self.tool_error_count = 0
        self._rounds = 0

        self.history_manager.archive_context()   # 归档上一轮

        while self.running:
            if not await self.should_continue():
                logger.info("会话结束条件满足，退出循环")
                break
            await self.process_cycle()

        return self._build_result()

    async def process_cycle(self) -> None:
        """处理单次 LLM 调用和工具执行（固定骨架）。

        流程：
        1. 自我保护检查（连续 max_no_tool_call 次无工具 / max_tool_error 次工具错误 → 崩溃）。
        2. 注入 user 消息：第一轮注入首条，后续轮次注入增量。
        3. 调用 LLM，并把 LLM 输出的消息注入上下文（持久化）。
        4. 若使用工具：执行工具并回灌，正式进入下一轮循环。
        5. 若未使用工具：注入警告 user 消息，继续调用 LLM，直到使用工具或达到上限。
        """
        # 自我保护检查（max_* 为 None 表示不启用该上限）
        if self.max_no_tool_call is not None and self.no_tool_call_count >= self.max_no_tool_call:
            raise AbyssCoreError(
                f"LLM 累计 {self.no_tool_call_count} 次未使用工具，强制停止"
            )
        if self.max_tool_error is not None and self.tool_error_count >= self.max_tool_error:
            raise AbyssCoreError(
                f"工具调用累计 {self.tool_error_count} 次参数错误，强制停止"
            )

        # 1. 注入 user 消息（第一轮首条，后续增量）
        if self._rounds == 0:
            await self._inject_first_user_message()
        else:
            await self._inject_incremental_user_message()

        # 2. 调用 LLM（内部循环：无工具则注入警告并继续，直到使用工具或达上限）
        while True:
            assistant_message = await self._call_llm()
            if assistant_message is None:
                logger.warning("无效的 LLM 响应格式")
                return

            # 3. 将 LLM 输出的消息注入上下文（持久化）
            self._write_assistant_message(assistant_message)

            # 4. 处理响应
            if assistant_message.tool_calls:
                self.no_tool_call_count = 0
                for tool_call in assistant_message.tool_calls:
                    tool_response = await self._handle_tool_call(tool_call)
                    if isinstance(tool_response, str) and tool_response.startswith("[错误]"):
                        self.tool_error_count += 1
                    if self._should_append_tool_response(tool_response):
                        self.history_manager.append(tool_response)
                break  # 使用工具，正式进入下一轮循环
            else:
                # 5. 无工具调用：注入警告 user 消息，继续下一轮 LLM 输出
                self.no_tool_call_count += 1
                logger.warning(
                    f"LLM 未使用工具 ({self.no_tool_call_count}/{self.max_no_tool_call}): "
                    f"{(assistant_message.content or '')[:50]}..."
                )
                if self.max_no_tool_call is not None and self.no_tool_call_count >= self.max_no_tool_call:
                    raise AbyssCoreError(
                        f"LLM 累计 {self.no_tool_call_count} 次未使用工具，强制停止"
                    )
                self._inject_no_tool_call_warning()

        self._rounds += 1

    def _write_assistant_message(self, assistant_message) -> None:
        """把 LLM 输出的消息注入上下文（持久化）。无论是否带 tool_calls。"""
        assistant_dict = {
            "role": "assistant",
            "content": assistant_message.content,
            "reasoning_content": getattr(assistant_message, 'reasoning_content', None),
        }
        if assistant_message.tool_calls:
            assistant_dict["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in assistant_message.tool_calls
            ]
        self.history_manager.append(assistant_dict)

    async def stop(self, exit_reason: Optional[str] = None) -> None:
        """停止会话（固定骨架）"""
        self.running = False
        if exit_reason is not None:
            self._exit_reason = exit_reason
        logger.debug(f"{self.__class__.__name__} 已停止")
