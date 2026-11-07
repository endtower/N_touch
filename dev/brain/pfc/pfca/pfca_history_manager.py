# -*- coding: utf-8 -*-
"""PFCA模块上下文管理器：负责处理pfca模块的上下文存储和管理"""

from paths import data_root
from pathlib import Path
from utils.logger import get_logger
from utils.history_manager import HistoryManager
from utils.error_handling import handle_errors

logger = get_logger('PFCA_HistoryManager', 'WARNING')

class PFCA_HistoryManager(HistoryManager):
    """PFCA模块上下文管理器"""
    
    def get_memory_dir(self) -> Path:
        """获取存储目录路径"""
        return data_root() / 'memory' / 'pfca'
    
    def get_file_prefix(self) -> str:
        """获取文件名前缀"""
        return 'pfca'
    
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
_global_pfca_history_manager = None

def get_pfca_history_manager():
    """获取全局PFCA历史管理器实例"""
    global _global_pfca_history_manager
    if _global_pfca_history_manager is None:
        _global_pfca_history_manager = PFCA_HistoryManager()
    return _global_pfca_history_manager
