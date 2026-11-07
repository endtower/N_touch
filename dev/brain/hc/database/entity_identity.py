# -*- coding: utf-8 -*-
"""
实体身份表操作（SQLite）：名称与联系方式

记忆库把「这个人/这个群」与「怎么找到他/叫什么」彻底分开：
- entity_names   ：(entity_type, name) 主键，同名即同一实体；
- entity_contacts：session_id 主键，一条会话只能属于一个实体。

跨平台合并完全依赖名称相等，不做任何基于 ID 相似度 / 内容 / 头像的推测；
真正的身份归并交给大模型在改记忆时自行操作。

冲突处理遵循项目规则：不静默覆盖、不 try-except 兜底，直接抛错阻断。
"""

from datetime import datetime
from typing import Optional

from .core import _split_contact

CHARACTER = "character"
GROUP = "group"


class NameConflictError(ValueError):
    """名称已被同类型的其他实体占用。"""


class ContactConflictError(ValueError):
    """会话已归属其他实体，或作用域与实体类型不符。"""


# ============================================================
# 读
# ============================================================

async def get_names(conn, entity_type: str, entity_uuid: str) -> list:
    """实体的全部名称，主名在首位。"""
    cur = await conn.execute(
        """SELECT name FROM entity_names
           WHERE entity_type=? AND entity_uuid=?
           ORDER BY is_primary DESC, rowid ASC""",
        (entity_type, entity_uuid),
    )
    return [r["name"] for r in await cur.fetchall()]


async def get_primary_name(conn, entity_type: str, entity_uuid: str) -> str:
    """实体的主名；无名称时返回空串。"""
    names = await get_names(conn, entity_type, entity_uuid)
    return names[0] if names else ""


async def get_contacts(conn, entity_type: str, entity_uuid: str) -> list:
    """实体的全部联系方式，形状与旧 contacts JSON 兼容。"""
    cur = await conn.execute(
        """SELECT session_id FROM entity_contacts
           WHERE entity_type=? AND entity_uuid=?
           ORDER BY rowid ASC""",
        (entity_type, entity_uuid),
    )
    return [{"session_id": r["session_id"], "usage_count": 0} for r in await cur.fetchall()]


async def find_entity_by_name(conn, entity_type: str, name: str) -> Optional[str]:
    """按名称反查实体 uuid（同类型内唯一，不可能多匹配）。"""
    if not name:
        return None
    cur = await conn.execute(
        "SELECT entity_uuid FROM entity_names WHERE entity_type=? AND name=?",
        (entity_type, name),
    )
    row = await cur.fetchone()
    return row["entity_uuid"] if row else None


async def find_entity_by_contact(conn, session_id: str) -> Optional[tuple]:
    """按会话反查 (entity_type, entity_uuid)。"""
    if not session_id:
        return None
    cur = await conn.execute(
        "SELECT entity_type, entity_uuid FROM entity_contacts WHERE session_id=?",
        (session_id,),
    )
    row = await cur.fetchone()
    return (row["entity_type"], row["entity_uuid"]) if row else None


# ============================================================
# 写：名称
# ============================================================

