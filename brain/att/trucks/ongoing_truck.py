"""
OngoingTruck - 进行中任务卡车

功能：
- 定期检查进行中的未完成任务
- 根据任务的下次触发时间判断是否应该提醒
- 触发后投递到 proactive core（att -> proactive -> pfca -> conversation）
- 与 station 进行通信

存储格式（任务表）：
- status = 'active' 表示进行中的任务
- next_occurrence 存储下次触发时间（ISO 8601格式）
"""

import asyncio
import json
from typing import Optional, Dict, Any
import time
from datetime import datetime
from utils.logger import get_logger
from utils.chain_context import get_current_chain_id
from .base_truck import BaseTruck

logger = get_logger(__name__, "INFO")


class OngoingTruck(BaseTruck):
    """进行中任务卡车 - 基于任务表"""

    def __init__(self):
        super().__init__("ongoing_truck")

    def start(self):
        logger.debug(f"启动方法被调用，当前状态: {self.state}")
        if self.state == "stopped":
            logger.info("OngoingTruck 已启动")
            self.state = "running"
            self.pause_event.set()
            self._task = asyncio.create_task(self._main_loop())
            logger.debug("OngoingTruck启动完成")
        elif self.state == "paused":
            logger.debug("从暂停状态恢复运行")
            self.state = "running"
            self.pause_event.set()
            logger.info(f"OngoingTruck已恢复运行")
            logger.debug("OngoingTruck恢复完成")
        else:
            logger.debug(f"OngoingTruck已经在运行中，当前状态: {self.state}")

    def pause(self):
        logger.debug(f"暂停方法被调用，当前状态: {self.state}")
        if self.state == "running":
            logger.debug("开始暂停OngoingTruck")
            self.state = "paused"
            self.pause_time = time.time()
            self.pause_event.clear()
            logger.info(f"OngoingTruck已暂停")
            logger.debug("OngoingTruck暂停完成")
        else:
            logger.debug(f"OngoingTruck当前状态: {self.state}，无法暂停")

    def stop(self):
        logger.debug(f"停止方法被调用，当前状态: {self.state}")
        logger.debug("开始停止OngoingTruck")
        self.state = "stopped"
        self.pause_event.set()
        if self._task:
            self._task.cancel()
        logger.debug("OngoingTruck停止完成")

    async def _main_loop(self):
        logger.debug("主循环开始")
        while self.state != "stopped":
            logger.debug(f"主循环迭代开始，当前状态: {self.state}")

            triggered_tasks = await self.get_triggered_tasks()

            if triggered_tasks:
                logger.info(f"检测到{len(triggered_tasks)}个触发任务，准备提醒station")
                await self.remind_station()
                logger.debug("提醒station完成")
            else:
                logger.debug("进行中任务检查完成，无触发任务")

            await self.cycle_control()
            logger.debug("循环控制执行完成")

        logger.debug("主循环结束")

    async def cycle_control(self):
        from config.truck_settings import ongoing_truck_config
        total_sleep_time = ongoing_truck_config.CYCLE_TIME
        logger.debug(f"开始循环控制，总睡眠时间: {total_sleep_time}秒")
        await super().cycle_control(total_sleep_time)
        logger.debug("循环控制完成")

    async def get_triggered_tasks(self):
        """获取需要触发的任务 - 仅触发已到或超过触发时间的最早任务

        拉取失败直接上抛崩溃阻断（按 user_rules，不 try-ex 静默跳过）。"""
        from brain.hc.database.task import TaskOperations
        task_ops = TaskOperations()
        task_ids = await task_ops.get_active_tasks(limit=50)

        if not task_ids:
            return []

        current_time = datetime.now()
        triggered_task = None
        earliest_time = None

        for task_id in task_ids:
            task = await self._get_task(task_ops, task_id)
            if not task:
                continue

            # 触发时间取「下次处理时间」。
            due_time = self._resolve_due_time(task)
            if due_time is None:
                continue

            # 只触发已到或超过触发时间的任务
            if current_time >= due_time:
                if triggered_task is None or due_time < earliest_time:
                    triggered_task = task
                    earliest_time = due_time

        if triggered_task:
            logger.info(
                f"任务触发: {triggered_task.get('title')} "
                f"(next_occurrence: {triggered_task.get('next_occurrence')})"
            )
            # 触发即过期：任务一次性，交付后不再保留；续办由 PFCA 经 quest_agent 重新登记。
            # 不置过期会因 next_occurrence 已在过去而每轮重复触发。
            await task_ops.expire_task(triggered_task["task_id"])
            return [triggered_task]

        logger.debug("进行中任务检查完成，无触发任务")
        return []

    @staticmethod
    def _resolve_due_time(task: dict):
        """解析任务的触发时间（next_occurrence）。

        非 ISO 8601 的值（如空串）视为未设置，返回 None。
        """
        raw = task.get("next_occurrence")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return None

    async def _get_task(self, task_ops, task_id):
        """获取单个任务详情

        获取失败直接上抛崩溃阻断（按 user_rules，不 try-ex 静默跳过）。"""
        rec = await task_ops.get_task_by_id(task_id)
        if not rec:
            return None
        return {
            "task_id": rec.get("task_id"),
            "title": rec.get("title"),
            "context": rec.get("context"),
            "next_occurrence": rec.get("next_occurrence"),
            "start_time": rec.get("start_time"),
        }

    async def get_truck_messages(self):
        """获取 truck 消息

        设计意图：
        - 校验阶段：get_triggered_tasks() 拿到任务
        - 决策阶段：把 task 投递到 proactive_core 并 await session 跑完
          （proactive_core.feed_proactive_task 现在会阻塞到 session 退出才返回）
        - 路由阶段：根据 proactive 的 decision_result 决定返回什么
          - 调 launch_pfca → 返回完整消息, 让 station 调 PFCA
          - 调 skip_proactive / 没决策 → 返回 None, station 直接跳过

        station 拿到返回值后:
          - 返回 None → 不调 PFCA, 直接恢复 truck
          - 返回 dict  → 调 PFCA, 完成后恢复 truck
        """
        logger.debug("开始获取truck任务")
        triggered_tasks = await self.get_triggered_tasks()
        logger.debug(f"获取任务完成，触发任务数: {len(triggered_tasks)}")

        if not triggered_tasks:
            logger.debug("没有触发任务，返回None")
            return None

        triggered = triggered_tasks[0]

        # === 把触发任务投递到 proactive_core 并等它决策 ===
        decision_result: Optional[Dict[str, Any]] = None
        try:
            from brain.att.proactive.proactive_core import get_proactive_core
            core = get_proactive_core()
            # 若 core 没启动, 自动 start 一次
            if not core.is_running:
                await core.start()
                logger.info("proactive core 启动 (lazy)")
            task_draft = {
                "intent": "ongoing_task",
                "reason": f"ongoing_truck 检测到任务 '{triggered.get('title')}' 到达 next_occurrence",
                "wakeup_reason": "存在未完成的任务，请处理！",
                "target_session_id": None,
                # station 出队即生成的任务链 ID（contextvar 不会传播到
                # proactive_core 主循环任务，需显式携带，主循环内重新 set）
                "chain_id": get_current_chain_id(),
                "payload": {
                    "task": triggered,
                    "source": "ongoing_truck",
                },
            }
            # 这一步会阻塞到 proactive_session 退出
            await core.feed_proactive_task(task_draft)
            # 拿到 session 的决策
            decision_result = task_draft.get("_decision_result")
            logger.info(
                f"ongoing_truck 拿到 proactive 决策: "
                f"{decision_result.get('tool_name') if decision_result else 'None'}"
            )
        except Exception as e:
            logger.error(f"ongoing_truck 投递 / 等待 proactive 失败: {e}")
            # proactive 链路崩了: 视为"跳过", 让 station 不要调 PFCA
            return None

        # === 根据决策结果决定是否调 PFCA ===
        if decision_result is None:
            logger.info("proactive 未给出明确决策（异常退出/超时），跳过 PFCA")
            return None
        if decision_result.get("skipped"):
            logger.info(f"proactive 主动跳过本次任务: {decision_result.get('tool_result')}")
            return None
        if decision_result.get("tool_name") != "launch_pfca":
            logger.info(
                f"proactive 决策为 {decision_result.get('tool_name')!r}, "
                f"非 launch_pfca, 跳过 PFCA"
            )
            return None

        # === proactive 决定调 launch_pfca: 返回消息让 station 调 PFCA ===
        formatted_message = await self.format_ongoing_tasks(triggered_tasks)

        # 把 proactive 决策里的 task_info / target_session_id 合并进
        # formatted_message：这是 PFCA 工厂收的 user content。
        # （proactive 自身不再调 PFCA，由 station 启动事件链并调 PFCA）
        tool_result = decision_result.get("tool_result")
        if isinstance(tool_result, str) and tool_result.strip():
            try:
                queued = json.loads(tool_result)
                if isinstance(queued, dict) and queued.get("status") == "queued":
                    task_info = (queued.get("task_info") or "").strip()
                    target_session_id = (queued.get("target_session_id") or "").strip()
                    if task_info:
                        formatted_message = (
                            f"{formatted_message}\n\n"
                            f"【proactive 决策】\n{task_info}"
                        )
                    if target_session_id:
                        formatted_message = (
                            f"{formatted_message}\n\n"
                            f"目标会话: {target_session_id}"
                        )
                    logger.info(
                        "proactive 决策 launch_pfca: 合并 task_info / "
                        "target_session_id 到 formatted_message, station 将调 PFCA"
                    )
                else:
                    logger.info(
                        "proactive 决策 launch_pfca 但 tool_result 非 queued payload, "
                        "station 仅按任务格式化消息调 PFCA"
                    )
            except (ValueError, TypeError):
                # 兼容老格式（直接执行 PFCA 时的 "PFCA 处理成功=..."）
                logger.info(
                    "proactive tool_result 非 JSON, 按老格式处理; station 将调 PFCA"
                )
        logger.info("proactive 决策 launch_pfca, station 将调 PFCA")

        return {
            "formatted_message": formatted_message,
            "raw_sessions": triggered_tasks,
            "decision": decision_result,
        }

    async def format_ongoing_tasks(self, tasks):
        """格式化任务信息 - 直接用内存中的任务快照（任务触发后 DB 已置 expired）。"""
        if not tasks:
            return "暂无进行中的任务"

        lines = ["你想起来有一件事还没有完成。"]
        for task in tasks:
            title = task.get("title") or "未知标题"
            context = task.get("context") or "（无）"
            lines.append(f"名称：{title}")
            lines.append(f"上下文：{context}")
        lines.append("")
        lines.append("该事项到点已结束（任务一次性），如需继续请经 quest_agent 重新登记新的接续事项。")

        return "\n".join(lines)
