#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 崩溃系统

会话内崩溃状态机：
- 三态单槽位：NONE / CRASHED / NOT_CRASHED
- check_and_end_crash()：每次访问都顺带检查到期
- should_trigger_crash()：基于 crash_index 概率判定
- trigger_crash() / trigger_not_crashed()：写入覆盖状态
- get_cover_description()：覆盖期返回的 amy 描述（崩溃/未崩各自一份）

崩溃 agent 在 amy 模块内部，本轮只设计骨架（trigger_* 入口可由外部 agent 调用）。
"""

import json
import random
import time
import asyncio
from pathlib import Path
from typing import Optional, Callable

from utils.logger import get_logger
from brain.amy.constants import (
    SystemState, SystemStateData, CrashInfo, MoodSnapshot,
    CRASH_LONG_DURATION, NOT_CRASHED_SHORT_DURATION,
    CRASH_POST_PROTECT_DURATION,
    CRASH_INDEX_THRESHOLD,
    BASE_VALUE,
    DEFAULT_CRASH_DESCRIPTION, DEFAULT_NOT_CRASHED_DESCRIPTION,
    DEFAULT_CRASH_DESCRIPTION_NE_HIGH,
)

logger = get_logger('amy_crash', 'INFO')


class CrashMode:
    """崩溃子类型：仅一类（NE_HIGH）。

    DA 不再触发崩溃——崩溃由 NE 单维度决定。
    """
    NE_HIGH = "ne_high"

    _DESCRIPTION_MAP = {
        NE_HIGH: DEFAULT_CRASH_DESCRIPTION_NE_HIGH,
    }

    @classmethod
    def get_description(cls, mode: str, last_straw_desc: str) -> str:
        """按 mode 取模板并填充 last_straw_desc。未知 mode 兜底用 NE_HIGH。"""
        template = cls._DESCRIPTION_MAP.get(mode, DEFAULT_CRASH_DESCRIPTION)
        return template.format(last_straw_desc=last_straw_desc or "（未知）")


class CrashSystem:
    """崩溃状态机"""

    def __init__(self, persistence_dir: Optional[Path] = None):
        if persistence_dir is None:
            persistence_dir = Path(__file__).resolve().parent / "persistence"
        persistence_dir.mkdir(exist_ok=True)
        self.persistence_file = persistence_dir / "system_state.json"
        self.state: SystemStateData = SystemStateData()
        self.last_crash_end_time: int = 0  # 上次崩溃结束的时间（用于崩溃后保护期）
        self._thought_cache = None  # 由 EmotionalInterface 注入
        self._event_log = None  # 情绪事件日志（由 EmotionalInterface 注入）
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        # 并发锁：保护崩溃状态读写（crash monitor 1Hz 与外部并发访问共享）
        self._lock = asyncio.Lock()
        self.load_from_file()

    def bind_thought_cache(self, thought_cache):
        """由 EmotionalInterface 在初始化时调用，注入 thought_cache 引用。"""
        self._thought_cache = thought_cache

    def bind_event_log(self, event_log):
        """由 EmotionalInterface 在初始化时调用，注入情绪事件日志引用。"""
        self._event_log = event_log

    async def _emit(self, event_type: str, **kwargs) -> None:
        """写一条情绪事件日志（无 event_log 时静默跳过）。"""
        if self._event_log is None:
            return
        try:
            await self._event_log.append_event(
                event_type=event_type,
                thought_id=kwargs.get("thought_id", ""),
                act=kwargs.get("act", ""),
                desc=kwargs.get("desc", ""),
                ne_delta=kwargs.get("ne_delta", 0.0),
                da_delta=kwargs.get("da_delta", 0.0),
            )
        except Exception as e:
            logger.warning(f"写情绪事件日志失败: {e}")

    # ===== 持久化 =====

    def load_from_file(self):
        if not self.persistence_file.exists():
            return
        try:
            with open(self.persistence_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.state = SystemStateData.from_dict(data)
                logger.info(f"Loaded system state: {self.state.state.value}")
        except Exception as e:
            logger.error(f"Failed to load system state: {e}")

    def save_to_file(self):
        try:
            with open(self.persistence_file, 'w', encoding='utf-8') as f:
                json.dump(self.state.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save system state: {e}")

    # ===== 状态查询 =====

    def is_crashed(self) -> bool:
        return self.state.state == SystemState.CRASHED

    def is_not_crashed(self) -> bool:
        return self.state.state == SystemState.NOT_CRASHED

    def is_covered(self) -> bool:
        return self.state.state in (SystemState.CRASHED, SystemState.NOT_CRASHED)

    def is_in_post_protect_period(self, now: Optional[int] = None) -> bool:
        """是否处于崩溃后保护期。
        崩溃结束时间 + CRASH_POST_PROTECT_DURATION 之前 → True
        期间内即使 ci 很高也拒绝重新触发崩溃。
        """
        if self.last_crash_end_time == 0:
            return False
        if now is None:
            now = int(time.time())
        return now < (self.last_crash_end_time + CRASH_POST_PROTECT_DURATION)

    def get_state(self) -> SystemState:
        return self.state.state

    def get_cover_description(self) -> Optional[str]:
        """覆盖期返回的描述（崩溃/未崩共用入口）"""
        if self.state.active_info:
            return self.state.active_info.description
        return None

    def get_crash_description(self) -> Optional[str]:
        """兼容旧接口，等价于 get_cover_description"""
        return self.get_cover_description()

    def get_crash_type(self) -> Optional[str]:
        """返回当前覆盖态的 mode 字符串"""
        if self.state.active_info:
            return self.state.active_info.mode.value
        return None

    def get_last_straw_desc(self) -> str:
        if self.state.active_info:
            return self.state.active_info.last_straw_desc
        return ""

    # ===== crash_index 计算 =====

    @staticmethod
    def calculate_crash_index(snapshot: MoodSnapshot) -> float:
        """基于快照算 crash_index (0~100)
        DA 不再参与崩溃判定：crash_index = clamp(ne, 0, 100)。
        """
        return max(0.0, min(100.0, snapshot.norepinephrine))

    @staticmethod
    def get_lowest_dimension(snapshot: MoodSnapshot) -> str:
        """取双值中更极端的那一项（用于描述"在哪条线上接近崩溃"）"""
        if snapshot.norepinephrine >= snapshot.dopamine:
            return "norepinephrine"
        return "dopamine"

    @staticmethod
    def classify_crash_mode(snapshot: MoodSnapshot) -> str:
        """崩溃模式判定：DA 已不参与，仅返回 NE_HIGH。
        函数签名保留以兼容调用方。
        """
        return CrashMode.NE_HIGH

    # ===== 概率判定（按时间，与调用频率解耦）=====

    @staticmethod
    def expected_collapse_seconds(crash_index: float) -> float:
        """按 crash_index 推算期望崩溃秒数（设计锚点）。
        - [70, 80): 中压 → 常值 300s（5min）
        - [80, 90): 高压 → 常值 60s（1min）
        - [90, ∞):  极高 → 常值 30s
        - <70:      不触发
        """
        if crash_index < CRASH_INDEX_THRESHOLD:
            return float('inf')
        if crash_index < 80.0:
            return 300.0
        if crash_index < 90.0:
            return 60.0
        return 30.0

    async def should_trigger_crash(
        self, snapshot: MoodSnapshot, dt: float = 1.0
    ) -> bool:
        """按"时间"概率判定，与调用频率解耦。
        - dt: 自检的时间窗口（秒）。amy 内部 1Hz monitor 用 dt=1.0
        - 返回 True 表示命中（已写入崩溃状态）；False 表示本 tick 未触发
        """
        # 闸 1：状态
        if self.is_covered():
            return False
        # 闸 2：保护期（崩溃结束后 CRASH_POST_PROTECT_DURATION 秒内不重新触发）
        now = int(time.time())
        if self.is_in_post_protect_period(now):
            return False
        # 闸 3：阈值
        crash_index = self.calculate_crash_index(snapshot)
        if crash_index < CRASH_INDEX_THRESHOLD:
            return False
        # 闸 4：概率（按 dt 推算本时间窗内是否触发）
        T = self.expected_collapse_seconds(crash_index)
        p_per_sec = 1.0 / T
        p_in_dt = 1.0 - (1.0 - p_per_sec) ** dt
        roll = random.uniform(0, 1)
        if roll < p_in_dt:
            # 命中即写状态（agent 已绕开）
            mode = self.classify_crash_mode(snapshot)
            await self.trigger_immediate_crash(
                mode=mode,
                last_straw_desc=snapshot.top_thoughts[0].desc
                if snapshot.top_thoughts else ""
            )
            logger.info(
                f"崩溃概率判定命中: mode={mode}, crash_index={crash_index:.1f}, "
                f"p_in_dt={p_in_dt:.4f}, roll={roll:.4f}, "
                f"T={T:.1f}s, ne={snapshot.norepinephrine:.1f}, "
                f"da={snapshot.dopamine:.1f}"
            )
            return True
        return False

    def get_crash_index(self, snapshot: MoodSnapshot) -> float:
        """兼容旧接口"""
        return self.calculate_crash_index(snapshot)

    # ===== 状态写入 =====

    async def trigger_crash(
        self,
        mode: str,
        last_straw_desc: str = "",
        description: Optional[str] = None,
    ) -> bool:
        """崩溃 agent 决策后调用：按 mode 写入对应描述的 CRASHED 状态。

        Args:
            mode: CrashMode 之一（NE_HIGH/NE_LOW/DA_HIGH/DA_LOW）。
                  description 不传时按 mode 自动生成。
            last_straw_desc: 最后一根稻草的 desc，用于填充描述模板。
            description: 自定义描述。给 agent 决策后填充个性化文本用。
        """
        now = int(time.time())
        text = description if description else CrashMode.get_description(mode, last_straw_desc)
        async with self._lock:
            self.state.state = SystemState.CRASHED
            self.state.active_info = CrashInfo(
                mode=SystemState.CRASHED,
                description=text,
                start_time=now,
                end_time=now + CRASH_LONG_DURATION,
                last_straw_desc=last_straw_desc,
                crash_subtype=mode,
            )
            self.last_crash_end_time = now + CRASH_LONG_DURATION
            self.save_to_file()
            await self._emit("crash_triggered", desc=text, act=mode)
        logger.info(
            f"CRASHED triggered: mode={mode}, end_time={self.state.active_info.end_time} "
            f"(duration={CRASH_LONG_DURATION}s, "
            f"protect_until={self.last_crash_end_time + CRASH_POST_PROTECT_DURATION})"
        )
        return True

    async def trigger_not_crashed(self, description: str) -> bool:
        """崩溃 agent 决策后调用：写入 NOT_CRASHED 状态（短安抚窗口）"""
        now = int(time.time())
        async with self._lock:
            self.state.state = SystemState.NOT_CRASHED
            self.state.active_info = CrashInfo(
                mode=SystemState.NOT_CRASHED,
                description=description,
                start_time=now,
                end_time=now + NOT_CRASHED_SHORT_DURATION,
                last_straw_desc="",
            )
            self.save_to_file()
            await self._emit("crash_triggered", desc=description, act="not_crashed")
        logger.info(
            f"NOT_CRASHED triggered: end_time={self.state.active_info.end_time} "
            f"(duration={NOT_CRASHED_SHORT_DURATION}s)"
        )
        return True

    # ===== 临时：绕过 agent 直接崩溃 =====

    async def trigger_immediate_crash(
        self,
        last_straw_desc: str = "",
        mode: Optional[str] = None,
    ) -> bool:
        """临时入口：绕过崩溃 agent，基于 mode 写入预设崩溃描述。

        - mode 显式传入时直接用。
        - mode 未传时基于当前 snapshot 自动 classify（按最大偏离，ne 优先）。
        - snapshot 不可用时兜底用 NE_HIGH。
        """
        if mode is None:
            if self._thought_cache:
                snapshot = await self._thought_cache.calculate_mood_snapshot()
                mode = self.classify_crash_mode(snapshot)
            else:
                mode = CrashMode.NE_HIGH
        return await self.trigger_crash(mode=mode, last_straw_desc=last_straw_desc)

    async def trigger_immediate_not_crashed(self) -> bool:
        """临时入口：绕过崩溃 agent，直接写入预设安抚描述"""
        return await self.trigger_not_crashed(
            description=DEFAULT_NOT_CRASHED_DESCRIPTION,
        )

    # ===== 到期检查 =====

    async def check_and_end_crash(self) -> bool:
        """每次访问都顺带检查：
        - 覆盖态到期 → 切回 NONE（不补偿，本轮不设计补偿 agent）
        返回：是否刚结束了某个覆盖态
        """
        async with self._lock:
            if not self.is_covered():
                return False
            if not self.state.active_info:
                return False
            now = int(time.time())
            if now >= self.state.active_info.end_time:
                prev_mode = self.state.state
                self.state.state = SystemState.NONE
                self.state.active_info = None
                self.save_to_file()
                await self._emit("crash_ended", desc=prev_mode.value)
                logger.info(f"{prev_mode.value} ended naturally, state -> NONE")
                return True
            return False

    # ===== 正向情绪缩短 =====

    async def maybe_shorten_crash(self, snapshot: MoodSnapshot) -> bool:
        """崩溃期间收到正向情绪时缩短剩余时间。
        - 仅对 CRASHED 生效
        - 正向判定：dopamine > 70 且 norepinephrine < 60
        - 缩短规则：剩余时间减 30s（至少保留 10s 宣泄窗口）
        返回：是否发生了缩短
        """
        async with self._lock:
            if not self.is_crashed():
                return False
            if not self.state.active_info:
                return False
            if snapshot.dopamine <= 70 or snapshot.norepinephrine >= 60:
                return False
            now = int(time.time())
            remaining = self.state.active_info.end_time - now
            if remaining <= 10:
                return False
            new_remaining = max(10, remaining - 30)
            self.state.active_info.end_time = now + new_remaining
            self.save_to_file()
            logger.info(
                f"CRASHED shortened by positive mood: "
                f"{remaining}s -> {new_remaining}s"
            )
            return True

    async def maybe_shorten_crash_relaxed(self, snapshot: MoodSnapshot) -> bool:
        """崩溃期间收到正向情绪时缩短剩余时间（宽松版）。
        - 仅对 CRASHED 生效
        - 正向判定：dopamine > 65 且 norepinephrine < 70（比 maybe_shorten_crash 宽松）
        - 缩短规则：剩余时间减 15s（至少保留 10s 宣泄窗口）
        返回：是否发生了缩短
        """
        async with self._lock:
            if not self.is_crashed():
                return False
            if not self.state.active_info:
                return False
            if snapshot.dopamine <= 65 or snapshot.norepinephrine >= 70:
                return False
            now = int(time.time())
            remaining = self.state.active_info.end_time - now
            if remaining <= 10:
                return False
            new_remaining = max(10, remaining - 15)
            self.state.active_info.end_time = now + new_remaining
            self.save_to_file()
            logger.info(
                f"CRASHED shortened by positive mood (relaxed): "
                f"{remaining}s -> {new_remaining}s"
            )
            return True

    # ===== 兼容旧接口（保留语义但不一定调用）=====

    def check_crash_trigger(self) -> bool:
        """旧接口占位：实际触发由 emotional_interface 协同完成"""
        return False

    def is_stressed(self) -> bool:
        return False

    # ===== 1Hz 自检循环（与调用频率解耦）=====

    async def start_monitor(self, tick_seconds: float = 1.0):
        """启动崩溃自检循环。amy 内部用，与外部 get_amy_description 调用频率无关。"""
        if self._running:
            logger.warning("CrashSystem monitor already running")
            return
        self._running = True
        self._tick_seconds = tick_seconds
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(f"CrashSystem monitor started (tick={tick_seconds}s)")

    async def stop_monitor(self):
        if not self._running:
            return
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._monitor_task = None
        logger.info("CrashSystem monitor stopped")

    async def _monitor_loop(self):
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"crash monitor tick error: {e}", exc_info=True)
            await asyncio.sleep(self._tick_seconds)

    async def _tick(self):
        """一次自检：1) 切到期态 2) 正常态下走概率闸"""
        # 1. 切到期态（覆盖态切回 NONE）
        if await self.check_and_end_crash():
            return  # 本 tick 状态刚切回，不重复判定
        # 2. 覆盖态 / 正常态分支
        if self.is_covered():
            return
        if not self._thought_cache:
            return
        snapshot = await self._thought_cache.calculate_mood_snapshot()
        await self.should_trigger_crash(snapshot, dt=self._tick_seconds)