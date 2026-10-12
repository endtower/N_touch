# -*- coding: utf-8 -*-
"""
群组操作类（SQLite）

以 00000000（LLM 扮演的角色）为中心，管理 00000000 指向的群组。
群组表字段：别名（JSON 数组）、印象（单值）、熟悉度（单值）、群组成员印象（单值）、
联系方式（JSON 数组）、self_image（我在该群聊的形象）。
"""

import json
from datetime import datetime
from typing import Optional, Dict, Any

from .core import get_memory_db, serialize_vector, deserialize_vector, cosine_similarity
from .common import generate_timestamp_id
from .vector_store import get_vector_store
from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


class GroupOperations:
    """群组操作类（纯异步）"""

    def __init__(self):
        self.db = get_memory_db()

    # ============================================================
    # 创建
    # ============================================================

    async def create_group_node(self, name: str, uuid: str = None) -> str:
        """创建群组节点。"""
        current_time = datetime.now().isoformat()
        if not uuid:
            uuid = generate_timestamp_id("Group")

        conn = await self.db._ensure_connection()
        await conn.execute(
            """INSERT INTO groups (uuid, name, aliases, usage_count, last_used_at, created_at)
               VALUES (?, ?, ?, 0, ?, ?)
               ON CONFLICT(uuid) DO UPDATE SET name=excluded.name""",
            (uuid, name, json.dumps([name], ensure_ascii=False), current_time, current_time),
        )
        await conn.commit()
        return uuid

    async def create_alias_node(self, name: str, owner_uuid: str) -> None:
        """为群组添加别名（追加到 aliases JSON 数组）。"""
        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT aliases FROM groups WHERE uuid=?", (owner_uuid,))
        row = await cur.fetchone()
        if row is None:
            return
        aliases = json.loads(row["aliases"] or "[]")
        if name not in aliases:
            aliases.append(name)
        await conn.execute(
            "UPDATE groups SET aliases=? WHERE uuid=?",
            (json.dumps(aliases, ensure_ascii=False), owner_uuid),
        )
        await conn.commit()

    async def create_contact_node(self, session_id: str, owner_uuid: str) -> None:
        """为群组添加联系方式（追加到 contacts JSON 数组）。"""
        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT contacts FROM groups WHERE uuid=?", (owner_uuid,))
        row = await cur.fetchone()
        if row is None:
            return
        contacts = json.loads(row["contacts"] or "[]")
        if not any(c.get("session_id") == session_id for c in contacts):
            contacts.append({"session_id": session_id, "usage_count": 0})
        await conn.execute(
            "UPDATE groups SET contacts=? WHERE uuid=?",
            (json.dumps(contacts, ensure_ascii=False), owner_uuid),
        )
        await conn.commit()

    async def replace_aliases(self, owner_uuid: str, new_aliases: list) -> None:
        """整组替换群组别名（覆盖 aliases JSON 数组）。"""
        conn = await self.db._ensure_connection()
        await conn.execute(
            "UPDATE groups SET aliases=? WHERE uuid=?",
            (json.dumps(new_aliases, ensure_ascii=False), owner_uuid),
        )
        await conn.commit()

    async def update_group(self, uuid: str, **fields: Any) -> bool:
        """更新群组可编辑字段（名称/别名/印象/熟悉度/成员印象/self_image），成功后重新向量化。

        fields 支持:
          name: str              — 群组名称
          aliases: list[str]     — 别名（整组替换，覆盖 aliases JSON 数组）
          impression: str        — 印象（单值）
          familiarity: str       — 熟悉度（单值）
          member_impression: str — 群组成员印象（单值）
          self_image: str        — 我在该群聊的形象（单值）
        传 None 的字段跳过不修改。返回 False 表示群组 uuid 不存在。
        """
        if not uuid:
            return False

        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT uuid FROM groups WHERE uuid=?", (uuid,))
        row = await cur.fetchone()
        if row is None:
            return False

        sets: List[str] = []
        params: List[Any] = []
        if fields.get("name") is not None:
            sets.append("name=?")
            params.append(fields["name"])
        if fields.get("aliases") is not None:
            sets.append("aliases=?")
            params.append(json.dumps(fields["aliases"], ensure_ascii=False))
        if fields.get("impression") is not None:
            sets.append("impression=?")
            params.append(fields["impression"])
        if fields.get("familiarity") is not None:
            sets.append("familiarity=?")
            params.append(fields["familiarity"])
        if fields.get("member_impression") is not None:
            sets.append("member_impression=?")
            params.append(fields["member_impression"])
        if fields.get("self_image") is not None:
            sets.append("self_image=?")
            params.append(fields["self_image"])

        if sets:
            params.append(uuid)
            await conn.execute(
                f"UPDATE groups SET {', '.join(sets)} WHERE uuid=?",
                params,
            )
            await conn.commit()

        # 描述性字段变更后重新向量化（内部自行处理未找到的兜底）
        await self.update_group_vector(uuid)
        return True

    async def delete_group(self, uuid: str) -> bool:
        """删除群组行。

        别名 / 印象 / 熟悉度 / 成员印象 / 联系方式 / 群组自我形象 / 向量都在同一
        行的 JSON/BLOB 列内，随行一并删除；返回 False 表示 uuid 不存在。
        """
        if not uuid:
            return False
        conn = await self.db._ensure_connection()
        cur = await conn.execute("DELETE FROM groups WHERE uuid=?", (uuid,))
        await conn.commit()
        return cur.rowcount > 0

    # ============================================================
    # 查询
    # ============================================================

    async def get_group_by_uuid(self, uuid: str) -> Optional[Dict[str, Any]]:
        """根据 UUID 获取群组信息。"""
        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT * FROM groups WHERE uuid=?", (uuid,))
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "uuid": row["uuid"],
            "name": row["name"],
            "usage_count": row["usage_count"],
            "last_used_at": row["last_used_at"],
            "created_at": row["created_at"],
        }

    async def get_group_by_contact(self, session_id: str) -> Optional[str]:
        """根据会话 ID 查找对应群组 UUID。"""
        if not session_id:
            return None
        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT uuid, contacts FROM groups")
        rows = await cur.fetchall()
        for r in rows:
            contacts = json.loads(r["contacts"] or "[]")
            if any(c.get("session_id") == session_id for c in contacts):
                return r["uuid"]
        return None

    async def get_group_with_details(self, uuid: str) -> Optional[Dict[str, Any]]:
        """根据 UUID 获取群组详细信息，包括别名、印象、熟悉度、联系方式。"""
        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT * FROM groups WHERE uuid=?", (uuid,))
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "uuid": row["uuid"],
            "name": row["name"],
            "aliases": [{"name": a, "usage_count": 0} for a in json.loads(row["aliases"] or "[]")],
            "impressions": [{"content": row["impression"]}] if row["impression"] else [],
            "familiarities": [{"content": row["familiarity"]}] if row["familiarity"] else [],
            "member_impressions": [{"content": row["member_impression"]}] if row["member_impression"] else [],
            "contacts": json.loads(row["contacts"] or "[]"),
            "relationships_with_default": [{"type": row["self_image"]}] if row["self_image"] else [],
            "usage_count": row["usage_count"],
            "last_used_at": row["last_used_at"],
            "created_at": row["created_at"],
        }

    # ============================================================
    # 格式化（合并 format_group_info 与 format_group_info_for_database）
    # ============================================================

    def format_group_info(self, group_info: Optional[Dict[str, Any]]) -> str:
        """格式化群组信息为字符串。"""
        if not group_info:
            return "未找到群组信息"

        info_parts = []

        aliases = group_info.get("aliases", [])
        names = [a.get("name", "") for a in aliases if a.get("name")]
        if not names and group_info.get("name"):
            names = [group_info.get("name")]
        info_parts.append(f"名称：{', '.join(names)}" if names else "名称：未知")

        impressions = group_info.get("impressions", [])
        if impressions:
            info_parts.append(f"印象：{impressions[0].get('content', '')}")
        else:
            info_parts.append("印象：未设置")

        familiarities = group_info.get("familiarities", [])
        if familiarities:
            info_parts.append(f"熟悉程度：{familiarities[0].get('content', '')}")
        else:
            info_parts.append("熟悉程度：未设置")

        images = [
            r.get("type", "")
            for r in group_info.get("relationships_with_default", [])
            if r.get("type")
        ]
        info_parts.append(f"我在该群聊的形象：{'，'.join(images) if images else '未设置'}")

        contacts = group_info.get("contacts", [])
        if contacts:
            contact_info = [c.get("session_id", "") for c in contacts[:2]]
            info_parts.append(f"联系方式：{', '.join(contact_info)}")

        return " ".join(info_parts)

    # ============================================================
    # 向量
    # ============================================================

    async def update_group_vector(self, uuid: str) -> bool:
        """更新群组向量，拼接名称+别名+印象+熟悉度+关系后向量化。"""
        group_info = await self.get_group_with_details(uuid)
        if not group_info:
            logger.warning(f"未找到群组 {uuid}，无法更新向量")
            return False

        text_parts = []
        if group_info.get("name"):
            text_parts.append(group_info["name"])
        text_parts.extend(a.get("name", "") for a in group_info.get("aliases", []) if a.get("name"))
        for i in group_info.get("impressions", []):
            if i.get("content"):
                text_parts.append(i["content"])
        for f in group_info.get("familiarities", []):
            if f.get("content"):
                text_parts.append(f["content"])
        for r in group_info.get("relationships_with_default", []):
            if r.get("type"):
                text_parts.append(r["type"])

        combined_text = " ".join(text_parts) or group_info.get("name", "")
        vector_store = get_vector_store()
        vector = await vector_store.generate_vector(combined_text, target="group")

        conn = await self.db._ensure_connection()
        await conn.execute(
            """UPDATE groups SET vector=?, is_vectorized=?, vector_updated_at=?
               WHERE uuid=?""",
            (serialize_vector(vector), 1 if vector else 0, datetime.now().isoformat(), uuid),
        )
        await conn.commit()
        return True
