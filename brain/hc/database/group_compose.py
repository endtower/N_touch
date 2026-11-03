# -*- coding: utf-8 -*-
"""
群组复合操作类（SQLite）

组合 GroupOperations / RelationshipOperations / Impression / Familiarity 等操作，
实现群组完整信息查询与更新。
"""

from typing import Optional

from . import entity_identity as identity
from .group import GroupOperations
from .relationship import RelationshipOperations
from .impression import get_impression
from .familiarity import get_familiarity
from utils.logger import get_logger

logger = get_logger(__name__, "INFO")


class GroupCompose:
    """群组复合操作类（纯异步）"""

    def __init__(self):
        self.group_ops = GroupOperations()

    async def _resolve_group_uuid(self, kwargs) -> Optional[str]:
        """优先用 uuid 定位群组，其次用名称。

        uuid 是唯一可靠的定位方式：名称可能被大模型改动，不能靠名字反查
        来决定往谁身上挂数据。
        """
        uuid = kwargs.get("uuid") or kwargs.get("group_id")
        if uuid:
            info = await self.group_ops.get_group_by_uuid(uuid)
            if not info:
                raise ValueError(f"群组 uuid={uuid} 不存在")
            return uuid
        return await self._find_group_by_name(kwargs.get("name") or kwargs.get("group_name"))

    async def group_operation(self, operation_type, **kwargs):
        """群组相关的复合操作方法。"""
        if operation_type == "create":
            name = kwargs.get("name")
            group_id = kwargs.get("group_id")
            if not name:
                raise ValueError("创建群组需要提供name参数")

            if group_id:
                await self.group_ops.create_group_node(name, uuid=group_id)
                group_uuid = group_id
            else:
                await self.group_ops.create_group_node(name)
                group_uuid = await self._find_group_by_name(name)

            if kwargs.get("impression") and group_uuid:
                impression_instance = get_impression()
                await impression_instance.create_impression(kwargs["impression"], group_uuid)

            if kwargs.get("familiarity") and group_uuid:
                familiarity_instance = get_familiarity()
                await familiarity_instance.create_familiarity(kwargs["familiarity"], group_uuid)

            if kwargs.get("self_image") and group_uuid:
                relationship_ops = RelationshipOperations()
                await relationship_ops.create_group_relationship(group_uuid, kwargs["self_image"])

            if kwargs.get("member_impression") and group_uuid:
                await self._set_member_impression(group_uuid, kwargs["member_impression"])

            action = "更新" if group_id else "创建"
            return {"status": "success", "message": f"群组 {name} {action}成功"}

        elif operation_type == "add_alias":
            group_name = kwargs.get("group_name")
            alias = kwargs.get("alias")
            if not group_name or not alias:
                raise ValueError("添加别名需要提供group_name和alias参数")
            group_uuid = await self._find_group_by_name(group_name)
            if not group_uuid:
                raise ValueError(f"未找到名称为 {group_name} 的群组")
            await self.group_ops.create_alias_node(alias, group_uuid)
            return {"status": "success", "message": f"为群组 {group_name} 添加别名 {alias} 成功"}

        elif operation_type == "add_impression":
            group_name = kwargs.get("group_name")
            impression = kwargs.get("impression")
            if not group_name or not impression:
                raise ValueError("添加印象需要提供group_name和impression参数")
            group_uuid = await self._find_group_by_name(group_name)
            if not group_uuid:
                raise ValueError(f"未找到名称为 {group_name} 的群组")
            impression_instance = get_impression()
            await impression_instance.create_impression(impression, group_uuid)
            return {"status": "success", "message": f"为群组 {group_name} 添加印象成功"}

        elif operation_type == "add_familiarity":
            group_name = kwargs.get("group_name")
            familiarity = kwargs.get("familiarity")
            if not group_name or not familiarity:
                raise ValueError("添加熟悉程度需要提供group_name和familiarity参数")
            group_uuid = await self._find_group_by_name(group_name)
            if not group_uuid:
                raise ValueError(f"未找到名称为 {group_name} 的群组")
            familiarity_instance = get_familiarity()
            familiarity_id = await familiarity_instance.create_familiarity(familiarity, group_uuid)
            return {"status": "success", "message": f"为群组 {group_name} 设置熟悉程度成功，ID: {familiarity_id}"}

        elif operation_type == "add_contact":
            group_name = kwargs.get("group_name")
            session_id = kwargs.get("session_id")
            if not group_name or not session_id:
                raise ValueError("添加联系方式需要提供group_name和session_id参数")
            group_uuid = await self._find_group_by_name(group_name)
            if not group_uuid:
                raise ValueError(f"未找到名称为 {group_name} 的群组")
            if await self._check_contact_exists(group_uuid, session_id):
                raise ValueError(f"群组 {group_name} 已存在该联系方式")
            await self.group_ops.create_contact_node(session_id, group_uuid)
            return {"status": "success", "message": f"为群组 {group_name} 添加联系方式成功"}

        elif operation_type == "overwrite":
            name = kwargs.get("name")
            group_uuid = await self._resolve_group_uuid(kwargs)
            if not group_uuid:
                if not name:
                    raise ValueError("覆盖群组信息需要提供 name 或 group_id")
                group_uuid = await self.group_ops.create_group_node(name)

            if kwargs.get("impression"):
                impression_instance = get_impression()
                await impression_instance.create_impression(kwargs["impression"], group_uuid)

            if kwargs.get("familiarity"):
                familiarity_instance = get_familiarity()
                await familiarity_instance.create_familiarity(kwargs["familiarity"], group_uuid)

            if kwargs.get("self_image"):
                relationship_ops = RelationshipOperations()
                await relationship_ops.create_group_relationship(group_uuid, kwargs["self_image"])

            if kwargs.get("member_impression"):
                await self._set_member_impression(group_uuid, kwargs["member_impression"])

            return {"status": "success", "message": f"群组 {name or group_uuid} 信息覆盖成功"}

        else:
            raise ValueError(f"不支持的操作类型: {operation_type}")

    async def modify_group_info(self, group_name, field_type, operation_type, old_value, new_value=None):
        """修改或删除群组信息。"""
        group_uuid = await self._find_group_by_name(group_name)
        if not group_uuid:
            raise ValueError(f"未找到名称为 {group_name} 的群组")

        if field_type == "alias":
            if operation_type == "modify":
                if not new_value:
                    raise ValueError("修改别名需要提供新值")
                await self._modify_alias(group_uuid, old_value, new_value)
                return {"status": "success", "message": f"成功修改群组 {group_name} 的别名为 {new_value}"}
            elif operation_type == "delete":
                await self._delete_alias(group_uuid, old_value)
                return {"status": "success", "message": f"成功删除群组 {group_name} 的别名 {old_value}"}

        elif field_type == "impression":
            impression_instance = get_impression()
            if operation_type == "modify":
                if not new_value:
                    raise ValueError("修改印象需要提供新值")
                await impression_instance.create_impression(new_value, group_uuid)
                return {"status": "success", "message": f"成功修改群组 {group_name} 的印象为 {new_value}"}
            elif operation_type == "delete":
                await self._clear_impression(group_uuid)
                return {"status": "success", "message": f"成功删除群组 {group_name} 的印象"}

        elif field_type == "familiarity":
            familiarity_instance = get_familiarity()
            if operation_type == "modify":
                if not new_value:
                    raise ValueError("修改熟悉程度需要提供 new_value")
                await familiarity_instance.create_familiarity(new_value, group_uuid)
                return {"status": "success", "message": f"成功修改群组 {group_name} 的熟悉程度为 {new_value}"}
            elif operation_type == "delete":
                await familiarity_instance.delete_familiarity(group_uuid)
                return {"status": "success", "message": f"成功删除群组 {group_name} 的熟悉程度"}

        elif field_type == "contact":
            if operation_type == "modify":
                if not new_value:
                    raise ValueError("修改联系方式需要提供新值")
                if await self._check_contact_exists(group_uuid, new_value):
                    raise ValueError(f"群组 {group_name} 已存在该联系方式")
                await self._modify_contact(group_uuid, old_value, new_value)
                return {"status": "success", "message": f"成功修改群组 {group_name} 的联系方式为 {new_value}"}
            elif operation_type == "delete":
                await self._delete_contact(group_uuid, old_value)
                return {"status": "success", "message": f"成功删除群组 {group_name} 的联系方式 {old_value}"}

        else:
            raise ValueError(f"不支持的字段类型: {field_type}")

    # ============================================================
    # 内部辅助
    # ============================================================

    async def _set_member_impression(self, group_uuid: str, content: str) -> None:
        conn = await self.group_ops.db._ensure_connection()
        await conn.execute("UPDATE groups SET member_impression=? WHERE uuid=?", (content, group_uuid))
        await conn.commit()

    async def _modify_alias(self, uuid: str, old_value: str, new_value: str) -> None:
        """把旧别名改成新名字；旧名不存在则忽略，新名被占用则抛错。"""
        if not new_value:
            raise ValueError("修改别名需要提供新值")
        conn = await self.group_ops.db._ensure_connection()
        names = await identity.get_names(conn, identity.GROUP, uuid)
        if old_value not in names:
            return
        renamed = [new_value if n == old_value else n for n in names]
        await identity.replace_names(conn, identity.GROUP, uuid, renamed)
        await conn.commit()

    async def _delete_alias(self, uuid: str, old_value: str) -> None:
        """删除别名；主名不可删（要改主名用 _modify_alias）。"""
        if not old_value:
            return
        conn = await self.group_ops.db._ensure_connection()
        names = await identity.get_names(conn, identity.GROUP, uuid)
        if not names or names[0] == old_value:
            logger.info("[modify_group_info] 群组 %s 的主名「%s」不可删除，已忽略", uuid, old_value)
            return
        await identity.delete_name(conn, identity.GROUP, uuid, old_value)
        await conn.commit()

    async def _clear_impression(self, uuid: str) -> None:
        conn = await self.group_ops.db._ensure_connection()
        await conn.execute("UPDATE groups SET impression='' WHERE uuid=?", (uuid,))
        await conn.commit()

    async def _modify_contact(self, uuid: str, old_value: str, new_value: str) -> None:
        conn = await self.group_ops.db._ensure_connection()
        await identity.move_contact(conn, identity.GROUP, uuid, old_value, new_value)
        await conn.commit()

    async def _delete_contact(self, uuid: str, old_value: str) -> None:
        conn = await self.group_ops.db._ensure_connection()
        await identity.delete_contact(conn, identity.GROUP, uuid, old_value)
        await conn.commit()

    async def _find_group_by_name(self, name):
        """根据名称查找群组 UUID（同类型内唯一，不会多匹配）。"""
        if not name:
            return None
        conn = await self.group_ops.db._ensure_connection()
        return await identity.find_entity_by_name(conn, identity.GROUP, name)

    async def _check_contact_exists(self, group_uuid, session_id):
        """检查该会话是否已属于这个群组。"""
        if not session_id:
            return False
        conn = await self.group_ops.db._ensure_connection()
        found = await identity.find_entity_by_contact(conn, session_id)
        return bool(found and found[0] == identity.GROUP and found[1] == group_uuid)
