# -*- coding: utf-8 -*-
"""PRE（pre_truck）上下文管理器。

PRE 是零 LLM 的契机规则层，但它同样需要在大脑的事件链面板里可见：每次规则
命中到判断层给出决策，都写一份自己的上下文文件
（memory/event_chains/{chain_id}/pre_{HHMMSS}.json），记录：

1. system    —— 本次命中的规则与扫描范围（PRE 的判断依据）；
2. user      —— 交给 proactive 判断层的契机上下文（wakeup_reason 原文）；
3. assistant —— 判断层的决策（launch_pfca / skip_proactive）。

落盘走 HistoryManager 的事件链模型，链尾由
station._force_archive_chain_contexts() 归档终结写入资格。
"""

from pathlib import Path

from paths import data_root
from utils.history_manager import HistoryManager


class PreHistoryManager(HistoryManager):
    """PRE 上下文管理器（链内文件名前缀 pre）"""

    def get_memory_dir(self) -> Path:
        """获取存储目录路径"""
        return data_root() / "memory" / "pre"

    def get_file_prefix(self) -> str:
        """获取文件名前缀"""
        return "pre"


_global_pre_history_manager = None


def get_pre_history_manager() -> PreHistoryManager:
    """获取全局 PRE 历史管理器实例"""
    global _global_pre_history_manager
    if _global_pre_history_manager is None:
        _global_pre_history_manager = PreHistoryManager()
    return _global_pre_history_manager
