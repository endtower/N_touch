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

import json
import time
from typing import Any, Dict, List, Optional

from utils import setup_logging

from memory.message_repository import get_message_store

from .models import WebSession

logger = setup_logging("adapt.web.db_store")


def _split_session_id(session_id: str) -> tuple[str, str]:
    """'private_xxx' / 'group_xxx' -> (scope, bare_id)。"""
    for prefix in ("private_", "group_"):
        if session_id.startswith(prefix):
            return prefix[:-1], session_id[len(prefix):]
    return "private", session_id


class DBStore:
    """Web 层数据源：所有操作走 MessageRepository（SQLite）。"""

    def __init__(self) -> None:
        self._repo = get_message_store()

    # ------------------------------------------------------------------
    # 列举
    # ------------------------------------------------------------------

    async def _display_names(self) -> Dict[str, str]:
        """按完整会话联系方式取首个别名，避免用消息中的临时身份名命名。"""
        from brain.hc.database.core import get_memory_db

        names: Dict[str, str] = {}
        try:
            conn = await get_memory_db()._ensure_connection()
            for scope, table in (("private", "characters"), ("group", "groups")):
                cur = await conn.execute(
                    f"SELECT name, aliases, contacts FROM {table} ORDER BY uuid"
                )
                for row in await cur.fetchall():
                    try:
                        aliases = json.loads(row["aliases"] or "[]")
                        contacts = json.loads(row["contacts"] or "[]")
                    except (TypeError, ValueError):
                        logger.warning("会话显示名读取失败：%s 的别名或联系方式无效", table)
                        continue
                    if not isinstance(aliases, list) or not isinstance(contacts, list):
                        continue
                    first_alias = aliases[0] if aliases else None
                    name = (
                        (first_alias.strip() if isinstance(first_alias, str) else "")
                        or row["name"]
                    )
                    if not name:
                        continue
                    for contact in contacts:
                        sid = contact.get("session_id") if isinstance(contact, dict) else None
                        if isinstance(sid, str) and sid.startswith(f"{scope}_"):
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
                user_id=bare_id,
                user_name=name,
                session_name=name,
                role=None,
                created_at=float(r.get("last_update_time") or 0),
            ))
        return out

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def load_messages(self, scope: str, bare_id: str) -> List[Dict[str, Any]]:
        """按 (scope, bare_id) 加载全部消息（保持与 SessionStore 同签名）。"""
        data = await self._repo._get_session_data(f"{scope}_{bare_id}")
        return data["message_list"]

    async def get_messages_page(self, scope: str, bare_id: str,
                                before_id: Optional[str], limit: int) -> Dict[str, Any]:
        """分页拉取：before_id 为空取末尾 limit 条；否则取 before_id 之前 limit 条。"""
        session_id = f"{scope}_{bare_id}"
        if not before_id:
            return await self._repo.get_latest_messages(session_id, limit)
        return await self._repo.get_messages_before(session_id, before_id, limit)

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------

    async def delete_message(self, scope: str, bare_id: str, message_id: str) -> bool:
        return await self._repo.delete_message(f"{scope}_{bare_id}", message_id)

    async def delete_messages(self, scope: str, bare_id: str, message_ids: List[str]) -> List[str]:
        """批量删除, 返回实际删除的 message_id 列表。"""
        return await self._repo.delete_messages(f"{scope}_{bare_id}", message_ids)

    async def delete_session(self, scope: str, bare_id: str) -> bool:
        session_id = f"{scope}_{bare_id}"
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
        session_id = f"{scope}_{bare_id}"
        name = session_name or user_name or f"{scope}_{bare_id}"
        await self._repo.create_session(session_id, name)
        name = (await self._display_names()).get(session_id) or name
        return WebSession(
            session_id=bare_id, scope=scope, user_id=bare_id,
            user_name=name, session_name=name, role=role,
            created_at=time.time(),
        )
