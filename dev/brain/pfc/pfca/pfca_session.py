# -*- coding: utf-8 -*-
"""
PFCA Session - 统一的消息注入器

负责：
1. 构造并注入 user 消息到 history（唯一注入点）
2. 管理 LLM 调用循环
3. 处理工具调用

设计原则：
- 唯一注入点：所有 user 消息必须通过本模块构造并注入 history
- 单条限制：每轮 LLM 调用最多一条 user 消息
- 优先级：首次消息请求 > 增量更新（情绪变化）

基本结构与 conversation_session / social_chat_session 对齐：
- 继承重型 Session 基类（HeavySession），复用固定骨架 start() / process_cycle() / stop()。
- 实现抽象方法 _inject_first_user_message / _inject_incremental_user_message
  （每轮最多一条 user 消息，首条 > 增量 优先级）。
- 工具能力统一由 tool_manager 提供（get_tools / handle_tool_call 委托）。
- 自我保护上限由 config.llm_settings.llm_config 驱动（max_no_tool_call）；空转防护仅保留
  会话累计 token 预算熔断（轮数上限与重复调用检测已禁用，2026-09-09 决策）。
"""
from typing import List, Dict, Any, Literal
from utils.logger import get_logger
from config.llm_settings import llm_config
from brain.base import HeavySession
from brain.pfc.pfca.pfca_history_manager import get_pfca_history_manager
from brain.pfc.pfca.pfca_tools import get_available_tools, get_pfca_tool_manager
from brain.pfc.pfca.pfca_prompt import get_full_system_prompt
from brain.pfc.pfca.pfca_input_manager import get_pfca_input_manager

logger = get_logger('PFCASession', "DEBUG")


class PFCASession(HeavySession):
    """PFCA Session - 统一的消息注入器"""

    # 单会话累计 token 预算（唯一的空转熔断；token_stats.json 实测 pfca 单次均值 ~15.5k，
    # 正常会话远低于该值，2M 仅拦截病理性空转）。
    # 2026-09-09 决策：PFCA 移除轮数上限与重复调用检测，空转防护仅保留本预算熔断。
    TOKEN_BUDGET = 2_000_000
    # 连续重复调用检测已禁用（None = 不启用，见 HeavySession.repeat_threshold）
    repeat_threshold = None

    def __init__(self):
        self.history_manager = get_pfca_history_manager()
        self.tool_manager = get_pfca_tool_manager()
        self._input_manager = get_pfca_input_manager()

        self.running = True
        self._is_first_cycle = True
        self._exit_reason = None
        self._exit_content = None
        # 自我保护上限：统一从 llm_config 读取（对齐 conversation）
        self.no_tool_call_count = 0
        self.max_no_tool_call = llm_config.MAX_NO_TOOL_CALLS
        self.tool_error_count = 0
        self.max_tool_error = 3
        # 轮数上限已禁用（None = 不限轮数）：空转防护仅保留 token 预算熔断
        self.max_rounds = None
        self._rounds = 0

    def get_tools(self) -> List[Dict]:
        """获取工具列表（委托给 tool_manager）"""
        return get_available_tools()

    async def _handle_tool_call(self, tool_call):
        """处理单个工具调用（委托给 tool_manager）"""
        return await self.tool_manager.handle_tool_call(tool_call)

    # ============ 消息注入（唯一注入点，HeavySession 抽象方法） ============

    async def _inject_first_user_message(self) -> bool:
        """注入首条 user 消息（唯一注入点）。

        优先级 1：首次消息请求（由外部 prepare_first_message 准备，
        input_manager 通过 get_user_input 消费）。
        """
        content = await self._input_manager.get_user_input()
        if content:
            self.history_manager.append({"role": "user", "content": content})
            logger.debug(f"注入首次 user 消息: {content[:50]}...")
            return True
        return False

    async def _inject_incremental_user_message(self) -> bool:
        """注入增量 user 消息（唯一注入点）。

        优先级 2：检测 amy 情绪变化，变化时构造增量消息注入。
        """
        incremental_content = await self._input_manager.get_user_input()
        if incremental_content:
            self.history_manager.append({"role": "user", "content": incremental_content})
            logger.debug(f"注入增量 user 消息: {incremental_content[:50]}...")
            return True
        return False

    async def _call_llm(self):
        """调用 LLM 并返回响应"""
        messages = self.history_manager.get_history()
        full_messages = [{"role": "system", "content": get_full_system_prompt()}] + messages

        from utils.llm_client_manager import call_llm, extract_reasoning
        response = await call_llm(
            level='flash',
            messages=full_messages,
            tools=self.get_tools(),
        )
        assistant_message = response.choices[0].message
        logger.debug(f"LLM完整响应: {assistant_message.model_dump()}")
        logger.info(f"LLM响应 content: {assistant_message.content}, reasoning: {extract_reasoning(assistant_message)}")

        # 第一次循环后，标记为非第一次
        if self._is_first_cycle:
            self._is_first_cycle = False

        return assistant_message

    def _should_append_tool_response(self, tool_response: Any) -> bool:
        """已归档上下文的工具响应不写入 history"""
        if isinstance(tool_response, dict) and tool_response.get("archived"):
            logger.info(f"工具 {tool_response.get('name')} 已归档上下文，跳过添加到 history")
            return False
        return True

    def _build_result(self) -> Dict[str, Any]:
        """构造返回值：running 状态 + exit_reason

        返回 dict（非 None），供 pfca_core / core.py 判断处理结果。
        """
        return {
            "running": self.running,
            "exit_reason": self._exit_reason
        }

    async def stop(self, exit_reason: Literal["basic_think", "deep_think", "no_think"] | None = None) -> None:
        """停止会话

        Args:
            exit_reason: 退出原因，可选值：
                - basic_think: 基本思考模式退出
                - deep_think: 缜密思考模式退出
                - no_think: 不进行思考模式退出
                - None: 未指定退出原因
        """
        logger.debug(f"PFCA会话已停止，原因: {exit_reason}")
        self.running = False
        self._exit_reason = exit_reason
