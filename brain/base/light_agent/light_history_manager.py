# -*- coding: utf-8 -*-
"""LightweightAgent 系上下文管理器：事件链本地保留（父类机制）。

所有复用父类 `_llm_loop` 的 light agent（persona / vision / bequest / memory）
经 `LightweightAgent.history_prefix` 指定前缀，落盘统一收敛到 event_chains 存储模型：

- 链内：memory/event_chains/{chain_id}/{prefix}_{HHMMSS}.json
  （**每次召唤独立文件**——light agent 无状态，每次 _llm_loop 调用新实例，
  尤其三个人格各自独立的 system / user / 思考链绝不共享；同格多轮工具调用
  在同一文件内累积）
- 链外：memory/event_chains/unlinked/{prefix}.json（常驻）

文件首条为 system prompt（提示词文档随链落盘，供前端按链查看全部提示词）。
前缀按子类区分，前端 /context 以 kind 展示（persona / vision / bequest / memory）。

关键设计（2026-09-23 修正）：父类 `_llm_loop` 用 `new_light_history_manager()`
**每次创建独立实例**，而不是共享按前缀单例——因为多个人格并行（asyncio.gather）
召唤时，共享单例的 `_session_file` 实例属性会被并行任务互相覆盖，导致上下文
文件错乱（多个格堆进同一文件 / 出现残缺文件）。

链尾由 station._force_archive_chain_contexts() 调 archive_light_contexts()
终结全部已登记实例的写入资格（归档后实例即弃，后续召唤重新创建）。
"""

from pathlib import Path
from typing import Dict, List

from utils.history_manager import HistoryManager


class LightAgent_HistoryManager(HistoryManager):
    """Light agent 通用上下文管理器（前缀可配置，一次召唤一个实例）。"""

    def __init__(self, prefix: str):
        self._prefix = prefix
        super().__init__()

    def get_memory_dir(self) -> Path:
        """获取存储目录路径（light agent 统一收敛到 memory/light_agent）。"""
        project_root = Path(__file__).resolve().parent.parent.parent.parent
        return project_root / 'memory' / 'light_agent'

    def get_file_prefix(self) -> str:
        """获取文件名前缀（由子类 history_prefix 决定，区分 agent 类型）。"""
        return self._prefix


# 全部已创建实例（一次召唤一个）：链尾归档遍历终结写入资格，归档后即弃
_all_instances: List[LightAgent_HistoryManager] = []


def new_light_history_manager(prefix: str) -> LightAgent_HistoryManager:
    """创建一次召唤专用的独立上下文管理器实例（父类 _llm_loop 使用）。

    每次 _llm_loop 调用（= 一次 agent 召唤）创建新实例，`_session_file` 天然
    独立——并行召唤（asyncio.gather 多个人格）不会互相覆盖；同实例内的多轮
    工具调用仍写同一文件。
    """
    mgr = LightAgent_HistoryManager(prefix)
    _all_instances.append(mgr)
    return mgr


# 按前缀缓存的单例（仅供 /context 预实例化触发 legacy 迁移；生产写入不经过这里）
_instances: Dict[str, LightAgent_HistoryManager] = {}


def get_light_history_manager(prefix: str) -> LightAgent_HistoryManager:
    """获取指定前缀的全局历史管理器实例（仅供 context_api.ensure_legacy_migrated
    触发幂等迁移）。生产写入用 new_light_history_manager（每次召唤独立实例）。"""
    if prefix not in _instances:
        _instances[prefix] = LightAgent_HistoryManager(prefix)
    return _instances[prefix]


def archive_light_contexts() -> None:
    """链结束强制归档：终结全部已创建 light agent 实例的写入资格。

    由 station._force_archive_chain_contexts() 在链尾调用；归档后实例即弃
    （下次召唤重新创建），列表随之清空。
    """
    for mgr in _all_instances:
        mgr.archive_context()
    _all_instances.clear()
