# -*- coding: utf-8 -*-
"""轻型 Agent 基类：无状态、单次任务、不持久化上下文的原子执行单元。

设计记录：docs/已完成.md（light_agent 框架基类）

本基类抽象出轻型 agent 的五个核心段落：
  1. 前处理 `_preprocess`   —— LLM 进入正式循环之前，对输入信息进行初次处理。
  2. LLM 循环 `_llm_loop`   —— 正式循环主体，内部通过 `_should_continue` 判断是否继续。
  3. 是否继续 `_should_continue` —— 完成循环标记方法，决定循环是否终止。
  4. 工具处理 `_process_tool` —— 执行单个工具调用。
  5. 后处理 `_postprocess`  —— LLM 退出循环之后，拼装最终返回值。

子类统一入口需自行拼接：前处理 → LLM 循环 → 后处理（可参考 `_llm_loop` 的复用方式）。
最终返回值固定为 `{content: str, extra_info}` 格式。
"""

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional, Callable

from utils.logger import get_logger
from utils.llm_client_manager import extract_reasoning

logger = get_logger('lightweight_agent', "DEBUG")


class LightweightAgent(ABC):
    """轻型 Agent 基类：无状态、单次任务、不持久化上下文的原子执行单元"""

    # 事件链上下文本地保留的前缀（子类覆盖以区分 agent 类型，如 persona / vision）：
    # 复用 HistoryManager 存储模型，链内写入 memory/event_chains/{chain_id}/{prefix}_{HHMMSS}.json，
    # 链外写入 unlinked/{prefix}.json，供前端 /context 按链查看本 agent 的完整上下文
    # （system prompt + user 消息 + 各轮思考/工具调用与结果 + 最终结论）。
    history_prefix: str = "light_agent"

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

    async def _llm_loop(self, processed_input: Any, *, level: str = "flash",
                        max_rounds: int = 10) -> Dict[str, Any]:
        """LLM 循环：正式循环主体。

        参考 heavy_session.process_cycle 的循环骨架，适配无状态场景：
        调 LLM → 判断是否继续 → 有工具则执行并回灌 → 直到 _should_continue 返回 False。

        事件链上下文本地保留（2026-09-22 父类机制）：每次调用随 messages 双写
        落盘（system prompt + user 消息 + 各轮思考/工具调用与结果 + 最终结论），
        链内收敛到 event_chains/{chain_id}/{history_prefix}_{HHMMSS}.json，
        链外写 unlinked/{history_prefix}.json——供前端 /context 按链查看结果。
        HistoryManager 内部 @handle_errors 兜底，不影响主流程（对齐 quest 先例）。

        Args:
            processed_input: `_preprocess` 处理后的输入（作为首条 user 消息）。
            level: 模型等级（flash / pro / vision / embedding），统一从 LLMSettings 解析。
            max_rounds: 最大循环轮数（防死循环烧 token）。

        Returns:
            {"content": str, "extra_info": {"tools_called": [...]}} 循环产出。
        """
        from brain.base.light_agent.light_history_manager import get_light_history_manager

        messages = [{"role": "user", "content": processed_input}]
        system_prompt = self.get_system_prompt()
        results: List[Dict[str, Any]] = []

        # 事件链上下文：每次召唤创建独立管理器实例（light agent 无状态，各自独立
        # 上下文文件——尤其多个人格并行召唤绝不共享文件），随后 system + 首条 user 落盘
        from brain.base.light_agent.light_history_manager import new_light_history_manager
        history = new_light_history_manager(self.history_prefix)
        history.append({"role": "system", "content": system_prompt})
        history.append(dict(messages[0]))

        for _ in range(max_rounds):
            response = await self._call_llm(messages, level=level, tool_choice="auto",
                                            system_prompt=system_prompt)
            response_message = response.choices[0].message

            # 是否继续：完成循环标记方法
            if not self._should_continue(response_message, results):
                # 最终结论（无工具调用收尾）落盘后返回
                history.append(self._history_entry(response_message))
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
                reasoning_content = extract_reasoning(response_message)
                if reasoning_content:
                    assistant_msg["reasoning_content"] = reasoning_content
                messages.append(assistant_msg)
                history.append(assistant_msg)

                # 并行执行同轮全部工具调用（2026-09-21）：工具结果按 tool_call_id
                # 回灌，与执行顺序无关，可安全 gather 并行；各工具间假定无相互依赖
                # （同一轮 LLM 并发发起的工具调用本就是独立的）。
                async def _run_one(tool_call):
                    function_name = tool_call.function.name
                    try:
                        function_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        function_args = {}
                    result = await self._process_tool(function_name, function_args)
                    return {"tool": function_name, "result": result, "tool_call_id": tool_call.id}

                done = await asyncio.gather(*(_run_one(tc) for tc in response_message.tool_calls))
                for d in done:
                    results.append({"tool": d["tool"], "result": d["result"]})
                    tool_entry = {
                        "role": "tool",
                        "content": str(d["result"]),
                        "tool_call_id": d["tool_call_id"],
                    }
                    messages.append(tool_entry)
                    history.append(tool_entry)
            # 无工具调用但 _should_continue 仍要求继续的子类：不再注入任何 user 消息
            # （2026-09-23 对齐 quest「全程仅首条 user 注入」语义），直接进入下一轮，
            # 由 max_rounds 兜底防死循环；首条 user 是全程唯一一条 user 消息。

        logger.warning(f"{self.__class__.__name__} 达到 max_rounds={max_rounds}，强制收尾")
        return {
            "content": "",
            "extra_info": {"tools_called": results},
        }

    @staticmethod
    def _history_entry(message) -> Dict[str, Any]:
        """把 LLM 响应消息转成可落盘的 assistant 条目（含思考与最终结论）。

        最终收尾轮（无工具调用）的响应不在 messages 里，单独经本方法落盘，
        保证事件链上下文包含最终结论 content 与 reasoning。
        """
        entry: Dict[str, Any] = {"role": "assistant", "content": message.content or ""}
        reasoning = extract_reasoning(message)
        if reasoning:
            entry["reasoning_content"] = reasoning
        return entry

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
    async def _call_llm(self, messages: list, *, level: str = "flash",
                        tool_choice: Optional[str] = None,
                        system_prompt: Optional[str] = None, **kwargs):
        """统一 LLM 调用：由子类实现具体调用方式（如识图 agent 走视觉模型）。

        `_llm_loop` 依赖本方法完成每次模型调用，因此必须由子类提供实现。

        Args:
            messages: 不含 system 的消息列表（user / assistant / tool）。
            level: 模型等级（flash / pro / vision / embedding），统一从 LLMSettings 解析。
            tool_choice: 工具选择策略（"none" / "auto" / None）。
            system_prompt: 覆盖系统提示词；默认取 self.get_system_prompt()。

        Returns:
            OpenAI 风格响应对象（含 .choices[0].message）。
        """
