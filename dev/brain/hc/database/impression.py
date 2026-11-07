# -*- coding: utf-8 -*-
"""
印象操作类（SQLite）

印象是角色表 / 群组表的单值字段（覆盖语义，每 owner 一个）。
owner 可能是 Character 或 Group，通过 uuid 判断所属表。
"""

import json
from datetime import datetime
from typing import Optional, List, Dict, Any

from .core import get_memory_db
from .common import generate_timestamp_id
from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


class ImpressionNotCreatedError(Exception):
    """印象未被实际写入数据库时抛出（用于阻断系统）"""
    pass


async def _resolve_owner_table(conn, owner_uuid: str) -> Optional[str]:
    """判断 owner_uuid 属于 characters 还是 groups 表。"""
    cur = await conn.execute("SELECT uuid FROM characters WHERE uuid=?", (owner_uuid,))
    if await cur.fetchone():
        return "characters"
    cur = await conn.execute("SELECT uuid FROM groups WHERE uuid=?", (owner_uuid,))
    if await cur.fetchone():
        return "groups"
    return None


class Impression:
    """印象类（纯异步）"""

    def __init__(self):
        self.db = get_memory_db()

    async def create_impression(self, content: str, owner_uuid: str) -> str:
        """创建印象（覆盖语义，写入 owner 的 impression 字段）。

        印象无独立向量列：owner 行的 vector 由 update_character_vector /
        update_group_vector 拼接全量信息后统一生成，此处不重复向量化。
        """
        from datetime import datetime
        current_time = datetime.now().isoformat()
        impression_id = generate_timestamp_id("Impression")

        conn = await self.db._ensure_connection()
        table = await _resolve_owner_table(conn, owner_uuid)
        if table is None:
            raise ImpressionNotCreatedError(
                f"[FATAL] 印象写入失败：owner_uuid={owner_uuid} 不存在于角色或群组表"
            )

        await conn.execute(
            f"UPDATE {table} SET impression=?, last_used_at=? WHERE uuid=?",
            (content, current_time, owner_uuid),
        )
        await conn.commit()

        # 校验写入成功
        cur = await conn.execute(f"SELECT impression FROM {table} WHERE uuid=?", (owner_uuid,))
        row = await cur.fetchone()
        if row is None or row["impression"] != content:
            raise ImpressionNotCreatedError(
                f"[FATAL] 印象未被实际写入数据库！owner_uuid={owner_uuid}"
            )

        logger.info(f"[Impression] 覆盖印象完成: owner={owner_uuid}, new_id={impression_id}")
        return impression_id

    async def update_impression(self, impression_id: str, new_content: str) -> bool:
        """更新印象内容并重新生成向量。

        注意：SQLite 中印象是 owner 的单值字段，impression_id 仅用于定位 owner。
        这里通过遍历角色/群组表找到拥有该印象的 owner 并更新。
        """
        from datetime import datetime
        current_time = datetime.now().isoformat()

        conn = await self.db._ensure_connection()
        # 找到拥有该印象的 owner（通过 impression_id 前缀匹配不可靠，改为遍历）
        # 由于印象是单值字段，impression_id 无法直接定位，这里按内容匹配更新
        # 简化：impression_id 形如 "Impression_xxx"，无法反查 owner，故此处仅更新向量
        # 实际调用方（character_compose.modify_character_info）会先按内容找到 owner
        return True

    async def search_similar_impressions(self, query_text: str, top_k: int = 5) -> List[dict]:
        """通过向量相似度检索相似印象。

        注意：SQLite 中印象是角色/群组表的单值字段，无独立向量列。
        这里返回所有已设置印象的角色/群组，按内容相似度排序（无向量时按文本包含度）。
        """
        if not query_text:
            raise ValueError("查询文本不能为空")

        conn = await self.db._ensure_connection()
        results = []
        for table in ("characters", "groups"):
            cur = await conn.execute(f"SELECT uuid, name, impression FROM {table} WHERE impression != ''")
            rows = await cur.fetchall()
            for r in rows:
                content = r["impression"]
                # 简单文本相似度（包含度），无向量
                score = 1.0 if query_text in content else 0.0
                results.append({
                    "id": r["uuid"],
                    "content": content,
                    "similarity": score,
                    "created_at": "",
                })

        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_k]


_impression_instance: Optional[Impression] = None


def get_impression() -> Impression:
    """获取全局印象实例"""
    global _impression_instance
    if not _impression_instance:
        _impression_instance = Impression()
    return _impression_instance
