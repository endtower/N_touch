# -*- coding: utf-8 -*-
"""
adapt.web.graph_api
===================

长期事件列表的后端接口（SQLite 版）。

端点:
  /events/list      — 长期事件列表 (page + 类型筛选)
  /events/set_type  — 状态快改
  /events           — 创建
  /events/{id}      — PATCH / DELETE

所有方法都是 async，基于 SQLite 记忆数据库（brain.hc.database）。

注：旧的 /graph/characters 角色关系图已废弃（系统重构后角色图范式被移除），
社交实体可视化改由 adapt.web.memory_api 的 /memory/social 提供。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web

from brain.hc.database.core import get_memory_db
from brain.hc.database.event import EventOperations
from brain.hc.database.vector_store import get_vector_store
from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


def _safe_int(s: Optional[str], default: int) -> int:
    try:
        return int(s) if s is not None else default
    except (TypeError, ValueError):
        return default


# ============================================================
# /events/list — 长期事件列表
# ============================================================

_EVENT_FIELDS = (
    "event_id", "title", "summary", "event_type",
    "next_occurrence", "is_vectorized",
    "start_time", "end_time", "updated_at",
)

_EVENT_TYPE_ALLOW = {"ongoing", "completed", "failed", "aborted", "planned"}


async def handle_list_events(request: web.Request) -> web.Response:
    """GET /events/list?status=ongoing&limit=200&offset=0"""
    status = (request.query.get("status") or "ongoing").strip().lower()
    limit = _safe_int(request.query.get("limit"), 200)
    offset = _safe_int(request.query.get("offset"), 0)
    if limit > 1000:
        limit = 1000
    if limit <= 0:
        limit = 200

    if status != "all" and status not in _EVENT_TYPE_ALLOW:
        return web.json_response(
            {"error": f"status 必须是 {sorted(_EVENT_TYPE_ALLOW | {'all'})}"},
            status=400,
        )

    try:
        db = get_memory_db()
        conn = await db._ensure_connection()
    except Exception as e:
        return web.json_response({"error": f"数据库未就绪: {e}"}, status=503)

    try:
        where = ""
        params: List[Any] = []
        if status != "all":
            where = "WHERE event_type=?"
            params.append(status)

        cur = await conn.execute(f"SELECT COUNT(*) AS c FROM events {where}", params)
        total = (await cur.fetchone())["c"]

        cur = await conn.execute(
            f"SELECT * FROM events {where} ORDER BY coalesce(updated_at, start_time) DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await cur.fetchall()

        nodes = []
        for rec in rows:
            d: Dict[str, Any] = {}
            for f in _EVENT_FIELDS:
                d[f] = rec[f]
            d["id"] = rec["event_id"]
            nodes.append(d)
    except Exception as e:
        logger.exception("[/events/list] 拉取失败")
        return web.json_response({"error": f"读取失败: {e}"}, status=500)

    return web.json_response({
        "events": nodes,
        "total": total,
        "status": status,
        "limit": limit,
        "offset": offset,
    })


async def handle_set_event_type(request: web.Request) -> web.Response:
    """POST /events/set_type  body: {event_id, event_type}"""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "body 必须是 JSON"}, status=400)

    event_id = (data.get("event_id") or "").strip()
    event_type = (data.get("event_type") or "").strip().lower()
    if not event_id:
        return web.json_response({"error": "event_id 必填"}, status=400)
    if event_type not in _EVENT_TYPE_ALLOW:
        return web.json_response(
            {"error": f"event_type ∈ {sorted(_EVENT_TYPE_ALLOW)}"},
            status=400,
        )

    try:
        ops = EventOperations()
        db = get_memory_db()
        conn = await db._ensure_connection()
        cur = await conn.execute("SELECT title FROM events WHERE event_id=?", (event_id,))
        rec = await cur.fetchone()
        if rec is None:
            return web.json_response({"error": f"Event {event_id} 不存在"}, status=404)
        current_title = rec["title"] or ""

        ok = await ops.update_event(current_title, event_type=event_type)
        if not ok:
            return web.json_response({"error": "update_event 返回 False"}, status=500)

        cur = await conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,))
        rec = await cur.fetchone()
        if rec is None:
            return web.json_response({"error": "更新后无法回读"}, status=500)
        d: Dict[str, Any] = {}
        for f in _EVENT_FIELDS:
            d[f] = rec[f]
        d["id"] = event_id
        return web.json_response({"event": d, "updated": True})
    except Exception as e:
        logger.exception("[/events/set_type] 失败")
        return web.json_response({"error": f"更新失败: {e}"}, status=500)


_EVENT_EDITABLE = {"title", "event_type", "summary", "next_occurrence",
                   "start_time", "updated_at"}

_EVENT_TIME_FIELDS = {"start_time", "updated_at", "next_occurrence"}


def _validate_iso_datetime(value: Any, field_name: str) -> Tuple[bool, str]:
    """校验字符串能否被 datetime.fromisoformat 解析。空串视为清空。"""
    if value is None:
        return True, ""
    if not isinstance(value, str):
        return False, f"{field_name} 必须是字符串 (ISO datetime) 或 null"
    s = value.strip()
    if not s:
        return True, ""
    try:
        datetime.fromisoformat(s)
    except ValueError:
        return False, f"{field_name} 不是合法的 ISO datetime: '{s}'"
    return True, ""


async def handle_update_event(request: web.Request) -> web.Response:
    """PATCH /events/{event_id}"""
    event_id = request.match_info.get("event_id", "")
    if not event_id:
        return web.json_response({"error": "event_id 必填"}, status=400)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "body 必须是 JSON"}, status=400)

    kwargs: Dict[str, Any] = {}
    for k in _EVENT_EDITABLE:
        if k in data and data[k] is not None:
            v = data[k]
            if isinstance(v, str):
                v = v.strip()
            kwargs[k] = v

    if not kwargs:
        return web.json_response(
            {"error": f"没有可改字段 (允许: {sorted(_EVENT_EDITABLE)})"},
            status=400,
        )

    for f in _EVENT_TIME_FIELDS:
        if f in kwargs:
            ok, err = _validate_iso_datetime(kwargs[f], f)
            if not ok:
                return web.json_response({"error": err}, status=400)

    if kwargs.get("event_type") and kwargs["event_type"] not in _EVENT_TYPE_ALLOW:
        return web.json_response(
            {"error": f"event_type ∈ {sorted(_EVENT_TYPE_ALLOW)}"},
            status=400,
        )

    try:
        ops = EventOperations()
        db = get_memory_db()
        conn = await db._ensure_connection()

        cur = await conn.execute("SELECT title, summary FROM events WHERE event_id=?", (event_id,))
        rec = await cur.fetchone()
        if rec is None:
            return web.json_response({"error": f"Event {event_id} 不存在"}, status=404)
        current_title = rec["title"] or ""
        current_summary = rec["summary"] or ""

        non_title_kwargs = {k: v for k, v in kwargs.items() if k != "title"}
        new_title = kwargs.get("title")
        title_changed = bool(new_title) and new_title != current_title
        user_set_updated_at = "updated_at" in kwargs and kwargs["updated_at"] is not None

        if non_title_kwargs:
            ok = await ops.update_event(
                current_title,
                preserve_updated_at=user_set_updated_at,
                **non_title_kwargs,
            )
            if not ok:
                return web.json_response({"error": "update_event 返回 False"}, status=500)

        if title_changed:
            if user_set_updated_at:
                await conn.execute("UPDATE events SET title=? WHERE event_id=?", (new_title, event_id))
            else:
                now = datetime.now().isoformat()
                await conn.execute("UPDATE events SET title=?, updated_at=? WHERE event_id=?", (new_title, now, event_id))
            await conn.commit()

        new_summary = kwargs.get("summary")
        if new_summary is not None and new_summary != current_summary:
            vector_to_set: Optional[List[float]] = None
            try:
                vector_store = get_vector_store()
                vector_to_set = vector_store.generate_vector(new_summary)
            except Exception as e:
                logger.warning(f"重生成 summary_vector 失败: {e}")
            from brain.hc.database.core import serialize_vector
            if vector_to_set:
                if user_set_updated_at:
                    await conn.execute("UPDATE events SET summary_vector=?, is_vectorized=1 WHERE event_id=?", (serialize_vector(vector_to_set), event_id))
                else:
                    now = datetime.now().isoformat()
                    await conn.execute("UPDATE events SET summary_vector=?, is_vectorized=1, updated_at=? WHERE event_id=?", (serialize_vector(vector_to_set), now, event_id))
            else:
                if user_set_updated_at:
                    await conn.execute("UPDATE events SET summary_vector=NULL, is_vectorized=0 WHERE event_id=?", (event_id,))
                else:
                    now = datetime.now().isoformat()
                    await conn.execute("UPDATE events SET summary_vector=NULL, is_vectorized=0, updated_at=? WHERE event_id=?", (now, event_id))
            await conn.commit()

        cur = await conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,))
        rec = await cur.fetchone()
        if rec is None:
            return web.json_response({"error": "更新后无法回读"}, status=500)
        d: Dict[str, Any] = {}
        for f in _EVENT_FIELDS:
            d[f] = rec[f]
        d["id"] = event_id
        return web.json_response({"event": d, "updated": True})
    except Exception as e:
        logger.exception("[/events PATCH] 失败")
        return web.json_response({"error": f"更新失败: {e}"}, status=500)


_EVENT_CREATABLE = {"title", "event_type", "summary", "next_occurrence", "start_time"}


async def handle_create_event(request: web.Request) -> web.Response:
    """POST /events"""
    try:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "body 必须是 JSON"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "body 必须是 JSON 对象"}, status=400)

        title = (data.get("title") or "").strip()
        if not title:
            return web.json_response({"error": "title 必填且非空"}, status=400)

        event_type = data.get("event_type") or "ongoing"
        if event_type not in _EVENT_TYPE_ALLOW:
            return web.json_response(
                {"error": f"event_type ∈ {sorted(_EVENT_TYPE_ALLOW)}"},
                status=400,
            )

        summary = data.get("summary") or ""
        next_occurrence = data.get("next_occurrence")
        start_time_user = data.get("start_time")

        for f in ("next_occurrence", "start_time"):
            v = data.get(f)
            if v is None:
                continue
            ok, err = _validate_iso_datetime(v, f)
            if not ok:
                return web.json_response({"error": err}, status=400)

        clean: Dict[str, Any] = {"title": title, "event_type": event_type, "summary": summary}
        if next_occurrence not in (None, ""):
            clean["next_occurrence"] = next_occurrence
        if start_time_user not in (None, ""):
            clean["start_time"] = start_time_user

        ops = EventOperations()
        event_id = await ops.create_event(
            title=clean["title"],
            event_type=clean["event_type"],
            summary=clean["summary"],
            next_occurrence=clean.get("next_occurrence"),
        )

        if "start_time" in clean:
            db = get_memory_db()
            conn = await db._ensure_connection()
            await conn.execute("UPDATE events SET start_time=? WHERE event_id=?", (clean["start_time"], event_id))
            await conn.commit()

        db = get_memory_db()
        conn = await db._ensure_connection()
        cur = await conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,))
        rec = await cur.fetchone()
        if rec is None:
            return web.json_response({"error": "创建后无法回读"}, status=500)
        d: Dict[str, Any] = {}
        for f in _EVENT_FIELDS:
            d[f] = rec[f]
        d["id"] = event_id
        return web.json_response({"event": d, "created": True, "event_id": event_id})
    except Exception as e:
        logger.exception("[/events POST] 失败")
        return web.json_response({"error": f"创建失败: {e}"}, status=500)


async def handle_delete_event(request: web.Request) -> web.Response:
    """DELETE /events/{event_id}"""
    event_id = request.match_info.get("event_id", "")
    if not event_id:
        return web.json_response({"error": "event_id 必填"}, status=400)
    try:
        db = get_memory_db()
        conn = await db._ensure_connection()
        cur = await conn.execute("DELETE FROM events WHERE event_id=? RETURNING event_id", (event_id,))
        rec = await cur.fetchone()
        await conn.commit()
        if rec is None:
            return web.json_response({"error": f"Event {event_id} 不存在"}, status=404)
        return web.json_response({"deleted": True, "event_id": rec["event_id"]})
    except Exception as e:
        logger.exception("[/events DELETE] 失败")
        return web.json_response({"error": f"删除失败: {e}"}, status=500)
