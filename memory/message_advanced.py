#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
高级消息管理模块 (Advanced Message Manager)

此模块是系统的高级服务层，专注于提供复杂的消息检索和分析功能。
会话标识统一使用 session_id，格式为 "type_id"，例如 "private_1234567890" / "group_198431299"。

重构说明：
- 不再直接调用 MessageRepository 的私有方法（_get_session_file_path / _read_session_file）。
- 不再自己用 aiofiles 写文件。
- 所有数据访问统一通过 MessageRepository 的公开方法。
- 已读/未读状态统一由消费者游标（seq > 游标位置）管理，read 字段已废弃并移除。

主要类与方法：

MessageAdvancedManager类：
- get_unread_sessions_by_type: 获取指定类型的所有未读会话
- get_session_messages_by_time_range: 获取目标会话在指定时间范围内的消息

模块级别函数：
- get_message_advanced_manager: 获取MessageAdvancedManager的单例实例
"""


import logging
from typing import Dict, Any, List, Optional

# 导入工具模块
from utils.logger import setup_logging
from utils.error_handling import async_handle_errors, ValidationError, StorageError
# 导入基础检索模块
from memory.message_repository import get_message_store

# 设置日志
logger = setup_logging(__name__, 'WARNING')


def _extract_session_type(session_id: str) -> str:
    """从统一 session_id 中提取 session_type（type_id 格式的第一段）"""
    if not session_id or "_" not in session_id:
        return ""
    return session_id.split("_", 1)[0]


class MessageAdvancedManager:
    """消息高级管理类，负责提供消息处理的高级功能

    这是系统的高级服务层，专注于提供复杂的消息检索和分析功能，
    不直接与文件系统交互，而是通过MessageRepository来获取基础数据。
    """
    def __init__(self, storage_path: str = None):
        """
        初始化MessageAdvancedManager实例

        Args:
            storage_path: 消息存储路径，如果为None则使用默认路径
        """
        # 防止重复初始化
        if not hasattr(self, '_initialized'):
            # 获取消息仓库实例
            self._message_repository = get_message_store(storage_path)
            self.storage_path = self._message_repository.storage_path
            logger.info(f"高级消息管理器已连接到存储路径: {self.storage_path}")
            self._initialized = True

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def get_unread_sessions_by_type(self, session_type: str, consumer_id: str = "pfca") -> List[Dict[str, Any]]:
        """获取指定类型的所有未读会话（基于消费者游标：seq > 游标位置）。

        Args:
            session_type: 会话类型，如 "private" / "group"。
            consumer_id: 消费者标识（默认 "pfca"——所有 truck 共用 pfca 游标检测，
                自身不移动游标；游标由 PFCA/conversation 处理时推进）。

        Returns:
            未读会话信息列表。
        """
        if not session_type:
            raise ValidationError("无效的会话类型")

        # 通过消息仓库获取所有会话，再按 session_id 前缀过滤
        sessions = await self._message_repository.list_sessions()

        unread_sessions = []
        for session_info in sessions:
            try:
                session_id = session_info.get("session_id", "")
                if _extract_session_type(session_id) != session_type:
                    continue

                # 基于消费者游标统计未读【用户消息】（seq > 游标位置 且 user_id != 'system'），
                # 排除助手自身回复，避免 conversation 回复落库后误判为新的未读
                unread_count = await self._message_repository.count_unread_user_for(session_id, consumer_id)
                if unread_count <= 0:
                    continue

                status = await self._message_repository.get_session_status(session_id)
                unread_sessions.append({
                    "session_id": session_id,
                    "session_type": session_type,
                    "session_name": status["session_info"].get("session_name", ""),
                    "remark": status["session_info"].get("remark", ""),
                    "message_count": status.get("total_count", 0),
                    "unread_count": unread_count,
                    "last_update_time": status["session_info"].get("last_update_time", 0),
                })
            except Exception as e:
                logger.error(f"处理会话时出错: {e}")
                continue

        # 按最后更新时间排序，最新的在前
        unread_sessions.sort(key=lambda x: x["last_update_time"], reverse=True)

        logger.debug(f"已获取{session_type}类型的未读会话共{len(unread_sessions)}个")
        return unread_sessions

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def get_session_messages_by_time_range(self, session_id: str,
                                               start_time: float, end_time: Optional[float] = None) -> Dict[str, Any]:
        """获取目标会话在指定时间范围内的消息，旧消息在前。

        Args:
            session_id: 会话标识，格式为 "type_id"。
            start_time: 开始时间戳。
            end_time: 结束时间戳，不填则默认到最新。

        Returns:
            {"status", "session_id", "message_list", "total_count"}
        """
        if not session_id:
            raise ValidationError("无效的会话标识符: session_id为空")

        if start_time < 0:
            raise ValidationError("无效的开始时间: start_time必须为非负数值")

        # 通过 repository 读取会话全部消息
        session_data = await self._message_repository._get_session_data(session_id)
        message_list = session_data.get("message_list", [])

        # 过滤时间范围内的消息
        filtered_messages = []
        for message in message_list:
            msg_timestamp = message.get("timestamp", 0)
            if msg_timestamp >= start_time:
                if end_time is None or msg_timestamp <= end_time:
                    filtered_messages.append(message)

        # 按时间戳升序排列，旧消息在前
        filtered_messages.sort(key=lambda x: x.get("timestamp", 0))

        logger.debug(f"已获取会话 {session_id} 在时间范围 {start_time} 到 {end_time or '最新'} 的 {len(filtered_messages)} 条消息")

        return {
            "status": "success",
            "session_id": session_id,
            "message_list": filtered_messages,
            "total_count": len(filtered_messages),
        }


_message_advanced_instance: Optional[MessageAdvancedManager] = None


def get_message_advanced_manager() -> MessageAdvancedManager:
    """获取MessageAdvancedManager的单例实例"""
    global _message_advanced_instance
    if _message_advanced_instance is None:
        _message_advanced_instance = MessageAdvancedManager()
    return _message_advanced_instance
