# -*- coding: utf-8 -*-
"""事件检索 Agent - 用于检索记忆（事件）。

按 `lightweight_agent.LightweightAgent` 的五段式骨架订正：
  1. 前处理 `_preprocess`    —— 透传查询
  2. LLM 循环 `_llm_loop`    —— 复用父类骨架
  3. 是否继续 `_should_continue` —— 有工具调用继续，否则终止
  4. 工具处理 `_process_tool` —— 复用父类实现
  5. 后处理 `_postprocess`   —— 所有检索工具均为终结工具，命中即返回该工具结果
"""

from typing import Dict, List, Any, Optional

from brain.base.light_agent.lightweight_agent import LightweightAgent
from brain.hc.database.event import EventOperations
from utils.logger import get_logger
from utils.error_handling import async_handle_errors

logger = get_logger('EventAgent', "DEBUG")

SEARCH_HINT = "可以通过大概的时间，对目标会话进行聊天记录检索，来获取完整的事件聊天记录原文。"


class EventAgent(LightweightAgent):
    """事件管理 Agent，用于检索记忆（事件）"""

    SYSTEM_PROMPT = """你是记忆检索助手，负责根据用户问题检索相关事件记忆。

工作流程：
1. 分析用户问题，提取关键信息（人物、关键词等）
2. 使用搜索工具检索相关事件
3. 整合检索结果，直接输出答案

重要规则：
- 根据问题内容选择合适的搜索方式（按标题、按角色、按关键词）
- 检索到足够信息后直接输出结果，不要调用多余的工具
- 输出时尽量体现时间、环境（会话可能发生于xxx的私聊会话或者是群聊）、人物三个要素
- 不要使用markdown格式（如加粗、列表符号等）
- 使用自然段落形式输出，直接回答用户问题"""

    def __init__(self):
        super().__init__()
        self.event_ops = EventOperations()

    def _initialize_tool_functions(self) -> Dict[str, Any]:
        """初始化工具映射表"""
        return {
            "search_event_by_title": self.search_event_by_title,
            "search_events_by_character": self.search_events_by_character,
            "search_events_by_vector": self.search_events_by_vector
        }

    def get_available_tools(self) -> List[Dict[str, Any]]:
        """获取可用的工具列表，用于LLM的function call"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_event_by_title",
                    "description": "通过事件名称精确查询事件。如果用户提到了具体的事件名称或标题，使用此工具",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "event_title": {
                                "type": "string",
                                "description": "事件的完整标题"
                            }
                        },
                        "required": ["event_title"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "search_events_by_character",
                    "description": "通过参与角色名称查询相关事件。如果用户提到与某人的互动、约定、交流等，使用此工具",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "character_name": {
                                "type": "string",
                                "description": "角色名称"
                            },
                            "limit": {
                                "type": "integer",
                                "description": "返回结果数量限制，默认5",
                                "default": 5
                            }
                        },
                        "required": ["character_name"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "search_events_by_vector",
                    "description": "通过语义关键词检索相关事件。当不确定具体人物或标题时，使用此工具进行模糊搜索",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query_text": {
                                "type": "string",
                                "description": "查询关键词，用于语义检索"
                            },
                            "top_k": {
                                "type": "integer",
                                "description": "返回结果数量，默认5",
                                "default": 5
                            }
                        },
                        "required": ["query_text"]
                    }
                }
            }
        ]

    def get_system_prompt(self) -> str:
        """获取系统提示词"""
        return self.SYSTEM_PROMPT

    # ===================== 1. 前处理 =====================
    def _preprocess(self, query: str) -> Any:
        """前处理：透传查询作为首条 user 消息。"""
        return query

    # ===================== 3. 是否继续 =====================
    def _should_continue(self, response_message, results: List[Dict[str, Any]]) -> bool:
        """是否继续：有工具调用则继续执行并回灌；无工具调用（LLM 收尾）则终止。"""
        return bool(response_message.tool_calls)

    # ===================== 5. 后处理 =====================
    def _postprocess(self, content: str, extra_info: Any) -> str:
        """后处理：所有检索工具均为终结工具，命中即返回该工具结果。"""
        tools_called = (extra_info or {}).get("tools_called") or []
        if tools_called:
            first = tools_called[0]
            exit_result = first.get("result")
            if exit_result is not None:
                return f"{exit_result}\n\n{SEARCH_HINT}"
        return f"{content}\n\n{SEARCH_HINT}"

    # ===================== LLM 调用 =====================
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
    @async_handle_errors()
    async def search_event_by_title(self, event_title: str) -> str:
        """通过事件名称精确查询事件"""
        event_id = await self.event_ops.get_event_id_by_title(event_title)

        if not event_id:
            return f"未找到标题为「{event_title}」的事件"

        formatted = await self.event_ops.format_event_by_id(event_id)
        logger.debug(f"按标题查询事件: {event_title}")
        return formatted

    @async_handle_errors()
    async def search_events_by_character(self, character_name: str, limit: int = 5) -> str:
        """通过参与角色查询相关事件"""
        event_ids = await self.event_ops.search_events_by_session_name(character_name)

        if not event_ids:
            return f"未找到与「{character_name}」相关的事件"

        event_ids = event_ids[:limit]
        formatted = await self.event_ops.format_events_by_ids(event_ids)

        logger.debug(f"按角色查询事件: {character_name}, 结果数量: {len(event_ids)}")
        return formatted

    @async_handle_errors()
    async def search_events_by_vector(self, query_text: str, top_k: int = 5) -> str:
        """通过向量相似度检索相关事件"""
        event_ids = await self.event_ops.search_event_by_vector(query_text, top_k)

        if not event_ids:
            return f"未找到与「{query_text}」相关的事件"

        formatted = await self.event_ops.format_events_by_ids(event_ids)

        logger.debug(f"向量检索事件: {query_text}, 结果数量: {len(event_ids)}")
        return formatted

    # ===================== 统一入口（三段式骨架） =====================
    @async_handle_errors()
    async def run(self, query: str) -> str:
        """运行 Agent 处理查询，并在结果后添加提示"""
        processed_input = self._preprocess(query)
        loop_result = await self._llm_loop(processed_input, mode='basic-reason')
        content = loop_result.get("content", "")
        extra_info = loop_result.get("extra_info")
        return self._postprocess(content, extra_info)
