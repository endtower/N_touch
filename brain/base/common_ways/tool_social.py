# -*- coding: utf-8 -*-
"""通用工具：ToolSocial（社交环境查询工具）

通讯录与会话历史查询的唯一实现，收口于本模块（pfca / proactive /
reorganize / opinion / reflection 的工具表均委托这里，pfca_tools.py 本地版本已删除）：
- query_address_book：查看通讯录（可联系的会话 / 群聊），或直接查询指定角色的印象
- query_history：查询指定会话的历史聊天记录。consumer_id 默认 None＝纯只读、
  不推进任何游标（reflection 等只读核实场景复用）；需要标记"已读"的场景
  （pfca / proactive / reorganize / opinion 显式传 "pfca"，social_chat /
  conversation 传 CONVERSATION_CONSUMER_ID）才推进游标到本次拉取的最大 seq

schema 与实现统一收口在本模块，pfca / proactive / reflection 的工具注册表与
reorganize_agent / opinion_agent 的工具表均引用此处，避免各处复制漂移。
"""

import re
from typing import Dict, Any, List, Optional

from utils.logger import get_logger

logger = get_logger('ToolSocial', "INFO")


class ToolSocial:
    """社交环境查询通用工具：通讯录 + 会话历史。"""

    # ============ 工具 schema（提示词） ============

    @classmethod
    def get_query_address_book_tool_schema(cls) -> Dict[str, Any]:
        """返回 query_address_book 工具的 schema（对齐 PFCA 版）。"""
        return {
            "type": "function",
            "function": {
                "name": "query_address_book",
                "description": (
                    "查看通讯录（可联系的会话 / 群聊），或直接查询指定角色的印象。"
                    "查找范围可选「私聊」「群聊」「全部」；也可以输入具体角色名称，"
                    "此时将直接查询该角色在记忆中的印象。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "search_range": {
                            "type": "string",
                            "description": "查找范围：「私聊」「群聊」「全部」之一，或具体角色名称（如「主人」）。输入具体名称时，将直接查询该角色的印象。"
                        }
                    },
                    "required": ["search_range"]
                }
            }
        }

    @classmethod
    def get_query_history_tool_schema(cls) -> Dict[str, Any]:
        """返回 query_history 工具的 schema（对齐 PFCA 版）。"""
        return {
            "type": "function",
            "function": {
                "name": "query_history",
                "description": "查询某个会话的历史聊天记录。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "目标会话 id，格式 'type_id'。"
                        },
                        "search_range": {
                            "type": "string",
                            "description": "格式 'last_n'，例如 'last_10' 表示最后 10 条。"
                        }
                    },
                    "required": ["session_id", "search_range"]
                }
            }
        }

    @classmethod
    def get_query_tool_schemas(cls) -> List[Dict[str, Any]]:
        """返回两个社交查询工具的 schema 列表（供工具表直接拼装）。"""
        return [
            cls.get_query_address_book_tool_schema(),
            cls.get_query_history_tool_schema(),
        ]

    # ============ 通用执行方法 ============

    @classmethod
    async def query_address_book(cls, search_range: str) -> str:
        """查询通讯录 / 指定角色的印象（对齐 PFCA_tool_manager._query_address_book）。

        Args:
            search_range: 查找范围。可选'私聊'、'群聊'、'全部'；
                          或直接输入具体角色名称，此时将直接查询该角色的印象。

        Returns:
            str: 通讯录信息；输入具体名称时返回该角色的印象信息。
        """
        logger.debug(f"执行query_address_book操作，查找范围: {search_range}")

        from memory.address_book import get_address_book
        address_book = get_address_book()

        if search_range == '私聊':
            return await address_book.get_private_contacts()
        if search_range == '群聊':
            return await address_book.get_group_contacts()
        if search_range == '全部':
            return await address_book.get_all_contacts()

        # 其他输入视为具体角色名称 → 直接查询该角色的印象
        from brain.hc.database.character_compose import CharacterCompose
        character_compose = CharacterCompose()
        impression_result = await character_compose.first_session_impression_search(name=search_range)
        formatted_info = impression_result.get(
            "formatted_info", f"你对{search_range}没有任何印象。"
        )
        return f"你查询了「{search_range}」的印象：\n{formatted_info}"

    @classmethod
    async def query_history(
        cls,
        session_id: str,
        search_range: str,
        consumer_id: Optional[str] = None,
    ) -> str:
        """查询指定会话的最新历史聊天记录（只读，默认不推进任何游标）。

        Args:
            session_id: 会话标识，格式为 "type_id"，如 "private_1234567890" / "group_198431299"
            search_range: 历史记录查询范围，格式为'last_n'，例如'last_10'
            consumer_id: 消费者游标标识。None（默认）＝纯只读，不推进任何游标
                （reflection 只做事实核实，读历史不得影响未读检测）；
                需要标记"已读"的场景须显式传入：pfca / proactive /
                reorganize / opinion 传 "pfca"（LLM 已读到这些消息，truck
                未读检测不再重复触发本会话），conversation / social_chat
                传 CONVERSATION_CONSUMER_ID 共用 conversation 游标

        Returns:
            str: 查询到的历史消息内容
        """
        logger.debug(f"执行历史记录查询操作，时间范围: {search_range}")

        message_count = 10
        if search_range:
            last_n_match = re.match(r'last_(\d+)', search_range)
            if last_n_match:
                message_count = int(last_n_match.group(1))
            else:
                logger.error(f"消息数量格式错误: {search_range}")
                return "消息数量格式错误，请使用正确的格式: 'last_n'，其中n为要返回的消息条数，例如'last_10'表示返回最后10条消息。"

        from memory.message_repository import get_message_store
        from memory.session_history import (
            get_global_session_history,
            FetchOptions,
            MessageFormatOptions,
        )

        # 用单例（测试隔离库/真实库），勿新建默认路径实例
        store = get_message_store()
        session_data = await store.get_latest_session_messages(session_id, message_count)
        messages = session_data.get("message_list", [])

        # consumer_id 非空时才推进消费者游标到本次拉取的最大 seq ——
        # LLM 已读到这些消息，基于该游标的未读检测不再重复触发本会话。
        # consumer_id 为 None 时纯只读（reflection 等场景），不动任何游标。
        if consumer_id is not None and messages:
            target_seq = max(m.get("_seq") or -1 for m in messages)
            current = await store.get_cursor(session_id, consumer_id)
            if target_seq > current:
                await store.advance_cursor(session_id, consumer_id, target_seq)
                logger.info(
                    f"query_history 已推进 {consumer_id} 游标 {current} -> {target_seq} ({session_id})"
                )

        history = get_global_session_history()
        formatted_lines, _ = history.format_messages(
            messages,
            FetchOptions(
                message_format=MessageFormatOptions(
                    show_new_marker=False,
                    show_timestamp=True
                )
            )
        )
        messages_str = "\n".join(formatted_lines) if formatted_lines else ""
        chat_result = messages_str if messages_str else "未找到匹配的消息"
        logger.debug(f"历史记录查询模块返回结果: {chat_result}")
        return chat_result
