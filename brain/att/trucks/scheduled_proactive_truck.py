"""
ScheduledProactiveTruck - 定时主动任务卡车

按固定周期唤醒 proactive_session，意图 = scheduled_checkin。
- 严格定时：truck 启动后等满 CYCLE_TIME 才触发，不立即触发
- 同时只有一个 truck 激活（由 station 调度保证）
- truck 自带暂停机制，proactive_session 跑完前 truck 不再唤醒
"""

import asyncio
import time
from typing import Dict, Any

from utils.logger import get_logger
from utils.chain_context import get_current_chain_id
from .base_truck import BaseTruck

logger = get_logger('scheduled_proactive_truck', 'INFO')


class ScheduledProactiveTruck(BaseTruck):
    """定时主动任务卡车"""

    def __init__(self):
        super().__init__("scheduled_proactive_truck")

    async def _main_loop(self):
        """主循环 - 严格按 CYCLE_TIME 周期触发 proactive"""
        from config.truck_settings import scheduled_proactive_truck_config

        logger.info(
            f"ScheduledProactiveTruck 已启动, "
            f"interval={scheduled_proactive_truck_config.CYCLE_TIME}s"
        )

        while self.state != "stopped":
            try:
                # 严格定时：等满 CYCLE_TIME 后触发一次
                await self.cycle_control()
                await self._trigger_proactive()
            except Exception as e:
                logger.error(f"ScheduledProactiveTruck 循环异常: {e}", exc_info=True)

    async def cycle_control(self):
        from config.truck_settings import scheduled_proactive_truck_config
        await super().cycle_control(scheduled_proactive_truck_config.CYCLE_TIME)

    async def _trigger_proactive(self) -> None:
        """触发 proactive。truck 自带暂停机制，proactive 跑完前不会再次触发。"""
        logger.info("定时触发 proactive")
        await self.remind_station()

    async def get_truck_messages(self) -> Dict[str, Any]:
        """station 调度时调用：构造定时任务草稿并投递。

        truck 不携带任何业务 payload，仅 intent 标识定时场景。
        根据 proactive 的 decision_result 决定返回什么：
        - launch_pfca → 返回 dict，station 调 PFCA
        - skip / 没决策 → 返回 None
        """
        try:
            import json
            from brain.att.proactive.proactive_core import get_proactive_core

            core = get_proactive_core()
            if not core.is_running:
                await core.start()
                logger.info("proactive core 启动 (lazy)")

            task_draft = {
                "intent": "scheduled_checkin",
                "reason": "定时主动任务",
                "wakeup_reason": "定时巡检时间到，请处理！",
                "target_session_id": None,
                # station 出队即生成的任务链 ID（contextvar 不会传播到
                # proactive_core 主循环任务，需显式携带，主循环内重新 set）
                "chain_id": get_current_chain_id(),
                "payload": {
                    "source": "scheduled_proactive_truck",
                },
            }
            logger.info("scheduled_proactive_truck 投递定时任务到 proactive")
            # 阻塞到 proactive_session 退出
            await core.feed_proactive_task(task_draft)

            decision_result = task_draft.get("_decision_result")
            logger.info(
                f"scheduled_proactive_truck 拿到 proactive 决策: "
                f"{decision_result.get('tool_name') if decision_result else 'None'}"
            )

            if not decision_result:
                return None
            if decision_result.get("skipped"):
                logger.info("proactive 主动跳过本次定时任务")
                return None
            if decision_result.get("tool_name") != "launch_pfca":
                logger.info(
                    f"proactive 决策为 {decision_result.get('tool_name')!r}, "
                    f"非 launch_pfca, 跳过 PFCA"
                )
                return None

            # === launch_pfca：构造 PFCA user content ===
            tool_result = decision_result.get("tool_result")
            formatted_message = ""
            target_session_id = ""
            target_task_title = ""
            if isinstance(tool_result, str) and tool_result.strip():
                try:
                    queued = json.loads(tool_result)
                    if isinstance(queued, dict) and queued.get("status") == "queued":
                        task_info = (queued.get("task_info") or "").strip()
                        target_session_id = (queued.get("target_session_id") or "").strip()
                        target_task_title = (
                            (queued.get("target_task_title") or "").strip()
                        )
                        if task_info:
                            formatted_message = f"【proactive 决策】\n{task_info}"
                except (ValueError, TypeError):
                    logger.info("proactive tool_result 非 JSON, 按空消息处理")

            if not formatted_message:
                formatted_message = "（定时主动任务触发，但 proactive 未提供 task_info）"

            if target_session_id:
                formatted_message = f"{formatted_message}\n\n目标会话: {target_session_id}"

            # 若 proactive 传入了 target_task_title，说明本次启动任务
            # 原因是未完成任务到期或提前触发。在末尾追加一条提示，要求 PFCA 在
            # 结束阶段通知 quest_agent 更新该任务。
            if target_task_title:
                formatted_message = (
                    f"{formatted_message}\n\n"
                    f"本次启动任务原因为到期任务触发（任务标题：{target_task_title}），"
                    f"你必须在结束阶段通知quest_agent更新该任务。"
                )

            logger.info("scheduled_proactive_truck 决策 launch_pfca, station 将调 PFCA")

            # 通用 raw_sessions 槽位：dict 含 session_id（proactive 选的 target_session_id）。
            # 按 name 的契约定义，定时主动任务不是"由消息类型触发的会话"，
            # 不应当传 session_name，避免 first_session_impression_search
            # 用占位串 ("(主动任务)") 误命中/新建记忆数据库节点。
            # station 会按需从 message_store 兜底 name 或传 None。
            raw_sessions = None
            if target_session_id:
                raw_sessions = {
                    "session_id": target_session_id,
                }

            return {
                "formatted_message": formatted_message,
                "raw_sessions": raw_sessions,
                "decision": decision_result,
            }
        except Exception as e:
            logger.error(f"scheduled_proactive_truck 投递 proactive 失败: {e}", exc_info=True)
            return None