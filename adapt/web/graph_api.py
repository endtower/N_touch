# -*- coding: utf-8 -*-
"""
adapt.web.graph_api
===================

任务列表的后端接口（SQLite 版）。

端点（URL 路由暂沿用 /events/*，待数据结构稳定后再迁移）:
  /events/list      — 任务列表 (page + status 筛选)
  /events/set_type  — 状态快改
  /events           — 创建
  /events/{id}      — PATCH / DELETE

内部已全部切到 tasks 表（brain.hc.database.task.TaskOperations）。

任务字段：title / context / next_occurrence，status 仅 active / expired
（任务一次性：到期交付后自动置 expired，续办靠重新登记）。

所有方法都是 async，基于 SQLite 记忆数据库（brain.hc.database）。

注：旧的 /graph/characters 角色关系图已废弃（系统重构后角色图范式被移除），
社交实体可视化改由 adapt.web.memory_api 的 /memory/social 提供。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web

from brain.hc.database.core import get_memory_db
from brain.hc.database.task import TaskOperations, TASK_STATUS_ACTIVE, TASK_STATUS_EXPIRED
from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


def _safe_int(s: Optional[str], default: int) -> int:
    try:
        return int(s) if s is not None else default
    except (TypeError, ValueError):
        return default


# ============================================================
# /events/list — 任务列表
# ============================================================

_TASK_FIELDS = (
    "task_id", "title", "context", "status",
    "next_occurrence",
    "start_time", "end_time", "updated_at",
)

_TASK_STATUS_ALLOW = {TASK_STATUS_ACTIVE, TASK_STATUS_EXPIRED}


async def handle_list_tasks(request: web.Request) -> web.Response:
    """GET /events/list?status=active&limit=200&offset=0"""
    status = (request.query.get("status") or TASK_STATUS_ACTIVE).strip().lower()
    limit = _safe_int(request.query.get("limit"), 200)
    offset = _safe_int(request.query.get("offset"), 0)
    if limit > 1000:
        limit = 1000
    if limit <= 0:
        limit = 200

    if status != "all" and status not in _TASK_STATUS_ALLOW:
        return web.json_response(
            {"error": f"status 必须是 {sorted(_TASK_STATUS_ALLOW | {'all'})}"},
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
            where = "WHERE status=?"
            params.append(status)

        cur = await conn.execute(f"SELECT COUNT(*) AS c FROM tasks {where}", params)
        total = (await cur.fetchone())["c"]

        cur = await conn.execute(
            f"SELECT * FROM tasks {where} ORDER BY coalesce(updated_at, start_time) DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await cur.fetchall()

        nodes = []
        for rec in rows:
            d: Dict[str, Any] = {}
            for f in _TASK_FIELDS:
                d[f] = rec[f]
            d["id"] = rec["task_id"]
            nodes.append(d)
    except Exception as e:
        logger.exception("[/events/list] 拉取失败")
        return web.json_response({"error": f"读取失败: {e}"}, status=500)

    return web.json_response({
        "tasks": nodes,
        "total": total,
        "status": status,
        "limit": limit,
        "offset": offset,
    })


async def handle_set_task_status(request: web.Request) -> web.Response:
    """POST /events/set_type  body: {task_id, status}"""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "body 必须是 JSON"}, status=400)

    task_id = (data.get("task_id") or "").strip()
    status = (data.get("status") or "").strip().lower()
    if not task_id:
        return web.json_response({"error": "task_id 必填"}, status=400)
    if status not in _TASK_STATUS_ALLOW:
        return web.json_response(
            {"error": f"status ∈ {sorted(_TASK_STATUS_ALLOW)}"},
            status=400,
        )

    try:
        db = get_memory_db()
        conn = await db._ensure_connection()
        cur = await conn.execute("SELECT task_id FROM tasks WHERE task_id=?", (task_id,))
        rec = await cur.fetchone()
        if rec is None:
            return web.json_response({"error": f"Task {task_id} 不存在"}, status=404)

        now = datetime.now().isoformat()
        await conn.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
            (status, now, task_id),
        )
        await conn.commit()

        cur = await conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        rec = await cur.fetchone()
        if rec is None:
            return web.json_response({"error": "更新后无法回读"}, status=500)
        d: Dict[str, Any] = {}
        for f in _TASK_FIELDS:
            d[f] = rec[f]
        d["id"] = task_id
        return web.json_response({"task": d, "updated": True})
    except Exception as e:
        logger.exception("[/events/set_type] 失败")
        return web.json_response({"error": f"更新失败: {e}"}, status=500)


_TASK_EDITABLE = {"title", "context", "status", "next_occurrence", "start_time"}

_TASK_TIME_FIELDS = {"start_time", "next_occurrence"}


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


async def handle_update_task(request: web.Request) -> web.Response:
    """PATCH /events/{event_id}"""
    task_id = request.match_info.get("event_id", "")
    if not task_id:
        return web.json_response({"error": "task_id 必填"}, status=400)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "body 必须是 JSON"}, status=400)

    kwargs: Dict[str, Any] = {}
    for k in _TASK_EDITABLE:
        if k in data and data[k] is not None:
            v = data[k]
            if isinstance(v, str):
                v = v.strip()
            kwargs[k] = v

    if not kwargs:
        return web.json_response(
            {"error": f"没有可改字段 (允许: {sorted(_TASK_EDITABLE)})"},
            status=400,
        )

    for f in _TASK_TIME_FIELDS:
        if f in kwargs:
            ok, err = _validate_iso_datetime(kwargs[f], f)
            if not ok:
                return web.json_response({"error": err}, status=400)

    if kwargs.get("status") and kwargs["status"] not in _TASK_STATUS_ALLOW:
        return web.json_response(
            {"error": f"status ∈ {sorted(_TASK_STATUS_ALLOW)}"},
            status=400,
        )

    try:
        db = get_memory_db()
        conn = await db._ensure_connection()

        cur = await conn.execute("SELECT task_id FROM tasks WHERE task_id=?", (task_id,))
        rec = await cur.fetchone()
        if rec is None:
            return web.json_response({"error": f"Task {task_id} 不存在"}, status=404)

        # 字段名取自 _TASK_EDITABLE 白名单，拼入 SQL 安全
        sets = [f"{k}=?" for k in kwargs]
        params: List[Any] = list(kwargs.values())
        sets.append("updated_at=?")
        params.append(datetime.now().isoformat())
        params.append(task_id)
        await conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=?",
            params,
        )
        await conn.commit()

        cur = await conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        rec = await cur.fetchone()
        if rec is None:
            return web.json_response({"error": "更新后无法回读"}, status=500)
        d: Dict[str, Any] = {}
        for f in _TASK_FIELDS:
            d[f] = rec[f]
        d["id"] = task_id
        return web.json_response({"task": d, "updated": True})
    except Exception as e:
        logger.exception("[/events PATCH] 失败")
        return web.json_response({"error": f"更新失败: {e}"}, status=500)


async def handle_create_task(request: web.Request) -> web.Response:
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

        status = data.get("status") or TASK_STATUS_ACTIVE
        if status not in _TASK_STATUS_ALLOW:
            return web.json_response(
                {"error": f"status ∈ {sorted(_TASK_STATUS_ALLOW)}"},
                status=400,
            )

        context = data.get("context") or ""
        next_occurrence = data.get("next_occurrence")
        start_time_user = data.get("start_time")

        for f in ("next_occurrence", "start_time"):
            v = data.get(f)
            if v is None:
                continue
            ok, err = _validate_iso_datetime(v, f)
            if not ok:
                return web.json_response({"error": err}, status=400)

        ops = TaskOperations()
        task_id = await ops.create_task(
            title=title,
            context=context,
            next_occurrence=next_occurrence,
            status=status,
        )

        if start_time_user not in (None, ""):
            db = get_memory_db()
            conn = await db._ensure_connection()
            await conn.execute("UPDATE tasks SET start_time=? WHERE task_id=?", (start_time_user, task_id))
            await conn.commit()

        db = get_memory_db()
        conn = await db._ensure_connection()
        cur = await conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        rec = await cur.fetchone()
        if rec is None:
            return web.json_response({"error": "创建后无法回读"}, status=500)
        d: Dict[str, Any] = {}
        for f in _TASK_FIELDS:
            d[f] = rec[f]
        d["id"] = task_id
        return web.json_response({"task": d, "created": True, "task_id": task_id})
    except Exception as e:
        logger.exception("[/events POST] 失败")
        return web.json_response({"error": f"创建失败: {e}"}, status=500)


async def handle_delete_task(request: web.Request) -> web.Response:
    """DELETE /events/{event_id}"""
    task_id = request.match_info.get("event_id", "")
    if not task_id:
        return web.json_response({"error": "task_id 必填"}, status=400)
    try:
        db = get_memory_db()
        conn = await db._ensure_connection()
        cur = await conn.execute("DELETE FROM tasks WHERE task_id=? RETURNING task_id", (task_id,))
        rec = await cur.fetchone()
        await conn.commit()
        if rec is None:
            return web.json_response({"error": f"Task {task_id} 不存在"}, status=404)
        return web.json_response({"deleted": True, "task_id": rec["task_id"]})
    except Exception as e:
        logger.exception("[/events DELETE] 失败")
        return web.json_response({"error": f"删除失败: {e}"}, status=500)
