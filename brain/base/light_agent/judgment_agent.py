# -*- coding: utf-8 -*-
"""判断 Agent - 基于核心身份、当前感觉和短期记忆进行判断。

按 `lightweight_agent.LightweightAgent` 的五段式骨架订正。
核心入口 `make_judgment` 直接调用 `_call_llm` 并解析 judgment_result 工具返回。
"""

import json
from typing import Dict, List, Any, Optional

from brain.base.light_agent.lightweight_agent import LightweightAgent
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from brain.pfc.pfca.pfca_prompt import get_full_system_prompt
from utils.dynamic_prompt_manager import get_dynamic_prompt


logger = get_logger('judgment_agent', "DEBUG")


class JudgmentAgent(LightweightAgent):
    """判断Agent，用于基于核心身份、当前感觉和短期记忆进行判断"""

    def _initialize_tool_functions(self) -> Dict[str, Any]:
        return {}

    def get_available_tools(self) -> List[Dict[str, Any]]:
        """获取可用的工具列表"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "judgment_result",
                    "description": "判断结果返回工具",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "thinking_mode": {
                                "type": "string",
                                "description": "思考模式选择：不思考、基本思考、缜密思考",
                                "enum": ["no_think", "basic_think", "deep_think"]
                            }
                        },
                        "required": ["thinking_mode"]
                    }
                }
            }
        ]

    def get_system_prompt(self) -> str:
        get_full_system_prompt()

        return f'''
你需要带入当前事件的【目标角色】和【当前感觉】，并根据这些信息，根据当前事件的聊天记录选择合适的思考模式。
【目标角色】{get_dynamic_prompt("role_name")}
【当前感觉】{get_dynamic_prompt("amy_effect")}
请选择合适的思考模式：
1. 不思考：适用于靠直觉，不需要思考就能解决的任务与聊天环境，如没有目的的闲聊。
- 当你被要求快速思考时，选择基本思考。
2. 基本思考：用于基本策略调整，如是否参与事件，是否执行事件，能处理稍复杂的问题。
3. 深度思考：集合了基本思考的方向性结论，格外输出了完整的分析以及操作步骤，处理最复杂，困难的问题。
- 被认为表现平庸、需要改进时，选择深度思考。
- 当你否定时，选择深度思考。

请使用judgment_result工具返回判断结果。
'''

    # ===================== 五段式骨架（make_judgment 直接调 _call_llm，其余给默认实现） =====================
    def _preprocess(self, query: str) -> Any:
        return query

    def _should_continue(self, response_message, results: List[Dict[str, Any]]) -> bool:
        return bool(response_message.tool_calls)

    def _postprocess(self, content: str, extra_info: Any) -> Dict[str, Any]:
        return {"content": content, "extra_info": extra_info}

    async def _call_llm(self, messages: list, *, mode: str = "basic-reason",
                        tool_choice: Optional[str] = None,
                        system_prompt: Optional[str] = None, **kwargs):
        """统一 LLM 调用：走 call_llm（mode='basic'）。"""
        from utils.llm_client_manager import call_llm

        sp = system_prompt if system_prompt is not None else self.get_system_prompt()
        full_messages = [{"role": "system", "content": sp}] + messages
        return await call_llm(
            mode=mode,
            messages=full_messages,
            tools=self.get_available_tools(),
            **kwargs,
        )

    # ===================== 核心入口 =====================
    @async_handle_errors()
    async def make_judgment(self, messages=None) -> str:
        if messages is None:
            messages = []

        user_messages = []
        # 只有当有用户输入时才添加用户消息
        if messages:
            user_messages.append({"role": "user", "content": messages})

        response = await self._call_llm(
            user_messages,
            mode='basic',
            timeout=30,
        )

        assistant_message = response.choices[0].message
        if not assistant_message.tool_calls:
            return {"thinking_mode": "no_think"}
        for tool_call in assistant_message.tool_calls:
            function_args = json.loads(tool_call.function.arguments)
            return function_args
