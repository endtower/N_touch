# -*- coding: utf-8 -*-
"""
群组操作类（SQLite）

以 00000000（LLM 扮演的角色）为中心，管理 00000000 指向的群组。
群组行只存单值字段（印象、熟悉度、群组成员印象、self_image）；
名称（主名 + 别名）与联系方式分别存在 entity_names / entity_contacts，
由 entity_identity 统一读写，(类型, 名字) 与 session_id 都是主键。
"""

import json
from datetime import datetime
from typing import Optional, List, Dict, Any

from . import entity_identity as identity
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
        """创建群组节点。传入 uuid 表示按既有 uuid 幂等写入（同名覆盖主名）。"""
        current_time = datetime.now().isoformat()
        if not uuid:
            uuid = generate_timestamp_id("Group")

        conn = await self.db._ensure_connection()
        await conn.execute(
            """INSERT INTO groups (uuid, usage_count, last_used_at, created_at)
               VALUES (?, 0, ?, ?)
               ON CONFLICT(uuid) DO NOTHING""",
            (uuid, current_time, current_time),
        )
        await identity.set_primary_name(conn, identity.GROUP, uuid, name)
        await conn.commit()
        return uuid

    async def create_alias_node(self, name: str, owner_uuid: str) -> None:
        """为群组添加别名。"""
        conn = await self.db._ensure_connection()
        await identity.add_name(conn, identity.GROUP, owner_uuid, name)
        await conn.commit()

    async def create_contact_node(self, session_id: str, owner_uuid: str) -> None:
        """为群组添加联系方式。"""
        conn = await self.db._ensure_connection()
        await identity.add_contact(conn, identity.GROUP, owner_uuid, session_id)
        await conn.commit()

    async def replace_aliases(self, owner_uuid: str, new_aliases: list) -> None:
        """整组替换群组名称（new_aliases[0] 为主名）。"""
        conn = await self.db._ensure_connection()
        await identity.replace_names(conn, identity.GROUP, owner_uuid, new_aliases)
        await conn.commit()

    async def update_group(self, uuid: str, **fields: Any) -> bool:
        """更新群组可编辑字段（名称/别名/印象/熟悉度/成员印象/self_image），成功后重新向量化。

        fields 支持:
          name: str              — 主名（被其他群组占用则抛 NameConflictError）
          aliases: list[str]     — 名称整组替换（首项为主名）
          impression: str        — 印象（单值）
          familiarity: str       — 熟悉度（单值）
          member_impression: str — 群组成员印象（单值）
          self_image: str        — 我在该群聊的形象（单值）
          allow_conversation: bool — 是否允许 conversation_agent 对该群的会话发起对话
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
        if fields.get("allow_conversation") is not None:
            sets.append("allow_conversation=?")
            params.append(1 if fields["allow_conversation"] else 0)

        if sets:
            params.append(uuid)
            await conn.execute(
                f"UPDATE groups SET {', '.join(sets)} WHERE uuid=?",
                params,
            )

        if fields.get("aliases") is not None:
            await identity.replace_names(conn, identity.GROUP, uuid, fields["aliases"])
        elif fields.get("name") is not None:
            await identity.set_primary_name(conn, identity.GROUP, uuid, fields["name"])

        await conn.commit()

        # 仅切换放行开关不影响描述文本，避免无谓的向量重算
        if set(fields) - {"allow_conversation"}:
            await self.update_group_vector(uuid)
        return True

    async def delete_group(self, uuid: str) -> bool:
        """删除群组行及其名称与联系方式；返回 False 表示 uuid 不存在。"""
        if not uuid:
            return False
        conn = await self.db._ensure_connection()
        cur = await conn.execute("DELETE FROM groups WHERE uuid=?", (uuid,))
        if cur.rowcount == 0:
            await conn.commit()
            return False
        await identity.delete_entity(conn, identity.GROUP, uuid)
        await conn.commit()
        return True

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
            "name": await identity.get_primary_name(conn, identity.GROUP, uuid),
            "usage_count": row["usage_count"],
            "last_used_at": row["last_used_at"],
            "created_at": row["created_at"],
        }

    async def get_group_by_contact(self, session_id: str) -> Optional[str]:
        """根据会话 ID 查找对应群组 UUID（只认群组，不认角色）。"""
        if not session_id:
            return None
        conn = await self.db._ensure_connection()
        found = await identity.find_entity_by_contact(conn, session_id)
        if found and found[0] == identity.GROUP:
            return found[1]
        return None

    async def get_group_with_details(self, uuid: str) -> Optional[Dict[str, Any]]:
        """根据 UUID 获取群组详细信息，包括别名、印象、熟悉度、联系方式。"""
        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT * FROM groups WHERE uuid=?", (uuid,))
        row = await cur.fetchone()
        if row is None:
            return None
        names = await identity.get_names(conn, identity.GROUP, uuid)
        return {
            "uuid": row["uuid"],
            "name": names[0] if names else "",
            "aliases": [{"name": n, "usage_count": 0} for n in names],
            "impressions": [{"content": row["impression"]}] if row["impression"] else [],
            "familiarities": [{"content": row["familiarity"]}] if row["familiarity"] else [],
            "member_impressions": [{"content": row["member_impression"]}] if row["member_impression"] else [],
            "contacts": await identity.get_contacts(conn, identity.GROUP, uuid),
            "relationships_with_default": [{"type": row["self_image"]}] if row["self_image"] else [],
            "allow_conversation": bool(row["allow_conversation"]),
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
