# -*- coding: utf-8 -*-
"""轻型 Agent 基类：无状态、单次任务、不持久化上下文的原子执行单元。

设计文档：docs/lightweight_agent_base_design.md

本基类抽象出轻型 agent 的五个核心段落：
  1. 前处理 `_preprocess`   —— LLM 进入正式循环之前，对输入信息进行初次处理。
  2. LLM 循环 `_llm_loop`   —— 正式循环主体，内部通过 `_should_continue` 判断是否继续。
  3. 是否继续 `_should_continue` —— 完成循环标记方法，决定循环是否终止。
  4. 工具处理 `_process_tool` —— 执行单个工具调用。
  5. 后处理 `_postprocess`  —— LLM 退出循环之后，拼装最终返回值。

子类统一入口需自行拼接：前处理 → LLM 循环 → 后处理（可参考 `_llm_loop` 的复用方式）。
最终返回值固定为 `{content: str, extra_info}` 格式。
"""

import json
from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional, Callable

from utils.logger import get_logger

logger = get_logger('lightweight_agent', "DEBUG")


class LightweightAgent(ABC):
    """轻型 Agent 基类：无状态、单次任务、不持久化上下文的原子执行单元"""

    def __init__(self):
        self.tool_functions = self._initialize_tool_functions()

    # ============ 基础设施抽象方法（LLM 循环依赖） ============

    @abstractmethod
    def _initialize_tool_functions(self) -> Dict[str, Callable]:
        """工具名 → async 函数 的映射表"""

    @abstractmethod
    def get_available_tools(self) -> List[Dict[str, Any]]:
        """工具 schema 列表"""

    @abstractmethod
    def get_system_prompt(self) -> str:
        """系统提示词"""

    # ============ 五个核心段落（抽象方法） ============

    @abstractmethod
    def _preprocess(self, query: str) -> Any:
        """前处理：LLM 进入正式循环之前，对输入信息进行初次处理。

        返回处理后的输入，作为 `_llm_loop` 的入参。
        """

    @abstractmethod
    def _should_continue(self, response_message, results: List[Dict[str, Any]]) -> bool:
        """是否继续：完成循环标记方法，判断是否继续循环。

        Args:
            response_message: 最近一次 LLM 响应消息。
            results: 已执行的工具结果列表。

        Returns:
            True 继续循环；False 终止循环。
        """

    async def _process_tool(self, function_name: str, function_args: dict) -> Any:
        """工具处理：执行单个工具调用，返回工具结果。

        从 self.tool_functions 查找并执行；未知函数返回错误提示。
        """
        if function_name in self.tool_functions:
            return await self.tool_functions[function_name](**function_args)
        return f"未知函数: {function_name}"

    async def _llm_loop(self, processed_input: Any, *, mode: str = "basic-reason",
                        max_rounds: int = 10) -> Dict[str, Any]:
        """LLM 循环：正式循环主体。

        参考 heavy_session.process_cycle 的循环骨架，适配无状态场景：
        调 LLM → 判断是否继续 → 有工具则执行并回灌 → 直到 _should_continue 返回 False。

        Args:
            processed_input: `_preprocess` 处理后的输入（作为首条 user 消息）。
            mode: LLM 模式（basic / basic-reason / advanced / advanced-reason）。
            max_rounds: 最大循环轮数（防死循环烧 token）。

        Returns:
            {"content": str, "extra_info": {"tools_called": [...]}} 循环产出。
        """
        messages = [{"role": "user", "content": processed_input}]
        system_prompt = self.get_system_prompt()
        results: List[Dict[str, Any]] = []

        for _ in range(max_rounds):
            response = await self._call_llm(messages, mode=mode, tool_choice="auto",
                                            system_prompt=system_prompt)
            response_message = response.choices[0].message

            # 是否继续：完成循环标记方法
            if not self._should_continue(response_message, results):
                return {
                    "content": response_message.content or "",
                    "extra_info": {"tools_called": results},
                }

            if response_message.tool_calls:
                # 有工具调用：执行并回灌
                assistant_msg = {
                    "role": "assistant",
                    "content": response_message.content,
                    "tool_calls": [{
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                    } for tc in response_message.tool_calls],
                }
                reasoning_content = getattr(response_message, 'reasoning_content', None) or getattr(response_message, 'thinking', None)
                if reasoning_content:
                    assistant_msg["reasoning_content"] = reasoning_content
                messages.append(assistant_msg)

                for tool_call in response_message.tool_calls:
                    function_name = tool_call.function.name
                    try:
                        function_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        function_args = {}
                    result = await self._process_tool(function_name, function_args)
                    results.append({"tool": function_name, "result": result})
                    messages.append({
                        "role": "tool",
                        "content": str(result),
                        "tool_call_id": tool_call.id,
                    })
            else:
                # 无工具调用：注入警告 user 消息，继续下一轮 LLM 输出
                messages.append({
                    "role": "user",
                    "content": "【系统警告】未使用工具，请使用工具回复。",
                })

        logger.warning(f"{self.__class__.__name__} 达到 max_rounds={max_rounds}，强制收尾")
        return {
            "content": "",
            "extra_info": {"tools_called": results},
        }

    @abstractmethod
    def _postprocess(self, content: str, extra_info: Any) -> Dict[str, Any]:
        """后处理：LLM 退出循环之后，拼装最终返回值。

        Args:
            content: 循环产出的最终文本。
            extra_info: 循环产出的附加信息。

        Returns:
            固定格式 {"content": str, "extra_info": ...}。
        """

    @abstractmethod
    async def _call_llm(self, messages: list, *, mode: str = "basic-reason",
                        tool_choice: Optional[str] = None,
                        system_prompt: Optional[str] = None, **kwargs):
        """统一 LLM 调用：由子类实现具体调用方式（如识图 agent 走视觉模型）。

        `_llm_loop` 依赖本方法完成每次模型调用，因此必须由子类提供实现。

        Args:
            messages: 不含 system 的消息列表（user / assistant / tool）。
            mode: LLM 模式（basic / basic-reason / advanced / advanced-reason）。
            tool_choice: 工具选择策略（"none" / "auto" / None）。
            system_prompt: 覆盖系统提示词；默认取 self.get_system_prompt()。

        Returns:
            OpenAI 风格响应对象（含 .choices[0].message）。
        """
