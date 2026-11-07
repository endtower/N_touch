# -*- coding: utf-8 -*-
"""
adapt.web.memory_api
====================

记忆社交实体可视化接口（SQLite 版）。

单一端点:
  GET /memory/social — 返回社交记忆实体快照，供前端以 00000000 为中心的
  星形图 + 详情面板展示。

覆盖三种社交实体:
  1) Character   — 角色（00000000 指向的对象）
  2) Group       — 群组（00000000 指向的对象）
  3) FuzzyRole   — 模糊角色（依附性节点，锚定到来源实体）

所有方法都是 async，基于 SQLite 记忆数据库（brain.hc.database）。
"""

from __future__ import annotations

import json
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from aiohttp import web

from brain.hc.database.core import get_memory_db
from brain.hc.database import entity_identity as identity
from brain.hc.database.character import CharacterOperations
from brain.hc.database.group import GroupOperations
from brain.hc.database.fuzzy_role import FuzzyRoleOperations
from brain.hc.database.bequest import BequestOperations
from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


# 00000000 是 LLM 扮演的角色（未命名），星形图中心节点
ROOT_ID = "00000000"
_creation_lock = asyncio.Lock()


def _create_fields(data, allowed, required):
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise ValueError("必须提交 JSON 对象，且只能包含页面可填写的字段")
    result = {}
    for key, value in data.items():
        if not isinstance(value, str):
            raise ValueError(f"{key} 必须是文字")
        result[key] = value.strip()
    if any(not result.get(key) for key in required):
        raise ValueError("请填写必填字段：" + "、".join(required))
    return result


async def handle_create_social_memory(request: web.Request) -> web.Response:
    """POST /memory/social/{entity_type}，复用既有实体操作与向量维护。"""
    entity_type = request.match_info["entity_type"]
    fields = {
        "character": ({"name", "impression", "session_id"}, {"name"}),
        "group": ({"name", "impression", "session_id"}, {"name"}),
        "fuzzy_role": ({"name", "content", "source_session_id"}, {"name", "content", "source_session_id"}),
    }
    if entity_type not in fields:
        return web.json_response({"error": "未知实体类型"}, status=400)
    try:
        data = _create_fields(await request.json(), *fields[entity_type])
        contact = data.get("session_id") or data.get("source_session_id", "")
        from adapt.session_identity import session_scope
        if contact and session_scope(contact) not in ("private", "group"):
            raise ValueError("会话 ID 应为已知平台的私聊或群聊会话")
        if entity_type == "character" and contact and session_scope(contact) != "private":
            raise ValueError("角色应关联 private_ 私聊会话")
        if entity_type == "group" and contact and session_scope(contact) != "group":
            raise ValueError("群组应关联 group_ 群聊会话")
    except (ValueError, TypeError) as error:
        return web.json_response({"error": str(error)}, status=400)
    async with _creation_lock:
        conn = await get_memory_db()._ensure_connection()
        entity_id = None
        table = {"character": "characters", "group": "groups", "fuzzy_role": "fuzzy_roles"}[entity_type]
        try:
            if entity_type == "fuzzy_role":
                existing = await conn.execute("SELECT uuid FROM fuzzy_roles WHERE name=? AND source_session_id=?", (data["name"], contact))
                if await existing.fetchone():
                    return web.json_response({"error": "该来源下已存在同名模糊角色，请编辑现有条目"}, status=409)
                result = await FuzzyRoleOperations().create_fuzzy_role(data["name"], contact, data["content"])
                if result["status"] != "success":
                    return web.json_response({"error": result["message"]}, status=400)
                entity_id = result["fuzzy_role_id"]
            else:
                ops = CharacterOperations() if entity_type == "character" else GroupOperations()
                entity_id = await (ops.create_character_node(data["name"]) if entity_type == "character" else ops.create_group_node(data["name"]))
                if contact:
                    await ops.create_contact_node(contact, entity_id)
                kwargs = {"impression": data.get("impression", "")}
                await (ops.update_character(entity_id, **kwargs) if entity_type == "character" else ops.update_group(entity_id, **kwargs))
            row = await (await conn.execute(f"SELECT * FROM {table} WHERE uuid=?", (entity_id,))).fetchone()
            formatter, node_type = {"character": (_character_full, "Character"), "group": (_group_full, "Group"), "fuzzy_role": (_fuzzy_role_full, "FuzzyRole")}[entity_type]
            return web.json_response({"node": _add_id_type_label(await formatter(conn, row), node_type)}, status=201)
        except Exception:
            # 既有操作分步 commit；失败时只清理本次创建的新实体，避免页面重试留下半条记忆。
            if entity_id:
                await conn.execute(f"DELETE FROM {table} WHERE uuid=?", (entity_id,))
                if entity_type != "fuzzy_role":
                    entity_kind = identity.CHARACTER if entity_type == "character" else identity.GROUP
                    await identity.delete_entity(conn, entity_kind, entity_id)
                await conn.commit()
            logger.exception("创建社交实体失败")
            return web.json_response({"error": "创建失败，请检查数据库或嵌入服务"}, status=500)


