#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""全局会话历史管理模块 - 游标拉取 + 格式化组件

会话标识统一使用 session_id，格式为 "type_id"，例如 "private_1234567890" / "group_198431299"。

本模块仅保留游标拉取（fetch_unread），已移除旧的时间戳拉取（fetch / fetch_all）。
"""

from datetime import datetime
from typing import Dict, Any, List, Optional

from utils.logger import setup_logging
from memory.message_repository import get_message_store
from adapt.session_identity import session_scope

logger = setup_logging(__name__, log_level="INFO")


def _extract_session_type(session_id: str) -> str:
    """从统一 session_id 中提取 session_type（type_id 格式的第一段）

    例如 "private_1234567890" -> "private"
        "group_198431299" -> "group"
    """
    if not session_id or "_" not in session_id:
        return ""
    return session_scope(session_id)


class MessageFormatOptions:
    """消息格式化选项"""

    def __init__(
        self,
        show_new_marker: bool = True,
        show_timestamp: bool = False,
        exclude_system: bool = False,
    ):
        self.show_new_marker = show_new_marker
        self.show_timestamp = show_timestamp
        self.exclude_system = exclude_system


class FetchOptions:
    """消息拉取选项 - 统一配置"""

    def __init__(
        self,
        message_format: MessageFormatOptions | None = None,
        header_model: str | None = None
    ):
        self.message_format = message_format or MessageFormatOptions()
        self.header_model = header_model


class FetchResult:
    """拉取结果"""

    def __init__(
        self,
        messages: List[Dict],
        formatted_lines: List[str],
        user_infos: List[Dict],
        has_new: bool = None
    ):
        self.messages = messages
        self.formatted_lines = formatted_lines
        self.user_infos = user_infos
        self.has_new = has_new

    def to_dict(self) -> Dict:
        return {
            "messages": self.messages,
            "formatted_lines": self.formatted_lines,
            "user_infos": self.user_infos,
            "has_new": self.has_new
        }


class GlobalSessionHistory:
    """全局会话历史管理类 - 游标拉取 + 格式化组件"""

    def __init__(self):
        if not hasattr(self, '_initialized'):
            self._message_store = get_message_store()
            self._initialized = True

    def format_messages(
        self,
        messages: List[Dict],
        options: FetchOptions
    ) -> tuple:
        """通用消息格式化：把 raw message dict 列表格式化为展示行，并提取人员信息。

        供所有消费者复用（fetch_unread / fetch_session_range 等）。
        """
        msg_format = options.message_format

        filtered_messages = []
        for msg in messages:
            user_id = msg.get("user_id")
            if msg_format.exclude_system and user_id == "system":
                continue
            filtered_messages.append(msg)

        formatted_messages = []
        user_infos = []
        processed_users = set()

        for msg in filtered_messages:
            user_name = msg.get("user_name", "未知用户")
            content = msg.get("content_text", "")
            msg_timestamp = msg.get("timestamp", 0)
            user_id = msg.get("user_id")

            prefix_parts = []
            if msg_format.show_new_marker and msg.get("_unread", False):
                prefix_parts.append("[新]")
            if msg_format.show_timestamp:
                dt = datetime.fromtimestamp(msg_timestamp)
                time_str = f"[{dt.month}月{dt.day}号 {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}]"
                prefix_parts.append(time_str)

            prefix = " ".join(prefix_parts)
            if prefix:
                prefix = f"{prefix} "

            formatted_line = f"{prefix}{user_name}:{content}"
            formatted_messages.append(formatted_line)

            if user_id and user_name and user_id != "system":
                user_info = {"id": user_id, "name": user_name}
                if (user_id, user_name) not in processed_users:
                    user_infos.append(user_info)
                    processed_users.add((user_id, user_name))

        return formatted_messages, user_infos

    def _build_header(
        self,
        session_id: str,
        session_name: str,
        header_model: str
    ) -> str:
        """构建消息头（仅当 header_model 不为 None 时调用）

        session_type 从 session_id 前缀推导（"private" / "group"）。
        """
        session_type = _extract_session_type(session_id)
        if header_model == "pfca":
            if session_type == "group":
                return f"你收到了来自群聊{session_id} {session_name} 的信息："
            else:
                return f"你收到了来自私聊{session_id} {session_name} 发送给你的信息："
        elif header_model == "chat_message":
            return f"在会话[{session_type}][{session_id}][{session_name}] 中的聊天信息如下："
        else:
            if session_type == "group":
                return f"在此次聊天前，过去发生的聊天记录如下：\n[在群聊{session_name}中]"
            else:
                return f"在此次聊天前，过去发生的聊天记录如下：\n[在与{session_name}的交谈中]"

    async def fetch_unread(
        self,
        session_id: str,
        session_name: str,
        consumer_id: str,
        options: FetchOptions,
        advance_cursor: bool = True,
        max_unread: int = 20,
        min_messages: int = 10,
    ) -> FetchResult:
        """游标拉取：获取指定消费者未处理的消息，格式化并推进游标。

        这是游标机制的统一入口：每个消费者（如 "conversation"）只拿自己
        未读（seq > 游标）的新消息，互不顶掉、不重复。

        钳制原则（在本层统一管理）：
        - max_unread：未读最多返回 max_unread 条（取最新的），超出部分不返回，
          但在消息头附加提示"共 xxxx 条未读消息，仅显示最新 N 条"。默认 20。
        - min_messages：未读不足时，从游标之前（已读历史）额外拉取补足到
          min_messages 条。默认 10；设为 0 表示不补齐（只返回未读）。

        Args:
            session_id: 会话标识。
            session_name: 会话名称（用于构建消息头）。
            consumer_id: 消费者标识（如 "conversation" / "pfca"）。
            options: FetchOptions（message_format / header_model 生效）。
            advance_cursor: 是否推进游标。True 表示本次消费后推进游标（默认）；
                False 表示只读不推进（用于监听/等待场景，消息仍保持未读，供后续消费）。
            max_unread: 未读返回上限（见上述钳制原则），默认 20。
            min_messages: 最少返回条数（未读不足时从历史补足），默认 10；0 表示不补齐。

        Returns:
            FetchResult（messages / formatted_lines / user_infos / has_new）。
        """
        from memory.message_repository import get_message_store, MAX_UNREAD_FETCH_LIMIT

        store = get_message_store()

        # 实际生效的未读上限（数据层保底钳制）
        effective_max = min(max_unread, MAX_UNREAD_FETCH_LIMIT) if max_unread > 0 else 0

        # 未读总数：用于溢出提示 + 判断是否需补齐
        unread_count = await store.count_unread_for(session_id, consumer_id)

        # 1) 未读部分：只取最新的 effective_max 条（seq 最大者优先）
        overflowed = unread_count > effective_max
        unread_messages = []
        if effective_max > 0:
            unread_messages = await store.get_unread_for(
                session_id, consumer_id, limit=effective_max, newest=True
            )

        # 2) 补齐部分：未读不足 min_messages 时，从游标之前（已读历史）补足
        filler_messages = []
        if min_messages > 0 and len(unread_messages) < min_messages:
            need = min_messages - len(unread_messages)
            filler_messages = await store.get_messages_before_cursor(
                session_id, consumer_id, limit=need
            )

        # 合并：历史(旧)在前 + 未读(新)在后，保持时间升序观感
        messages = filler_messages + unread_messages

        # 3) 推进游标到最新未读的 max_seq（补齐的历史不推进游标）
        if unread_messages and advance_cursor:
            max_seq = max(m["_seq"] for m in unread_messages)
            await store.advance_cursor(session_id, consumer_id, max_seq)

        if not messages:
            return FetchResult(
                messages=[],
                formatted_lines=[],
                user_infos=[],
                has_new=False,
            )

        # 4) 消息头：会话头 + 溢出提示
        header = ""
        if options.header_model is not None:
            header = self._build_header(session_id, session_name, options.header_model)
        if overflowed:
            overflow_hint = f"共{unread_count}条未读消息，仅显示最新{effective_max}条"
            header = f"{header}\n{overflow_hint}" if header else overflow_hint

        formatted_lines, user_infos = self.format_messages(messages, options)
        if header and formatted_lines:
            formatted_lines = [header] + formatted_lines

        return FetchResult(
            messages=messages,
            formatted_lines=formatted_lines,
            user_infos=user_infos,
            has_new=bool(unread_messages),
        )


_session_history_instance: Optional[GlobalSessionHistory] = None


def get_global_session_history() -> GlobalSessionHistory:
    """获取GlobalSessionHistory单例实例"""
    global _session_history_instance
    if _session_history_instance is None:
        _session_history_instance = GlobalSessionHistory()
    return _session_history_instance
