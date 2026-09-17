# -*- coding: utf-8 -*-
"""通用工具：ToolBequest（话术经验登记工具：upsert_bequest / delete_bequest）

原 BequestAgent 侧实现（bequest_agent._upsert_bequest / _delete_bequest 及其
schema）于 2026-09-15 收口迁移至本模块，schema 与实现统一在此管理，
供各 agent 及反思系统（reflection_tools）直接拼装复用，避免复制漂移。

工具语义（话术经验专用：经验库 category 固定 "speech"，工具不暴露分类参数）：
- upsert_bequest：按 title 定位，存在则覆盖内容，否则创建新话术经验
- delete_bequest：按 title 删除话术经验

全异步：底层走 BequestOperations（纯异步），本模块不引入同步 IO。
"""

from typing import Dict, Any, List, Callable

from utils.logger import get_logger

logger = get_logger('ToolBequest', "DEBUG")


class ToolBequest:
    """话术经验通用工具：upsert_bequest（创建/覆盖）+ delete_bequest（删除）。

    经验库已固定为话术专用（category = "speech"），工具不再暴露分类参数。
    """

    # ============ 工具 schema（提示词） ============

    @classmethod
    def get_upsert_bequest_tool_schema(cls) -> Dict[str, Any]:
        """返回 upsert_bequest 工具的 schema（原样迁自 bequest_agent，2026-09-15）。"""
        return {
            "type": "function",
            "function": {
                "name": "upsert_bequest",
                "description": "创建话术经验（发言/回复技巧类）。如果已存在同名经验则覆盖内容，否则创建新的话术经验。"
                "话术经验的评判标准是是否能更加快速便捷的表达出具体意思，而不是对于事件结果的影响"
                "示例： 标题：表达惊讶，滑稽。 内容：用‘卧槽啊’表达惊叹、滑稽、震撼。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "标题"},
                        "summary": {"type": "string", "description": "内容"}
                    },
                    "required": ["title", "summary"]
                }
            }
        }

    @classmethod
    def get_delete_bequest_tool_schema(cls) -> Dict[str, Any]:
        """返回 delete_bequest 工具的 schema（原样迁自 bequest_agent，2026-09-15）。"""
        return {
            "type": "function",
            "function": {
                "name": "delete_bequest",
                "description": "删除话术经验。如果已登记的话术经验被认为不恰当，删除该经验",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "标题"}
                    },
                    "required": ["title"]
                }
            }
        }

    @classmethod
    def get_bequest_tool_schemas(cls) -> List[Dict[str, Any]]:
        """返回经验工具的 schema 列表（供工具表直接拼装）。"""
        return [
            cls.get_upsert_bequest_tool_schema(),
            cls.get_delete_bequest_tool_schema(),
        ]

    # ============ 工具实现 ============

    @classmethod
    async def upsert_bequest(cls, title: str, summary: str) -> Dict[str, Any]:
        """创建或覆盖话术经验（category 固定 "speech"）。

        按 title 精确匹配定位：命中则覆盖 summary，否则新建。

        Returns:
            {"bequest_id": str, "title": str, "is_new": bool}
        """
        from brain.hc.database.bequest import BequestOperations

        bequest_ops = BequestOperations()
        existing_id = await bequest_ops.get_bequest_id_by_title(title)

        if existing_id:
            await bequest_ops.update_bequest(title, summary=summary)
            is_new = False
            bequest_id = existing_id
        else:
            bequest_id = await bequest_ops.create_bequest(title=title, summary=summary)
            is_new = True

        logger.debug(f"upsert_bequest 完成: title={title}, is_new={is_new}")
        return {"bequest_id": bequest_id, "title": title, "is_new": is_new}

    @classmethod
    async def delete_bequest(cls, title: str) -> Dict[str, Any]:
        """删除话术经验（按 title 精确匹配）。

        Returns:
            {"title": str, "success": bool}
        """
        from brain.hc.database.bequest import BequestOperations

        bequest_ops = BequestOperations()
        success = await bequest_ops.delete_bequest_by_title(title)
        logger.debug(f"delete_bequest 完成: title={title}, success={success}")
        return {"title": title, "success": success}

    # ============ 分发表 ============

    @classmethod
    def get_tool_functions(cls) -> Dict[str, Callable]:
        """返回工具名 -> 实现的分发表（供 agent 的 _initialize_tool_functions 直接使用）。"""
        return {
            "upsert_bequest": cls.upsert_bequest,
            "delete_bequest": cls.delete_bequest,
        }
