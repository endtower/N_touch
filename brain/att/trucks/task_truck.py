"""
TaskTruck - 任务到期触发卡车

定期检查任务表中已到期的进行中任务，一旦发现有到期任务，提醒 station
并直接启动 PFCA 处理（不经 proactive 决策层）。

到期判定（2026-10-01 改造，硬化点 A）：
    取 min(下次处理时间 next_occurrence, 最终汇报时间 deadline) 到期即触发——
    避免 quest 把下次处理时间排到最终汇报时间之后，导致任务永不唤醒。

展示信息格式（station 调 get_truck_messages 拿到的第一轮消息，直接作为 PFCA 首条）。
2026-10-01 起不再注入记录全文，只给「任务 / 当前阶段 / 距最终汇报时间还剩 X /
要求推进并汇报」：

    单个到期任务：
    一个任务已到达处理时间。
    任务「<标题>」，当前阶段：<当前阶段>。距最终汇报时间还有<X>。
    请推进该任务到下一阶段，并把成果汇报给 quest 登记，直到 quest 判定达标。

    已过最终汇报时间（收尾口径）：
    任务「<标题>」，当前阶段：<当前阶段>。已超过最终汇报时间<X>。
    请立刻整理已有材料与未达标说明，上报 quest 收尾，不再继续推进。

    多个任务同时到期（一次触发全部展示，交 PFCA 统筹排序处理）：
    N个任务已到达处理时间：
    1. 任务「<任务1>」，当前阶段：<...>。……
    2. 任务「<任务2>」，当前阶段：<...>。……

设计：
- 一次触发展示**全部**到期任务（2026-09-19 决策）：同时到期的任务一起交给 PFCA
  统筹，避免逐个触发带来的延迟，也规避「单任务防循环守卫卡死后续任务」的缺陷。
- 防循环：每个 task_id 记录最后一次已触发的到期键（时间取值），同一次到期不重复触发；
  quest_agent 更新时间后 key 变化，才可能再次触发。
- 断链兜底（2026-10-01，硬化点 B）：key 未变但距上次触发超过
  TaskTruckConfig.RE_TRIGGER_TTL_SECONDS 时强制重新触发——兜底 PFCA 未汇报、
  quest 未被咨询、next_occurrence 不变导致任务静默卡死的场景。
- 关键环节失败（任务拉取失败）直接崩溃阻断，不 try-ex 继续（项目硬约定）。
- 默认关闭（ENABLED=False），由前端 Truck 控制面板启用，避免在真实环境自动触发。
"""

import os
import traceback
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from utils.logger import get_logger
from .base_truck import BaseTruck
from .private_truck import _format_duration  # 复用相对时长描述（x分钟/x小时）

logger = get_logger('task_truck', 'INFO')


