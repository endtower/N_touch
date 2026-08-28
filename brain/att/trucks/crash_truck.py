"""
CrashTruck - amy 模块崩溃监控卡车

功能：
- 周期检查 amy 模块的 crash_system 是否处于崩溃态（CRASHED）
- 处于崩溃态时，truck 向 station 提醒并投递到 proactive，
  让 proactive_session 决定如何处理（宣泄 / 找人聊 / 跳过等）
- 同一崩溃态只投递一次（用 last_triggered_crash_id 去重）
- 崩溃结束（切回 NONE）时重置去重标记，下一次崩溃可重新触发
"""

import asyncio
from typing import Optional, Dict, Any

from utils.logger import get_logger
from .base_truck import BaseTruck

logger = get_logger('crash_truck', 'INFO')


class CrashTruck(BaseTruck):
    """amy 崩溃监控卡车"""

    def __init__(self):
        super().__init__("crash_truck")
        # 去重：记录已经投递过 proactive 的崩溃 ID
        # amy 崩溃由 CrashInfo.start_time 唯一标识，
        # 同一崩溃只投递一次；崩溃结束（NONE）后重置
        self._last_dispatched_crash_id: Optional[int] = None

    async def _main_loop(self):
        """主循环 - 每隔 CYCLE_TIME 秒检查一次 amy 崩溃态"""
        logger.info("CrashTruck 已启动")
        while self.state != "stopped":
            try:
                triggered = await self.check_crash_state()
                if triggered:
                    logger.info("检测到 amy 崩溃，准备提醒 station")
                    await self.remind_station()
            except Exception as e:
                logger.error(f"CrashTruck 循环异常: {e}", exc_info=True)

            await self.cycle_control()

    async def cycle_control(self):
        """循环控制 - 控制 truck 的运行时间，支持暂停功能"""
        from config.truck_settings import crash_truck_config
        await super().cycle_control(crash_truck_config.CYCLE_TIME)

    async def check_crash_state(self) -> bool:
        """检查 amy 是否处于崩溃态。
        返回 True 表示新崩溃被检测到（且已构造好消息等 station 调 get_truck_messages）。
        同一崩溃态去重，只在"从 NONE 切到 CRASHED"那一帧返回 True。
        """
        try:
            from brain.amy import get_emotional_interface
            ei = await get_emotional_interface()
            cs = ei.crash_system
        except Exception as e:
            logger.error(f"获取 amy 接口失败: {e}")
            return False

        if not cs.is_crashed():
            # 没在崩溃态，重置去重标记（崩溃结束后允许再次触发）
            self._last_dispatched_crash_id = None
            return False

        # 处于崩溃态：去重判断
        active = cs.state.active_info
        if active is None:
            return False
        crash_id = active.start_time
        if crash_id == self._last_dispatched_crash_id:
            # 同一崩溃已经投递过，不重复
            return False

        # 新崩溃，标记
        self._last_dispatched_crash_id = crash_id
        logger.info(
            f"检测到新崩溃: crash_id={crash_id}, "
            f"crash_type={cs.get_crash_type()}, "
            f"end_time={active.end_time}"
        )
        return True

    async def get_truck_messages(self) -> Optional[Dict[str, Any]]:
        """station 调度时调用：构造 proactive 任务草稿并投递。
        返回值给 station 决定是否调 PFCA（这里始终 None，让 proactive 自己决定）。
        """
        try:
            from brain.amy import get_emotional_interface
            ei = await get_emotional_interface()
            cs = ei.crash_system

            if not cs.is_crashed():
                return None
            active = cs.state.active_info
            if active is None:
                return None

            # 构造 proactive 任务草稿
            from brain.att.proactive.proactive_core import get_proactive_core
            core = get_proactive_core()
            if not core.is_running:
                await core.start()
                logger.info("proactive core 启动 (lazy)")

            # 收集 crash 信息作为 payload
            last_straw = cs.get_last_straw_desc() or ""
            crash_desc = active.description or ""
            remaining = max(0, active.end_time - int(__import__('time').time()))

            task_draft = {
                "intent": "amy_crash",
                "reason": (
                    f"amy 模块检测到崩溃 (crash_id={active.start_time}, "
                    f"剩余 {remaining}s, 最后一根稻草='{last_straw[:80]}')"
                ),
                "wakeup_reason": "当前情绪处于崩溃状态，需要宣泄或缓解！",
                "target_session_id": None,
                "payload": {
                    "crash_id": active.start_time,
                    "crash_type": cs.get_crash_type(),
                    "crash_description": crash_desc,
                    "last_straw_desc": last_straw,
                    "remaining_seconds": remaining,
                    "source": "crash_truck",
                },
            }
            logger.info(
                f"crash_truck 投递崩溃到 proactive: "
                f"crash_id={active.start_time}"
            )
            # 这一步会阻塞到 proactive_session 退出
            await core.feed_proactive_task(task_draft)
            decision_result = task_draft.get("_decision_result")
            logger.info(
                f"crash_truck 拿到 proactive 决策: "
                f"{decision_result.get('tool_name') if decision_result else 'None'}"
            )
            # crash_truck 不调 PFCA（崩溃是 amy 自身状态，不走对话）
            return None
        except Exception as e:
            logger.error(f"crash_truck 投递 proactive 失败: {e}", exc_info=True)
            return None
