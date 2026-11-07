# -*- coding: utf-8 -*-
"""
关系操作类（SQLite）

关系以 00000000 为中心：
- 角色与 00000000 的关系 → 写入 characters.relationship 字段
- 群组与 00000000 的关系（我在该群聊的形象）→ 写入 groups.self_image 字段
"""

from datetime import datetime

from .core import get_memory_db
from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


class RelationshipOperations:
    """关系操作类（纯异步）"""

    def __init__(self):
        self.db = get_memory_db()

    async def create_character_relationship(self, character_uuid: str, relation_type: str):
        """创建角色与 00000000 的关系（写入 characters.relationship 字段）。

        关系方向固定为 character_uuid -> 00000000（LLM 扮演的角色）。
        """
        current_time = datetime.now().isoformat()
        conn = await self.db._ensure_connection()
        await conn.execute(
            "UPDATE characters SET relationship=?, last_used_at=? WHERE uuid=?",
            (relation_type, current_time, character_uuid),
        )
        await conn.commit()

    async def create_group_relationship(self, group_uuid: str, relation_type: str):
        """创建群组与 00000000 的关系（写入 groups.self_image 字段）。"""
        current_time = datetime.now().isoformat()
        conn = await self.db._ensure_connection()
        await conn.execute(
            "UPDATE groups SET self_image=?, last_used_at=? WHERE uuid=?",
            (relation_type, current_time, group_uuid),
        )
        await conn.commit()
