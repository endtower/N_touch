# -*- coding: utf-8 -*-
"""
事件操作类（SQLite）

事件是独立表，记录长期事件（ongoing / completed / failed / aborted / planned）。
"""

import json
from datetime import datetime
from typing import List, Dict, Optional

from .core import get_memory_db, serialize_vector, deserialize_vector, cosine_similarity
from .common import generate_timestamp_id
from .vector_store import get_vector_store
from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


class EventOperations:
    """事件操作类（纯异步）"""

    def __init__(self):
        self.db = get_memory_db()

    async def get_event_id_by_title(self, title: str) -> Optional[str]:
        """通过 title 精确查找事件。"""
        if not title:
            return None
        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT event_id FROM events WHERE title=? LIMIT 1", (title,))
        row = await cur.fetchone()
        return row["event_id"] if row else None

    async def create_event(self, title, event_type, summary, evaluations=None,
                           next_occurrence=None, amy_states=None) -> str:
        """创建事件。"""
        current_time = datetime.now().isoformat()
        event_id = generate_timestamp_id("Event")
        amy_states_json = json.dumps(amy_states or {}, ensure_ascii=False)

        vector_store = get_vector_store()
        summary_vector = vector_store.generate_vector(summary) if summary else None

        conn = await self.db._ensure_connection()
        await conn.execute(
            """INSERT INTO events (event_id, title, event_type, summary, evaluations,
               next_occurrence, amy_states, summary_vector, is_vectorized, start_time, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, title, event_type, summary,
             json.dumps(evaluations, ensure_ascii=False) if evaluations else "",
             next_occurrence or "", amy_states_json,
             serialize_vector(summary_vector), 1 if summary_vector else 0,
             current_time, current_time),
        )
        await conn.commit()
        return event_id

    async def update_event(self, title, preserve_updated_at: bool = False, **kwargs) -> bool:
        """更新事件（通过 title 定位）。"""
        event_id = await self.get_event_id_by_title(title)
        if not event_id:
            return False

        current_time = datetime.now().isoformat()
        allowed_fields = ['title', 'event_type', 'summary', 'evaluations', 'next_occurrence',
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
                if key == 'event_type' and value == 'completed':
                    set_clauses.append("end_time=?")
                    params.append(current_time)

        if not set_clauses:
            return False

        params.append(event_id)
        conn = await self.db._ensure_connection()
        await conn.execute(
            f"UPDATE events SET {', '.join(set_clauses)} WHERE event_id=?",
            params,
        )
        await conn.commit()
        return True

    async def delete_event_by_id(self, title: str) -> bool:
        """删除事件（通过 title 定位）。"""
        event_id = await self.get_event_id_by_title(title)
        if not event_id:
            return False
        conn = await self.db._ensure_connection()
        await conn.execute("DELETE FROM events WHERE event_id=?", (event_id,))
        await conn.commit()
        return True

    async def search_event_by_vector(self, query_text: str, top_k: int = 5) -> List[str]:
        """通过向量相似度检索相似事件。"""
        if not query_text:
            raise ValueError("查询文本不能为空")

        vector_store = get_vector_store()
        query_vector = vector_store.generate_vector(query_text)
        if not query_vector:
            return []

        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT event_id, summary_vector FROM events WHERE is_vectorized=1"
        )
        rows = await cur.fetchall()

        results = []
        for r in rows:
            vec = deserialize_vector(r["summary_vector"])
            if vec and len(vec) == len(query_vector):
                sim = cosine_similarity(query_vector, vec)
                results.append((r["event_id"], sim))

        results.sort(key=lambda x: x[1], reverse=True)
        return [event_id for event_id, _ in results[:top_k]]

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

    async def get_event_by_id(self, event_id: str) -> Optional[Dict]:
        """获取单个事件的原始数据字典。"""
        if not event_id:
            return None
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT event_id, title, summary, event_type, next_occurrence, start_time, end_time "
            "FROM events WHERE event_id=?",
            (event_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "event_id": row["event_id"],
            "title": row["title"],
            "summary": row["summary"],
            "event_type": row["event_type"],
            "next_occurrence": row["next_occurrence"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
        }

    async def format_event_by_id(self, event_id: str) -> str:
        """格式化单个事件信息。"""
        if not event_id:
            return "未指定事件ID"

        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT event_id, title, summary, event_type, next_occurrence, start_time, end_time "
            "FROM events WHERE event_id=?",
            (event_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return f"未找到事件ID为「{event_id}」的事件"

        formatted_output = []
        formatted_output.append(f"名称：{row['title'] or '未知标题'}")
        formatted_output.append(f"概况：{row['summary'] or '无'}")

        event_type = row["event_type"] or ""
        next_occurrence = row["next_occurrence"] or ""

        if event_type == "ongoing":
            if next_occurrence:
                time_until = self._calculate_time_until(next_occurrence)
                formatted_output.append(f"状态：正在进行，下次触发时间为{time_until}")
            else:
                formatted_output.append("状态：正在进行，无下次触发时间")
        elif event_type == "completed":
            formatted_output.append("状态：已完成")
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

    async def format_events_by_ids(self, event_ids: List[str]) -> str:
        """格式化多个事件信息。"""
        if not event_ids:
            return "未找到相关事件"
        formatted_output = []
        for i, event_id in enumerate(event_ids, 1):
            event_str = await self.format_event_by_id(event_id)
            if event_str.startswith("未找到"):
                continue
            formatted_output.append(f"{i}. {event_str}")
            formatted_output.append("")
        return "\n".join(formatted_output).rstrip()

    async def clear_database(self):
        """清空所有事件。"""
        conn = await self.db._ensure_connection()
        await conn.execute("DELETE FROM events")
        await conn.commit()
        print("事件数据库已清空！")

    async def get_ongoing_events(self, limit: int = 10) -> List[str]:
        """获取所有正在进行的事件。"""
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT event_id FROM events WHERE event_type='ongoing' "
            "ORDER BY start_time DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [r["event_id"] for r in rows if r["event_id"]]

    async def get_recent_completed_events(self, limit: int = 3) -> List[str]:
        """获取最近创建的已完成事件。"""
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT event_id FROM events WHERE event_type='completed' "
            "ORDER BY CASE WHEN end_time != '' THEN end_time ELSE start_time END DESC, "
            "start_time DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [r["event_id"] for r in rows if r["event_id"]]

    async def search_events_by_session_name(self, session_name: str = None, default_keyword: str = None) -> List[str]:
        """根据会话名称匹配事件中的 summary 字段。"""
        from config.role_settings import role_config
        search_keyword = session_name if session_name else (default_keyword or role_config.MASTER_NAME)
        if not search_keyword:
            return []

        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT event_id FROM events WHERE summary LIKE ? ORDER BY start_time DESC",
            (f"%{search_keyword}%",),
        )
        rows = await cur.fetchall()
        return [r["event_id"] for r in rows if r["event_id"]]
