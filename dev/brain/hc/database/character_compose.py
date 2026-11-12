# -*- coding: utf-8 -*-
"""
角色复合操作类（SQLite）

组合 CharacterOperations / RelationshipOperations / Impression / Familiarity 等操作，
实现角色完整信息查询与更新。
"""

from typing import Optional

from . import entity_identity as identity
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

    async def _resolve_character_uuid(self, kwargs) -> Optional[str]:
        """优先用 uuid 定位角色，其次用名称。

        uuid 是唯一可靠的定位方式：名称可能被大模型改动，且在「同名即同一人」
        之外，也不该靠名字反查来决定往谁身上挂数据。
        """
        uuid = kwargs.get("uuid") or kwargs.get("character_id")
        if uuid:
            info = await self.character_ops.get_character_full_info(uuid)
            if not info:
                raise ValueError(f"角色 uuid={uuid} 不存在")
            return uuid
        return await self.character_ops.find_character_uuid(kwargs.get("name"))

    @handle_errors()
    async def character_operation(self, operation_type, **kwargs):
        """角色相关的复合操作方法。"""
        if operation_type == "create":
            name = kwargs["name"]
            existing = await self.character_ops.find_character_uuid(name)
            if existing:
                # 同名即同一人：不重复建节点，直接把后续信息挂到既有实体上
                character_uuid = existing
            else:
                character_uuid = await self.character_ops.create_character_node(name)

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
            character_uuid = await self._resolve_character_uuid(kwargs)
            await self.character_ops.create_alias_node(alias, character_uuid)
            return {"status": "success", "message": f"为角色 {name} 添加别名 {alias} 成功"}

        elif operation_type == "add_impression":
            name, impression = kwargs["name"], kwargs["impression"]
            character_uuid = await self._resolve_character_uuid(kwargs)
            impression_instance = get_impression()
            impression_id = await impression_instance.create_impression(impression, character_uuid)
            return {"status": "success", "message": f"为角色 {name} 添加印象成功，印象ID: {impression_id}"}

        elif operation_type == "add_familiarity":
            name, familiarity = kwargs["name"], kwargs["familiarity"]
            character_uuid = await self._resolve_character_uuid(kwargs)
            familiarity_instance = get_familiarity()
            familiarity_id = await familiarity_instance.create_familiarity(familiarity, character_uuid)
            return {"status": "success", "message": f"为角色 {name} 设置熟悉程度成功，ID: {familiarity_id}"}

        elif operation_type == "add_contact":
            name, session_id = kwargs["name"], kwargs["session_id"]
            character_uuid = await self._resolve_character_uuid(kwargs)
            await self.character_ops.create_contact_node(session_id, character_uuid)
            return {"status": "success", "message": f"为角色 {name} 添加联系方式成功"}

        elif operation_type == "add_relationship":
            name, relation_type = kwargs["name"], kwargs["relation_type"]
            character_uuid = await self._resolve_character_uuid(kwargs)
            relationship_ops = RelationshipOperations()
            await relationship_ops.create_character_relationship(character_uuid, relation_type)
            return {"status": "success", "message": f"为角色 {name} 添加关系成功"}

        elif operation_type == "add_self_relationship":
            name, relation_type = kwargs["name"], kwargs["relation_type"]
            character_uuid = await self._resolve_character_uuid(kwargs)
            relationship_ops = RelationshipOperations()
            await relationship_ops.create_character_relationship(character_uuid, relation_type)
            return {"status": "success", "message": f"为角色 {name} 添加与自己的关系成功"}

        elif operation_type == "overwrite":
            name = kwargs.get("name")
            character_uuid = await self._resolve_character_uuid(kwargs)
            if not character_uuid:
                if not name:
                    raise ValueError("覆盖角色信息需要提供 name 或 character_id")
                character_uuid = await self.character_ops.create_character_node(name)

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

            return {"status": "success", "message": f"角色 {name or character_uuid} 信息覆盖成功"}

        else:
            raise ValueError(f"不支持的操作类型: {operation_type}")

    @handle_errors()
    async def modify_character_info(self, character_name, field_type, operation_type, old_value,
                                    new_value=None):
        """修改或删除角色信息，包括关系。character_name 需能唯一定位到角色。"""
        character_uuid = await self._resolve_character_uuid({"name": character_name})
        if not character_uuid:
            raise ValueError(f"未找到名称为 {character_name} 的角色")
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
        """把旧别名改成新名字；旧名不存在则忽略，新名被占用则抛错。"""
        conn = await self.character_ops.db._ensure_connection()
        names = await identity.get_names(conn, identity.CHARACTER, uuid)
        if old_value not in names:
            return
        renamed = [new_value if n == old_value else n for n in names]
        await identity.replace_names(conn, identity.CHARACTER, uuid, renamed)
        await conn.commit()

    async def _delete_alias(self, uuid: str, old_value: str) -> None:
        """删除别名；主名不可删（要改主名用 _modify_alias）。"""
        if not old_value:
            return
        conn = await self.character_ops.db._ensure_connection()
        names = await identity.get_names(conn, identity.CHARACTER, uuid)
        if not names or names[0] == old_value:
            logger.info("[modify_character_info] 角色 %s 的主名「%s」不可删除，已忽略", uuid, old_value)
            return
        await identity.delete_name(conn, identity.CHARACTER, uuid, old_value)
        await conn.commit()

    async def _modify_contact(self, uuid: str, old_value: str, new_value: str) -> None:
        conn = await self.character_ops.db._ensure_connection()
        await identity.move_contact(conn, identity.CHARACTER, uuid, old_value, new_value)
        await conn.commit()

    async def _delete_contact(self, uuid: str, old_value: str) -> None:
        conn = await self.character_ops.db._ensure_connection()
        await identity.delete_contact(conn, identity.CHARACTER, uuid, old_value)
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
                # 不给不存在的会话新建实体：查询通讯录等只读路径也会走到这里，
                # 一旦盲目建节点就会留下指向幽灵会话的联系方式（2026-11-11 事故）。
                # 这里只跳过建实体，仍返回一个不落库的说明，避免阻断通讯录渲染。
                from memory.message_repository import get_message_store
                if not await get_message_store().session_exists(session_id):
                    return {
                        'uuid': None,
                        'created': False,
                        'info': None,
                        'formatted_info': (
                            f"会话「{session_id}」在消息库中不存在，未建立实体。"
                            if not name else
                            f"「{name}」（会话 {session_id}）在消息库中不存在，未建立实体。"
                        ),
                        'found_by': 'none',
                    }
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
        """确保 name 出现在实体的名称集合中。

        名称归另一实体所有时不追加：会话与实体的绑定关系已经建立，名称唯一约束
        不该反过来把已成立的绑定推翻。冲突只记录，由大模型在改记忆时自行归并。
        """
        if not entity_uuid or not name:
            return
        conn = await self.character_ops.db._ensure_connection()
        entity_type = identity.GROUP if is_group else identity.CHARACTER
        names = await identity.get_names(conn, entity_type, entity_uuid)
        if name in names:
            return
        owner = await identity.find_entity_by_name(conn, entity_type, name)
        if owner and owner != entity_uuid:
            logger.warning(
                "[first_session_impression_search] 名称「%s」已属于 %s，"
                "会话已绑定 %s，跳过别名追加", name, owner, entity_uuid,
            )
            return
        await identity.add_name(conn, entity_type, entity_uuid, name)
        await conn.commit()

    async def _find_group_by_name(self, name):
        """根据名称查找群组 UUID（同类型内唯一，不会多匹配）。"""
        if not name:
            return None
        conn = await self.character_ops.db._ensure_connection()
        return await identity.find_entity_by_name(conn, identity.GROUP, name)
