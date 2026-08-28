# -*- coding: utf-8 -*-
"""观点 Agent - 接受任务输入，根据现状总结和必要的工具查询，形成观点并交付。

按 `lightweight_agent.LightweightAgent` 的五段式骨架订正：
  1. 前处理 `_preprocess`    —— 透传已拼装好的 user 消息
  2. LLM 循环 `_llm_loop`    —— 复用父类骨架（mode='advanced-reason'）
  3. 是否继续 `_should_continue` —— 有工具调用继续，否则终止
  4. 工具处理 `_process_tool` —— 复用父类实现
  5. 后处理 `_postprocess`   —— 返回 LLM 最终文本作为观点
"""

from typing import Dict, List, Any, Optional

from brain.base.light_agent.lightweight_agent import LightweightAgent
from brain.pfc.pfca.pfca_tools import PFCA_tool_manager
from utils.logger import get_logger
from utils.error_handling import async_handle_errors

logger = get_logger('opinion_agent', "DEBUG")


class OpinionAgent(LightweightAgent):
    """
    观点 Agent
    接受任务输入，根据现状总结和必要的工具查询，形成观点并交付。
    支持多轮工具调用，只有当 LLM 直接输出（无 tool_calls）时才结束。
    """

    def __init__(self, current_summary: str = ""):
        self.current_summary = current_summary
        self.tool_manager = PFCA_tool_manager()
        super().__init__()

    def _initialize_tool_functions(self) -> Dict[str, Any]:
        return {
            "query_address_book": self.query_address_book,
            "query_conversation_history": self.query_conversation_history,
        }

    def get_available_tools(self) -> List[Dict[str, Any]]:
        return [
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
你是一个社交智能体的一部分，在这个系统中，你是一个"观点分析师"，负责根据给定的现状总结形成观点。
你的名字是{role_config.ASSISTANT_NAME}
【你的任务】
1. 分析当前现状总结
2. 如需要更多信息，可以使用 query_address_book 查询通讯录，或使用 query_conversation_history 查询会话历史
3. 形成观点，直接作为文本输出

【观点要求】
- 观点应明确
- 需要包含对当前情况的分析和判断
- 可以使用工具查询必要信息后再形成观点
- 当你已经获取足够信息形成观点时，请直接输出观点，不要再调用工具

禁止输出 markdown 格式。
'''

    # ===================== 五段式骨架 =====================
    def _preprocess(self, query: str) -> Any:
        """前处理：透传已拼装好的 user 消息。"""
        return query

    def _should_continue(self, response_message, results: List[Dict[str, Any]]) -> bool:
        """是否继续：有工具调用则继续执行并回灌；无工具调用（LLM 收尾）则终止。"""
        return bool(response_message.tool_calls)

    def _postprocess(self, content: str, extra_info: Any) -> Dict[str, Any]:
        """后处理：返回 LLM 最终文本作为观点。"""
        return {"opinion": content}

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
    async def query_address_book(self, chat_type: str) -> str:
        return await self.tool_manager._query_address_book(chat_type)

    async def query_conversation_history(self, session_id: str, search_range: str) -> str:
        return await self.tool_manager.query_history(
            session_id=session_id,
            search_range=search_range
        )

    # ===================== 统一入口（三段式骨架） =====================
    @async_handle_errors()
    async def run(self) -> Dict[str, Any]:
        query = f"""【现状梗概】
{self.current_summary}"""

        # 三段式骨架：前处理 → LLM 循环 → 后处理
        processed_input = self._preprocess(query)
        loop_result = await self._llm_loop(processed_input, mode='advanced-reason')
        content = loop_result.get("content", "")
        extra_info = loop_result.get("extra_info")
        return self._postprocess(content, extra_info)
