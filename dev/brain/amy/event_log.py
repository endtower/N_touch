#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 情绪事件日志 + 消费者游标（内存实现）

用"事件日志 + 消费者游标"替代"记录上次情绪值"的去重逻辑：
- 每次想法（thought）变动（增/删/改/刷新）都写一条事件日志，带递增序号 seq。
- 每个消费者（pfca / conversation / proactive）维护独立游标 position，
  通过 get_unread_events() 精确拉取自己未消费的情绪事件，再 advance_cursor() 推进。

事件类型：
- thought_added:    build_thought 新增 thought
- thought_expired:  thought 过期消失
- thought_refreshed: max_stack=1 续期
- thought_replaced:  max_stack>1 FIFO 替换

内存存储：不持久化，重启后事件日志清空（游标归零）。
"""

import asyncio
import time
from typing import Dict, List, Optional

from utils.logger import get_logger

logger = get_logger('amy_event_log', 'INFO')


class EventLog:
    """情绪事件日志（内存实现，asyncio.Lock 保护并发读写）。"""

    def __init__(self):
        self._lock = asyncio.Lock()
        # 事件列表：{"seq", "event_type", "thought_id", "act", "desc",
        #            "ne_delta", "da_delta", "create_time"}
        self._events: List[Dict] = []
        # 消费者游标：consumer_id -> 已消费的最大 seq（默认 -1 表示一个都没消费）
        self._cursors: Dict[str, int] = {}
        self._next_seq = 0

    async def append_event(self, event_type: str, thought_id: str = "", act: str = "",
                           desc: str = "", ne_delta: float = 0.0, da_delta: float = 0.0) -> int:
        """追加一条情绪事件日志，返回其 seq。"""
        async with self._lock:
            self._next_seq += 1
            event = {
                "seq": self._next_seq,
                "event_type": event_type,
                "thought_id": thought_id,
                "act": act,
                "desc": desc,
                "ne_delta": ne_delta,
                "da_delta": da_delta,
                "create_time": time.time(),
            }
            self._events.append(event)
            # 上限保护：日志只保留最近 1000 条
            if len(self._events) > 1000:
                self._events = self._events[-1000:]
        return self._next_seq

    async def get_unread_events(self, consumer_id: str) -> List[Dict]:
        """拉取该消费者未消费的情绪事件（seq > 游标位置）。"""
        async with self._lock:
            position = self._cursors.get(consumer_id, -1)
            return [e for e in self._events if e["seq"] > position]

    async def advance_cursor(self, consumer_id: str, seq: int) -> None:
        """推进消费者游标到指定 seq（只前进，不后退）。"""
        async with self._lock:
            current = self._cursors.get(consumer_id, -1)
            if seq > current:
                self._cursors[consumer_id] = seq

    async def get_cursor(self, consumer_id: str) -> int:
        """读取消费者当前游标位置。"""
        async with self._lock:
            return self._cursors.get(consumer_id, -1)

    async def reset(self) -> None:
        """清空事件日志与游标（一般用于测试/重置）。"""
        async with self._lock:
            self._events = []
            self._cursors = {}
            self._next_seq = 0
