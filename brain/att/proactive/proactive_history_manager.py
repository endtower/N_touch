# -*- coding: utf-8 -*-
"""
proactive 历史/消息管理器

类比 PFCAHistoryManager：
- 维护 proactive session 的 messages list (system / user / assistant / tool)
- 提供 append / get_history 接口
- 持久化到 memory/proactive/ 目录下（active.json + 归档文件）
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.error_handling import handle_errors
from utils.history_manager import HistoryManager
from utils.logger import get_logger

logger = get_logger("ProactiveHistoryManager", "WARNING")


class ProactiveHistoryManager(HistoryManager):
    """proactive 模块上下文管理器

    与 PFCAHistoryManager 对齐：
    - append(message)         追加一条消息（落盘）
    - get_history() -> list   获取全部消息
    - clear()                 清空
    - get_formatted_history() 格式化为字符串（便于 prompt 拼接）
    """

    def get_memory_dir(self) -> Path:
        """获取存储目录路径"""
        project_root = Path(__file__).resolve().parent.parent.parent.parent
        return project_root / "memory" / "proactive"

    def get_file_prefix(self) -> str:
        """获取文件名前缀"""
        return "proactive"

    @handle_errors()
    def get_formatted_history(self) -> str:
        """获取并格式化当前消息历史

        Returns:
            str: 格式化后的消息历史，格式为"使用了xxx工具:参数 结果：xxxxx"
        """
        history = self.get_history()
        formatted_lines = []

        for item in history:
            role = item.get("role")
            content = item.get("content")
            tool_calls = item.get("tool_calls", [])

            if role == "assistant" and tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {}).get("name")
                    args = tc.get("function", {}).get("arguments", "{}")
                    formatted_lines.append(f"使用了{fn}工具:{args}")
            elif role == "tool" and content:
                formatted_lines.append(f"结果：{content}")

        return "\n".join(formatted_lines)


_global_proactive_history_manager: Optional[ProactiveHistoryManager] = None


def get_proactive_history_manager() -> ProactiveHistoryManager:
    """获取全局 proactive history manager 单例"""
    global _global_proactive_history_manager
    if _global_proactive_history_manager is None:
        _global_proactive_history_manager = ProactiveHistoryManager()
    return _global_proactive_history_manager


def reset_proactive_history_manager() -> None:
    """重置单例（一般用于会话结束/进程重置）

    注意：仅清空单例引用，磁盘上的 active.json 不会自动清理。
    若需归档当前上下文再重置，请先调用 instance.archive_context()。
    """
    global _global_proactive_history_manager
    _global_proactive_history_manager = None