def _parse_iso(value: str) -> Optional[datetime]:
    """宽松解析 ISO 时间串；无/无效返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


class TaskTruck(BaseTruck):
    """任务到期触发卡车 - 直接启动 PFCA 处理到期任务"""

    def __init__(self):
        super().__init__("task_truck")
        # 本次触发的到期任务列表（_main_loop 设置，station 调 get_truck_messages 时消费）
        self._triggered_tasks: List[Dict] = []
        # 防循环：task_id -> 该任务最后一次已触发过的「到期键」
        # （next_occurrence|deadline 的当前取值；任一变化即视为新一次到期）。
        # 用 dict 而非集合，保证大小 ≤ 任务数，不随更新次数增长。
        self._triggered_keys: Dict[str, str] = {}
        # 断链兜底：task_id -> 上次触发时刻（TTL 到期后允许重触发）
        self._triggered_at: Dict[str, datetime] = {}

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
                    # 记录本次已触发的 key 与时刻（防循环，消费前置入，
                    # 防 get_truck_messages 前再触发）
                    now = datetime.now()
                    for t in tasks:
                        tid = t.get("task_id")
                        self._triggered_keys[tid] = self._due_key(t)
                        self._triggered_at[tid] = now
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
        """取 min(下次处理时间, 最终汇报时间) 作为到期时间；两者都无效则 None。

        硬化点 A：对最终汇报时间保底，避免下次处理时间被排到截止之后永不唤醒。
        """
        candidates = []
        for field in ("next_occurrence", "deadline"):
            dt = _parse_iso(rec.get(field) or "")
            if dt is not None:
                candidates.append(dt)
        return min(candidates) if candidates else None

    @staticmethod
    def _due_key(rec: Dict) -> str:
        """到期键：两个时间取值的组合；任一变化即视为新一次到期。"""
        return f"{rec.get('next_occurrence') or ''}|{rec.get('deadline') or ''}"

    async def _find_due_tasks(self) -> List[Dict]:
        """找所有已到期的 active 任务（min(下次处理时间, 最终汇报时间) 已到或已过），
        过滤掉同一次到期已触发过的（防循环 + TTL 兜底）。拉取失败直接上抛崩溃阻断。"""
        from brain.hc.database.task import TaskOperations
        from config.truck_settings import task_truck_config

        task_ops = TaskOperations()
        task_ids = await task_ops.get_active_tasks(limit=50)
        if not task_ids:
            return []

        current_time = datetime.now()
        ttl = task_truck_config.RE_TRIGGER_TTL_SECONDS
        due: List[Dict] = []
        for task_id in task_ids:
            rec = await task_ops.get_task_by_id(task_id)
            if not rec:
                continue
            due_time = self._resolve_due_time(rec)
            if due_time is None or current_time < due_time:
                continue
            # 防循环：同一 task_id 的本次到期（时间取值未变）不重复触发
            due_key = self._due_key(rec)
            if self._triggered_keys.get(task_id) == due_key:
                last_at = self._triggered_at.get(task_id)
                if last_at is not None and (current_time - last_at).total_seconds() < ttl:
                    logger.debug(
                        f"任务 {rec.get('title')} 的本次到期已触发过，跳过"
                        f"（等待时间更新，或距上次触发满 TTL 兜底重触发）"
                    )
                    continue
                logger.warning(
                    f"任务 {rec.get('title')} 本次到期已触发但超过 TTL={ttl}s 仍未推进，"
                    f"兜底重新触发（疑似 PFCA 未汇报导致断链）"
                )
            due.append(rec)
        return due

    @staticmethod
    def _wake_block(rec: Dict) -> str:
        """单个到期任务的唤醒文案（2026-10-01 新格式：不再注入记录全文）。"""
        title = (rec.get("title") or "未知任务").strip()
        stage = (rec.get("stage") or "").strip() or "（未标注）"
        deadline_dt = _parse_iso(rec.get("deadline") or "")
        if deadline_dt is None:
            tail = (
                "（最终汇报时间未设置）请推进该任务到下一阶段，"
                "并把成果汇报给 quest 登记，直到 quest 判定达标。"
            )
        else:
            remaining = (deadline_dt - datetime.now()).total_seconds()
            if remaining < 0:
                tail = (
                    f"已超过最终汇报时间{_format_duration(-remaining)}。"
                    "请立刻整理已有材料与未达标说明，上报 quest 收尾，不再继续推进。"
                )
            else:
                tail = (
                    f"距最终汇报时间还有{_format_duration(remaining)}。"
                    "请推进该任务到下一阶段，并把成果汇报给 quest 登记，"
                    "直到 quest 判定达标。"
                )
        return f"任务「{title}」，当前阶段：{stage}。{tail}"

    async def get_truck_messages(self) -> Optional[Dict]:
        """station 调度时调用：把所有到期任务格式化为唤醒文案返回，
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
            f"TaskTruck 返回到期任务消息（{len(tasks)} 个），station 将直接启动 PFCA: "
            f"{[t.get('title') for t in tasks]}"
        )
        return {
            "formatted_message": formatted_message,
            # 任务触发无会话上下文，PFCA 按无 session_id 处理
            "raw_sessions": None,
        }