def _validate_experience_shape(title: str, summary: str, category: str) -> None:
    """网页录入同样遵守简洁约束：话术短俚语、风格单句原话。"""
    if not title or not summary:
        raise ValueError("标题与内容都不能为空")
    if len(title) > 20:
        raise ValueError("标题超长：话术填 2-4 字意图，风格填 2-6 字标签")
    limit = {"speech": 30, "style": 60}.get(category)
    if limit and len(summary) > limit:
        raise ValueError(
            f"内容超长（{len(summary)} 字 > {limit} 字）："
            "话术只填短俚语/短句，风格只填一句原话"
        )


async def handle_create_experience(request: web.Request) -> web.Response:
    """POST /memory/experiences，复用经验创建与向量化。"""
    try:
        data = _create_fields(await request.json(), {"title", "summary", "category"}, {"title", "summary", "category"})
        if data["category"] not in ("speech", "style", "decision"):
            raise ValueError("经验分类应为 speech、style 或 decision")
        _validate_experience_shape(data["title"], data["summary"], data["category"])
    except (ValueError, TypeError) as error:
        return web.json_response({"error": str(error)}, status=400)
    async with _creation_lock:
        ops = BequestOperations()
        if await ops.get_bequest_id_by_title(data["title"], data["category"]):
            return web.json_response({"error": "该分类下已有同名经验，请编辑现有条目"}, status=409)
        try:
            bequest_id = await ops.create_bequest(**data)
            conn = await get_memory_db()._ensure_connection()
            row = await (await conn.execute("SELECT bequest_id, title, summary, category, start_time, updated_at, retrieval_count, last_maintained_at, manual_maintenance_count FROM bequests WHERE bequest_id=?", (bequest_id,))).fetchone()
            from brain.hc.maintenance import MaintenanceService
            service = MaintenanceService()
            await service.review(bequest_id)
            return web.json_response({"experience": dict(row)}, status=201)
        except Exception:
            logger.exception("创建经验失败")
            return web.json_response({"error": "创建失败，请检查数据库或嵌入服务"}, status=500)


def _safe_int(s: Optional[str], default: int) -> int:
    try:
        return int(s) if s is not None else default
    except (TypeError, ValueError):
        return default


# ============================================================
# 行 → 完整详情字典（与 database.character / database.group 对齐）
# ============================================================

async def _character_full(conn, rec) -> Dict[str, Any]:
    """characters 行 → 完整详情字典（同 get_character_full_info 字段）。"""
    uuid = rec["uuid"]
    names = await identity.get_names(conn, identity.CHARACTER, uuid)
    return {
        "uuid": uuid,
        "name": names[0] if names else "",
        "aliases": [{"name": n, "usage_count": 0} for n in names],
        "impressions": [{"content": rec["impression"]}] if rec["impression"] else [],
        "familiarities": [{"content": rec["familiarity"]}] if rec["familiarity"] else [],
        "contacts": await identity.get_contacts(conn, identity.CHARACTER, uuid),
        "relationships_with_default": [{"type": rec["relationship"]}] if rec["relationship"] else [],
        "allow_conversation": bool(rec["allow_conversation"]),
        "affinity_events": json.loads(rec["affinity_events"] or "[]"),
        "usage_count": rec["usage_count"],
        "last_used_at": rec["last_used_at"],
        "created_at": rec["created_at"],
    }


async def _group_full(conn, rec) -> Dict[str, Any]:
    """groups 行 → 完整详情字典（同 get_group_with_details 字段）。"""
    uuid = rec["uuid"]
    names = await identity.get_names(conn, identity.GROUP, uuid)
    return {
        "uuid": uuid,
        "name": names[0] if names else "",
        "aliases": [{"name": n, "usage_count": 0} for n in names],
        "impressions": [{"content": rec["impression"]}] if rec["impression"] else [],
        "familiarities": [{"content": rec["familiarity"]}] if rec["familiarity"] else [],
        "member_impressions": [{"content": rec["member_impression"]}] if rec["member_impression"] else [],
        "contacts": await identity.get_contacts(conn, identity.GROUP, uuid),
        "relationships_with_default": [{"type": rec["self_image"]}] if rec["self_image"] else [],
        "allow_conversation": bool(rec["allow_conversation"]),
        "usage_count": rec["usage_count"],
        "last_used_at": rec["last_used_at"],
        "created_at": rec["created_at"],
    }


