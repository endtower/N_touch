# -*- coding: utf-8 -*-
"""
任务操作类（SQLite）

任务是独立表，记录长期任务（active 进行中 / completed 已完结）。
title 为定位键；summary 覆盖式更新；next_occurrence 为下次发生时间。
阶段进展（quest_agent 的拆解与裁决）以概况文本承载，不落结构化列。

概况格式约定（2026-09-18 笔记化）：首行「当前阶段：<一句话>」（全系统读首行即知
道进展到哪一步），之后为分级笔记——每行一条、以「- 」开头、缩进两个空格表示
从属层级。append_task_log 永远在概况末尾追加一条一级笔记（「- 」开头，末尾即
新进的未整理笔记）；格式约定不校验，漂移由 quest agent 重写概况时统一修正
（quest 独占整理权：归级、合并归纳成段、删过时）。
"""

from datetime import datetime
from typing import List, Dict, Optional

from .core import get_memory_db
from .common import generate_timestamp_id
from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")

# 任务状态常量
TASK_STATUS_ACTIVE = "active"
TASK_STATUS_COMPLETED = "completed"

# 已到处理时间的短语前缀（_calculate_time_until 的过期分支产出，
# 调用方据此判断是否叠加「下次发生时间为」前缀——见 format_task_by_id / format_tasks_brief）
_OVERDUE_PREFIX = "已到处理时间"


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

    async def create_task(self, title, summary, next_occurrence=None,
                          status=TASK_STATUS_ACTIVE) -> str:
        """创建任务。"""
        current_time = datetime.now().isoformat()
        task_id = generate_timestamp_id("Task")

        # 任务不落向量（2026-09-19）：任务无语义检索消费方，summary_vector
        # 恒写 NULL、is_vectorized 恒 0，避免无谓的嵌入 API 调用与成本。
        conn = await self.db._ensure_connection()
        await conn.execute(
            """INSERT INTO tasks (task_id, title, status, summary, next_occurrence,
               summary_vector, is_vectorized, start_time, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, title, status, summary, next_occurrence or "",
             None, 0, current_time, current_time),
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

    async def clear_all_tasks(self) -> int:
        """清空任务表（active 与 completed 一并删除），返回删除的行数。

        测试/重置用；执行失败直接上抛，不吞错。
        """
        conn = await self.db._ensure_connection()
        cur = await conn.execute("DELETE FROM tasks")
        await conn.commit()
        logger.info(f"清空任务表: 删除 {cur.rowcount} 条")
        return cur.rowcount

    def _calculate_time_until(self, future_time_str: str) -> str:
        """计算与 next_occurrence 的相对时间描述（完整语义短语）。

        - 未来：如「3天2小时后」（仅时长，调用方叠加「下次发生时间为」前缀）。
        - 已到/已过：如「已到处理时间（超出计划3小时）」——明确告诉 LLM 任务
          已到达下次处理时间（该处理了），并给出与预期计划的时间差距。
        - 无/无效：返回「未知」。
        """
        if not future_time_str:
            return "未知"
        try:
            future_time = datetime.fromisoformat(future_time_str)
        except ValueError:
            return "未知"
        current_time = datetime.now()
        delta = future_time - current_time
        if delta.total_seconds() < 0:
            # 已到达下次处理时间：超出的时长 = 预期时间点距现在的时长
            overdue = _format_duration(-delta.total_seconds())
            return f"{_OVERDUE_PREFIX}（超出计划{overdue}）"
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

    async def append_task_log(self, task_title: str, content: str) -> bool:
        """向任务笔记末尾添加一条一级笔记（记录级写入，quest agent 裁决之外的低权限通道）。

        仅允许 active 任务（已完结任务不可再记录）；定位失败/已完结返回 False
        （业务级失败，由调用方反馈，非系统故障不上抛）。
        概况格式约定不校验：永远追加一行 "- <content>" 到概况末尾——末尾即
        新进的未整理笔记（首行「当前阶段：」约定见模块 docstring），本方法
        零解析、纯字符串拼接；分级整理由 quest agent 重写概况时统一进行。
        """
        if not task_title or not content or not content.strip():
            return False

        task_id = await self.get_task_id_by_title(task_title)
        if not task_id:
            return False
        task = await self.get_task_by_id(task_id)
        if task is None or task.get("status") != TASK_STATUS_ACTIVE:
            return False

        summary = (task.get("summary") or "").rstrip()
        log_line = f"- {content.strip()}"
        new_summary = f"{summary}\n{log_line}" if summary else log_line

        return await self.update_task(task_title, summary=new_summary)

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
        # 概况多行原样透出（首行「当前阶段：」、其后进度总览，约定见模块 docstring）
        formatted_output.append("概况：")
        formatted_output.append(row["summary"] if row["summary"] else "（无）")

        status = row["status"] or ""
        next_occurrence = row["next_occurrence"] or ""

        if status == TASK_STATUS_ACTIVE:
            if next_occurrence:
                time_until = self._calculate_time_until(next_occurrence)
                # 已到处理时间时短语自含完整语义（含「超出计划X」），
                # 不再叠加「下次发生时间为」前缀，避免「下次发生时间为已到…」拗口
                if time_until.startswith(_OVERDUE_PREFIX):
                    formatted_output.append(f"状态：进行中，{time_until}")
                else:
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

    async def format_tasks_brief(self, task_ids: List[str]) -> str:
        """格式化任务简表：每行仅「{title} {相对时间描述}」。

        供 PFCA / 反思的第一轮上下文槽位使用——任务概况全文极易带偏 LLM
        表现，首条只给轻量索引（细节由 query_task_status 按需查询）。
        相对时间复用 _calculate_time_until：
        - 未来：如「{title} 下次处理时间3天2小时后」
        - 已到：如「{title} 已到处理时间（超出计划3小时）」
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
