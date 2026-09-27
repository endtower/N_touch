"""
反思系统自动触发卡车（拆分为两个独立 truck，2026-09-21）：

1. ReflectionCharTruck（reflection_char_truck）——字符触发
   未处理笔记字符数 >= CHAR_THRESHOLD 时唤醒反思。
2. ReflectionTimeTruck（reflection_time_truck）——定时触发
   距上次反思满 REFLECTION_INTERVAL（默认 3 小时）时唤醒反思。

设计：
- 两个 truck 共用同一"上次反思完成时间"基线（模块级 _last_reflection_time）：
  任一方驱动反思会话结束后同时重置基线，避免另一方紧接着再次触发。
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

# 共享的上次反思完成时间戳（模块级）：字符触发与定时触发两个 truck 共用同一基线，
# 任一方完成反思后同时重置，避免另一方紧接着再次触发。
_last_reflection_time: Optional[float] = None


def _get_last_reflection_time() -> float:
    """取共享基线；None 表示尚未建立（首次以当前时刻初始化）。"""
    global _last_reflection_time
    if _last_reflection_time is None:
        _last_reflection_time = time.time()
    return _last_reflection_time


def _reset_last_reflection_time() -> None:
    """反思会话结束后重置共享基线。"""
    global _last_reflection_time
    _last_reflection_time = time.time()


class _BaseReflectionTruck(BaseTruck):
    """反思触发 truck 公共基类：驱动反思会话直至结束，恒返回 None。

    子类只需实现 _get_config（本 truck 配置）与 _check_trigger（触发条件）；
    共享的反思驱动逻辑（get_truck_messages）与主循环骨架收口在本类。
    """

    def __init__(self, truck_id: str):
        super().__init__(truck_id)
        # 本次触发原因（_check_trigger 设置，station 调 get_truck_messages 时消费）
        self._trigger_reason: str = ""

    def _get_config(self):
        raise NotImplementedError

    async def cycle_control(self):
        await super().cycle_control(self._get_config().CHECK_CYCLE_TIME)

    async def _main_loop(self):
        """主循环 - 短周期检查触发条件，满足即 remind_station"""
        config = self._get_config()

        logger.info(
            f"{self.truck_id} 已启动, "
            f"check_interval={config.CHECK_CYCLE_TIME}s"
        )
        # 定时巡检以 truck 启动时刻为基线（共享，首次初始化）
        _get_last_reflection_time()

        while self.state != "stopped":
            try:
                await self.cycle_control()
                reason = await self._check_trigger(config)
                if reason:
                    self._trigger_reason = reason
                    logger.info(f"满足反思触发条件: {reason}")
                    await self.remind_station()
            except Exception as e:
                # 硬约定：关键环节失败不允许继续运行，直接崩溃阻断
                # （CancelledError 继承自 BaseException，不会进入此处，stop() 不受影响）
                logger.critical(
                    f"{self.truck_id} 主循环发生致命错误，强制退出进程: "
                    f"{e}\n{traceback.format_exc()}"
                )
                for h in getattr(logger, 'handlers', []):
                    try:
                        h.flush()
                    except Exception:
                        pass
                os._exit(1)

    async def _check_trigger(self, config) -> str:
        """检查触发条件。返回触发原因描述，不满足返回空串。"""
        raise NotImplementedError

    async def get_truck_messages(self):
        """station 调度时调用：驱动反思会话直至结束，恒返回 None。

        反思系统自行完成笔记归档与新笔记落库（archive_notes 工具，
        finalize_reflection 收尾），不需要 station 进行 PFCA 路由。
        启动/执行失败直接上抛，由 station 的致命错误通道崩溃阻断。
        """
        from brain.hc.shortlight.notepad import get_shortlight_notepad

        notes = await get_shortlight_notepad().get_all()
        note_count = len(notes)
        unprocessed_chars = await get_shortlight_notepad().count_unprocessed_chars()
        elapsed_hours = (time.time() - _get_last_reflection_time()) / 3600

        # purpose 即反思首条消息的 message_base 槽位；
        # 笔记全文由首条消息的【记事本手册】槽位自动注入，这里只给触发背景。
        titles = [(doc.get("title") or "").strip() for doc in notes]
        titles = [t for t in titles if t]
        purpose = (
            f"【自动反思触发】原因：{self._trigger_reason or '触发原因未记录'}\n"
            f"当前活动笔记共 {note_count} 篇"
            f"（{'、'.join(titles) if titles else '无标题'}），"
            f"未处理笔记字符数 {unprocessed_chars}，"
            f"距上次反思约 {elapsed_hours:.1f} 小时。\n"
            f"请通读本轮活动笔记进行反思：提炼经验、总结规律，"
            f"并按流程收尾（归档旧笔记、落库新笔记）。"
        )

        logger.info(f"启动反思会话（{self.truck_id}: {self._trigger_reason or '未记录原因'}）")
        # 经 run_pfc_cycle 协调器运行：反思 finalize 可选接力进入行动（enter_action），
        # 构成 反思↔行动 链式会话；行动腿的事件链由协调器托管。
        from brain.pfc.core import run_pfc_cycle
        await run_pfc_cycle(
            initial_state="reflection",
            initial_purpose=purpose,
            current_session=None,
        )

        # 反思结束后重置共享定时基线与触发原因（两个 truck 共用基线）
        _reset_last_reflection_time()
        self._trigger_reason = ""
        logger.info("反思会话结束")
        return None


class ReflectionCharTruck(_BaseReflectionTruck):
    """反思字符触发 truck：未处理笔记字符数达到阈值时唤醒反思。"""

    def __init__(self):
        super().__init__("reflection_char_truck")

    def _get_config(self):
        from config.truck_settings import reflection_char_truck_config
        return reflection_char_truck_config

    async def _check_trigger(self, config) -> str:
        """字符触发：未处理笔记字符数 >= 阈值。

        笔记读取失败直接上抛（notepad 读盘/解析失败本就崩溃阻断），不做任何兜底。
        """
        from brain.hc.shortlight.notepad import get_shortlight_notepad

        unprocessed_chars = await get_shortlight_notepad().count_unprocessed_chars()
        if unprocessed_chars >= config.CHAR_THRESHOLD:
            return (
                f"未处理笔记字符数达到阈值"
                f"（{unprocessed_chars}/{config.CHAR_THRESHOLD}）"
            )
        return ""


class ReflectionTimeTruck(_BaseReflectionTruck):
    """反思定时触发 truck：距上次反思满间隔时唤醒反思。"""

    def __init__(self):
        super().__init__("reflection_time_truck")

    def _get_config(self):
        from config.truck_settings import reflection_time_truck_config
        return reflection_time_truck_config

    async def _check_trigger(self, config) -> str:
        elapsed = time.time() - _get_last_reflection_time()
        if elapsed >= config.REFLECTION_INTERVAL:
            return f"距上次反思已满 {elapsed / 3600:.1f} 小时（定时巡检）"
        return ""
