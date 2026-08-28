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
from datetime import datetime
from typing import Any, Dict, List, Optional

from aiohttp import web

from brain.hc.database.core import get_memory_db
from brain.hc.database.character import CharacterOperations
from brain.hc.database.group import GroupOperations
from brain.hc.database.fuzzy_role import FuzzyRoleOperations
from brain.hc.database.bequest import BequestOperations
from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


# 00000000 是 LLM 扮演的角色（未命名），星形图中心节点
ROOT_ID = "00000000"


def _safe_int(s: Optional[str], default: int) -> int:
    try:
        return int(s) if s is not None else default
    except (TypeError, ValueError):
        return default


# ============================================================
# 行 → 完整详情字典（与 database.character / database.group 对齐）
# ============================================================

def _character_full(rec) -> Dict[str, Any]:
    """characters 行 → 完整详情字典（同 get_character_full_info 字段）。"""
    return {
        "uuid": rec["uuid"],
        "name": rec["name"],
        "aliases": [{"name": a, "usage_count": 0} for a in json.loads(rec["aliases"] or "[]")],
        "impressions": [{"content": rec["impression"]}] if rec["impression"] else [],
        "familiarities": [{"content": rec["familiarity"]}] if rec["familiarity"] else [],
        "contacts": json.loads(rec["contacts"] or "[]"),
        "relationships_with_default": [{"type": rec["relationship"]}] if rec["relationship"] else [],
        "affinity_events": json.loads(rec["affinity_events"] or "[]"),
        "usage_count": rec["usage_count"],
        "last_used_at": rec["last_used_at"],
        "created_at": rec["created_at"],
    }


def _group_full(rec) -> Dict[str, Any]:
    """groups 行 → 完整详情字典（同 get_group_with_details 字段）。"""
    return {
        "uuid": rec["uuid"],
        "name": rec["name"],
        "aliases": [{"name": a, "usage_count": 0} for a in json.loads(rec["aliases"] or "[]")],
        "impressions": [{"content": rec["impression"]}] if rec["impression"] else [],
        "familiarities": [{"content": rec["familiarity"]}] if rec["familiarity"] else [],
        "member_impressions": [{"content": rec["member_impression"]}] if rec["member_impression"] else [],
        "contacts": json.loads(rec["contacts"] or "[]"),
        "relationships_with_default": [{"type": rec["self_image"]}] if rec["self_image"] else [],
        "usage_count": rec["usage_count"],
        "last_used_at": rec["last_used_at"],
        "created_at": rec["created_at"],
    }


def _fuzzy_role_full(rec) -> Dict[str, Any]:
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
            node = _character_full(rec)
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
            "SELECT * FROM groups ORDER BY name ASC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        for rec in rows:
            uuid = rec["uuid"]
            if not uuid:
                continue
            node = _group_full(rec)
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
            node = _fuzzy_role_full(rec)
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
    完整经验列表（title / summary / apply_scenario），前端分组展示。
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
            "SELECT bequest_id, title, summary, apply_scenario, start_time, updated_at "
            "FROM bequests ORDER BY coalesce(start_time, updated_at) DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        experiences = [
            {
                "bequest_id": r["bequest_id"],
                "title": r["title"] or "",
                "summary": r["summary"] or "",
                "apply_scenario": r["apply_scenario"] or "",
                "start_time": r["start_time"] or "",
                "updated_at": r["updated_at"] or "",
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

_BEQUEST_EDITABLE = {"title", "summary", "apply_scenario"}


async def handle_update_experience(request: web.Request) -> web.Response:
    """PATCH /memory/experiences/{bequest_id}

    body (JSON, 只提交要改的字段):
      { title?, summary?, apply_scenario? }

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
        ok = await BequestOperations().update_bequest_by_id(bequest_id, **kwargs)
        if not ok:
            return web.json_response({"error": f"经验 {bequest_id} 不存在"}, status=404)

        conn = await get_memory_db()._ensure_connection()
        cur = await conn.execute(
            "SELECT bequest_id, title, summary, apply_scenario, start_time, updated_at "
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
            "apply_scenario": row["apply_scenario"] or "",
            "start_time": row["start_time"] or "",
            "updated_at": row["updated_at"] or "",
        }
        logger.debug(f"[/memory/experiences/{bequest_id}] 已更新")
        return web.json_response({"experience": experience, "updated": True})
    except Exception as e:
        logger.exception(f"[/memory/experiences/{bequest_id}] 更新失败")
        return web.json_response({"error": f"更新失败: {e}"}, status=500)


# ============================================================
# PATCH /memory/social/{entity_type}/{entity_id} — 编辑具体记忆内容
# ============================================================

# 三种实体的可编辑字段白名单
_CHARACTER_EDITABLE = {"name", "aliases", "impression", "familiarity", "relationship"}
_GROUP_EDITABLE = {
    "name", "aliases", "impression", "familiarity",
    "member_impression", "self_image",
}
_FUZZY_EDITABLE = {"name", "content"}


async def handle_update_social_memory(request: web.Request) -> web.Response:
    """PATCH /memory/social/{entity_type}/{entity_id}

    entity_type ∈ {character, group, fuzzy_role}

    body (JSON, 只提交要改的字段):
      character: { name?, aliases?: [str], impression?, familiarity?, relationship? }
      group:     { name?, aliases?: [str], impression?, familiarity?,
                   member_impression?, self_image? }
      fuzzy_role:{ name?, content? }

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
            node = _add_id_type_label(_character_full(rec), "Character")
        elif entity_type == "group":
            ok = await GroupOperations().update_group(entity_id, **kwargs)
            if not ok:
                return web.json_response({"error": f"Group {entity_id} 不存在"}, status=404)
            cur = await conn.execute("SELECT * FROM groups WHERE uuid=?", (entity_id,))
            rec = await cur.fetchone()
            if rec is None:
                return web.json_response({"error": "更新后无法回读"}, status=500)
            node = _add_id_type_label(_group_full(rec), "Group")
        else:
            ok = await FuzzyRoleOperations().update_fuzzy_role(entity_id, **kwargs)
            if not ok:
                return web.json_response({"error": f"FuzzyRole {entity_id} 不存在"}, status=404)
            cur = await conn.execute("SELECT * FROM fuzzy_roles WHERE uuid=?", (entity_id,))
            rec = await cur.fetchone()
            if rec is None:
                return web.json_response({"error": "更新后无法回读"}, status=500)
            node = _add_id_type_label(_fuzzy_role_full(rec), "FuzzyRole")

        logger.debug(f"[/memory/social PATCH] {entity_type} {entity_id} 已更新: {sorted(kwargs)}")
        return web.json_response({"node": node, "updated": True})
    except Exception as e:
        logger.exception("[/memory/social PATCH] 更新失败")
        return web.json_response({"error": f"更新失败: {e}"}, status=500)
