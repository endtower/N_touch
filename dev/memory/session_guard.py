# -*- coding: utf-8 -*-
"""会话 id 存在性校验（发送/对话入口共用）。

背景（2026-11-11）：LLM 曾把微信会话写成旧格式的 ``private_10003``，
而库里存的是带平台前缀的 ``wechat_private_10003``。旧代码在入口不校验，
于是一路走到路由层才被拦下（"该会话没有可验证的平台来源，拒绝发送"），
并且还在记忆库中为这个不存在的会话建了一个幽灵角色实体。

这里把校验前移到入口，并给出"最可能的正确 id"，让 LLM 能一次自纠。
抛 ValueError 是刻意选择：工具层会把它回给模型当作可自纠的参数错误。
"""

from typing import List


def _identity_of(session_id: str) -> str:
    """取会话 id 的末段地址（用于找回正确 id）。

    'wechat_private_10003' -> '10003'
    'private_10003'        -> '10003'
    'group_1095799561'     -> '1095799561'
    """
    return session_id.rsplit("_", 1)[-1] if session_id else ""


async def _session_known(store, session_id: str) -> bool:
    """会话是否真实可寻址。

    两个来源都算：sessions 表（有过消息）与 session_routes 表（有平台路由）。
    只看 sessions 会误杀"建过路由但还没有消息"的会话——这类会话发送目标是合法的。
    """
    if await store.session_exists(session_id):
        return True
    return await store.session_route(session_id) is not None


async def suggest_session_ids(session_id: str, limit: int = 5) -> List[str]:
    """为不存在的会话 id 找候选（同末段地址优先，其次同 scope）。"""
    from memory.message_repository import get_message_store

    store = get_message_store()
    target = _identity_of(session_id)
    candidates: List[str] = []
    try:
        sessions = await store.list_sessions()
        candidates.extend(s["session_id"] for s in sessions)
        # 仅存在于路由表的会话也要能提示出来
        conn = await store._ensure_connection()
        cur = await conn.execute("SELECT session_id FROM session_routes")
        candidates.extend(r["session_id"] for r in await cur.fetchall())
    except Exception:
        return []

    existing: List[str] = []
    for sid in candidates:
        if sid and sid not in existing:
            existing.append(sid)
    same_identity = [s for s in existing if _identity_of(s) == target and target]
    same_scope = [s for s in existing if s.split("_", 1)[0] == session_id.split("_", 1)[0]]
    ordered: List[str] = []
    for group in (same_identity, same_scope):
        for sid in group:
            if sid not in ordered:
                ordered.append(sid)
    return ordered[:limit]


async def require_existing_session(session_id: str) -> None:
    """确认会话在消息库里真实存在，否则抛可自纠的 ValueError。

    只做"存在性"判断，不做任何模糊匹配路由——绝不把消息发给另一个会话。
    """
    from memory.message_repository import get_message_store

    session_id = (session_id or "").strip()
    if not session_id:
        raise ValueError("会话 id 为空")

    store = get_message_store()
    if await _session_known(store, session_id):
        return

    candidates = await suggest_session_ids(session_id)
    if candidates:
        hint = "、".join(f"'{c}'" for c in candidates)
        raise ValueError(
            f"会话 '{session_id}' 不存在。最可能的正确 id 是：{hint}。"
            "带平台前缀的会话必须写全前缀（如 wechat_private_10003 / qq_private_123456），"
            "请用上面的 id 重新调用。"
        )
    raise ValueError(
        f"会话 '{session_id}' 不存在，当前消息库里也没有相近的会话。"
        "请先用 query_address_book 查清确切 session_id 再调用。"
    )
