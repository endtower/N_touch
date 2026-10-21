"""任务到期接续卡车。

到期时启动 PFCA，交付该事项的标题与上下文（上下文即传递下次启动时要用的信息）。
任务一次性：truck 交付 PFCA 后即自动将任务标记为 expired，续办由 PFCA 经 quest_agent 重新登记。
到期只依据 next_occurrence，不再持有 deadline 与 TTL 重触发兜底。
持续任务（kind=ongoing）没有时间点、永不到期，不在此处处理，由 PRE 规则层巡检。
卡车开关保持原配置，由用户在控制面板启用。
"""

import os
import traceback
from datetime import datetime
from typing import Dict, List, Optional

from utils.logger import get_logger
from .base_truck import BaseTruck

logger = get_logger('task_truck', 'INFO')


def _parse_iso(value: str) -> Optional[datetime]:
    """宽松解析 ISO 时间串；无/无效返回 None。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    except ValueError:
        return None


class TaskTruck(BaseTruck):
    """任务到期触发卡车 - 直接启动 PFCA 处理到期任务"""

    def __init__(self):
        super().__init__("task_truck")
        # 本次触发的到期任务列表（_main_loop 设置，station 调 get_truck_messages 时消费）
        self._triggered_tasks: List[Dict] = []

    async def cycle_control(self):
        from config.truck_settings import task_truck_config
        await super().cycle_control(task_truck_config.CYCLE_TIME)

    async def _main_loop(self):
        """主循环 - 周期检查到期任务，有则 remind_station。

        启动第一瞬间先检测一次（对齐 private truck 的「先检测后等待」），
        再进入周期轮询，避免启用后白等满一个周期才首次检查。
        """
        from config.truck_settings import task_truck_config

        logger.info(f"TaskTruck 已启动, check_interval={task_truck_config.CYCLE_TIME}s")

        while self.state != "stopped":
            try:
                # 先检测再等待：启动即检 + 每周期一检
                tasks = await self._find_due_tasks()
                if tasks:
                    self._triggered_tasks = tasks
                    logger.info(
                        f"检测到 {len(tasks)} 个到期任务: "
                        f"{[t.get('title') for t in tasks]}"
                    )
                    await self.remind_station()
                await self.cycle_control()
            except Exception as e:
                # 硬约定：关键环节失败不允许继续运行，直接崩溃阻断
                # （CancelledError 继承自 BaseException，不会进入此处，stop() 不受影响）
                logger.critical(
                    f"TaskTruck 主循环发生致命错误，强制退出进程: "
                    f"{e}\n{traceback.format_exc()}"
                )
                for h in getattr(logger, 'handlers', []):
                    try:
                        h.flush()
                    except Exception:
                        pass
                os._exit(1)

    @staticmethod
    def _resolve_due_time(rec: Dict) -> Optional[datetime]:
        """取「下次处理时间」(next_occurrence) 作为到期时间；无效则 None。"""
        return _parse_iso(rec.get("next_occurrence") or "")

    async def _find_due_tasks(self) -> List[Dict]:
        """找所有已到期的 active 一次性任务（next_occurrence 已到或已过）。

        持续任务（kind=ongoing）没有时间点、永不到期，由 PRE 规则层巡检，此处不处理。
        拉取失败直接上抛崩溃阻断（按 user_rules，不 try-ex 静默跳过）。"""
        from brain.hc.database.task import TASK_KIND_ONETIME, TaskOperations

        task_ops = TaskOperations()
        task_ids = await task_ops.get_active_tasks(limit=50, kind=TASK_KIND_ONETIME)
        if not task_ids:
            return []

        current_time = datetime.now()
        due: List[Dict] = []
        for task_id in task_ids:
            rec = await task_ops.get_task_by_id(task_id)
            if not rec or rec.get("status") != "active":
                continue
            due_time = self._resolve_due_time(rec)
            if due_time is None or current_time < due_time:
                continue
            due.append(rec)
        return due

    @staticmethod
    def _wake_block(rec: Dict) -> str:
        """交付单个到期事项的接续信息（标题 + 传递上下文）。"""
        title = (rec.get("title") or "未知任务").strip()
        context = (rec.get("context") or "").strip() or "（无）"
        return (
            f"事项「{title}」已到处理时间。\n"
            f"上下文：{context}\n"
            "请核实相关会话与当前情况，再决定行动。"
            "没人回应不等于无事可做：不得以「没等到回复」为由静默结束，"
            "至少要做一件对外可见的推进——提醒相关的人、在相关群聊露面说句话或发起话题、"
            "换个对象或渠道去问；只有目的已达成或对方明确终止才可收尾。"
            "如需继续，经 quest_agent 重新登记新的接续事项（上下文里写明到点无人回应时的兜底动作）；"
            "本事项交付后即告结束。"
        )

    async def get_truck_messages(self) -> Optional[Dict]:
        """station 调度时调用：把所有到期任务格式化为唤醒文案返回，
        station 据此直接启动 PFCA（不经 proactive 决策层）。

        Returns:
            dict: {"formatted_message": ..., "raw_sessions": None}
            None: 无到期任务
        """
        # Cached notifications may predate a Quest reschedule. Re-read before delivery.
        self._triggered_tasks = []
        tasks = await self._find_due_tasks()
        if not tasks:
            return None

        from brain.hc.database.task import TaskOperations
        task_ops = TaskOperations()
        claimed = []
        for task in tasks:
            if await task_ops.expire_task(task["task_id"], expected_updated_at=task.get("updated_at"),
                                          expected_next_occurrence=task["next_occurrence"]):
                claimed.append(task)
        tasks = claimed
        if not tasks:
            return None

        if len(tasks) == 1:
            formatted_message = (
                "一个任务已到达处理时间。\n" + self._wake_block(tasks[0])
            )
        else:
            blocks = "\n".join(
                f"{i}. {self._wake_block(t)}" for i, t in enumerate(tasks, 1)
            )
            formatted_message = (
                f"{len(tasks)}个任务已到达处理时间：\n{blocks}"
            )

        logger.info(
            f"TaskTruck 返回到期任务消息（{len(tasks)} 个，已置过期），"
            f"station 将直接启动 PFCA: {[t.get('title') for t in tasks]}"
        )
        return {
            "formatted_message": formatted_message,
            # 任务触发无会话上下文，PFCA 按无 session_id 处理
            "raw_sessions": None,
        }
