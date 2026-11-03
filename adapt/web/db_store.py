# -*- coding: utf-8 -*-
"""
adapt.web.db_store
==================

DBStore：Web 层数据源，直接复用主系统 MessageRepository（SQLite chat_history.db）。

替代 SessionStore（JSON 文件），消除 Web 层与主系统存储割裂：
- 写侧（用户入站 / LLM 回复）本来就落库到 chat_history.db
- 读/删/管理侧（列会话 / 拉历史 / 删消息 / 建删会话）改走同一 DB
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from utils import setup_logging

from memory.message_repository import get_message_store

from adapt.session_identity import PLATFORM_PREFIXES
from adapt.session_identity import join_session_id as _session_key
from adapt.session_identity import session_platform as _session_platform
from adapt.session_identity import split_bare as _split_session_id

from .models import WebSession

logger = setup_logging("adapt.web.db_store")


class DBStore:
    """Web 层数据源：所有操作走 MessageRepository（SQLite）。"""

    def __init__(self) -> None:
        self._repo = get_message_store()

    # ------------------------------------------------------------------
    # 列举
    # ------------------------------------------------------------------

    async def _display_names(self) -> Dict[str, str]:
        """按完整会话联系方式取主名，避免用消息中的临时身份名命名。"""
        from brain.hc.database.core import get_memory_db

        names: Dict[str, str] = {}
        try:
            conn = await get_memory_db()._ensure_connection()
            cur = await conn.execute(
                """SELECT c.session_id, n.name
                   FROM entity_contacts c
                   JOIN entity_names n
                     ON n.entity_type = c.entity_type
                    AND n.entity_uuid = c.entity_uuid
                    AND n.is_primary = 1
                   ORDER BY c.rowid"""
            )
            for row in await cur.fetchall():
                sid, name = row["session_id"], row["name"]
                if isinstance(sid, str) and name:
                    names.setdefault(sid, name)
        except Exception:
            # 记忆库不可用时仍允许打开聊天记录；回退到持久化名。
            logger.warning("无法读取会话别名，暂用聊天记录中的名称", exc_info=True)
        return names

    async def list_sessions(self) -> List[WebSession]:
        rows = await self._repo.list_sessions()
        names = await self._display_names() if rows else {}
        out: List[WebSession] = []
        for r in rows:
            scope, bare_id = _split_session_id(r["session_id"])
            name = names.get(r["session_id"]) or r.get("session_name") or bare_id
            out.append(WebSession(
                session_id=bare_id,
                scope=scope,
                user_id=await self._contact_id(r["session_id"], bare_id),
                user_name=name,
                session_name=name,
                role=None,
                platform=_session_platform(r["session_id"]) or "qq",
                created_at=float(r.get("last_update_time") or 0),
            ))
        return out

    async def _contact_id(self, session_id: str, bare_id: str) -> str:
        """私聊对端的真实身份 ID（消息里的 user_id），用于前端区分收发双方。

        带平台前缀的会话裸 ID 只是本地数值地址，对端真实身份在路由里
        （如 'private_wc26533d615450' -> 'wc26533d615450'）。
        """
        platform, sep, _ = bare_id.partition("_")
        if not sep or platform not in PLATFORM_PREFIXES:
            return bare_id
        route = await self._repo.session_route(session_id)
        remote = (route or {}).get("remote_session_id") or ""
        scope, sep2, identity = remote.partition("_")
        return identity if sep2 and identity else bare_id

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def load_messages(self, scope: str, bare_id: str) -> List[Dict[str, Any]]:
        """按 (scope, bare_id) 加载全部消息（保持与 SessionStore 同签名）。"""
        data = await self._repo._get_session_data(_session_key(scope, bare_id))
        return data["message_list"]

    async def session_route(self, scope: str, bare_id: str) -> Optional[Dict[str, Any]]:
        """该会话已持久化的平台来源；Web 只是入口，发消息时沿用它，不另立来源。"""
        return await self._repo.session_route(_session_key(scope, bare_id))

    async def get_messages_page(self, scope: str, bare_id: str,
                                before_id: Optional[str], limit: int) -> Dict[str, Any]:
        """分页拉取：before_id 为空取末尾 limit 条；否则取 before_id 之前 limit 条。"""
        session_id = _session_key(scope, bare_id)
        if not before_id:
            return await self._repo.get_latest_messages(session_id, limit)
        return await self._repo.get_messages_before(session_id, before_id, limit)

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------

    async def delete_message(self, scope: str, bare_id: str, message_id: str) -> bool:
        return await self._repo.delete_message(_session_key(scope, bare_id), message_id)

    async def delete_messages(self, scope: str, bare_id: str, message_ids: List[str]) -> List[str]:
        """批量删除, 返回实际删除的 message_id 列表。"""
        return await self._repo.delete_messages(_session_key(scope, bare_id), message_ids)

    async def delete_session(self, scope: str, bare_id: str) -> bool:
        session_id = _session_key(scope, bare_id)
        # 存在性判断：sessions 表是否有该行（不依赖 session_name，
        # 避免被 store_message 的 ON CONFLICT 覆盖为空后误判不存在）
        exists = any(
            s.session_id == bare_id and s.scope == scope
            for s in await self.list_sessions()
        )
        if not exists:
            return False
        await self._repo.delete_session(session_id)
        return True

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    async def create_empty(self, scope: str, bare_id: str, user_name: str = "",
                           session_name: str = "", role: Optional[str] = None) -> WebSession:
        session_id = _session_key(scope, bare_id)
        name = session_name or user_name or session_id
        await self._repo.create_session(session_id, name)
        name = (await self._display_names()).get(session_id) or name
        return WebSession(
            session_id=bare_id, scope=scope, user_id=bare_id,
            user_name=name, session_name=name, role=role,
            platform=_session_platform(session_id) or "qq",
            created_at=time.time(),
        )
