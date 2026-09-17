# -*- coding: utf-8 -*-
"""重新整理思绪 Agent - 在频繁受挫时重新思索根本性的行动方针。

按 `lightweight_agent.LightweightAgent` 的五段式骨架订正：
  1. 前处理 `_preprocess`    —— 透传已拼装好的 user 消息
  2. LLM 循环 `_llm_loop`    —— 复用父类骨架（level='flash'）
  3. 是否继续 `_should_continue` —— 有工具调用继续，否则终止
  4. 工具处理 `_process_tool` —— 复用父类实现
  5. 后处理 `_postprocess`   —— 返回 LLM 最终文本
"""

from functools import partial
from typing import Dict, List, Any, Optional

from brain.base.light_agent.lightweight_agent import LightweightAgent
from brain.base import ToolSocial
from utils.logger import get_logger
from utils.error_handling import async_handle_errors

logger = get_logger('reorganize_agent', "DEBUG")


class ReorganizeAgent(LightweightAgent):
    """
    重新整理思绪 Agent
    在频繁受挫时选择该参数进行思考，将会重新思索根本性的行动方针。
    以确定什么是优先任务。
    """

    def __init__(self, current_summary: str = ""):
        self.current_summary = current_summary
        self.clear_mind = False
        super().__init__()

    def _initialize_tool_functions(self) -> Dict[str, Any]:
        return {
            "query_address_book": ToolSocial.query_address_book,
            # consumer_id 显式传 "pfca"：LLM 已读到的消息推进 pfca 游标，truck 不再重复触发
            "query_history": partial(ToolSocial.query_history, consumer_id="pfca"),
        }

    def get_available_tools(self) -> List[Dict[str, Any]]:
        return ToolSocial.get_query_tool_schemas()

    def get_system_prompt(self) -> str:
        from config.role_settings import role_config
        return f'''
【角色】
你是一个复杂类人agent系统的一部分，你在其中是“战略决策核心”，负责制定方向性策略。
你永远不直接参与内容生成，与具体执行决策。
你的名字是{role_config.ASSISTANT_NAME}
【注意】
你接收到的信息可能存在相当的主观性，你需要根据信息进行判断。
你的分析需要是方向性、客观性的，你需要相当理性的思考这些信息，谨慎的建议，而是更多给出一些自己对于当前局势的分析。
你的分析不能是就外部告知的问题而进行分析， 因为这些问题存在很强的误导与错误，一定要跳出问题的框架进行思考。
考虑的维度要在多个角度进行分析，包括但不限于、双边身份、逻辑事理。你需要以理性的长远实际发展出发进行考虑，而不是以及时情绪为准。
分析要重点说明，哪些是可信的，哪些更可能是主观臆断，哪些可能是真实的。
你的建议不能是和稀泥的中性建议，若你没有十足的把握，就不应就问题提出中性建议等，而是转化出更加务实的行动方向，如信息的确认、询问等。
缺少信息时可以使用其他工具获取更多信息。
最终输出不要使用markdown格式。
'''

    # ===================== 五段式骨架 =====================
    def _preprocess(self, query: str) -> Any:
        """前处理：透传已拼装好的 user 消息。"""
        return query

    def _should_continue(self, response_message, results: List[Dict[str, Any]]) -> bool:
        """是否继续：有工具调用则继续执行并回灌；无工具调用（LLM 收尾）则终止。"""
        return bool(response_message.tool_calls)

    def _postprocess(self, content: str, extra_info: Any) -> Dict[str, Any]:
        """后处理：返回 LLM 最终文本。"""
        return {"content": content, "extra_info": extra_info}

    async def _call_llm(self, messages: list, *, level: str = "flash",
                        tool_choice: Optional[str] = None,
                        system_prompt: Optional[str] = None, **kwargs):
        """统一 LLM 调用：走 call_llm。"""
        from utils.llm_client_manager import call_llm

        sp = system_prompt if system_prompt is not None else self.get_system_prompt()
        full_messages = [{"role": "system", "content": sp}] + messages
        return await call_llm(
            level=level,
            messages=full_messages,
            tools=self.get_available_tools(),
        )



    # ===================== 统一入口（三段式骨架） =====================
    async def run(self) -> Dict[str, Any]:
        # 通用上下文管理器装配：当前环境 + AMY 情绪 provider（含 top_thoughts 摘要）+ 现状总结
        from brain.base.light_agent.context_manager import build_default_context

        manager = build_default_context()
        manager.add_block("current_summary", f"【现状总结】\n{self.current_summary}")
        query = await manager.build()

        # 三段式骨架：前处理 → LLM 循环 → 后处理
        processed_input = self._preprocess(query)
        loop_result = await self._llm_loop(processed_input, max_rounds=10)
        decision_thought = loop_result.get("content", "")

        if not decision_thought:
            logger.warning("达到最大轮次限制，强制结束")
            decision_thought = "无法在规定时间内形成决策"
        else:
            tools_called = (loop_result.get("extra_info") or {}).get("tools_called") or []
            logger.info(f"决策完成，共 {len(tools_called)} 次工具调用")

        return {
            "clear_mind": self.clear_mind,
            "decision_thought": decision_thought
        }
