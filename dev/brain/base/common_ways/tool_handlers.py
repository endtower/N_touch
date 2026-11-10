# -*- coding: utf-8 -*-
"""通用工具基类：ToolHandlers

把 conversation / social_chat 共用的工具能力抽到 brain/base/common_ways，
工具描述（schema）与具体执行逻辑对应存放。

本类只保留"等待观察"类通用原语（wait_and_observe / peek_unread）；
会话发送类通用工具（send_message / send_image 及发送原语）见
tool_conversation.ToolConversation。

通用方法要求外部传入目标 session_id（格式 "type_id"，如 "private_xxx" / "group_xxx"），
由调用方（各 ToolManager）在内部自动注入，LLM 无需感知 session_id。

子类：
- ToolConversation：会话发送类工具（send_message / send_image 及发送原语）
- ToolHC：hc 通用工具（记事本）
"""

import asyncio
import time
from typing import Dict, Any

from utils.logger import get_logger

logger = get_logger('ToolHandlers', "INFO")


class ToolHandlers:
    """通用工具基类：wait_and_observe / peek_unread 通用原语及其 schema。"""

    # ============ 工具 schema（提示词） ============

    @classmethod
    def get_wait_and_observe_tool_schema(cls) -> Dict[str, Any]:
        """返回 wait_and_observe 工具的 schema（通用部分）。"""
        return {
            "type": "function",
            "function": {
                "name": "wait_and_observe",
                "description": "等待一段时间，等待对方消息的回复。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "seconds": {
                            "type": "number",
                            "description": "等待的秒数",
                            "minimum": 10.0,
                            "maximum": 30.0
                        },
                    },
                    "required": ["seconds"]
                }
            }
        }

    # ============ 通用执行方法 ============

    @classmethod
    async def wait_and_observe(
        cls,
        session_id: str,
        seconds: float,
        consumer_id: str,
    ) -> str:
        """通用等待并监听：等待目标会话的新消息，超时或检测到新消息即返回。

        Args:
            session_id: 目标会话标识，格式 "type_id"，如 "private_xxx" / "group_xxx"。
            seconds: 最长等待秒数。
            consumer_id: 消费者标识（如 "conversation" / "social_chat"），用于游标读取。

        Returns:
            结果字符串（检测到新消息 / 等待完成）。
        """
        start_time = time.time()
        elapsed = 0

        while elapsed < seconds:
            wait_time = min(1.0, seconds - elapsed)
            await asyncio.sleep(wait_time)
            elapsed = time.time() - start_time
            # 监听场景：只读探测（advance_cursor=False），消息保持未读供后续消费。
            # max_unread=20 / min_messages=10 为 session_history 默认值，显式传参保持原行为。
            result = await cls.peek_unread(
                session_id,
                consumer_id,
                max_unread=20,
                min_messages=10,
            )
            if result.has_new:
                import datetime
                current_time = datetime.datetime.now().strftime("%H:%M:%S")
                result_str = f"检测到用户输入，中断等待，总共等待 {int(elapsed)} 秒，当前时间：{current_time}"
                logger.info(result_str)
                return result_str

        import datetime
        current_time = datetime.datetime.now().strftime("%H:%M:%S")
        result_str = f"等待观察完成，总共等待 {int(elapsed)} 秒，当前时间：{current_time}"
        logger.info(result_str)

        return result_str

    # ============ 通用只读探测原语 ============

    @staticmethod
    async def peek_unread(
        session_id: str,
        consumer_id: str,
        format_options=None,
        max_unread: int = 50,
        min_messages: int = 0,
    ):
        """只读探测会话未读消息（advance_cursor=False，游标不推进，消息保持未读）。

        通用原语：conversation 的 wait_and_observe / social_chat 的 has_new_messages
        共用"检测新消息但不消费"这一语义。返回 session_history 的 fetch_unread 原始结果，
        由调用方按自身语义过滤与渲染（文案 / 字段提取各不相同，不放入通用层）。

        Args:
            session_id: 会话 ID（格式 "type_id"）。
            consumer_id: 消费者游标标识。
            format_options: MessageFormatOptions，缺省 exclude_system=True + show_timestamp=True。
            max_unread: 未读返回上限。
            min_messages: 未读不足时从历史补足的条数（0 = 不补齐）。

        Returns:
            session_history.fetch_unread 的结果对象（messages / formatted_lines / has_new 等）。
        """
        from memory.session_history import (
            get_global_session_history,
            FetchOptions,
            MessageFormatOptions,
        )

        if not session_id:
            raise ValueError("peek_unread 缺少 session_id")

        opts = format_options or MessageFormatOptions(exclude_system=True, show_timestamp=True)
        history = get_global_session_history()
        return await history.fetch_unread(
            session_id,
            "",
            consumer_id=consumer_id,
            options=FetchOptions(message_format=opts),
            advance_cursor=False,
            max_unread=max_unread,
            min_messages=min_messages,
        )
