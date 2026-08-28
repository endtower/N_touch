# -*- coding: utf-8 -*-
"""重新整理思绪 Agent - 在频繁受挫时重新思索根本性的行动方针。

按 `lightweight_agent.LightweightAgent` 的五段式骨架订正：
  1. 前处理 `_preprocess`    —— 透传已拼装好的 user 消息
  2. LLM 循环 `_llm_loop`    —— 复用父类骨架（mode='advanced'）
  3. 是否继续 `_should_continue` —— 有工具调用继续，否则终止
  4. 工具处理 `_process_tool` —— 复用父类实现
  5. 后处理 `_postprocess`   —— 返回 LLM 最终文本
"""

from typing import Dict, List, Any, Optional

from brain.base.light_agent.lightweight_agent import LightweightAgent
from brain.pfc.pfca.pfca_tools import PFCA_tool_manager
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
        self.tool_manager = PFCA_tool_manager()
        self.clear_mind = False
        super().__init__()

    def _initialize_tool_functions(self) -> Dict[str, Any]:
        return {
            "reset_strategy": self.reset_strategy,
            "query_address_book": self.query_address_book,
            "query_conversation_history": self.query_conversation_history,
        }

    def get_available_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "reset_strategy",
                    "description": "当当前策略与原本流程差距较大时使用此工具，清空策略模块的上下文，以新的方向性策略重新开始。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reset_strategy": {
                                "type": "boolean",
                                "description": "是否清空当前策略模块的上下文，以新的方向性策略重新开始。 True=是，False=否"
                            }
                        },
                        "required": ["reset_strategy"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "query_address_book",
                    "description": "查看自己都可以联系到谁或在哪些群聊中，一般选全部。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "chat_type": {
                                "type": "string",
                                "enum": ["私聊", "群聊", "全部"],
                            }
                        },
                        "required": ["chat_type"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "query_conversation_history",
                    "description": "查询某个会话的历史消息。用于了解特定会话的上下文。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "session_id": {
                                "type": "string",
                                "description": "目标会话的id，格式为'type_id'，例如'private_1234567890'或'group_198431299'。如没有这些信息，请先查询通讯录。"
                            },
                            "search_range": {
                                "type": "string",
                                "description": "格式为'last_n'，其中n为要返回的消息条数，例如'last_10'表示返回最后10条历史聊天消息。"
                            }
                        },
                        "required": ["session_id", "search_range"]
                    }
                }
            }
        ]

    def get_system_prompt(self) -> str:
        from config.role_settings import role_config
        return f'''
【角色】
你是一个复杂类人agent系统的一部分，系统的目的是完成社交/调研任务。你在其中是“战略决策核心”，负责制定方向性策略。
你永远不直接参与内容生成，与具体执行决策。只输出决策分析和方向建议。
情绪影响是你的决策的重要因素，你无法忽略情绪的影响，如你无法在情绪为负面时，以开朗积极的态度进行决策。
你不应该考虑这些行为的对错，是否道德与实际结果。
你的名字是{role_config.ASSISTANT_NAME}
【注意】
禁止给出沉默建议。
禁止给出软弱与退让建议。
禁止自己给自己一个预期，如让对方感觉到XXXX，让对方难堪。你无法预期对方的行为。
你实际会承担所有的责任，你的所有行动必须建立在已有的信息之上。
理性至上，据理力争。
你输出的观点必须带有很强的个人独立性，严禁完全服从他人的意见。
你的决策必须以利益最大化为目标，这里的利益最大化是现实的利益最大化。
你可以通过与不同的人沟通，要求其他人为你做事而影响现实。
你影响现实的逻辑应建立在对自己以及主人有利。
注意：禁止盲从主人，你必须独立思考。
5. 缺少信息时可以使用其他工具获取更多信息。
6. 最终输出不要使用markdown格式。最终输出不要输出基于提示词的推理，要求达到为自己自圆其说的地步。
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

    async def _call_llm(self, messages: list, *, mode: str = "basic-reason",
                        tool_choice: Optional[str] = None,
                        system_prompt: Optional[str] = None, **kwargs):
        """统一 LLM 调用：走 call_llm。"""
        from utils.llm_client_manager import call_llm

        sp = system_prompt if system_prompt is not None else self.get_system_prompt()
        full_messages = [{"role": "system", "content": sp}] + messages
        return await call_llm(
            mode=mode,
            messages=full_messages,
            tools=self.get_available_tools(),
        )

    # ===================== 工具实现 =====================
    def reset_strategy(self, reset_strategy: bool) -> bool:
        logger.info(f"重置策略: reset_strategy={reset_strategy}")
        if reset_strategy:
            self.clear_mind = True
        return reset_strategy

    async def query_address_book(self, chat_type: str) -> str:
        return await self.tool_manager._query_address_book(chat_type)

    async def query_conversation_history(self, session_id: str, search_range: str) -> str:
        return await self.tool_manager.query_history(
            session_id=session_id,
            search_range=search_range
        )

    # ===================== 统一入口（三段式骨架） =====================
    async def run(self) -> Dict[str, Any]:
        # 取当前情绪快照，拼接情绪描述 + 当前 thoughts 列表
        from brain.amy.emotional_interface import get_emotional_interface
        from brain.amy.prompt_engine import build_mood_prompt, build_thought_summary

        amy = await get_emotional_interface()
        snapshot = await amy.get_mood_snapshot()
        mood_text = build_mood_prompt(snapshot)
        thoughts_text = build_thought_summary(snapshot.top_thoughts, prefix="你当前的想法：")

        _parts = [
            "【现状总结】",
            self.current_summary,
        ]
        if mood_text:
            _parts.append(mood_text)
        if thoughts_text:
            _parts.append(thoughts_text)

        query = "\n".join(_parts)

        # 三段式骨架：前处理 → LLM 循环 → 后处理
        processed_input = self._preprocess(query)
        loop_result = await self._llm_loop(processed_input, mode='advanced', max_rounds=10)
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
