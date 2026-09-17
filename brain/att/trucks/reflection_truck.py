"""
ReflectionTruck - 反思系统自动触发卡车

负责在满足触发条件时唤醒 reflection 会话：
- 活动笔记数达到阈值：灵光记事本活动池（notes/ 顶层）笔记数 >= NOTE_THRESHOLD
- 定时巡检：距上次反思满 REFLECTION_INTERVAL（默认 3 小时）

设计：
- truck 仅负责触发判断与调度；get_truck_messages 内部直接驱动
  reflection_core.start() 并阻塞至反思会话结束（finalize_reflection 收尾）
- 反思不是对话任务，无需 PFCA 路由，因此恒返回 None
- 任何关键环节失败（笔记读取失败、反思启动失败）不做兜底继续，
  直接崩溃阻断，防止错误扩大蔓延
"""

import os
import time
import traceback
from typing import Optional

from utils.logger import get_logger
from .base_truck import BaseTruck

logger = get_logger('reflection_truck', 'INFO')


class ReflectionTruck(BaseTruck):
    """反思系统自动触发卡车"""

    def __init__(self):
        super().__init__("reflection_truck")
        # 上次反思完成的时间戳；None 表示尚未建立基线（_main_loop 首轮以启动时刻初始化）
        self.last_reflection_time: Optional[float] = None
        # 本次触发原因（_check_trigger 设置，station 调 get_truck_messages 时消费）
        self._trigger_reason: str = ""

    async def cycle_control(self):
        from config.truck_settings import reflection_truck_config
        await super().cycle_control(reflection_truck_config.CHECK_CYCLE_TIME)

    async def _main_loop(self):
        """主循环 - 短周期检查触发条件，满足即 remind_station"""
        from config.truck_settings import reflection_truck_config

        logger.info(
            f"ReflectionTruck 已启动, "
            f"check_interval={reflection_truck_config.CHECK_CYCLE_TIME}s, "
            f"note_threshold={reflection_truck_config.NOTE_THRESHOLD}, "
            f"reflection_interval={reflection_truck_config.REFLECTION_INTERVAL}s"
        )
        # 定时巡检以 truck 启动时刻为基线
        self.last_reflection_time = time.time()

        while self.state != "stopped":
            try:
                await self.cycle_control()
                reason = await self._check_trigger(reflection_truck_config)
                if reason:
                    self._trigger_reason = reason
                    logger.info(f"满足反思触发条件: {reason}")
                    await self.remind_station()
            except Exception as e:
                # 硬约定：关键环节失败不允许继续运行，直接崩溃阻断
                # （CancelledError 继承自 BaseException，不会进入此处，stop() 不受影响）
                logger.critical(
                    f"ReflectionTruck 主循环发生致命错误，强制退出进程: "
                    f"{e}\n{traceback.format_exc()}"
                )
                for h in getattr(logger, 'handlers', []):
                    try:
                        h.flush()
                    except Exception:
                        pass
                os._exit(1)

    async def _check_trigger(self, config) -> str:
        """检查触发条件。返回触发原因描述，不满足返回空串。

        笔记读取失败直接上抛（notepad 读盘/解析失败本就崩溃阻断），不做任何兜底。
        """
        from brain.hc.shortlight.notepad import get_shortlight_notepad

        notes = await get_shortlight_notepad().get_all()
        note_count = len(notes)
        if note_count >= config.NOTE_THRESHOLD:
            return f"活动笔记数达到阈值（{note_count}/{config.NOTE_THRESHOLD}）"

        elapsed = time.time() - self.last_reflection_time
        if elapsed >= config.REFLECTION_INTERVAL:
            return f"距上次反思已满 {elapsed / 3600:.1f} 小时（定时巡检）"

        return ""

    async def get_truck_messages(self):
        """station 调度时调用：驱动反思会话直至结束，恒返回 None。

        反思系统自行完成笔记归档与新笔记落库（finalize_reflection 收尾），
        不需要 station 进行 PFCA 路由。启动/执行失败直接上抛，
        由 station 的致命错误通道崩溃阻断。
        """
        from config.truck_settings import reflection_truck_config
        from brain.hc.shortlight.notepad import get_shortlight_notepad
        from brain.pfc.reflection.reflection_core import get_reflection_instance

        notes = await get_shortlight_notepad().get_all()
        note_count = len(notes)
        elapsed_hours = (time.time() - self.last_reflection_time) / 3600

        # purpose 即反思首条消息的 message_base 槽位；
        # 笔记全文由首条消息的【记事本手册】槽位自动注入，这里只给触发背景。
        titles = [(doc.get("title") or "").strip() for doc in notes]
        titles = [t for t in titles if t]
        purpose = (
            f"【自动反思触发】原因：{self._trigger_reason or '触发原因未记录'}\n"
            f"当前活动笔记共 {note_count} 篇"
            f"（{'、'.join(titles) if titles else '无标题'}），"
            f"距上次反思约 {elapsed_hours:.1f} 小时。\n"
            f"请通读本轮活动笔记进行反思：提炼经验、总结规律，"
            f"并按流程收尾（归档旧笔记、落库新笔记）。"
        )

        logger.info(f"启动反思会话（{self._trigger_reason or '未记录原因'}）")
        core = get_reflection_instance()
        await core.start(purpose=purpose, current_session=None)

        # 反思结束后重置定时基线与触发原因
        self.last_reflection_time = time.time()
        self._trigger_reason = ""
        logger.info("反思会话结束")
        return None