async def _fuzzy_role_full(_conn, rec) -> Dict[str, Any]:
    """fuzzy_roles 行 → 完整详情字典。"""
    return {
        "uuid": rec["uuid"],
        "name": rec["name"],
        "content": rec["content"],
        "source_session_id": rec["source_session_id"],
        "source_entity_type": rec["source_entity_type"],
        "source_entity_uuid": rec["source_entity_uuid"],
        "created_at": rec["created_at"],
        "updated_at": rec["updated_at"],
    }


def _add_id_type_label(d: Dict[str, Any], node_type: str) -> Dict[str, Any]:
    """统一给节点补 id / type / label 字段。"""
    d["id"] = d["uuid"]
    d["type"] = node_type
    d["label"] = d["name"] or d["uuid"]
    return d


# ============================================================
# GET /memory/social — 社交记忆实体快照
# ============================================================

async def handle_get_social_memory(request: web.Request) -> web.Response:
    """GET /memory/social?limit=500

    返回 { nodes, edges, character_count, group_count, fuzzy_role_count, root_id }。
    nodes 内嵌完整详情字段，前端无需二次请求即可填充详情面板。
    星形结构：00000000 指向 Character/Group；FuzzyRole 依附到其来源实体。
    """
    limit = _safe_int(request.query.get("limit"), 500)
    if limit > 2000:
        limit = 2000
    if limit <= 0:
        limit = 500

    try:
        db = get_memory_db()
        conn = await db._ensure_connection()
    except Exception as e:
        # 数据库未就绪 → 阻断（不继续返回空数据，防止错误扩大蔓延）
        return web.json_response({"error": f"数据库未就绪: {e}"}, status=503)

    try:
        now = datetime.now().isoformat()
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []
        char_count = 0
        group_count = 0
        fuzzy_count = 0

        # 中心节点：00000000（LLM 扮演的角色，未命名）
        nodes.append({
            "id": ROOT_ID,
            "type": "Root",
            "label": ROOT_ID,
            "uuid": ROOT_ID,
            "name": ROOT_ID,
            "description": "LLM 扮演的角色（未命名）",
        })

        # ---- Character ----
        cur = await conn.execute(
            "SELECT * FROM characters ORDER BY coalesce(last_used_at, created_at) DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        for rec in rows:
            uuid = rec["uuid"]
            if not uuid:
                continue
            node = await _character_full(conn, rec)
            nodes.append(_add_id_type_label(node, "Character"))
            char_count += 1
            # 星形边：00000000 → 角色（关系描述作为边标签，无则默认 HAS_RELATIONSHIP）
            rel_type = rec["relationship"] or "HAS_RELATIONSHIP"
            edges.append({
                "source": ROOT_ID,
                "target": uuid,
                "type": rel_type,
                "source_type": "Root",
                "target_type": "Character",
            })

        # ---- Group ----
        cur = await conn.execute(
            """SELECT g.* FROM groups g
               LEFT JOIN entity_names n
                 ON n.entity_type='group' AND n.entity_uuid=g.uuid AND n.is_primary=1
               ORDER BY n.name ASC LIMIT ?""",
            (limit,),
        )
        rows = await cur.fetchall()
        for rec in rows:
            uuid = rec["uuid"]
            if not uuid:
                continue
            node = await _group_full(conn, rec)
            nodes.append(_add_id_type_label(node, "Group"))
            group_count += 1
            rel_type = rec["self_image"] or "HAS_RELATIONSHIP"
            edges.append({
                "source": ROOT_ID,
                "target": uuid,
                "type": rel_type,
                "source_type": "Root",
                "target_type": "Group",
            })

        # ---- FuzzyRole（依附到来源实体）----
        cur = await conn.execute(
            "SELECT * FROM fuzzy_roles ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        for rec in rows:
            uuid = rec["uuid"]
            if not uuid:
                continue
            node = await _fuzzy_role_full(conn, rec)
            nodes.append(_add_id_type_label(node, "FuzzyRole"))
            fuzzy_count += 1
            # 只在来源实体存在时连边（依附性节点 → 来源实体）
            if rec["source_entity_uuid"]:
                edges.append({
                    "source": rec["source_entity_uuid"],
                    "target": uuid,
                    "type": "ANCHORED_TO",
                    "source_type": rec["source_entity_type"] or "Character",
                    "target_type": "FuzzyRole",
                })

        logger.debug(f"[/memory/social] char={char_count} group={group_count} fuzzy={fuzzy_count}")
        return web.json_response({
            "nodes": nodes,
            "edges": edges,
            "character_count": char_count,
            "group_count": group_count,
            "fuzzy_role_count": fuzzy_count,
            "root_id": ROOT_ID,
        })
    except Exception as e:
        logger.exception("[/memory/social] 拉取失败")
        return web.json_response({"error": f"读取失败: {e}"}, status=500)


# ============================================================
# GET /memory/experiences — 当前存储的经验/嘱托 (bequests 表)
# ============================================================

async def handle_get_experiences(request: web.Request) -> web.Response:
    """GET /memory/experiences?limit=200

    返回 { experiences, total }。experiences 为 bequests 表按 start_time 倒序的
    完整经验列表（title / summary / category），前端分组展示。
    """
    limit = _safe_int(request.query.get("limit"), 200)
    if limit > 2000:
        limit = 2000
    if limit <= 0:
        limit = 200

    try:
        db = get_memory_db()
        conn = await db._ensure_connection()
    except Exception as e:
        # 数据库未就绪 → 阻断（不继续返回空数据，防止错误扩大蔓延）
        return web.json_response({"error": f"数据库未就绪: {e}"}, status=503)

    try:
        cur = await conn.execute(
            "SELECT bequest_id, title, summary, category, start_time, updated_at, retrieval_count, last_maintained_at, manual_maintenance_count "
            "FROM bequests ORDER BY coalesce(start_time, updated_at) DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        experiences = [
            {
                "bequest_id": r["bequest_id"],
                "title": r["title"] or "",
                "summary": r["summary"] or "",
                "category": r["category"] or "",
                "start_time": r["start_time"] or "",
                "updated_at": r["updated_at"] or "",
                "retrieval_count": r["retrieval_count"],
                "last_maintained_at": r["last_maintained_at"],
                "manual_maintenance_count": r["manual_maintenance_count"],
            }
            for r in rows
        ]
        logger.debug(f"[/memory/experiences] total={len(experiences)}")
        return web.json_response({
            "experiences": experiences,
            "total": len(experiences),
        })
    except Exception as e:
        logger.exception("[/memory/experiences] 拉取失败")
        return web.json_response({"error": f"读取失败: {e}"}, status=500)


# ============================================================
# PATCH /memory/experiences/{bequest_id} — 编辑经验/嘱托
# ============================================================

_BEQUEST_EDITABLE = {"title", "summary"}


async def handle_update_experience(request: web.Request) -> web.Response:
    """PATCH /memory/experiences/{bequest_id}

    body (JSON, 只提交要改的字段):
      { title?, summary? }

    category 不开放编辑（创建时决定 speech/style/decision）。
    更新成功后返回最新经验对象 { experience, updated: True }。
    """
    bequest_id = request.match_info.get("bequest_id", "")
    if not bequest_id:
        return web.json_response({"error": "bequest_id 必填"}, status=400)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "body 必须是 JSON"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "body 必须是 JSON 对象"}, status=400)

    kwargs: Dict[str, Any] = {}
    for k in _BEQUEST_EDITABLE:
        if k in data and data[k] is not None:
            v = data[k]
            if not isinstance(v, str):
                return web.json_response({"error": f"{k} 必须是字符串"}, status=400)
            v = v.strip()
            kwargs[k] = v

    if not kwargs:
        return web.json_response({"error": "没有可改字段"}, status=400)

    try:
        conn = await get_memory_db()._ensure_connection()
        current = await (await conn.execute(
            "SELECT title, summary, category FROM bequests WHERE bequest_id=?",
            (bequest_id,),
        )).fetchone()
        if current is None:
            return web.json_response({"error": f"经验 {bequest_id} 不存在"}, status=404)
        _validate_experience_shape(
            kwargs.get("title", current["title"] or ""),
            kwargs.get("summary", current["summary"] or ""),
            current["category"] or "",
        )

        ok = await BequestOperations().update_bequest_by_id(bequest_id, **kwargs)
        if not ok:
            return web.json_response({"error": f"经验 {bequest_id} 不存在"}, status=404)

        cur = await conn.execute(
            "SELECT bequest_id, title, summary, category, start_time, updated_at, retrieval_count, last_maintained_at, manual_maintenance_count "
            "FROM bequests WHERE bequest_id=?",
            (bequest_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return web.json_response({"error": "更新后无法回读"}, status=500)

        experience = {
            "bequest_id": row["bequest_id"],
            "title": row["title"] or "",
            "summary": row["summary"] or "",
            "category": row["category"] or "",
            "start_time": row["start_time"] or "",
            "updated_at": row["updated_at"] or "",
            "retrieval_count": row["retrieval_count"],
            "last_maintained_at": row["last_maintained_at"],
            "manual_maintenance_count": row["manual_maintenance_count"],
        }
        logger.debug(f"[/memory/experiences/{bequest_id}] 已更新")
        from brain.hc.maintenance import MaintenanceService
        service = MaintenanceService()
        await service.review(bequest_id)
        return web.json_response({"experience": experience, "updated": True})
    except Exception as e:
        logger.exception(f"[/memory/experiences/{bequest_id}] 更新失败")
        return web.json_response({"error": f"更新失败: {e}"}, status=500)


# ============================================================
# DELETE /memory/experiences/{bequest_id} — 删除经验/嘱托
# ============================================================

async def handle_delete_experience(request: web.Request) -> web.Response:
    """DELETE /memory/experiences/{bequest_id}

    删除经验（话术/风格/决策），成功返回 { deleted: True, bequest_id }；不存在返回 404。
    """
    bequest_id = request.match_info.get("bequest_id", "")
    if not bequest_id:
        return web.json_response({"error": "bequest_id 必填"}, status=400)

    try:
        ok = await BequestOperations().delete_bequest_by_id(bequest_id)
        if not ok:
            return web.json_response({"error": f"经验 {bequest_id} 不存在"}, status=404)
        logger.debug(f"[/memory/experiences/{bequest_id}] 已删除")
        return web.json_response({"deleted": True, "bequest_id": bequest_id})
    except Exception as e:
        logger.exception(f"[/memory/experiences/{bequest_id}] 删除失败")
        return web.json_response({"error": f"删除失败: {e}"}, status=500)


# ============================================================
# DELETE /memory/social/{entity_type}/{entity_id} — 删除社交实体
# ============================================================

async def handle_delete_social_memory(request: web.Request) -> web.Response:
    """DELETE /memory/social/{entity_type}/{entity_id}

    entity_type ∈ {character, group, fuzzy_role}

    角色 / 群组的别名、印象、熟悉度、联系方式、关系与向量都在同一行内，随行删除；
    同时级联删除锚定到它的模糊角色。删除成功返回
    { deleted: True, entity_type, entity_id, cascaded_fuzzy_roles }；不存在返回 404。
    """
    entity_type = request.match_info.get("entity_type", "").lower()
    entity_id = request.match_info.get("entity_id", "")
    if entity_type not in ("character", "group", "fuzzy_role"):
        return web.json_response(
            {"error": "entity_type ∈ {character, group, fuzzy_role}"}, status=400
        )
    if not entity_id:
        return web.json_response({"error": "entity_id 必填"}, status=400)
    if entity_id == ROOT_ID:
        return web.json_response({"error": f"{ROOT_ID} 为中心角色，不可删除"}, status=400)

    try:
        if entity_type == "character":
            if not await CharacterOperations().delete_character(entity_id):
                return web.json_response({"error": f"Character {entity_id} 不存在"}, status=404)
            cascaded = await FuzzyRoleOperations().delete_by_source_entity(entity_id)
        elif entity_type == "group":
            if not await GroupOperations().delete_group(entity_id):
                return web.json_response({"error": f"Group {entity_id} 不存在"}, status=404)
            cascaded = await FuzzyRoleOperations().delete_by_source_entity(entity_id)
        else:
            result = await FuzzyRoleOperations().delete_fuzzy_role(entity_id)
            if result["status"] != "success":
                return web.json_response({"error": result["message"]}, status=404)
            cascaded = 0
        logger.debug(f"[/memory/social DELETE] {entity_type} {entity_id} 已删除，级联模糊角色 {cascaded}")
        return web.json_response({
            "deleted": True,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "cascaded_fuzzy_roles": cascaded,
        })
    except Exception as e:
        logger.exception(f"[/memory/social DELETE] {entity_type} {entity_id} 删除失败")
        return web.json_response({"error": f"删除失败: {e}"}, status=500)


# ============================================================
# PATCH /memory/social/{entity_type}/{entity_id} — 编辑具体记忆内容
# ============================================================

# 三种实体的可编辑字段白名单
_CHARACTER_EDITABLE = {
    "name", "aliases", "impression", "familiarity", "relationship",
    "allow_conversation",
}
_GROUP_EDITABLE = {
    "name", "aliases", "impression", "familiarity",
    "member_impression", "self_image", "allow_conversation",
}
_FUZZY_EDITABLE = {"name", "content"}


async def handle_update_social_memory(request: web.Request) -> web.Response:
    """PATCH /memory/social/{entity_type}/{entity_id}

    entity_type ∈ {character, group, fuzzy_role}

    body (JSON, 只提交要改的字段):
      character: { name?, aliases?: [str], impression?, familiarity?, relationship?,
                   allow_conversation?: bool }
      group:     { name?, aliases?: [str], impression?, familiarity?,
                   member_impression?, self_image?, allow_conversation?: bool }
      fuzzy_role:{ name?, content? }

    allow_conversation 勾选后，conversation_agent 保护模式放行该实体关联的会话。
    更新成功后重新向量化，并返回完整节点（含 id/type/label）。
    """
    entity_type = request.match_info.get("entity_type", "").lower()
    entity_id = request.match_info.get("entity_id", "")
    if entity_type not in ("character", "group", "fuzzy_role"):
        return web.json_response(
            {"error": "entity_type ∈ {character, group, fuzzy_role}"},
            status=400,
        )
    if not entity_id:
        return web.json_response({"error": "entity_id 必填"}, status=400)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "body 必须是 JSON"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "body 必须是 JSON 对象"}, status=400)

    editable = {
        "character": _CHARACTER_EDITABLE,
        "group": _GROUP_EDITABLE,
        "fuzzy_role": _FUZZY_EDITABLE,
    }[entity_type]

    kwargs: Dict[str, Any] = {}
    for k in editable:
        if k in data and data[k] is not None:
            v = data[k]
            if k == "aliases":
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    return web.json_response({"error": "aliases 必须是字符串数组"}, status=400)
                v = [x.strip() for x in v if x.strip()]
            elif k == "allow_conversation":
                if type(v) is not bool:
                    return web.json_response({"error": "allow_conversation 必须是布尔值"}, status=400)
            elif isinstance(v, str):
                v = v.strip()
            kwargs[k] = v

    if not kwargs:
        return web.json_response({"error": "没有可改字段"}, status=400)

    try:
        db = get_memory_db()
        conn = await db._ensure_connection()

        if entity_type == "character":
            ok = await CharacterOperations().update_character(entity_id, **kwargs)
            if not ok:
                return web.json_response({"error": f"Character {entity_id} 不存在"}, status=404)
            cur = await conn.execute("SELECT * FROM characters WHERE uuid=?", (entity_id,))
            rec = await cur.fetchone()
            if rec is None:
                return web.json_response({"error": "更新后无法回读"}, status=500)
            node = _add_id_type_label(await _character_full(conn, rec), "Character")
        elif entity_type == "group":
            ok = await GroupOperations().update_group(entity_id, **kwargs)
            if not ok:
                return web.json_response({"error": f"Group {entity_id} 不存在"}, status=404)
            cur = await conn.execute("SELECT * FROM groups WHERE uuid=?", (entity_id,))
            rec = await cur.fetchone()
            if rec is None:
                return web.json_response({"error": "更新后无法回读"}, status=500)
            node = _add_id_type_label(await _group_full(conn, rec), "Group")
        else:
            ok = await FuzzyRoleOperations().update_fuzzy_role(entity_id, **kwargs)
            if not ok:
                return web.json_response({"error": f"FuzzyRole {entity_id} 不存在"}, status=404)
            cur = await conn.execute("SELECT * FROM fuzzy_roles WHERE uuid=?", (entity_id,))
            rec = await cur.fetchone()
            if rec is None:
                return web.json_response({"error": "更新后无法回读"}, status=500)
            node = _add_id_type_label(await _fuzzy_role_full(conn, rec), "FuzzyRole")

        logger.debug(f"[/memory/social PATCH] {entity_type} {entity_id} 已更新: {sorted(kwargs)}")
        return web.json_response({"node": node, "updated": True})
    except Exception as e:
        logger.exception("[/memory/social PATCH] 更新失败")
        return web.json_response({"error": f"更新失败: {e}"}, status=500)
