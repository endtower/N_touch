#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""全局会话状态 - 聚合主类（GlobalSessionState）

组合各数据子类（mixin）为单一全局单例：
- BaseGlobalSessionState：公共基础设施（单例 / 数据字典 / 持久化 / 回调）
- SessionDataMixin：会话数据 + 游标收录
- ProcessingRecordMixin：处理记录（pfca_actions / emotion_results）
- EventChainMixin：事件链生命周期
- FrontendBroadcastMixin：事件链状态的前端广播

对外统一通过 get_global_session_state() 获取单例。
"""

from brain.hc.base_global_session_state import (
    BaseGlobalSessionState,
    register_state_change_callback,
    unregister_state_change_callback,
)
from brain.hc.session_data import SessionDataMixin
from brain.hc.processing_record import ProcessingRecordMixin
from brain.hc.event_chain import EventChainMixin
from brain.hc.frontend_broadcast import FrontendBroadcastMixin


class GlobalSessionState(
    BaseGlobalSessionState,
    SessionDataMixin,
    ProcessingRecordMixin,
    EventChainMixin,
    FrontendBroadcastMixin,
):
    """全局会话状态聚合主类（单例）"""


def get_global_session_state() -> GlobalSessionState:
    """获取全局会话状态单例"""
    return GlobalSessionState()
