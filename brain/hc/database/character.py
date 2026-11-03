# -*- coding: utf-8 -*-
"""
角色操作类（SQLite）

以 00000000（LLM 扮演的角色）为中心，管理 00000000 指向的其他角色。
角色行只存单值字段（印象、熟悉度、关系、好感度事件）；
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
from utils.error_handling import handle_errors, AbyssCoreError

logger = get_logger(__name__, "INFO")


class CharacterOperations:
    """角色操作类（纯异步）"""

    def __init__(self):
        self.db = get_memory_db()

    # ============================================================
    # 创建
    # ============================================================

    async def create_character_node(self, name: str) -> str:
        """创建角色节点，必须输入 name，同时把它记为主名。

        同名已存在时先抛错，避免「先建行再改名失败」留下半条记录。
        """
        uuid = generate_timestamp_id("Character")
        current_time = datetime.now().isoformat()

        conn = await self.db._ensure_connection()
        existing = await identity.find_entity_by_name(conn, identity.CHARACTER, name)
        if existing:
            raise identity.NameConflictError(f"角色名称「{name}」已存在（uuid={existing}）")

        await conn.execute(
            """INSERT INTO characters (uuid, usage_count, last_used_at, created_at)
               VALUES (?, 0, ?, ?)""",
            (uuid, current_time, current_time),
        )
        await identity.set_primary_name(conn, identity.CHARACTER, uuid, name)
        await conn.commit()
        return uuid

    async def create_alias_node(self, name: str, owner_uuid: str) -> None:
        """为角色添加别名。"""
        conn = await self.db._ensure_connection()
        await identity.add_name(conn, identity.CHARACTER, owner_uuid, name)
        await conn.commit()

    async def create_contact_node(self, session_id: str, owner_uuid: str) -> None:
        """为角色添加联系方式。"""
        conn = await self.db._ensure_connection()
        await identity.add_contact(conn, identity.CHARACTER, owner_uuid, session_id)
        await conn.commit()

    async def replace_aliases(self, owner_uuid: str, new_aliases: list) -> None:
        """整组替换角色名称（new_aliases[0] 为主名）。"""
        conn = await self.db._ensure_connection()
        await identity.replace_names(conn, identity.CHARACTER, owner_uuid, new_aliases)
        await conn.commit()

    async def update_character(self, uuid: str, **fields: Any) -> bool:
        """更新角色可编辑字段（名称/别名/印象/熟悉度/关系），成功后重新向量化。

        fields 支持:
          name: str          — 主名（改为该名称；被其他角色占用则抛 NameConflictError）
          aliases: list[str] — 名称整组替换（首项为主名）
          impression: str    — 印象（单值）
          familiarity: str   — 熟悉度（单值）
          relationship: str  — 与 00000000 的关系（单值）
          allow_conversation: bool — 是否允许 conversation_agent 对该角色的会话发起对话
        传 None 的字段跳过不修改。返回 False 表示角色 uuid 不存在。
        """
        if not uuid:
            return False

        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT uuid FROM characters WHERE uuid=?", (uuid,))
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
        if fields.get("relationship") is not None:
            sets.append("relationship=?")
            params.append(fields["relationship"])
        if fields.get("allow_conversation") is not None:
            sets.append("allow_conversation=?")
            params.append(1 if fields["allow_conversation"] else 0)

        if sets:
            params.append(uuid)
            await conn.execute(
                f"UPDATE characters SET {', '.join(sets)} WHERE uuid=?",
                params,
            )

        # 名称：aliases 优先（整组替换），否则单独改主名
        if fields.get("aliases") is not None:
            await identity.replace_names(conn, identity.CHARACTER, uuid, fields["aliases"])
        elif fields.get("name") is not None:
            await identity.set_primary_name(conn, identity.CHARACTER, uuid, fields["name"])

        await conn.commit()

        # 仅切换放行开关不影响描述文本，避免无谓的向量重算
        if set(fields) - {"allow_conversation"}:
            await self.update_character_vector(uuid)
        return True

    async def delete_character(self, uuid: str) -> bool:
        """删除角色行及其名称与联系方式；返回 False 表示 uuid 不存在。"""
        if not uuid:
            return False
        conn = await self.db._ensure_connection()
        cur = await conn.execute("DELETE FROM characters WHERE uuid=?", (uuid,))
        if cur.rowcount == 0:
            await conn.commit()
            return False
        await identity.delete_entity(conn, identity.CHARACTER, uuid)
        await conn.commit()
        return True

    # ============================================================
    # 查询
    # ============================================================

    async def find_character_uuid(self, identifier: str) -> Optional[str]:
        """根据主名或别名查找角色 UUID（同类型内名称唯一，不会多匹配）。"""
        if not identifier:
            return None
        conn = await self.db._ensure_connection()
        return await identity.find_entity_by_name(conn, identity.CHARACTER, identifier)

    async def get_character_by_contact(self, session_id: str) -> Optional[str]:
        """根据会话 ID 查找对应角色 UUID（只认角色，不认群组）。"""
        if not session_id:
            return None
        conn = await self.db._ensure_connection()
        found = await identity.find_entity_by_contact(conn, session_id)
        if found and found[0] == identity.CHARACTER:
            return found[1]
        return None

    async def get_character_full_info(self, uuid: str) -> Optional[Dict[str, Any]]:
        """通过 UUID 查询角色的完整信息。"""
        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT * FROM characters WHERE uuid=?", (uuid,))
        row = await cur.fetchone()
        if row is None:
            return None
        names = await identity.get_names(conn, identity.CHARACTER, uuid)
        return {
            "uuid": row["uuid"],
            "name": names[0] if names else "",
            "aliases": [{"name": n, "usage_count": 0} for n in names],
            "impressions": [{"content": row["impression"]}] if row["impression"] else [],
            "familiarities": [{"content": row["familiarity"]}] if row["familiarity"] else [],
            "contacts": await identity.get_contacts(conn, identity.CHARACTER, uuid),
            "relationships_with_default": [{"type": row["relationship"]}] if row["relationship"] else [],
            "allow_conversation": bool(row["allow_conversation"]),
            "affinity_events": json.loads(row["affinity_events"] or "[]"),
            "usage_count": row["usage_count"],
            "last_used_at": row["last_used_at"],
            "created_at": row["created_at"],
        }

    # ============================================================
    # 格式化（合并 format_character_info 与 format_character_info_for_database）
    # ============================================================

    @handle_errors()
    def format_character_info(self, character_info: Optional[Dict[str, Any]]) -> str:
        """格式化角色信息为字符串（口语化叙述，适合展示给用户）。"""
        if not character_info:
            raise AbyssCoreError("未找到角色信息")

        info_parts = []

        aliases = character_info.get("aliases", [])
        alias_names = [a.get("name", "") for a in aliases if a.get("name")]
        main_name = ""
        other_aliases = []
        if alias_names:
            main_name = alias_names[0]
            other_aliases = alias_names[1:]
        else:
            main_name = character_info.get("name", "")

        info_parts.append(f"名称：{main_name}")
        if other_aliases:
            info_parts.append(f"别名：{', '.join(other_aliases)}")

        impressions = character_info.get("impressions", [])
        if impressions:
            info_parts.append(f"印象：{impressions[0].get('content', '')}")
        else:
            info_parts.append("印象：未设置")

        familiarities = character_info.get("familiarities", [])
        if familiarities:
            info_parts.append(f"熟悉程度：{familiarities[0].get('content', '')}")
        else:
            info_parts.append("熟悉程度：未设置")

        relationships = character_info.get("relationships_with_default", [])
        if relationships:
            rel_text = "，".join(r.get("type", "") for r in relationships if r.get("type"))
            if rel_text:
                info_parts.append(f"关系：{rel_text}")

        contacts = character_info.get("contacts", [])
        if contacts:
            contact_info = [c.get("session_id", "") for c in contacts[:2]]
            info_parts.append(f"联系方式：{', '.join(contact_info)}")

        return " ".join(info_parts)

    # ============================================================
    # 向量
    # ============================================================

    async def update_character_vector(self, uuid: str) -> bool:
        """更新角色向量，拼接名称+别名+印象+熟悉度+关系后向量化。"""
        char_info = await self.get_character_full_info(uuid)
        if not char_info:
            logger.warning(f"未找到角色 {uuid}，无法更新向量")
            return False

        text_parts = []
        if char_info.get("name"):
            text_parts.append(char_info["name"])
        text_parts.extend(a.get("name", "") for a in char_info.get("aliases", []) if a.get("name"))
        for i in char_info.get("impressions", []):
            if i.get("content"):
                text_parts.append(i["content"])
        for f in char_info.get("familiarities", []):
            if f.get("content"):
                text_parts.append(f["content"])
        for r in char_info.get("relationships_with_default", []):
            if r.get("type"):
                text_parts.append(r["type"])

        combined_text = " ".join(text_parts) or char_info.get("name", "")
        vector_store = get_vector_store()
        vector = await vector_store.generate_vector(combined_text, target="character")

        conn = await self.db._ensure_connection()
        await conn.execute(
            """UPDATE characters SET vector=?, is_vectorized=?, vector_updated_at=?
               WHERE uuid=?""",
            (serialize_vector(vector), 1 if vector else 0, datetime.now().isoformat(), uuid),
        )
        await conn.commit()
        return True

    @handle_errors()
    async def search_character_or_group(self, query_text: str, top_k: int = 5,
                                        exclude_uuids: list = None,
                                        search_type: str = 'character') -> list:
        """通过向量匹配搜索角色或群聊。"""
        if not query_text:
            raise ValueError("查询文本不能为空")

        vector_store = get_vector_store()
        query_vector = await vector_store.generate_vector(query_text, target="character")
        if not query_vector:
            return []

        conn = await self.db._ensure_connection()
        table = "characters" if search_type == "character" else "groups"
        entity_type = identity.CHARACTER if search_type == "character" else identity.GROUP
        cur = await conn.execute(f"SELECT uuid, vector FROM {table} WHERE is_vectorized=1")
        rows = await cur.fetchall()

        results = []
        exclude_uuids = exclude_uuids or []
        for r in rows:
            if r["uuid"] in exclude_uuids:
                continue
            vec = deserialize_vector(r["vector"])
            if vec and len(vec) == len(query_vector):
                sim = cosine_similarity(query_vector, vec)
                results.append({
                    "uuid": r["uuid"],
                    "name": await identity.get_primary_name(conn, entity_type, r["uuid"]),
                    "similarity": sim,
                    "entity_type": search_type,
                })

        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_k]
