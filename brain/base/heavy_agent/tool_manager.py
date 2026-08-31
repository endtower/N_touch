# -*- coding: utf-8 -*-
"""工具管理器基类：统一 get_tools + handle_tool_call 接口。

设计文档：docs/archive/heavyweight_session_base_design.md

各 Session 的 *ToolManager 继承本基类，实现 get_tools() + handle_tool_call()。
统一错误处理逻辑抽到基类 _safe_execute()：
- 参数错误（TypeError/ValueError/KeyError）→ 返回 [错误]... 给 LLM 自纠
- 其余异常（模块缺失 / IO 错 / AbyssCoreError 等）→ 上抛不吞，直接崩溃阻断
"""

import json
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional

from utils.logger import get_logger

logger = get_logger('ToolManager', "INFO")


class ToolManager(ABC):
    """工具管理器基类：统一 get_tools + handle_tool_call 接口"""

    # ============ 子类必须实现的抽象方法 ============

    @abstractmethod
    def get_tools(self) -> List[Dict]:
        """获取工具 schema 列表"""

    @abstractmethod
    async def handle_tool_call(self, tool_call) -> Dict[str, Any]:
        """处理单个工具调用，返回 tool 响应"""

    # ============ 统一工具调用解析（基类实现） ============

    @staticmethod
    def _parse_tool_call(tool_call) -> Optional[Dict[str, Any]]:
        """解析 tool_call 形态（OpenAI 对象 / dict 透传）。

        Returns:
            {"name": str, "arguments": str, "id": str}；无法解析时返回 None。
        """
        if hasattr(tool_call, 'function') and hasattr(tool_call, 'id'):
            return {
                "name": tool_call.function.name,
                "arguments": tool_call.function.arguments,
                "id": tool_call.id,
            }
        if isinstance(tool_call, dict) and 'function' in tool_call:
            return {
                "name": tool_call['function']['name'],
                "arguments": tool_call['function']['arguments'],
                "id": tool_call['id'],
            }
        return None

    @staticmethod
    def _parse_arguments(arguments: str) -> Dict[str, Any]:
        """解析工具参数 JSON，容错 LLM 偶尔吐 Python dict 字面量。

        Raises:
            ValueError: 参数不是合法 JSON 对象时抛出。
        """
        tool_args = json.loads(arguments)
        if not isinstance(tool_args, dict):
            raise ValueError(
                f"工具参数根节点必须是 JSON 对象，实际得到 {type(tool_args).__name__}"
            )
        return tool_args

    @staticmethod
    def _tool_error_response(tool_call_id, tool_name: str, err: Exception) -> Dict[str, Any]:
        """构造一条 role=tool 的"错误消息"，让 LLM 看到后自纠。

        对齐各 ToolManager 的兜底格式：content 以 "[错误]" 起头。
        """
        return {
            "role": "tool",
            "name": tool_name or "<unknown>",
            "content": f"[错误] {type(err).__name__}: {err}",
            "tool_call_id": tool_call_id,
        }

    # ============ 统一错误处理（基类实现） ============

    async def _safe_execute(self, func, tool_call_id, tool_name: str, **kwargs) -> Any:
        """统一错误处理：
        - 参数错误（TypeError/ValueError/KeyError）→ 返回 [错误]... 给 LLM 自纠
        - 其余异常（模块缺失 / IO 错 / AbyssCoreError 等）→ 上抛不吞，直接崩溃阻断

        Args:
            func: 待执行的工具 async 函数。
            tool_call_id: 工具调用 id（用于构造错误响应）。
            tool_name: 工具名。
            **kwargs: 传给 func 的参数。

        Returns:
            工具执行结果；参数错误时返回 role=tool 的错误消息 dict。
        """
        try:
            logger.info(f"处理工具请求：工具名称={tool_name}，参数={kwargs}")
            return await func(**kwargs)
        except (TypeError, ValueError, KeyError) as e:
            # 参数错误：可恢复，回 LLM 自纠
            logger.warning(f"工具 {tool_name} 调用参数错误: {e}")
            return self._tool_error_response(tool_call_id, tool_name, e)
        # 其余异常（模块缺失 / IO 错 / AbyssCoreError 等）一律上抛，不吞