async def _claim_name(conn, entity_type: str, entity_uuid: str, name: str, is_primary: int) -> None:
    """占用一个名称；已被同一实体占用则幂等，被其他实体占用则抛错。"""
    cur = await conn.execute(
        "SELECT entity_uuid FROM entity_names WHERE entity_type=? AND name=?",
        (entity_type, name),
    )
    row = await cur.fetchone()
    if row is not None:
        if row["entity_uuid"] != entity_uuid:
            raise NameConflictError(
                f"名称「{name}」已被{'角色' if entity_type == CHARACTER else '群组'} "
                f"{row['entity_uuid']} 占用，不能同时属于 {entity_uuid}"
            )
        if is_primary:
            await conn.execute(
                """UPDATE entity_names SET is_primary=1
                   WHERE entity_type=? AND name=?""",
                (entity_type, name),
            )
        return
    await conn.execute(
        """INSERT INTO entity_names (entity_type, name, entity_uuid, is_primary, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (entity_type, name, entity_uuid, is_primary, datetime.now().isoformat()),
    )


async def set_primary_name(conn, entity_type: str, entity_uuid: str, name: str) -> None:
    """把 name 设为该实体的主名（其余名称降为别名）。"""
    if not name:
        raise ValueError("主名不能为空")
    await _claim_name(conn, entity_type, entity_uuid, name, 1)
    await conn.execute(
        """UPDATE entity_names SET is_primary=0
           WHERE entity_type=? AND entity_uuid=? AND name<>?""",
        (entity_type, entity_uuid, name),
    )


async def add_name(conn, entity_type: str, entity_uuid: str, name: str) -> None:
    """追加一个别名（不改变主名）。"""
    if not name:
        return
    await _claim_name(conn, entity_type, entity_uuid, name, 0)


async def replace_names(conn, entity_type: str, entity_uuid: str, names: list) -> None:
    """整组替换名称，names[0] 为主名。

    先整体校验冲突再落库：任一名称属于其他实体就整体失败，不留半套数据。
    """
    cleaned = []
    for name in names:
        text = (name or "").strip() if isinstance(name, str) else ""
        if text and text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        raise ValueError("名称列表不能为空")

    for name in cleaned:
        cur = await conn.execute(
            "SELECT entity_uuid FROM entity_names WHERE entity_type=? AND name=?",
            (entity_type, name),
        )
        row = await cur.fetchone()
        if row is not None and row["entity_uuid"] != entity_uuid:
            raise NameConflictError(
                f"名称「{name}」已被{'角色' if entity_type == CHARACTER else '群组'} "
                f"{row['entity_uuid']} 占用，整组替换中止"
            )

    await conn.execute(
        "DELETE FROM entity_names WHERE entity_type=? AND entity_uuid=?",
        (entity_type, entity_uuid),
    )
    now = datetime.now().isoformat()
    for index, name in enumerate(cleaned):
        await conn.execute(
            """INSERT INTO entity_names (entity_type, name, entity_uuid, is_primary, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (entity_type, name, entity_uuid, 1 if index == 0 else 0, now),
        )


async def delete_name(conn, entity_type: str, entity_uuid: str, name: str) -> None:
    """删除一个别名；主名不允许删除（改主名请用 set_primary_name）。"""
    cur = await conn.execute(
        """SELECT is_primary FROM entity_names
           WHERE entity_type=? AND entity_uuid=? AND name=?""",
        (entity_type, entity_uuid, name),
    )
    row = await cur.fetchone()
    if row is None:
        return
    if row["is_primary"]:
        raise ValueError(f"「{name}」是主名，不能直接删除；请先改主名")
    await conn.execute(
        "DELETE FROM entity_names WHERE entity_type=? AND entity_uuid=? AND name=?",
        (entity_type, entity_uuid, name),
    )


async def delete_entity(conn, entity_type: str, entity_uuid: str) -> None:
    """实体被删除时清理其全部名称与联系方式。"""
    await conn.execute(
        "DELETE FROM entity_names WHERE entity_type=? AND entity_uuid=?",
        (entity_type, entity_uuid),
    )
    await conn.execute(
        "DELETE FROM entity_contacts WHERE entity_type=? AND entity_uuid=?",
        (entity_type, entity_uuid),
    )


# ============================================================
# 写：联系方式
# ============================================================

async def _check_scope(entity_type: str, session_id: str) -> None:
    """角色只绑私聊、群组只绑群聊；无法识别作用域的旧格式跳过校验。"""
    _, scope = _split_contact(session_id)
    if not scope:
        return
    expected = "private" if entity_type == CHARACTER else "group"
    if scope != expected:
        raise ContactConflictError(
            f"会话 {session_id} 的 scope 是 {scope}，不能挂到 {entity_type} 上"
        )


async def add_contact(conn, entity_type: str, entity_uuid: str, session_id: str) -> None:
    """把会话挂到实体上；已被同一实体占用则幂等，被其他实体占用则抛错。"""
    if not session_id:
        return
    await _check_scope(entity_type, session_id)
    cur = await conn.execute(
        "SELECT entity_type, entity_uuid FROM entity_contacts WHERE session_id=?",
        (session_id,),
    )
    row = await cur.fetchone()
    if row is not None:
        if row["entity_type"] == entity_type and row["entity_uuid"] == entity_uuid:
            return
        raise ContactConflictError(
            f"会话 {session_id} 已属于 {row['entity_type']} {row['entity_uuid']}"
        )
    platform, scope = _split_contact(session_id)
    await conn.execute(
        """INSERT INTO entity_contacts
               (session_id, entity_type, entity_uuid, platform, scope, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, entity_type, entity_uuid, platform, scope, datetime.now().isoformat()),
    )


async def move_contact(conn, entity_type: str, entity_uuid: str, old_session_id: str,
                       new_session_id: str) -> None:
    """把一条联系方式改名（地址迁移），归属不变。"""
    if not old_session_id or not new_session_id:
        raise ValueError("新旧联系方式都不能为空")
    await _check_scope(entity_type, new_session_id)
    cur = await conn.execute(
        "SELECT entity_type, entity_uuid FROM entity_contacts WHERE session_id=?",
        (old_session_id,),
    )
    row = await cur.fetchone()
    if row is None or row["entity_type"] != entity_type or row["entity_uuid"] != entity_uuid:
        raise ContactConflictError(f"会话 {old_session_id} 不属于该实体，无法改址")
    await conn.execute("DELETE FROM entity_contacts WHERE session_id=?", (old_session_id,))
    await add_contact(conn, entity_type, entity_uuid, new_session_id)


async def delete_contact(conn, entity_type: str, entity_uuid: str, session_id: str) -> None:
    """解除一条联系方式；不存在则忽略。"""
    if not session_id:
        return
    await conn.execute(
        """DELETE FROM entity_contacts
           WHERE session_id=? AND entity_type=? AND entity_uuid=?""",
        (session_id, entity_type, entity_uuid),
    )
