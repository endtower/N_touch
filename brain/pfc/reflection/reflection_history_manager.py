# -*- coding: utf-8 -*-
"""Reflection模块上下文管理器：负责处理reflection模块的上下文存储和管理"""

from pathlib import Path
from utils.logger import get_logger
from utils.history_manager import HistoryManager
from utils.error_handling import handle_errors

logger = get_logger('Reflection_HistoryManager', 'WARNING')

class Reflection_HistoryManager(HistoryManager):
    """Reflection模块上下文管理器"""

    def get_memory_dir(self) -> Path:
        """获取存储目录路径"""
        project_root = Path(__file__).resolve().parent.parent.parent.parent
        return project_root / 'memory' / 'reflection'

    def get_file_prefix(self) -> str:
        """获取文件名前缀"""
        return 'reflection'

    @handle_errors()
    def get_formatted_history(self) -> str:
        """获取并格式化当前消息历史

        Returns:
            str: 格式化后的消息历史，格式为"使用了xxx工具:参数 结果：xxxxx"
        """
        # 获取历史记录
        history = self.get_history()
        formatted_lines = []

        # 遍历历史记录并格式化
        for item in history:
            role = item.get('role')
            content = item.get('content')
            tool_calls = item.get('tool_calls', [])

            # 处理工具调用
            if role == 'assistant' and tool_calls:
                for tool_call in tool_calls:
                    function_name = tool_call.get('function', {}).get('name')
                    arguments = tool_call.get('function', {}).get('arguments', '{}')
                    formatted_lines.append(f"使用了{function_name}工具:{arguments}")
            # 处理工具响应
            elif role == 'tool' and content:
                formatted_lines.append(f"结果：{content}")

        return '\n'.join(formatted_lines)


# 创建全局历史管理器实例
_global_reflection_history_manager = None

def get_reflection_history_manager():
    """获取全局Reflection历史管理器实例"""
    global _global_reflection_history_manager
    if _global_reflection_history_manager is None:
        _global_reflection_history_manager = Reflection_HistoryManager()
    return _global_reflection_history_manager
