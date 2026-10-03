# -*- coding: utf-8 -*-
"""
消息处理器模块
负责处理消息队列和历史记录管理
"""

from paths import data_root
from pathlib import Path
from utils.logger import get_logger
from utils.history_manager import HistoryManager

logger = get_logger('Chat_HistoryManager')

class Chat_HistoryManager(HistoryManager):
    """聊天历史管理器 - 内联实现，替代Chat_Cortex"""

    # 支持的模块前缀
    SUPPORTED_PREFIXES = ['conversation', 'observe', 'temporary', 'social_chat']

    def __init__(self, prefix):
        """初始化历史记录管理器
         
        Args:
            prefix: 模块名前缀，必须是 SUPPORTED_PREFIXES 中的一个
        """
        self.prefix = prefix
        super().__init__()

    def get_memory_dir(self) -> Path:
        """获取存储目录路径。

        - social_chat 前缀走独立的 memory/social_chat/ 目录，便于前端按 kind 隔离展示
        - 其余前缀（conversation / observe / temporary）共享 memory/chat/
        """
        if self.prefix == 'social_chat':
            return data_root() / 'memory' / 'social_chat'
        return data_root() / 'memory' / 'chat'

    def get_file_prefix(self) -> str:
        """获取文件名前缀"""
        return self.prefix

# 创建全局历史管理器实例
_history_manager_instances = {}

def get_history_manager(prefix):
    """获取聊天历史管理器实例

    Args:
        prefix: 模块名前缀，可选值为 'conversation', 'observe', 'temporary'
                不传则返回默认的 'conversation' 实例

    Returns:
        Chat_HistoryManager: 历史管理器实例
    """
    if prefix not in _history_manager_instances:
        _history_manager_instances[prefix] = Chat_HistoryManager(prefix=prefix)
    return _history_manager_instances[prefix]

def remove_history_manager(prefix):
    """移除历史管理器实例
    
    Args:
        prefix: 模块名前缀，可选值为 'conversation', 'observe', 'temporary'
    """
    if prefix in _history_manager_instances:
        del _history_manager_instances[prefix]
        logger.info(f'移除历史管理器实例: {prefix}')