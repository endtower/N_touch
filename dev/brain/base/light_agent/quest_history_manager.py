# -*- coding: utf-8 -*-
"""Quest模块上下文管理器：负责处理QuestAgent的上下文存储和管理

QuestAgent 非本基类子类的常规会话 agent（跨 run() 持久 messages、无 session 骨架），
但复用 HistoryManager 的事件链存储模型：
- 链内写入收敛到 memory/event_chains/{chain_id}/quest_{HHMMSS}.json，
  同一事件链内 PFCA 多次呼叫 quest 共享同一落盘文件（上下文延续语义对齐）；
- 链尾由 station._force_archive_chain_contexts() 强制归档终结写入资格；
- 文件首条为 system prompt（提示词文档随链落盘，供前端按链查看全部提示词）。
"""

from paths import data_root
from pathlib import Path
from utils.history_manager import HistoryManager


class Quest_HistoryManager(HistoryManager):
    """Quest模块上下文管理器"""

    def get_memory_dir(self) -> Path:
        """获取存储目录路径"""
        return data_root() / 'memory' / 'quest'

    def get_file_prefix(self) -> str:
        """获取文件名前缀"""
        return 'quest'


# 创建全局历史管理器实例
_global_quest_history_manager = None

def get_quest_history_manager():
    """获取全局Quest历史管理器实例"""
    global _global_quest_history_manager
    if _global_quest_history_manager is None:
        _global_quest_history_manager = Quest_HistoryManager()
    return _global_quest_history_manager
