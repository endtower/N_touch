"""
TaskTruck - 任务到期触发卡车

定期检查任务表中已到达「预计的再次处理时间」（next_occurrence）的进行中任务，
一旦发现有到期任务，提醒 station 并直接启动 PFCA 处理（不经 proactive 决策层）。

展示信息格式（station 调 get_truck_messages 拿到的第一轮消息，直接作为 PFCA 首条）：

    单个到期任务：
    一个任务已到达预计的再次处理时间。（预计触发事件<任务标题>，距离现在<X分钟/小时>前）
    任务内容：
    <format_task_by_id 通用任务展示格式>

    多个任务同时到期（一次触发全部展示，交 PFCA 统筹排序处理）：
    N个任务已到达预计的再次处理时间：
    （预计触发事件<任务1>，距离现在<X>前）
    （预计触发事件<任务2>，距离现在<Y>前）
    任务内容：
    1. <format_task_by_id(任务1)>

    2. <format_task_by_id(任务2)>

设计：
- 一次触发展示**全部**到期任务（2026-09-19 决策）：同时到期的任务一起交给 PFCA
  统筹，避免逐个触发带来的延迟，也规避「单任务防循环守卫卡死后续任务」的缺陷。
- 防循环：每个 task_id 记录最后一次已触发的 next_occurrence，同一次到期不重复触发；
  quest_agent 更新 next_occurrence（通常推到未来）后 key 变化，才可能再次触发。
- 关键环节失败（任务拉取失败）直接崩溃阻断，不 try-ex 继续（项目硬约定）。
- 默认关闭（ENABLED=False），由前端 Truck 控制面板启用，避免在真实环境自动触发。
"""

import os
import traceback
from datetime import datetime
from typing import Dict, List, Optional

from utils.logger import get_logger
from .base_truck import BaseTruck
from .private_truck import _format_duration  # 复用相对时长描述（x分钟/x小时）

logger = get_logger('task_truck', 'INFO')


class TaskTruck(BaseTruck):
    """任务到期触发卡车 - 直接启动 PFCA 处理到期任务"""

    def __init__(self):
        super().__init__("task_truck")
        # 本次触发的到期任务列表（_main_loop 设置，station 调 get_truck_messages 时消费）
        self._triggered_tasks: List[Dict] = []
        # 防循环：task_id -> 该任务最后一次已触发过的 next_occurrence。
        # 同一次到期不重复触发；next_occurrence 被 quest_agent 更新后（key 变化）才可能再触发。
        # 用 dict 而非集合，保证大小 ≤ 任务数，不随更新次数增长。
        self._triggered_keys: Dict[str, str] = {}

    async def cycle_control(self):
        from config.truck_settings import task_truck_config
        await super().cycle_control(task_truck_config.CYCLE_TIME)

    async def _main_loop(self):
        """主循环 - 周期检查到期任务，有则 remind_station。

        启动第一瞬间先检测一次（对齐 ongoing/private truck 的「先检测后等待」），
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
                    # 记录本次已触发的 key（防循环，消费前置入，防 get_truck_messages 前再触发）
                    for t in tasks:
                        self._triggered_keys[t.get("task_id")] = t.get("next_occurrence") or ""
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

    async def _find_due_tasks(self) -> List[Dict]:
        """找所有已到期的 active 任务（next_occurrence 已到或已过），
        过滤掉同一次到期已触发过的（防循环）。拉取失败直接上抛崩溃阻断。"""
        from brain.hc.database.task import TaskOperations

        task_ops = TaskOperations()
        task_ids = await task_ops.get_active_tasks(limit=50)
        if not task_ids:
            return []

        current_time = datetime.now()
        due: List[Dict] = []
        for task_id in task_ids:
            rec = await task_ops.get_task_by_id(task_id)
            if not rec:
                continue
            next_occurrence = rec.get("next_occurrence") or ""
            if not next_occurrence:
                continue
            try:
                next_time = datetime.fromisoformat(next_occurrence.replace('Z', '+00:00'))
            except ValueError:
                continue
            # 只触发已到或超过触发时间的任务
            if current_time < next_time:
                continue
            # 防循环：同一 task_id 的本次到期（next_occurrence 未变）不重复触发
            if self._triggered_keys.get(task_id) == next_occurrence:
                logger.debug(
                    f"任务 {rec.get('title')} 的本次到期已触发过，跳过（等待 next_occurrence 更新）"
                )
                continue
            due.append(rec)
        return due

    async def get_truck_messages(self) -> Optional[Dict]:
        """station 调度时调用：把所有到期任务格式化为指定展示格式返回，
        station 据此直接启动 PFCA（不经 proactive 决策层）。

        Returns:
            dict: {"formatted_message": ..., "raw_sessions": None}
            None: 无到期任务
        """
        tasks = self._triggered_tasks
        self._triggered_tasks = []
        if not tasks:
            # 兜底：未被 _main_loop 预置时现场查一次
            tasks = await self._find_due_tasks()
            if not tasks:
                return None

        from brain.hc.database.task import TaskOperations

        task_ops = TaskOperations()

        def _head_line(t: Dict) -> str:
            """头部行：预计触发事件<标题>，距离现在<时长>前。"""
            title = (t.get("title") or "未知任务").strip()
            overdue = ""
            next_occurrence = t.get("next_occurrence") or ""
            if next_occurrence:
                try:
                    next_time = datetime.fromisoformat(next_occurrence.replace('Z', '+00:00'))
                    overdue_seconds = (datetime.now() - next_time).total_seconds()
                    overdue = f"，距离现在{_format_duration(overdue_seconds)}前"
                except ValueError:
                    overdue = ""
            return f"（预计触发事件{title}{overdue}）"

        if len(tasks) == 1:
            head = f"一个任务已到达预计的再次处理时间。{_head_line(tasks[0])}"
            content = await task_ops.format_task_by_id(tasks[0].get("task_id") or "")
            formatted_message = f"{head}\n任务内容：\n{content}"
        else:
            heads = "\n".join(_head_line(t) for t in tasks)
            blocks = []
            for i, t in enumerate(tasks, 1):
                content = await task_ops.format_task_by_id(t.get("task_id") or "")
                blocks.append(f"{i}. {content}")
            formatted_message = (
                f"{len(tasks)}个任务已到达预计的再次处理时间：\n{heads}\n"
                f"任务内容：\n{'\n\n'.join(blocks)}"
            )

        logger.info(
            f"TaskTruck 返回到期任务消息（{len(tasks)} 个），station 将直接启动 PFCA: "
            f"{[t.get('title') for t in tasks]}"
        )
        return {
            "formatted_message": formatted_message,
            # 任务触发无会话上下文，PFCA 按无 session_id 处理
            "raw_sessions": None,
        }
