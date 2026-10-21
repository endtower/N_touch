# -*- coding: utf-8 -*-
"""
角色复合操作类（SQLite）

组合 CharacterOperations / RelationshipOperations / Impression / Familiarity 等操作，
实现角色完整信息查询与更新。
"""

import json

from .character import CharacterOperations
from .relationship import RelationshipOperations
from .impression import get_impression
from .familiarity import get_familiarity
from utils.error_handling import handle_errors
from utils.logger import get_logger

logger = get_logger(__name__, "INFO")


class CharacterCompose:
    """角色复合操作类（纯异步）"""

    DEFAULT_FAMILIARITY_TEXT = "你从未在此会话中发过言，你需要极度谨慎的进行对待"

    def __init__(self):
        self.character_ops = CharacterOperations()

    @handle_errors()
    async def character_operation(self, operation_type, **kwargs):
        """角色相关的复合操作方法。"""
        if operation_type == "create":
            name = kwargs["name"]
            await self.character_ops.create_character_node(name)
            character_uuid = await self.character_ops.find_character_uuid(name)

            if kwargs.get("impression"):
                impression_instance = get_impression()
                await impression_instance.create_impression(kwargs["impression"], character_uuid)

            if kwargs.get("familiarity"):
                familiarity_instance = get_familiarity()
                await familiarity_instance.create_familiarity(kwargs["familiarity"], character_uuid)

            if kwargs.get("self_relationship"):
                relationship_ops = RelationshipOperations()
                await relationship_ops.create_character_relationship(character_uuid, kwargs["self_relationship"])

            if kwargs.get("contacts"):
                for contact in kwargs["contacts"]:
                    session_id = contact.get("session_id")
                    if session_id:
                        await self.character_ops.create_contact_node(session_id, character_uuid)

            return {"status": "success", "message": f"角色 {name} 创建成功"}

        elif operation_type == "add_alias":
            name, alias = kwargs["name"], kwargs["alias"]
            character_uuid = await self.character_ops.find_character_uuid(name)
            await self.character_ops.create_alias_node(alias, character_uuid)
            return {"status": "success", "message": f"为角色 {name} 添加别名 {alias} 成功"}

        elif operation_type == "add_impression":
            name, impression = kwargs["name"], kwargs["impression"]
            character_uuid = await self.character_ops.find_character_uuid(name)
            impression_instance = get_impression()
            impression_id = await impression_instance.create_impression(impression, character_uuid)
            return {"status": "success", "message": f"为角色 {name} 添加印象成功，印象ID: {impression_id}"}

        elif operation_type == "add_familiarity":
            name, familiarity = kwargs["name"], kwargs["familiarity"]
            character_uuid = await self.character_ops.find_character_uuid(name)
            familiarity_instance = get_familiarity()
            familiarity_id = await familiarity_instance.create_familiarity(familiarity, character_uuid)
            return {"status": "success", "message": f"为角色 {name} 设置熟悉程度成功，ID: {familiarity_id}"}

        elif operation_type == "add_contact":
            name, session_id = kwargs["name"], kwargs["session_id"]
            character_uuid = await self.character_ops.find_character_uuid(name)
            await self.character_ops.create_contact_node(session_id, character_uuid)
            return {"status": "success", "message": f"为角色 {name} 添加联系方式成功"}

        elif operation_type == "add_relationship":
            name, relation_type = kwargs["name"], kwargs["relation_type"]
            character_uuid = await self.character_ops.find_character_uuid(name)
            relationship_ops = RelationshipOperations()
            await relationship_ops.create_character_relationship(character_uuid, relation_type)
            return {"status": "success", "message": f"为角色 {name} 添加关系成功"}

        elif operation_type == "add_self_relationship":
            name, relation_type = kwargs["name"], kwargs["relation_type"]
            character_uuid = await self.character_ops.find_character_uuid(name)
            relationship_ops = RelationshipOperations()
            await relationship_ops.create_character_relationship(character_uuid, relation_type)
            return {"status": "success", "message": f"为角色 {name} 添加与自己的关系成功"}

        elif operation_type == "overwrite":
            name = kwargs["name"]
            character_uuid = await self.character_ops.find_character_uuid(name)
            if not character_uuid:
                await self.character_ops.create_character_node(name)
                character_uuid = await self.character_ops.find_character_uuid(name)

            impression_instance = get_impression()
            if kwargs.get("impression"):
                await impression_instance.create_impression(kwargs["impression"], character_uuid)

            if kwargs.get("familiarity"):
                familiarity_instance = get_familiarity()
                await familiarity_instance.create_familiarity(kwargs["familiarity"], character_uuid)

            if kwargs.get("self_relationship"):
                relationship_ops = RelationshipOperations()
                await relationship_ops.create_character_relationship(character_uuid, kwargs["self_relationship"])

            if kwargs.get("contacts"):
                for contact in kwargs["contacts"]:
                    session_id = contact.get("session_id")
                    if session_id:
                        await self.character_ops.create_contact_node(session_id, character_uuid)

            return {"status": "success", "message": f"角色 {name} 信息覆盖成功"}

        else:
            raise ValueError(f"不支持的操作类型: {operation_type}")

    @handle_errors()
    async def modify_character_info(self, character_name, field_type, operation_type, old_value, new_value=None):
        """修改或删除角色信息，包括关系。"""
        character_uuid = await self.character_ops.find_character_uuid(character_name)
        relationship_ops = RelationshipOperations()

        if field_type == "alias":
            if operation_type == "add":
                await self.character_ops.create_alias_node(new_value, character_uuid)
                return {"status": "success", "message": f"为角色 {character_name} 添加别名 {new_value} 成功"}
            elif operation_type == "modify":
                await self._modify_alias(character_uuid, old_value, new_value)
                return {"status": "success", "message": f"成功修改角色 {character_name} 的别名为 {new_value}"}
            elif operation_type == "delete":
                await self._delete_alias(character_uuid, old_value)
                return {"status": "success", "message": f"成功删除角色 {character_name} 的别名 {old_value}"}

        elif field_type == "impression":
            impression_instance = get_impression()
            if operation_type == "add":
                impression_id = await impression_instance.create_impression(new_value, character_uuid)
                return {"status": "success", "message": f"覆盖角色 {character_name} 的印象为: {new_value}"}
            elif operation_type == "modify":
                await impression_instance.create_impression(new_value, character_uuid)
                return {"status": "success", "message": f"成功修改角色 {character_name} 的印象为 {new_value}"}
            elif operation_type == "delete":
                await self._clear_impression(character_uuid)
                return {"status": "success", "message": f"成功删除角色 {character_name} 的印象"}

        elif field_type == "contact":
            if operation_type == "modify":
                await self._modify_contact(character_uuid, old_value, new_value)
                return {"status": "success", "message": f"成功修改角色 {character_name} 的联系方式为 {new_value}"}
            elif operation_type == "delete":
                await self._delete_contact(character_uuid, old_value)
                return {"status": "success", "message": f"成功删除角色 {character_name} 的联系方式 {old_value}"}

        elif field_type == "familiarity":
            familiarity_instance = get_familiarity()
            if operation_type == "modify":
                if not new_value:
                    raise ValueError("修改熟悉程度需要提供 new_value")
                await familiarity_instance.create_familiarity(new_value, character_uuid)
                return {"status": "success", "message": f"成功修改角色 {character_name} 的熟悉程度为 {new_value}"}
            elif operation_type == "delete":
                await familiarity_instance.delete_familiarity(character_uuid)
                return {"status": "success", "message": f"成功删除角色 {character_name} 的熟悉程度"}

        elif field_type == "relationship":
            if operation_type == "modify":
                await relationship_ops.create_character_relationship(character_uuid, new_value)
                return {"status": "success", "message": f"成功修改角色 {character_name} 的关系为 {new_value}"}
            elif operation_type == "delete":
                await self._clear_relationship(character_uuid)
                return {"status": "success", "message": f"成功删除角色 {character_name} 的关系"}

        elif field_type == "self_relationship":
            if operation_type == "modify":
                await relationship_ops.create_character_relationship(character_uuid, new_value)
                return {"status": "success", "message": f"成功修改角色 {character_name} 与自己的关系为 {new_value}"}
            elif operation_type == "delete":
                await self._clear_relationship(character_uuid)
                return {"status": "success", "message": f"成功删除角色 {character_name} 与自己的关系"}

        else:
            raise ValueError(f"不支持的字段类型: {field_type}")

    # ============================================================
    # 内部辅助（别名 / 联系方式 / 印象 / 关系 的增删改）
    # ============================================================

    async def _modify_alias(self, uuid: str, old_value: str, new_value: str) -> None:
        conn = await self.character_ops.db._ensure_connection()
        cur = await conn.execute("SELECT aliases FROM characters WHERE uuid=?", (uuid,))
        row = await cur.fetchone()
        if row is None:
            return
        aliases = json.loads(row["aliases"] or "[]")
        if old_value in aliases:
            aliases[aliases.index(old_value)] = new_value
        await conn.execute("UPDATE characters SET aliases=? WHERE uuid=?", (json.dumps(aliases, ensure_ascii=False), uuid))
        await conn.commit()

    async def _delete_alias(self, uuid: str, old_value: str) -> None:
        conn = await self.character_ops.db._ensure_connection()
        cur = await conn.execute("SELECT aliases FROM characters WHERE uuid=?", (uuid,))
        row = await cur.fetchone()
        if row is None:
            return
        aliases = json.loads(row["aliases"] or "[]")
        if old_value in aliases:
            aliases.remove(old_value)
        await conn.execute("UPDATE characters SET aliases=? WHERE uuid=?", (json.dumps(aliases, ensure_ascii=False), uuid))
        await conn.commit()

    async def _modify_contact(self, uuid: str, old_value: str, new_value: str) -> None:
        conn = await self.character_ops.db._ensure_connection()
        cur = await conn.execute("SELECT contacts FROM characters WHERE uuid=?", (uuid,))
        row = await cur.fetchone()
        if row is None:
            return
        contacts = json.loads(row["contacts"] or "[]")
        for c in contacts:
            if c.get("session_id") == old_value:
                c["session_id"] = new_value
        await conn.execute("UPDATE characters SET contacts=? WHERE uuid=?", (json.dumps(contacts, ensure_ascii=False), uuid))
        await conn.commit()

    async def _delete_contact(self, uuid: str, old_value: str) -> None:
        conn = await self.character_ops.db._ensure_connection()
        cur = await conn.execute("SELECT contacts FROM characters WHERE uuid=?", (uuid,))
        row = await cur.fetchone()
        if row is None:
            return
        contacts = json.loads(row["contacts"] or "[]")
        contacts = [c for c in contacts if c.get("session_id") != old_value]
        await conn.execute("UPDATE characters SET contacts=? WHERE uuid=?", (json.dumps(contacts, ensure_ascii=False), uuid))
        await conn.commit()

    async def _clear_impression(self, uuid: str) -> None:
        conn = await self.character_ops.db._ensure_connection()
        await conn.execute("UPDATE characters SET impression='' WHERE uuid=?", (uuid,))
        await conn.commit()

    async def _clear_relationship(self, uuid: str) -> None:
        conn = await self.character_ops.db._ensure_connection()
        await conn.execute("UPDATE characters SET relationship='' WHERE uuid=?", (uuid,))
        await conn.commit()

    # ============================================================
    # 第一会话印象查找
    # ============================================================

    @handle_errors()
    async def first_session_impression_search(self, name=None, session_id=None):
        """第一会话印象查找（陌生人首次交流场景）。"""
        if not name and not session_id:
            raise ValueError("至少需要提供 name 或 session_id")

        from adapt.session_identity import session_scope
        is_group = session_scope(session_id) == "group"

        entity_uuid = None
        created = False
        found_by = None
        consumed_fuzzy_role_uuid = None
        consumed_fuzzy_content = None

        # 1. 优先按联系方式查找
        if session_id:
            if is_group:
                from .group import GroupOperations
                group_ops = GroupOperations()
                entity_uuid = await group_ops.get_group_by_contact(session_id)
            else:
                entity_uuid = await self.character_ops.get_character_by_contact(session_id)

            if entity_uuid:
                found_by = 'contact'
                if name:
                    await self._ensure_name_alias(entity_uuid, name, is_group)

        # 2. 联系方式未命中 → 按名称查找
        if not entity_uuid and name:
            if is_group:
                entity_uuid = await self._find_group_by_name(name)
            else:
                entity_uuid = await self.character_ops.find_character_uuid(name)

            if entity_uuid:
                found_by = 'name'
                if session_id:
                    if is_group:
                        from .group import GroupOperations
                        group_ops = GroupOperations()
                        await group_ops.create_contact_node(session_id, entity_uuid)
                    else:
                        await self.character_ops.create_contact_node(session_id, entity_uuid)

        # 3. 名称也未命中 → 进一步查找 FuzzyRole
        if not entity_uuid and name:
            from .fuzzy_role import get_fuzzy_role_ops
            fuzzy_ops = get_fuzzy_role_ops()
            fuzzy_role = await fuzzy_ops.find_fuzzy_role_by_name(name)

            if fuzzy_role:
                created_name = name
                if is_group:
                    from .group import GroupOperations
                    group_ops = GroupOperations()
                    entity_uuid = await group_ops.create_group_node(created_name)
                    if session_id:
                        await group_ops.create_contact_node(session_id, entity_uuid)
                    if fuzzy_role.get("content"):
                        from .impression import get_impression
                        impression_instance = get_impression()
                        await impression_instance.create_impression(fuzzy_role["content"], entity_uuid)
                else:
                    entity_uuid = await self.character_ops.create_character_node(created_name)
                    if session_id:
                        await self.character_ops.create_contact_node(session_id, entity_uuid)
                    if fuzzy_role.get("content"):
                        from .impression import get_impression
                        impression_instance = get_impression()
                        await impression_instance.create_impression(fuzzy_role["content"], entity_uuid)

                consumed_fuzzy_role_uuid = fuzzy_role.get("uuid")
                consumed_fuzzy_content = fuzzy_role.get("content")
                found_by = 'fuzzy_role'
                created = True
                if consumed_fuzzy_role_uuid:
                    await fuzzy_ops.delete_fuzzy_role(consumed_fuzzy_role_uuid)

        # 4. 全部未命中 → 直接按联系方式 + 名称创建节点
        if not entity_uuid:
            if session_id:
                created_name = name if name else session_id
                if is_group:
                    from .group import GroupOperations
                    group_ops = GroupOperations()
                    entity_uuid = await group_ops.create_group_node(created_name)
                    await group_ops.create_contact_node(session_id, entity_uuid)
                else:
                    entity_uuid = await self.character_ops.create_character_node(created_name)
                    await self.character_ops.create_contact_node(session_id, entity_uuid)
                created = True
                found_by = 'new'
            else:
                found_by = 'none'
                return {
                    'uuid': None,
                    'created': False,
                    'info': None,
                    'formatted_info': f"名称「{name}」不存于记忆中",
                    'found_by': found_by,
                }

        # 5. 确保熟悉程度存在
        await self._ensure_default_familiarity(entity_uuid)

        # 6. 拉取最终实体信息并格式化
        if is_group:
            from .group import GroupOperations
            group_ops = GroupOperations()
            info = await group_ops.get_group_with_details(entity_uuid)
            formatted_info = group_ops.format_group_info(info)
        else:
            info = await self.character_ops.get_character_full_info(entity_uuid)
            formatted_info = self.character_ops.format_character_info(info)

        return {
            'uuid': entity_uuid,
            'created': created,
            'info': info,
            'formatted_info': formatted_info,
            'found_by': found_by,
        }

    async def _ensure_default_familiarity(self, entity_uuid: str) -> None:
        """确保实体拥有 Familiarity；缺失则写入默认熟悉程度。"""
        if not entity_uuid:
            return
        try:
            familiarity_instance = get_familiarity()
            existing = await familiarity_instance.get_familiarity(entity_uuid)
            if existing:
                return
            await familiarity_instance.create_familiarity(self.DEFAULT_FAMILIARITY_TEXT, entity_uuid)
            logger.info(f"[first_session_impression_search] 已为 {entity_uuid} 写入默认熟悉程度")
        except Exception as e:
            logger.warning(f"[first_session_impression_search] 写入默认熟悉程度失败: {e}")

    async def _ensure_name_alias(self, entity_uuid: str, name: str, is_group: bool) -> None:
        """确保 name 出现在实体的别名集合中。"""
        if not entity_uuid or not name:
            return
        if is_group:
            from .group import GroupOperations
            group_ops = GroupOperations()
            info = await group_ops.get_group_with_details(entity_uuid)
            aliases = info.get("aliases", []) if info else []
            if any(a.get("name") == name for a in aliases):
                return
            if info and info.get("name") == name:
                return
            await group_ops.create_alias_node(name, entity_uuid)
        else:
            info = await self.character_ops.get_character_full_info(entity_uuid)
            aliases = info.get("aliases", []) if info else []
            if any(a.get("name") == name for a in aliases):
                return
            if info and info.get("name") == name:
                return
            await self.character_ops.create_alias_node(name, entity_uuid)

    async def _find_group_by_name(self, name):
        """根据名称查找群组 UUID。"""
        from .group import GroupOperations
        group_ops = GroupOperations()
        conn = await group_ops.db._ensure_connection()
        cur = await conn.execute("SELECT uuid, name, aliases FROM groups")
        rows = await cur.fetchall()
        matches = []
        for r in rows:
            if r["name"] == name:
                matches.append(r["uuid"])
                continue
            aliases = json.loads(r["aliases"] or "[]")
            if name in aliases:
                matches.append(r["uuid"])
        if len(matches) > 1:
            raise ValueError(f"找到多个匹配的群组，名称: {name}")
        return matches[0] if matches else None
