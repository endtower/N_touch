# -*- coding: utf-8 -*-
"""接续事项的 SQLite 记录操作。

字段收敛为：title（标题）、context（要传递给下次启动时的上下文）、
next_occurrence（下次唤醒时间）。status 仅 active / expired。

语义（2026-10-11 收敛）：任务一次性——到点被 task_truck 交付 PFCA 后即自动置
expired，不再回写；如需接续，由 PFCA 经 quest_agent 重新登记一条新任务。
deadline / stage / brief / conclusion / notes 等旧字段与更新语义一并移除。
"""

from datetime import datetime
from typing import List, Dict, Optional

from .core import get_memory_db
from .common import generate_timestamp_id
from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")

# 任务状态常量
TASK_STATUS_ACTIVE = "active"
TASK_STATUS_EXPIRED = "expired"

# 已到处理时间的短语前缀（_calculate_time_until 的过期分支产出，
# 调用方据此判断是否叠加「下次处理时间为」前缀——见 format_task_by_id / format_tasks_brief）
_OVERDUE_PREFIX = "已到"


def _format_duration(seconds: float) -> str:
    """把秒数格式化为时长描述（x年/x个月/x天/x小时/x分钟/x秒）。

    与 private_truck._format_duration 同构；此处独立保留（hc 层不反向依赖 att 层）。
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}小时"
    days = hours // 24
    if days < 30:
        return f"{days}天"
    months = days // 30
    if months < 12:
        return f"{months}个月"
    return f"{months // 12}年"


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

    async def create_task(self, title: str, context: str, next_occurrence: str = "",
                          status: str = TASK_STATUS_ACTIVE) -> str:
        """创建任务。

        title 为标题；context 为要传递给下次启动时的上下文（目的、进展、线索等）；
        next_occurrence 为下次唤醒时间（ISO 8601）。
        """
        current_time = datetime.now().isoformat()
        task_id = generate_timestamp_id("Task")

        # 任务不落向量（2026-09-19）：任务无语义检索消费方。
        conn = await self.db._ensure_connection()
        await conn.execute(
            """INSERT INTO tasks (task_id, title, context, status,
               next_occurrence, start_time, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_id, title, context, status,
             next_occurrence or "", current_time, current_time),
        )
        await conn.commit()
        return task_id

    async def expire_task(self, task_id: str) -> bool:
        """把任务置为 expired 并写 end_time（触发即过期）。

        执行失败直接上抛，不吞错。
        """
        if not task_id:
            return False
        current_time = datetime.now().isoformat()
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "UPDATE tasks SET status=?, end_time=?, updated_at=? WHERE task_id=?",
            (TASK_STATUS_EXPIRED, current_time, current_time, task_id),
        )
        await conn.commit()
        return cur.rowcount > 0

    async def clear_all_tasks(self) -> int:
        """清空任务表，返回删除的行数。

        测试/重置用；执行失败直接上抛，不吞错。
        """
        conn = await self.db._ensure_connection()
        cur = await conn.execute("DELETE FROM tasks")
        await conn.commit()
        logger.info(f"清空任务表: 删除 {cur.rowcount} 条")
        return cur.rowcount

    def _calculate_time_until(self, future_time_str: str) -> str:
        """计算与下次处理时间的相对时间描述（完整语义短语）。

        - 未来：如「3天2小时后」。
        - 已到/已过：如「已到处理时间（超出计划3小时）」。
        - 无/无效：返回「未知」。
        """
        if not future_time_str:
            return "未知"
        try:
            future_time = datetime.fromisoformat(future_time_str)
            if future_time.tzinfo is not None:
                future_time = future_time.astimezone().replace(tzinfo=None)
        except ValueError:
            return "未知"
        current_time = datetime.now()
        delta = future_time - current_time
        if delta.total_seconds() < 0:
            overdue = _format_duration(-delta.total_seconds())
            return f"{_OVERDUE_PREFIX}处理时间（超出计划{overdue}）"
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
            "SELECT task_id, title, context, status, next_occurrence, "
            "start_time, end_time, updated_at FROM tasks WHERE task_id=?",
            (task_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "task_id": row["task_id"],
            "title": row["title"],
            "context": row["context"],
            "status": row["status"],
            "next_occurrence": row["next_occurrence"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "updated_at": row["updated_at"],
        }

    async def format_task_by_id(self, task_id: str) -> str:
        """格式化单个任务信息。"""
        if not task_id:
            return "未指定任务ID"

        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT task_id, title, context, status, next_occurrence, "
            "start_time, end_time FROM tasks WHERE task_id=?",
            (task_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return f"未找到任务ID为「{task_id}」的任务"

        formatted_output = []
        formatted_output.append(f"名称：{row['title'] or '未知标题'}")
        formatted_output.append(f"上下文：{row['context'] or '（无）'}")

        status = row["status"] or ""
        next_occurrence = row["next_occurrence"] or ""

        if status == TASK_STATUS_ACTIVE:
            if next_occurrence:
                time_until = self._calculate_time_until(next_occurrence)
                if time_until.startswith(_OVERDUE_PREFIX):
                    formatted_output.append(f"状态：进行中，{time_until}")
                else:
                    formatted_output.append(f"状态：进行中，下次处理时间为{time_until}")
            else:
                formatted_output.append("状态：进行中，未设置下次处理时间")
        elif status == TASK_STATUS_EXPIRED:
            formatted_output.append("状态：已过期（触发后自动结束，如需继续须重新登记）")
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

    async def format_tasks_brief(self, task_ids: List[str]) -> str:
        """格式化任务简表：每行仅「{title} {相对时间描述}」。

        供 PFCA / 反思的第一轮上下文槽位使用——任务概况全文极易带偏 LLM
        表现，首条只给轻量索引（细节由 query_task_status 按需查询）。
        无 active 任务传入 / 全部失效时返回空串（调用方据此跳过槽位）。
        """
        if not task_ids:
            return ""
        conn = await self.db._ensure_connection()
        lines = []
        for task_id in task_ids:
            cur = await conn.execute(
                "SELECT title, status, next_occurrence FROM tasks WHERE task_id=?",
                (task_id,),
            )
            row = await cur.fetchone()
            if row is None or row["status"] != TASK_STATUS_ACTIVE:
                continue
            title = row["title"] or "未知标题"
            time_until = self._calculate_time_until(row["next_occurrence"] or "")
            if time_until.startswith(_OVERDUE_PREFIX):
                lines.append(f"{title} {time_until}")
            else:
                lines.append(f"{title} 下次处理时间{time_until}")
        return "\n".join(lines)

    async def format_active_task_overview(self, limit: int = 5) -> str:
        """有界接续目录：总数与最近唤醒安排，附上下文摘要。"""
        limit = max(1, min(int(limit), 20))
        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT COUNT(*) AS total FROM tasks WHERE status='active'")
        total = (await cur.fetchone())["total"]
        if not total:
            return ""
        cur = await conn.execute(
            "SELECT title, context, next_occurrence FROM tasks WHERE status='active' "
            "ORDER BY CASE WHEN next_occurrence='' OR next_occurrence IS NULL THEN 1 ELSE 0 END, "
            "next_occurrence ASC, start_time DESC LIMIT ?", (limit,),
        )
        rows = await cur.fetchall()
        lines = [f"待接续事项共 {total} 条，以下为最近的安排："]
        for row in rows:
            title = (row["title"] or "未知标题").replace("\n", " ")
            context = (row["context"] or "待查询").replace("\n", " ")
            context = context[:120] + ("…" if len(context) > 120 else "")
            when = row["next_occurrence"] or ""
            timing = self._calculate_time_until(when) if when else "未设置唤醒时间"
            lines.append(f"· {title}：{context}；{timing}")
        if total > len(rows):
            lines.append(f"另有 {total - len(rows)} 条未展示，可按标题查询。")
        lines.append("目录仅提示已有安排，不要求现在处理所有事项；细节用 query_task_status 按标题查询。")
        return "\n".join(lines)

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
