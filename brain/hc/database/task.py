# -*- coding: utf-8 -*-
"""
任务操作类（SQLite）

任务是独立表，记录长期任务（active 进行中 / completed 已完结）。
title 为定位键；next_occurrence 为下次处理时间；deadline 为最终汇报时间
（硬截止）。

任务记录字段（2026-10-01 改造：summary 单列拆为四个独立语义列）：
- brief（简要总结，前因与目的）：一句话说明这条任务从哪来、要达成什么；
- conclusion（已获取的汇报结论）：当前已拿到、可直接用于汇报的结论；
- stage（当前阶段）：还需要做什么，一句话刚性目标；
- notes（任务笔记）：低权限记录通道，其他 agent 经 append_task_log 追加，
  quest agent 推进时整列清理并填入一条简洁新笔记（quest 独占整理权）。

两个时间：
- next_occurrence（下次处理时间）：唤起频率，quest 每次被咨询时写死，
  想催紧就排近；达标即不再设置（配合置完结自动停触发）。
- deadline（最终汇报时间）：硬截止，到点仍未达标则由 quest 出「未达标说明 +
  已有材料」上报收尾（卡车触发对 deadline 保底：取两者较早者）。
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

    async def create_task(self, title, brief, conclusion="", stage="", notes="",
                          next_occurrence=None, deadline=None,
                          status=TASK_STATUS_ACTIVE) -> str:
        """创建任务。

        brief 为简要总结（前因与目的）；stage 为当前阶段（还需要做什么）；
        conclusion 为已获取的汇报结论；notes 为任务笔记。
        deadline 为最终汇报时间（硬截止，ISO 8601；缺省空串＝未设置）。
        """
        current_time = datetime.now().isoformat()
        task_id = generate_timestamp_id("Task")

        # 任务不落向量（2026-09-19）：任务无语义检索消费方。
        conn = await self.db._ensure_connection()
        await conn.execute(
            """INSERT INTO tasks (task_id, title, status, brief, conclusion,
               stage, notes, next_occurrence, deadline, start_time, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, title, status, brief, conclusion, stage, notes,
             next_occurrence or "", deadline or "", current_time, current_time),
        )
        await conn.commit()
        return task_id

    async def update_task(self, title, preserve_updated_at: bool = False, **kwargs) -> bool:
        """更新任务（通过 title 定位）。status 置为 completed 时自动写 end_time。

        kwargs 可含 brief / conclusion / stage / notes / next_occurrence /
        deadline / status 等。
        """
        task_id = await self.get_task_id_by_title(title)
        if not task_id:
            return False

        current_time = datetime.now().isoformat()
        allowed_fields = ['title', 'status', 'brief', 'conclusion', 'stage',
                          'notes', 'next_occurrence', 'deadline',
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

    def _calculate_time_until(self, future_time_str: str, on_time_label: str = "处理时间") -> str:
        """计算与某时间点的相对时间描述（完整语义短语）。

        - 未来：如「3天2小时后」（仅时长，调用方叠加前缀）。
        - 已到/已过：如「已到处理时间（超出计划3小时）」/「已到最终汇报时间（超出计划3小时）」，
          明确告诉 LLM 该时间点已到达，并给出与预期计划的时间差距。
        - 无/无效：返回「未知」。

        on_time_label 决定过期分支的措辞（下次处理时间 / 最终汇报时间）。
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
            # 已到达该时间点：超出的时长 = 预期时间点距现在的时长
            overdue = _format_duration(-delta.total_seconds())
            return f"{_OVERDUE_PREFIX}{on_time_label}（超出计划{overdue}）"
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
        """向任务笔记（notes 列）末尾添加一条记录（记录级写入，quest agent 裁决之外的低权限通道）。

        仅允许 active 任务（已完结任务不可再记录）；定位失败/已完结返回 False
        （业务级失败，由调用方反馈，非系统故障不上抛）。
        永远在 notes 末尾追加一行 "- <content>"——本方法零解析、纯字符串拼接；
        整理（清空旧笔记并填入一条简洁新笔记）由 quest agent 推进时统一进行。
        """
        if not task_title or not content or not content.strip():
            return False

        task_id = await self.get_task_id_by_title(task_title)
        if not task_id:
            return False
        task = await self.get_task_by_id(task_id)
        if task is None or task.get("status") != TASK_STATUS_ACTIVE:
            return False

        notes = (task.get("notes") or "").rstrip()
        log_line = f"- {content.strip()}"
        new_notes = f"{notes}\n{log_line}" if notes else log_line

        return await self.update_task(task_title, notes=new_notes)

    async def get_task_by_id(self, task_id: str) -> Optional[Dict]:
        """获取单个任务的原始数据字典。"""
        if not task_id:
            return None
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT task_id, title, brief, conclusion, stage, notes, status, "
            "next_occurrence, deadline, start_time, end_time FROM tasks "
            "WHERE task_id=?",
            (task_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "task_id": row["task_id"],
            "title": row["title"],
            "brief": row["brief"],
            "conclusion": row["conclusion"],
            "stage": row["stage"],
            "notes": row["notes"],
            "status": row["status"],
            "next_occurrence": row["next_occurrence"],
            "deadline": row["deadline"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
        }

    async def format_task_by_id(self, task_id: str) -> str:
        """格式化单个任务信息。"""
        if not task_id:
            return "未指定任务ID"

        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT task_id, title, brief, conclusion, stage, notes, status, "
            "next_occurrence, deadline, start_time, end_time FROM tasks "
            "WHERE task_id=?",
            (task_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return f"未找到任务ID为「{task_id}」的任务"

        formatted_output = []
        formatted_output.append(f"名称：{row['title'] or '未知标题'}")
        formatted_output.append(f"简要：{row['brief'] or '（无）'}")
        formatted_output.append(f"当前阶段：{row['stage'] or '（无）'}")
        formatted_output.append(f"已获结论：{row['conclusion'] or '（无）'}")
        formatted_output.append("任务笔记：")
        formatted_output.append(row["notes"] if row["notes"] else "（无）")

        status = row["status"] or ""
        next_occurrence = row["next_occurrence"] or ""
        deadline = row["deadline"] or ""

        if status == TASK_STATUS_ACTIVE:
            if next_occurrence:
                time_until = self._calculate_time_until(next_occurrence)
                # 已到处理时间时短语自含完整语义（含「超出计划X」），
                # 不再叠加「下次处理时间为」前缀，避免「下次处理时间为已到…」拗口
                if time_until.startswith(_OVERDUE_PREFIX):
                    formatted_output.append(f"状态：进行中，{time_until}")
                else:
                    formatted_output.append(f"状态：进行中，下次处理时间为{time_until}")
            else:
                formatted_output.append("状态：进行中，未设置下次处理时间")
            # 最终汇报时间（硬截止，2026-10-01 新增）
            if deadline:
                deadline_until = self._calculate_time_until(
                    deadline, on_time_label="最终汇报时间"
                )
                if deadline_until.startswith(_OVERDUE_PREFIX):
                    formatted_output.append(deadline_until)
                else:
                    formatted_output.append(f"距最终汇报时间：{deadline_until}")
            else:
                formatted_output.append("最终汇报时间：未设置")
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
