# -*- coding: utf-8 -*-
"""
熟悉程度操作类（SQLite）

熟悉程度是角色表 / 群组表的单值字段（覆盖语义，每 owner 一个）。
owner 可能是 Character 或 Group，通过 uuid 判断所属表。
"""

from datetime import datetime
from typing import Optional

from .core import get_memory_db
from .common import generate_timestamp_id
from .impression import _resolve_owner_table
from utils.logger import get_logger

logger = get_logger(__name__, "INFO")


class FamiliarityOperations:
    """熟悉程度操作类（纯异步）"""

    def __init__(self):
        self.db = get_memory_db()

    async def create_familiarity(self, content: str, owner_uuid: str) -> str:
        """为指定角色或群组设置熟悉程度（覆盖语义）。"""
        current_time = datetime.now().isoformat()
        familiarity_id = generate_timestamp_id("Familiarity")

        conn = await self.db._ensure_connection()
        table = await _resolve_owner_table(conn, owner_uuid)
        if table is None:
            logger.warning(f"owner_uuid={owner_uuid} 不存在于角色或群组表，跳过熟悉程度写入")
            return familiarity_id

        await conn.execute(
            f"UPDATE {table} SET familiarity=?, last_used_at=? WHERE uuid=?",
            (content, current_time, owner_uuid),
        )
        await conn.commit()

        logger.debug(f"已为 {owner_uuid} 设置熟悉程度: {familiarity_id}")
        return familiarity_id

    async def update_familiarity(self, owner_uuid: str, new_content: str) -> bool:
        """更新指定角色/群聊的熟悉程度（覆盖语义）。"""
        await self.create_familiarity(new_content, owner_uuid)
        return True

    async def delete_familiarity(self, owner_uuid: str) -> bool:
        """删除指定角色/群聊的熟悉程度。"""
        conn = await self.db._ensure_connection()
        table = await _resolve_owner_table(conn, owner_uuid)
        if table is None:
            return False
        cur = await conn.execute(
            f"UPDATE {table} SET familiarity='' WHERE uuid=? AND familiarity != ''",
            (owner_uuid,),
        )
        await conn.commit()
        return cur.rowcount > 0

    async def get_familiarity(self, owner_uuid: str) -> Optional[dict]:
        """获取指定角色/群聊的熟悉程度。"""
        conn = await self.db._ensure_connection()
        table = await _resolve_owner_table(conn, owner_uuid)
        if table is None:
            return None
        cur = await conn.execute(f"SELECT familiarity FROM {table} WHERE uuid=?", (owner_uuid,))
        row = await cur.fetchone()
        if row and row["familiarity"]:
            return {"content": row["familiarity"]}
        return None


_familiarity_instance: Optional[FamiliarityOperations] = None


def get_familiarity() -> FamiliarityOperations:
    """获取全局熟悉程度操作实例"""
    global _familiarity_instance
    if not _familiarity_instance:
        _familiarity_instance = FamiliarityOperations()
    return _familiarity_instance
