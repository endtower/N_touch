#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 好感度系统

管理长期关系事件和好感度计算
数据存储在角色表（characters.affinity_events）中
使用同类事件叠加衰减机制
"""

import asyncio
import time
import json
from typing import Dict, List
from collections import defaultdict

from utils.logger import get_logger
from brain.amy.constants import RelationEvent, AFFINITY_ALPHA

logger = get_logger('amy_affinity', 'INFO')

DECAY_FACTOR = 0.6
AMY_UUID = "00000000"

def get_affinity_label(affinity: float) -> str:
    if affinity >= 0.6:
        return "关系非常好"
    elif affinity >= 0.2:
        return "关系较好"
    elif affinity >= -0.2:
        return "关系一般"
    else:
        return "关系不好"

def format_affinity_description(affinity: float, top_events: List[str] = None) -> str:
    label = get_affinity_label(affinity)
    if top_events:
        events_str = ", ".join(top_events)
        return f"{label} 关键事件：{events_str}"
    return label

class AffinityStore:
    def __init__(self, persistence_dir=None):
        from brain.hc.database.core import get_memory_db
        self.db = get_memory_db()
        self._affinity_cache: Dict[str, float] = {}
        # 并发锁：保护 add_event 的"读-改-写"与 _affinity_cache 的读写
        self._lock = asyncio.Lock()

    def _generate_event_id(self) -> str:
        t = time.time()
        ms = int((t - int(t)) * 1000)
        return time.strftime("%Y%m%d%H%M%S") + f"{ms:03d}"

    async def _get_events_from_relation(self, subject_uuid: str) -> List[dict]:
        """从角色表的 affinity_events 字段获取事件列表"""
        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT affinity_events FROM characters WHERE uuid=?", (subject_uuid,))
        row = await cur.fetchone()
        if row and row["affinity_events"]:
            return json.loads(row["affinity_events"])
        return []

    async def _save_events_to_relation(self, subject_uuid: str, events: List[dict]):
        """保存事件列表到角色表的 affinity_events 字段"""
        conn = await self.db._ensure_connection()
        await conn.execute(
            "UPDATE characters SET affinity_events=? WHERE uuid=?",
            (json.dumps(events, ensure_ascii=False), subject_uuid),
        )
        await conn.commit()

    async def _relation_exists(self, subject_uuid: str) -> bool:
        """检查角色是否存在"""
        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT uuid FROM characters WHERE uuid=?", (subject_uuid,))
        row = await cur.fetchone()
        return row is not None

    async def add_event(self, subject_id: str, desc: str, raw_delta: float, act_type: str = "default") -> bool:
        """添加关系事件到 HAS_RELATIONSHIP 关系中

        Args:
            subject_id: 关系目标用户ID
            desc: 事件描述
            raw_delta: 原始好感度变化量
            act_type: 事件类型（用于同类叠加衰减）
        """
        if not await self._relation_exists(subject_id):
            logger.debug(f"关系不存在，跳过添加事件: {AMY_UUID} -> {subject_id}")
            return False

        async with self._lock:
            events = await self._get_events_from_relation(subject_id)

            now = int(time.time())

            new_event = {
                "id": self._generate_event_id(),
                "create_time": now,
                "desc": desc,
                "raw_delta": raw_delta,
                "act_type": act_type
            }

            events.append(new_event)

            await self._save_events_to_relation(subject_id, events)

            if subject_id in self._affinity_cache:
                del self._affinity_cache[subject_id]

        logger.debug(f"Added event: {new_event['id']}, {subject_id}: {desc} (raw_delta={raw_delta:.3f}, act_type={act_type})")
        return True

    async def get_affinity(self, subject_id: str) -> float:
        """计算用户好感度，使用同类事件叠加衰减机制"""
        async with self._lock:
            if subject_id in self._affinity_cache:
                return self._affinity_cache[subject_id]

            events = await self._get_events_from_relation(subject_id)

            if not events:
                self._affinity_cache[subject_id] = 0.0
                return 0.0

            grouped = defaultdict(list)
            for event in events:
                act_type = event.get('act_type', 'default')
                grouped[act_type].append(event)

            for act_type in grouped:
                grouped[act_type].sort(key=lambda x: x.get('create_time', 0))

            total = 0.0
            for act_type, type_events in grouped.items():
                for i, event in enumerate(type_events):
                    decay = DECAY_FACTOR ** i
                    contribution = event.get('raw_delta', 0) * decay
                    total += contribution

            total = max(-1.0, min(1.0, total))
            self._affinity_cache[subject_id] = total
            return total

    async def get_events_for_subject(self, subject_id: str) -> List[RelationEvent]:
        """获取指定用户的所有关系事件"""
        events_data = await self._get_events_from_relation(subject_id)

        return [
            RelationEvent(
                id=e['id'],
                create_time=e['create_time'],
                subject_id=subject_id,
                desc=e['desc'],
                raw_delta=e['raw_delta']
            )
            for e in events_data
        ]

    async def get_top_events(self, subject_id: str, limit: int = 3) -> List[RelationEvent]:
        """获取影响最大的事件"""
        events = await self.get_events_for_subject(subject_id)
        if not events:
            return []
        sorted_events = sorted(events, key=lambda x: abs(x.raw_delta), reverse=True)
        return sorted_events[:limit]

    async def get_relation_summary(self, subject_id: str) -> dict:
        """获取关系摘要"""
        affinity = await self.get_affinity(subject_id)
        top_events = await self.get_top_events(subject_id)
        return {
            "affinity": affinity,
            "label": get_affinity_label(affinity),
            "top_events": [e.desc for e in top_events]
        }

    @staticmethod
    def calculate_raw_delta(delta_relatedness: float, delta_autonomy: float) -> float:
        return (1.0 * delta_relatedness + 0.8 * delta_autonomy) * AFFINITY_ALPHA