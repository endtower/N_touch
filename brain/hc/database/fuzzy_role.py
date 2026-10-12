# -*- coding: utf-8 -*-
"""
模糊角色操作类（SQLite）

FuzzyRole 用于记录「可能存在 / 模糊存在」的重要人物，遵循会话锚定法则。
由 MemoryAgent 在 modification_type='mentioned_person' 时创建。
模糊角色是唯一单独建表的依附性节点。
"""

from datetime import datetime
from typing import Optional, Dict, Any, List

from .core import get_memory_db
from .common import generate_timestamp_id
from .character import CharacterOperations
from .group import GroupOperations
from utils.logger import get_logger

logger = get_logger(__name__, "INFO")


class FuzzyRoleOperations:
    """模糊角色操作类（纯异步）"""

    def __init__(self):
        self.db = get_memory_db()

    async def find_source_entity(self, source_session_id: str) -> Dict[str, Any]:
        """根据 session_id 查找对应的来源实体（Character 或 Group）。"""
        if not source_session_id:
            return {"found": False, "entity_type": None, "entity_uuid": None}

        session_type = source_session_id.split("_", 1)[0] if "_" in source_session_id else ""

        if session_type == "group":
            group_ops = GroupOperations()
            uuid = await group_ops.get_group_by_contact(source_session_id)
            entity_type = "group"
        else:
            char_ops = CharacterOperations()
            uuid = await char_ops.get_character_by_contact(source_session_id)
            entity_type = "character"

        if not uuid:
            return {"found": False, "entity_type": None, "entity_uuid": None}

        return {"found": True, "entity_type": entity_type, "entity_uuid": uuid}

    async def create_fuzzy_role(
        self,
        name: str,
        source_session_id: str,
        content: str,
    ) -> Dict[str, Any]:
        """创建一个 FuzzyRole 节点并锚定到来源会话对应的实体。"""
        if not name:
            return {"status": "error", "message": "name 必填", "fuzzy_role_id": None}
        if not source_session_id:
            return {"status": "error", "message": "source_session_id 必填（锚定会话）", "fuzzy_role_id": None}
        if not content:
            return {"status": "error", "message": "content 必填", "fuzzy_role_id": None}

        source_info = await self.find_source_entity(source_session_id)
        source_entity_type = source_info["entity_type"]
        source_entity_uuid = source_info["entity_uuid"]

        if not source_info["found"]:
            return {
                "status": "error",
                "message": (
                    f"无法锚定来源会话 {source_session_id}：未在数据库中找到对应的 Character 或 Group 节点。"
                ),
                "fuzzy_role_id": None,
                "source_entity_type": None,
                "source_entity_uuid": None,
            }

        current_time = datetime.now().isoformat()
        conn = await self.db._ensure_connection()

        # 去重：同 source_session_id + 同 name 复用现有 FuzzyRole
        cur = await conn.execute(
            "SELECT uuid, content FROM fuzzy_roles WHERE source_session_id=? AND name=? LIMIT 1",
            (source_session_id, name),
        )
        existing = await cur.fetchone()

        if existing:
            existing_uuid = existing["uuid"]
            existing_content = existing["content"] or ""
            merged_content = f"{existing_content}\n---\n{content}" if existing_content else content
            await conn.execute(
                "UPDATE fuzzy_roles SET content=?, updated_at=? WHERE uuid=?",
                (merged_content, current_time, existing_uuid),
            )
            await conn.commit()
            logger.info(f"合并 FuzzyRole: uuid={existing_uuid}, name={name}")
            return {
                "status": "success",
                "fuzzy_role_id": existing_uuid,
                "source_entity_type": source_entity_type,
                "source_entity_uuid": source_entity_uuid,
                "merged": True,
                "message": f"会话 {source_session_id} 中已存在「{name}」的 FuzzyRole 节点，已将新描述追加到原节点 content。",
            }

        # 未命中：创建新 FuzzyRole
        fuzzy_uuid = generate_timestamp_id("FuzzyRole")
        await conn.execute(
            """INSERT INTO fuzzy_roles (uuid, name, content, source_session_id,
               source_entity_type, source_entity_uuid, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (fuzzy_uuid, name, content, source_session_id,
             source_entity_type, source_entity_uuid, current_time, current_time),
        )
        await conn.commit()

        logger.info(f"创建 FuzzyRole: uuid={fuzzy_uuid}, name={name}")
        return {
            "status": "success",
            "fuzzy_role_id": fuzzy_uuid,
            "source_entity_type": source_entity_type,
            "source_entity_uuid": source_entity_uuid,
            "merged": False,
            "message": f"已为会话 {source_session_id} 中提及的「{name}」创建 FuzzyRole 节点",
        }

    async def get_fuzzy_role(self, fuzzy_role_id: str) -> Optional[Dict[str, Any]]:
        """根据 uuid 获取单个 FuzzyRole 节点。"""
        if not fuzzy_role_id:
            return None
        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT * FROM fuzzy_roles WHERE uuid=?", (fuzzy_role_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "uuid": row["uuid"],
            "name": row["name"],
            "content": row["content"],
            "source_session_id": row["source_session_id"],
            "source_entity_type": row["source_entity_type"],
            "source_entity_uuid": row["source_entity_uuid"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def find_fuzzy_role_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """按名字查找 FuzzyRole 节点（取最新一条）。"""
        if not name:
            return None
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT * FROM fuzzy_roles WHERE name=? ORDER BY updated_at DESC LIMIT 1",
            (name,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "uuid": row["uuid"],
            "name": row["name"],
            "content": row["content"],
            "source_session_id": row["source_session_id"],
            "source_entity_type": row["source_entity_type"],
            "source_entity_uuid": row["source_entity_uuid"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def list_fuzzy_roles(self, limit: int = 100) -> List[Dict[str, Any]]:
        """列出所有 FuzzyRole 节点。"""
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT * FROM fuzzy_roles ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [
            {
                "uuid": r["uuid"],
                "name": r["name"],
                "content": r["content"],
                "source_session_id": r["source_session_id"],
                "source_entity_type": r["source_entity_type"],
                "source_entity_uuid": r["source_entity_uuid"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    async def delete_fuzzy_role(self, fuzzy_role_id: str) -> Dict[str, Any]:
        """删除一个 FuzzyRole 节点。"""
        if not fuzzy_role_id:
            return {"status": "error", "message": "fuzzy_role_id 必填"}
        conn = await self.db._ensure_connection()
        cur = await conn.execute("DELETE FROM fuzzy_roles WHERE uuid=?", (fuzzy_role_id,))
        await conn.commit()
        if cur.rowcount == 0:
            return {"status": "error", "message": f"FuzzyRole {fuzzy_role_id} 不存在"}
        return {"status": "success", "message": f"已删除 FuzzyRole {fuzzy_role_id}"}

    async def delete_by_source_entity(self, source_entity_uuid: str) -> int:
        """级联删除锚定到某来源实体（角色/群组）的全部 FuzzyRole。

        角色或群组被删除时，其名下依附的模糊角色已失去锚点，一并清除。
        返回删除条数。
        """
        if not source_entity_uuid:
            return 0
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "DELETE FROM fuzzy_roles WHERE source_entity_uuid=?", (source_entity_uuid,)
        )
        await conn.commit()
        return cur.rowcount

    async def update_fuzzy_role(self, fuzzy_role_id: str, **fields: Any) -> bool:
        """更新 FuzzyRole 可编辑字段（名称/内容），成功后记录 updated_at。

        fields 支持:
          name: str    — 名称
          content: str — 内容
        传 None 的字段跳过不修改。返回 False 表示 uuid 不存在。
        """
        if not fuzzy_role_id:
            return False

        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT uuid FROM fuzzy_roles WHERE uuid=?", (fuzzy_role_id,))
        row = await cur.fetchone()
        if row is None:
            return False

        sets: List[str] = []
        params: List[Any] = []
        if fields.get("name") is not None:
            sets.append("name=?")
            params.append(fields["name"])
        if fields.get("content") is not None:
            sets.append("content=?")
            params.append(fields["content"])

        if sets:
            sets.append("updated_at=?")
            params.append(datetime.now().isoformat())
            params.append(fuzzy_role_id)
            await conn.execute(
                f"UPDATE fuzzy_roles SET {', '.join(sets)} WHERE uuid=?",
                params,
            )
            await conn.commit()
        return True


_global_fuzzy_role_ops: Optional[FuzzyRoleOperations] = None


def get_fuzzy_role_ops() -> FuzzyRoleOperations:
    """获取全局 FuzzyRoleOperations 单例"""
    global _global_fuzzy_role_ops
    if _global_fuzzy_role_ops is None:
        _global_fuzzy_role_ops = FuzzyRoleOperations()
    return _global_fuzzy_role_ops
