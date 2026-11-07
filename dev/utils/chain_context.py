# -*- coding: utf-8 -*-
"""
任务链上下文（contextvar 传递）
================================

一个"任务链"= station 从出队一条 truck 任务到处理完成的完整周期
（含链内的 proactive 决策阶段与 PFCA/conversation/social_chat 阶段）。

chain_id 由 station 出队即生成，通过 contextvar 在同任务内的 await 调用链上
自动传播；跨任务边界（如 ProactiveCore 主循环）由 task_draft 显式携带，
在目标任务内重新 set。

所有 HistoryManager 写入时读取当前 chain_id，用于：
- active 文件归属进行中链（供 /context API 透出）
- 归档文件名携带 chain_id（{prefix}_chain_{chain_id}_{HHMMSS}.json）
"""

import uuid
from contextvars import ContextVar, Token
from datetime import datetime
from typing import Optional

from utils.logger import get_logger

logger = get_logger('ChainContext', 'WARNING')

# 当前任务链 ID（None = 不在任何链内，写入不归属任何链）
_current_chain_id: ContextVar[Optional[str]] = ContextVar('current_chain_id', default=None)


def generate_chain_id() -> str:
    """生成新任务链 ID：event_chain_{YYYYMMDD_HHMMSS}_{4位随机}。

    随机后缀防同秒碰撞（station 全局串行，正常不会并发，但重启/测试环境可能同秒）。
    """
    return f"event_chain_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"


def get_current_chain_id() -> Optional[str]:
    """获取当前任务链 ID（不在链内返回 None）。"""
    return _current_chain_id.get()


def set_chain_context(chain_id: str) -> Token:
    """设置当前任务链 ID，返回 Token 供 reset_chain_context 恢复。"""
    return _current_chain_id.set(chain_id)


def reset_chain_context(token: Token) -> None:
    """恢复 set_chain_context 之前的链上下文。"""
    _current_chain_id.reset(token)
