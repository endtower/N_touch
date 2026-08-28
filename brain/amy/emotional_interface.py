#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 情绪系统核心接口

提供两个核心方法：
- build_thought: 根方法，不约束 act 输入，调用方全责（唯一写入入口）
- build_conversation_thought: 适配层（薄），用于 conversation tool 路径，内部调 build_thought
- get_amy_description: 统一出站出口（覆盖态/正常态），所有消费者都走这里
- get_mood_snapshot: 统一快照出口，供需要数值快照的消费者（reorganize/strategy_check/amy_api）使用

崩溃机制接入：
- get_amy_description() 内部早退分支，崩溃/未崩期返回覆盖描述
- build_thought() 末尾顺带缩短崩溃时间（正向情绪机制）

情绪消费者游标：
- 用"事件日志 + 消费者游标"替代"记录上次情绪值"去重逻辑
- get_unread_events(consumer_id) / advance_cursor(consumer_id, seq)
"""

import asyncio
import time
from pathlib import Path
from typing import Optional, Tuple, List

from brain.amy.constants import (
    ACT_EFFECTS, ACTEffects,
    SystemBuiltInAct,
    Thought, MoodSnapshot, generate_timestamp_id, THOUGHT_DURATION, SystemState,
)
from brain.amy.thought_cache import ThoughtCache, calculate_duration
from brain.amy.affinity import AffinityStore
from brain.amy.crash import CrashSystem
from brain.amy.event_log import EventLog
from brain.amy.prompt_engine import get_amy_description
from utils.logger import get_logger

logger = get_logger('amy_interface', 'INFO')


class EmotionalInterface:
    _instance = None

    def __new__(cls, persistence_dir: Optional[Path] = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, persistence_dir: Optional[Path] = None):
        if self._initialized:
            return
        if persistence_dir is None:
            persistence_dir = Path(__file__).resolve().parent / "persistence"
        persistence_dir.mkdir(exist_ok=True)
        # 情绪事件日志（内存实现），先创建并注入给 thought_cache / crash_system
        self.event_log = EventLog()
        self.thought_cache = ThoughtCache(persistence_dir, event_log=self.event_log)
        self.affinity_store = AffinityStore(persistence_dir)
        self.crash_system = CrashSystem(persistence_dir)
        self.crash_system.bind_thought_cache(self.thought_cache)
        self.crash_system.bind_event_log(self.event_log)
        self._initialized = True
        # 启动 1Hz 崩溃自检（与 get_amy_description 调用频率解耦）
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.crash_system.start_monitor(tick_seconds=1.0))
            else:
                # 无 event loop 时（如单元测试），跳过 monitor
                logger.debug("No running event loop, skip crash monitor start")
        except RuntimeError:
            logger.debug("No event loop, skip crash monitor start")
        logger.info("EmotionalInterface initialized")

    async def _bootstrap_system_acts(self) -> None:
        """启动时初始化所有系统内置 act（async，由 get_emotional_interface 首次创建后 await）。

        行为：
        1. 迁移：清理所有 act="长期正面心情" 的旧 thought
           （早期版本把它放在 SelfActionAct，普通 thought ID，启动时会产生重复注入）
        2. 按 SystemBuiltInAct.items 遍历，ensure_system_thought() 保证每个 act 唯一存在
        """
        # 迁移：清理旧版"长期正面心情" thought
        removed = self.thought_cache.remove_thoughts_by_act("长期正面心情")
        if removed:
            self.thought_cache.save_to_file()
            logger.info(f"迁移：清理旧版系统 thought '长期正面心情' x {removed}")

        # 注入所有系统内置 act
        for entry in SystemBuiltInAct.entries():
            await self.thought_cache.ensure_system_thought(
                act_name=entry["name"],
                thought_id=entry["system_id"],
                desc=f"系统内置：{entry['name']}（持续生效）",
                duration=entry["duration"],
            )

    # ===== 根方法：不约束 act（唯一写入入口） =====

    async def build_thought(
        self,
        act: str,
        desc: str,
        create_time: Optional[int] = None,
        duration: Optional[int] = None,
        norepinephrine_delta: Optional[float] = None,
        dopamine_delta: Optional[float] = None,
        desc_extra: str = "",
    ) -> Tuple[bool, str, Optional[str]]:
        """根方法：直接接受完整参数构造 thought。
        调用方负责传入所有信息；norepinephrine_delta / dopamine_delta 未提供时按 act 查表。
        注：ne_delta / da_delta 语义为"绝对偏移量"（可正可负），最终叠加公式见 thought_cache._calculate_multiplier。

        Returns:
            (success, message, prompt)
        """
        if not act or not desc:
            return False, "act 和 desc 必填", None

        # system 类 act 由启动时 ensure_system_thought 统一管理，禁止外部调用
        if ACTEffects.category_of(act) == "system":
            return False, f"act '{act}' 属于系统内置类，不由 build_thought 触发", None

        # delta 缺省时查 ACT_EFFECTS_BY_NAME（dict 格式）
        if norepinephrine_delta is None or dopamine_delta is None:
            effect = ACTEffects.lookup(act)
            if not effect:
                return False, f"act '{act}' 未在 ACT_EFFECTS 注册，且未提供 delta", None
            ne_delta = effect["ne_delta"]
            da_delta = effect["da_delta"]
            if norepinephrine_delta is None:
                norepinephrine_delta = ne_delta
            if dopamine_delta is None:
                dopamine_delta = da_delta

        # 顺带检查崩溃是否到期（覆盖态到期 -> NONE）
        await self.crash_system.check_and_end_crash()

        now = int(time.time())
        ts = create_time if create_time is not None else now
        # duration 优先使用外部传入；缺省时按 act 表中 duration 字段查，再缺省回退 THOUGHT_DURATION
        dur = duration if duration is not None else calculate_duration(act)

        thought = Thought(
            id=generate_timestamp_id(),
            create_time=ts,
            desc=desc,
            act=act,
            expire_time=ts + dur,
        )
        await self.thought_cache.add(thought)
        self.thought_cache.save_to_file()

        snapshot = await self.thought_cache.calculate_mood_snapshot()

        # 推送 amy_effect 到 dynamic_prompt（让 LLM 在 system prompt 看到情绪描述）
        prompt = get_amy_description(snapshot)
        try:
            from utils.dynamic_prompt_manager import dynamic_prompt_manager
            dynamic_prompt_manager.set('amy_effect', prompt)
        except Exception as e:
            logger.warning(f"推送 amy_effect 失败: {e}")

        # 崩溃期间正向情绪缩短
        if self.crash_system.is_crashed():
            await self.crash_system.maybe_shorten_crash(snapshot)
            await self.crash_system.maybe_shorten_crash_relaxed(snapshot)

        return True, "Thought built successfully", prompt

    # ===== 适配层：仅 conversation tool 使用 =====

    async def build_conversation_thought(
        self,
        act: str,
        session_type: str,
        session_name: str,
    ) -> Tuple[bool, str, Optional[str]]:
        """适配层：白名单 + 拼描述 + 查表 + 调根方法
        专供 conversation tool 路径使用
        """
        # 白名单：仅注册在 ACT_EFFECTS 的 act（conversation 路径默认全开放）
        if act not in ACT_EFFECTS:
            return False, f"Invalid act: {act}，必须注册在 ACT_EFFECTS 中", None

        # 描述拼接
        if session_type == "group":
            desc = f"在群{session_name}中，{act}"
        elif session_type == "private":
            desc = f"在与{session_name}的交流中，{act}"
        else:
            desc = f"在{session_name}中，{act}"

        # desc_extra 仅用于 build_thought_summary 渲染，从 act 表里查
        desc_extra = ACT_EFFECTS.get(act, {}).get("desc_extra", "") or ""

        return await self.build_thought(act=act, desc=desc, desc_extra=desc_extra)

    # ===== 统一出站接口 =====

    async def get_amy_description(self) -> str:
        """统一出口（所有消费者都走这里）：
        1. 检查崩溃是否到期
        2. 覆盖态 -> 返回覆盖描述
        3. 正常态 -> 生成描述并推 amy_effect 到 dynamic_prompt
        （崩溃概率判定由 1Hz monitor 异步处理，不在这里同步阻塞）
        （增量消息由 chat/pfca 各自的 input_manager 负责，这里不推）
        """
        # 1. 到期检查
        await self.crash_system.check_and_end_crash()

        # 2. 覆盖态早退
        if self.crash_system.is_covered():
            cover_desc = self.crash_system.get_cover_description()
            if cover_desc:
                # 覆盖态也推 amy_effect 槽位（避免 system prompt 滞后）
                try:
                    from utils.dynamic_prompt_manager import dynamic_prompt_manager
                    dynamic_prompt_manager.set('amy_effect', cover_desc)
                except Exception as e:
                    logger.warning(f"推送 amy_effect (覆盖态) 失败: {e}")
                return cover_desc

        # 3. 正常态：取快照 + 生成描述
        snapshot = await self.thought_cache.calculate_mood_snapshot()
        description = get_amy_description(snapshot)

        # 4. 推 amy_effect 到 dynamic_prompt_manager（chat/pfca 在 system prompt 读它）
        try:
            from utils.dynamic_prompt_manager import dynamic_prompt_manager
            dynamic_prompt_manager.set('amy_effect', description)
        except Exception as e:
            logger.warning(f"推送 amy_effect 失败: {e}")

        return description

    async def get_mood_snapshot(self) -> MoodSnapshot:
        """统一快照出口：供需要数值快照的消费者（reorganize/strategy_check/amy_api）使用。
        内部同样检查崩溃覆盖态（到期切回 NONE）。
        """
        await self.crash_system.check_and_end_crash()
        return await self.thought_cache.calculate_mood_snapshot()

    async def get_active_thought_ids(self) -> set:
        """代理 thought_cache.get_active_thought_ids()，
        input_manager 用它同步"已输出"缓存。
        """
        return await self.thought_cache.get_active_thought_ids()

    # ===== 情绪消费者游标 =====

    async def get_unread_events(self, consumer_id: str) -> List[dict]:
        """拉取该消费者未消费的情绪事件（seq > 游标位置）。"""
        return await self.event_log.get_unread_events(consumer_id)

    async def advance_cursor(self, consumer_id: str, seq: int) -> None:
        """推进消费者游标到指定 seq。"""
        await self.event_log.advance_cursor(consumer_id, seq)

    # ===== 辅助方法 =====

    async def get_final_moods(self) -> MoodSnapshot:
        return await self.get_mood_snapshot()

    async def get_affinity(self, user_id: str) -> float:
        """获取好感度（async，返回 float）。"""
        return await self.affinity_store.get_affinity(user_id)

    async def get_crash_index(self) -> float:
        snapshot = await self.thought_cache.calculate_mood_snapshot()
        return self.crash_system.get_crash_index(snapshot)

    def is_crashed(self) -> bool:
        return self.crash_system.is_crashed()

    def is_not_crashed(self) -> bool:
        return self.crash_system.is_not_crashed()

    def is_stressed(self) -> bool:
        return False

    def check_crash_trigger(self) -> bool:
        return False


_emotional_interface: Optional[EmotionalInterface] = None


async def get_emotional_interface(persistence_dir: Optional[Path] = None) -> EmotionalInterface:
    global _emotional_interface
    if _emotional_interface is None:
        _emotional_interface = EmotionalInterface(persistence_dir)
        await _emotional_interface._bootstrap_system_acts()
    return _emotional_interface
