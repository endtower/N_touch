# -*- coding: utf-8 -*-
"""
主动任务模块 (Proactive)

位于 attention 与 pfca 之间，负责：
- 主动构造任务（而非被动响应未读消息）
- 与 pfca 衔接：把构造好的 ProactiveTask 交给 PFCA 决策
- 与 conversation 衔接：执行由 PFCA 路由过来的会话

设计参照 brain/pfc/pfca/ 的分层：
    core -> session -> (input_manager / bequest / history_manager) + prompt + tools

本包当前只包含基础骨架，具体 driver / agent / 工具实现待后续补齐。
"""

from .proactive_core import ProactiveCore, get_proactive_core

__all__ = ["ProactiveCore", "get_proactive_core"]
