#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""全局会话状态 - 会话数据子类（SessionDataMixin）

管理多个会话，以 session_id 区分。每个会话包含：
- session_id / session_name：会话标识与名称
- start_time / end_time：注册时刻时间戳快照（供仍读取时间戳的旧消费者兼容）
- cursors：预留字段（暂未启用）

游标的"当前值"由 memory 模块（message_repository.cursors 表）作为唯一数据源
持久化维护，本模块不再重复维护一套游标快照。消息拉取统一走 memory 层游标接口
（fetch_unread / get_unread_for / advance_cursor）。

核心统合方法 add_pfca_session 承担"第一次接触一个会话"的义务：
- 注册/更新会话
- 可选收集第一印象（search_impression）写入临时记忆
"""

import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional

from utils.logger import setup_logging
from brain.hc.base_global_session_state import _extract_session_type

logger = setup_logging(__name__, log_level="INFO")


class SessionDataMixin:
    """会话数据子类 - 多会话登记"""

    # ============================================================
    # 会话数据访问
    # ============================================================

    def get_sessions(self) -> List[Dict[str, Any]]:
        """获取所有会话数据列表"""
        return list(self._data.get("sessions", {}).values())

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """获取指定会话数据"""
        return self._data.get("sessions", {}).get(session_id)

    # ============================================================
    # 兼容方法（时间戳语义，仅供仍读取时间戳的旧消费者使用）
    # ============================================================

    def get_session_conversation_end_time(self, session_id: str) -> Optional[float]:
        """获取会话的 end_time（注册时刻快照，兼容旧接口）。

        注：新游标系统以 memory 层 seq 为准；本方法仅保留给仍读取时间戳的旧消费者。
        """
        session = self.get_session(session_id)
        return session.get("end_time") if session else None

    def update_session_conversation_end_time(self, session_id: str, end_time: float) -> None:
        """更新会话的 end_time（兼容旧接口）。

        注：新游标系统以 memory 层 seq 为准；本方法仅保留给仍读取时间戳的旧消费者。
        """
        self._require_event_chain_active()
        session = self.get_session(session_id)
        if session is None:
            from utils.error_handling import SystemError
            raise SystemError(
                f"未找到会话 {session_id} 对应的会话数据，"
                "无法更新 end_time。请先调用 add_pfca_session 注册会话。"
            )
        session["end_time"] = end_time
        self._persist_real_time()
        logger.debug(f"已更新会话 {session_id} 的 end_time: {end_time}")

    # ============================================================
    # 核心统合方法：add_pfca_session
    # ============================================================

    async def add_pfca_session(
        self,
        session_id: str,
        session_name: str = "",
        search_impression: bool = False
    ) -> None:
        """添加或更新 PFCA 会话（大循环中的会话列表）。

        承担"第一次接触一个会话"的义务：
        - 注册会话（不存在时创建）
        - 更新会话名称
        - 可选收集第一印象（search_impression=True）写入临时记忆

        Args:
            session_id: 会话标识，格式为 "type_id"，如 "private_1234567890" / "group_198431299"
            session_name: 会话名称（可选，为空时从消息存储读取）
            search_impression: 是否执行印象收集（默认为False，设为True时收集会话相关的第一印象）
        """
        self._require_event_chain_active()

        if not session_id:
            logger.warning("会话信息缺少 session_id")
            return

        # 从统一 session_id 中推导 session_type（用于调用下游记忆数据库接口）
        session_type = _extract_session_type(session_id)

        if not session_name:
            from memory.message_repository import get_message_store
            message_store = get_message_store()
            session_status = await message_store.get_session_status(session_id)
            session_info = session_status.get("session_info", {})
            session_name = session_info.get("session_name", session_id) if session_info else session_id

        sessions = self._data.setdefault("sessions", {})
        session = sessions.get(session_id)

        if session is None:
            current_time = datetime.now().timestamp()
            session = {
                "session_id": session_id,
                "session_name": session_name,
                "start_time": current_time,
                "end_time": current_time,
                "cursors": {},   # 预留：consumer_id -> {"start": seq, "end": seq}
            }
            sessions[session_id] = session
            self._persist_real_time()
            logger.debug(f"会话已注册: {session_id} ({session_name})")
        else:
            if session_name and session.get("session_name") != session_name:
                session["session_name"] = session_name
                self._persist_real_time()

        if not search_impression:
            return

        from brain.hc.database.character_compose import CharacterCompose
        from brain.hc.temporary_memory import get_temporary_memory

        character_compose = CharacterCompose()
        session_impression_parts = []

        result = await character_compose.first_session_impression_search(
            name=session_name, session_id=session_id
        )

        group_impression = ""
        if result and result.get('formatted_info'):
            group_impression = result['formatted_info']

        if group_impression:
            session_impression_parts.append(group_impression)

        if session_impression_parts:
            temp_memory = get_temporary_memory()
            combined_impression = " | ".join(session_impression_parts)
            # 使用 first_session_impression_search 产出的 uuid 作为识别符，
            # 避免与原有的 session_id 字符串识别符冲突，并让临时记忆与记忆数据库实体一一对应。
            entity_uuid = (result or {}).get("uuid") if result else None
            identifier = entity_uuid if entity_uuid else f"session_impression_{session_id}"
            temp_memory.add(combined_impression, identifier=identifier)
            logger.debug(
                f"循环会话印象已收集: identifier={identifier}, impression={combined_impression}"
            )
