#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""全局会话状态 - 主要基类（公共逻辑）

提供所有数据子类（会话数据 / 事件链周期 / 处理记录）共用的基础设施：
- 单例模式
- 统一数据字典 _data 与持久化（JSON 快照，后续可替换为事件流）
- 事件链激活检查（崩溃阻断，符合项目"关键方法失败应崩溃阻断"规则）
- 状态变更回调（通知 WebChannel / 前端 WebSocket）

会话标识统一使用 session_id，格式为 "type_id"，例如 "private_1234567890" / "group_198431299"。
"""

from paths import data_root
import asyncio
import json
from pathlib import Path
from typing import Dict, Any, List, Callable, Optional

from utils.logger import setup_logging
from adapt.session_identity import session_scope

logger = setup_logging(__name__, log_level="INFO")

# 全局状态变更回调列表 (供 WebChannel / 监控使用)
# 每个回调签名: callback(change_type: str, **payload) -> None
_state_change_callbacks: List[Callable] = []
_state_change_callbacks_lock = asyncio.Lock()


async def register_state_change_callback(callback: Callable) -> None:
    """注册全局状态变更回调 (用于通知 WebSocket 客户端等)"""
    async with _state_change_callbacks_lock:
        if callback not in _state_change_callbacks:
            _state_change_callbacks.append(callback)


async def unregister_state_change_callback(callback: Callable) -> None:
    async with _state_change_callbacks_lock:
        if callback in _state_change_callbacks:
            _state_change_callbacks.remove(callback)


def _notify_state_change(change_type: str, **payload) -> None:
    """同步通知 (调度协程), 不阻塞调用方"""
    for cb in list(_state_change_callbacks):
        try:
            result = cb(change_type, **payload)
            if asyncio.iscoroutine(result):
                # 让回调自行调度, 不在这里 await
                asyncio.create_task(result)
        except Exception as e:
            logger.warning(f"状态变更回调失败 (change={change_type}): {e}")


def _extract_session_type(session_id: str) -> str:
    """从统一 session_id 中提取 session_type（type_id 格式的第一段）

    例如 "private_1234567890" -> "private"
        "group_198431299" -> "group"
    """
    if not session_id or "_" not in session_id:
        return ""
    return session_scope(session_id)


class BaseGlobalSessionState:
    """全局会话状态主要基类 - 提供公共基础设施"""

    _lock = asyncio.Lock()

    def __init__(self):
        if not hasattr(self, '_initialized'):
            # 事件链状态由实例属性持有（单一数据源）
            self._event_chain_active = False
            self._current_event_chain = None
            self._event_chain_permissions = None
            self._timed_social_grants = {}
            self._processed_by_hc = False

            # 统一数据字典：各数据子类读写自己的分区
            self._data: Dict[str, Any] = {
                "sessions": {},          # session_id -> 会话数据（见 SessionDataMixin）
                "processing": {          # 处理记录（见 ProcessingRecordMixin）
                    "pfca_actions": [],
                    "emotion_results": [],
                },
            }

            self._storage_path = data_root() / "memory" / "event_history"
            self._storage_path.mkdir(exist_ok=True)

            self._real_time_file = None

            logger.info("GlobalSessionState已初始化")
            self._initialized = True

    # ============================================================
    # 事件链激活检查（崩溃阻断）
    # ============================================================

    def _require_event_chain_active(self) -> None:
        """事件链未激活时直接抛错，避免静默失败导致后续链路在不健康状态下运行。

        Raises:
            SystemError: 事件链未处于活动状态
        """
        if not self._event_chain_active:
            from utils.error_handling import SystemError
            raise SystemError(
                "事件链未处于活动状态。"
                "调用方必须在调用本方法之前显式调用 start_new_event_chain() 启动事件链。"
            )

    # ============================================================
    # 持久化（JSON 快照，后续可替换为事件流）
    # ============================================================

    def _build_snapshot(self) -> Dict[str, Any]:
        """组装完整状态快照（供实时持久化与事件链结束持久化共用）"""
        return {
            "event_chain": {
                "active": self._event_chain_active,
                "current": self._current_event_chain,
                "processed_by_hc": self._processed_by_hc,
            },
            "sessions": self._data.get("sessions", {}),
            "processing": self._data.get("processing", {}),
        }

    def _persist_real_time(self) -> None:
        """实时持久化数据"""
        if not self._event_chain_active or not self._real_time_file:
            return

        with open(self._real_time_file, 'w', encoding='utf-8') as f:
            json.dump(self._build_snapshot(), f, ensure_ascii=False, indent=2)

    # ============================================================
    # 事件链生命周期钩子（子类覆写实现各自的前后处理）
    # ============================================================

    def _on_event_chain_start(self) -> None:
        """事件链开始钩子：由 EventChainMixin.start_new_event_chain 统一调用。

        各附属子类（mixin）覆写本方法，实现"事件链开始后"自己的前处理。
        本基类实现为链式调用：沿 MRO 继续调用后续 mixin 的覆写，
        保证无论 mixin 在继承列表中的位置，其钩子都会被触发。
        """
        next_hook = getattr(super(), "_on_event_chain_start", None)
        if next_hook is not None:
            next_hook()

    def _on_event_chain_end(self) -> None:
        """事件链结束钩子：由 EventChainMixin.end_event_chain 统一调用。

        各附属子类（mixin）覆写本方法，实现"事件链结束后"自己的后处理
        （如清理状态、广播空闲）。本基类实现为链式调用：沿 MRO 继续调用
        后续 mixin 的覆写，保证无论 mixin 在继承列表中的位置，其钩子都会被触发。
        """
        next_hook = getattr(super(), "_on_event_chain_end", None)
        if next_hook is not None:
            next_hook()

    # ============================================================
    # 状态变更回调（供子类调用）
    # ============================================================

    def _notify(self, change_type: str, **payload) -> None:
        """子类统一入口：触发状态变更回调"""
        _notify_state_change(change_type, **payload)
