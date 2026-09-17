# -*- coding: utf-8 -*-
"""
任务操作类（SQLite）

任务是独立表，记录长期任务（active 进行中 / completed 已完结）。
title 为定位键；summary 覆盖式更新；next_occurrence 为下次发生时间。
"""

from datetime import datetime
from typing import List, Dict, Optional

from .core import get_memory_db, serialize_vector
from .common import generate_timestamp_id
from .vector_store import get_vector_store
from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")

# 任务状态常量
TASK_STATUS_ACTIVE = "active"
TASK_STATUS_COMPLETED = "completed"


class TaskOperations:
    """任务操作类（纯异步）"""

    def __init__(self):
        self.db = get_memory_db()

    async def get_task_id_by_title(self, title: str) -> Optional[str]:
        """通过 title 精确查找任务。"""
        if not title:
            return None
        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT task_id FROM tasks WHERE title=? LIMIT 1", (title,))
        row = await cur.fetchone()
        return row["task_id"] if row else None

    async def create_task(self, title, summary, next_occurrence=None,
                          status=TASK_STATUS_ACTIVE) -> str:
        """创建任务。"""
        current_time = datetime.now().isoformat()
        task_id = generate_timestamp_id("Task")

        vector_store = get_vector_store()
        summary_vector = vector_store.generate_vector(summary) if summary else None

        conn = await self.db._ensure_connection()
        await conn.execute(
            """INSERT INTO tasks (task_id, title, status, summary, next_occurrence,
               summary_vector, is_vectorized, start_time, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, title, status, summary, next_occurrence or "",
             serialize_vector(summary_vector), 1 if summary_vector else 0,
             current_time, current_time),
        )
        await conn.commit()
        return task_id

    async def update_task(self, title, preserve_updated_at: bool = False, **kwargs) -> bool:
        """更新任务（通过 title 定位）。status 置为 completed 时自动写 end_time。"""
        task_id = await self.get_task_id_by_title(title)
        if not task_id:
            return False

        current_time = datetime.now().isoformat()
        allowed_fields = ['title', 'status', 'summary', 'next_occurrence',
                          'start_time', 'updated_at']

        set_clauses = []
        params = []
        if not preserve_updated_at:
            set_clauses.append("updated_at=?")
            params.append(current_time)

        for key, value in kwargs.items():
            if key in allowed_fields and value is not None:
                set_clauses.append(f"{key}=?")
                params.append(value)
                if key == 'status' and value == TASK_STATUS_COMPLETED:
                    set_clauses.append("end_time=?")
                    params.append(current_time)

        if not set_clauses:
            return False

        params.append(task_id)
        conn = await self.db._ensure_connection()
        await conn.execute(
            f"UPDATE tasks SET {', '.join(set_clauses)} WHERE task_id=?",
            params,
        )
        await conn.commit()
        return True

    def _calculate_time_until(self, future_time_str: str) -> str:
        """计算距离未来时间的相对时间。"""
        if not future_time_str:
            return "未知"
        try:
            future_time = datetime.fromisoformat(future_time_str)
        except ValueError:
            return "未知"
        current_time = datetime.now()
        delta = future_time - current_time
        if delta.total_seconds() < 0:
            return "已过期"
        days = delta.days
        seconds = delta.seconds
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        parts = []
        if days > 0:
            parts.append(f"{days}天")
        if hours > 0:
            parts.append(f"{hours}小时")
        if minutes > 0 or not parts:
            parts.append(f"{minutes}分钟")
        return "".join(parts) + "后"

    async def get_task_by_id(self, task_id: str) -> Optional[Dict]:
        """获取单个任务的原始数据字典。"""
        if not task_id:
            return None
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT task_id, title, summary, status, next_occurrence, start_time, end_time "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "task_id": row["task_id"],
            "title": row["title"],
            "summary": row["summary"],
            "status": row["status"],
            "next_occurrence": row["next_occurrence"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
        }

    async def format_task_by_id(self, task_id: str) -> str:
        """格式化单个任务信息。"""
        if not task_id:
            return "未指定任务ID"

        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT task_id, title, summary, status, next_occurrence, start_time, end_time "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return f"未找到任务ID为「{task_id}」的任务"

        formatted_output = []
        formatted_output.append(f"名称：{row['title'] or '未知标题'}")
        formatted_output.append(f"概况：{row['summary'] or '无'}")

        status = row["status"] or ""
        next_occurrence = row["next_occurrence"] or ""

        if status == TASK_STATUS_ACTIVE:
            if next_occurrence:
                time_until = self._calculate_time_until(next_occurrence)
                formatted_output.append(f"状态：进行中，下次发生时间为{time_until}")
            else:
                formatted_output.append("状态：进行中，未设置下次发生时间")
        elif status == TASK_STATUS_COMPLETED:
            formatted_output.append("状态：已完结")
        else:
            formatted_output.append("状态：未知")

        start_time = row["start_time"] or ""
        end_time = row["end_time"] or ""
        time_parts = []
        if start_time:
            try:
                time_parts.append(datetime.fromisoformat(start_time).strftime("%Y-%m-%d %H:%M"))
            except ValueError:
                pass
        if end_time:
            try:
                time_parts.append(datetime.fromisoformat(end_time).strftime("%Y-%m-%d %H:%M"))
            except ValueError:
                pass
        if time_parts:
            formatted_output.append(f"时间：{' - '.join(time_parts)}")

        return "\n".join(formatted_output)

    async def format_tasks_by_ids(self, task_ids: List[str]) -> str:
        """格式化多个任务信息。"""
        if not task_ids:
            return "未找到相关任务"
        formatted_output = []
        for i, task_id in enumerate(task_ids, 1):
            task_str = await self.format_task_by_id(task_id)
            if task_str.startswith("未找到"):
                continue
            formatted_output.append(f"{i}. {task_str}")
            formatted_output.append("")
        return "\n".join(formatted_output).rstrip()

    async def get_active_tasks(self, limit: int = 10) -> List[str]:
        """获取所有进行中的任务。"""
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT task_id FROM tasks WHERE status='active' "
            "ORDER BY start_time DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [r["task_id"] for r in rows if r["task_id"]]

    async def get_recent_completed_tasks(self, limit: int = 3) -> List[str]:
        """获取最近完结的任务。"""
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT task_id FROM tasks WHERE status='completed' "
            "ORDER BY CASE WHEN end_time != '' THEN end_time ELSE start_time END DESC, "
            "start_time DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [r["task_id"] for r in rows if r["task_id"]]
