#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 想法系统

管理短期情绪想法缓存，支持持久化和情绪快照计算
去甲肾上腺素与多巴胺双值系统

想法池语义：
- max_stack 决定同名 act 同时存在的 thought 实例数
- max_stack == 0：system 类专用，不应走 add() 路径
- max_stack == 1：单 instance。
  - 池空 → 新建 thought（intensity=1.0）
  - 池已有 thought → 刷新现有 thought 的 expire_time / create_time / desc，
    保持原 ID 不变（避免外部缓存失效）；intensity 保持 1.0
- max_stack > 1：多 instance。
  - 池未满 → 新建 thought
    - intensity = 1.0（池空时）/ (1 - coef)（池已有时）
  - 池满 → FIFO 替换：移除同 act 中最早的，新增
- snapshot 时每个 thought 贡献 = intensity * act 的 ne/da delta

并发安全：内部用 asyncio.Lock 保护所有读写方法（全异步项目）。
thought 变动时写情绪事件日志（EventLog），供消费者游标精确拉取。
"""

import asyncio
import json
import time
from pathlib import Path
from typing import List, Optional

from utils.logger import get_logger
from utils.error_handling import handle_errors
from brain.amy.constants import (
    Thought, MoodSnapshot, ACTEffects,
    BASE_VALUE, MIN_VALUE, MAX_VALUE,
    clamp, THOUGHT_DURATION,
    generate_timestamp_id,
)

logger = get_logger('amy_thought', 'INFO')


class ThoughtCache:
    def __init__(self, persistence_dir: Optional[Path] = None, event_log=None):
        if persistence_dir is None:
            persistence_dir = Path(__file__).resolve().parent / "persistence"
        persistence_dir.mkdir(exist_ok=True)
        self.persistence_file = persistence_dir / "thought_cache.json"
        self.thoughts: List[Thought] = []
        self.timestamp: int = int(time.time())
        # 情绪事件日志引用（由 EmotionalInterface 注入）
        self._event_log = event_log
        # 并发锁：保护 thoughts 列表与持久化的读写
        self._lock = asyncio.Lock()
        self.load_from_file()

    def bind_event_log(self, event_log):
        """由 EmotionalInterface 在初始化时调用，注入事件日志引用。"""
        self._event_log = event_log

    async def _emit(self, event_type: str, thought: Optional[Thought] = None, **kwargs) -> None:
        """写一条情绪事件日志（无 event_log 时静默跳过）。"""
        if self._event_log is None:
            return
        try:
            await self._event_log.append_event(
                event_type=event_type,
                thought_id=(thought.id if thought else kwargs.get("thought_id", "")),
                act=(thought.act if thought else kwargs.get("act", "")),
                desc=(thought.desc if thought else kwargs.get("desc", "")),
                ne_delta=kwargs.get("ne_delta", 0.0),
                da_delta=kwargs.get("da_delta", 0.0),
            )
        except Exception as e:
            logger.warning(f"写情绪事件日志失败: {e}")

    @handle_errors()
    def load_from_file(self):
        if not self.persistence_file.exists():
            return
        with open(self.persistence_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            self.thoughts = [Thought.from_dict(t) for t in data.get("thoughts", [])]
            self.timestamp = data.get("timestamp", int(time.time()))
            before_count = len(self.thoughts)
            self._cleanup_expired_sync()
            if before_count != len(self.thoughts):
                self.save_to_file()
            logger.info(f"Loaded {len(self.thoughts)} thoughts from persistence")

    @handle_errors()
    def save_to_file(self):
        self._cleanup_expired_sync()
        data = {
            "thoughts": [t.to_dict() for t in self.thoughts],
            "timestamp": int(time.time())
        }
        with open(self.persistence_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _cleanup_expired_sync(self):
        """同步清理过期 thought（供 load/save 等非并发路径使用，不写事件日志）。"""
        before = len(self.thoughts)
        self.thoughts = [t for t in self.thoughts if not t.is_expired()]
        if before != len(self.thoughts):
            logger.debug(f"Cleaned up {before - len(self.thoughts)} expired thoughts")

    async def _cleanup_expired(self):
        """异步清理过期 thought，并写 thought_expired 事件日志。"""
        expired = [t for t in self.thoughts if t.is_expired()]
        if expired:
            self.thoughts = [t for t in self.thoughts if not t.is_expired()]
            logger.debug(f"Cleaned up {len(expired)} expired thoughts")
            for t in expired:
                await self._emit("thought_expired", t)

    async def add(self, thought: Thought, max_stack_override: Optional[int] = None):
        """添加 thought。

        行为：
        1. 查 act 在 ACT_EFFECTS 中的配置（max_stack / stack_coefficient）
           - 未注册：跳过 intensity 设置（保持传入值）
        2. 决定 thought 的 intensity：
           - 同 act 池为空 → intensity = 1.0
           - 同 act 池已有 thought → intensity = (1 - stack_coefficient)
        3. 按 max_stack 处理：
           - max_stack == 0（system 类专用）→ 不应走此路径，应由 ensure_system_thought 管理
           - max_stack == 1（单 instance）→ 池已有同 act：刷新现有 thought 的 expire_time/create_time/desc，
             保持原 ID（不创建新 instance）
           - max_stack > 1（多 instance）→ 池满：FIFO 替换（移除最早，添加新）

        max_stack_override: 可选堆叠上限覆盖（仅对未注册进 ACT_EFFECTS 的 act 生效，
            如最终评价词），None 时按 ACT_EFFECTS 配置；池满同样 FIFO 替换最早。

        异常：
        - 若 act 属于 system 类（max_stack=0 含义为"无限"），拒绝添加
        """
        async with self._lock:
            await self._cleanup_expired()

            if ACTEffects.category_of(thought.act) == "system":
                logger.warning(f"add() 拒绝 system 类 act thought: {thought.act}（应由 ensure_system_thought 管理）")
                return

            effect = ACTEffects.lookup(thought.act)
            if effect:
                max_stack = effect["max_stack"]
                stack_coefficient = effect["stack_coefficient"]

                same_act_thoughts = [t for t in self.thoughts if t.act == thought.act]
                same_act_count = len(same_act_thoughts)

                # 决定 intensity
                if same_act_count == 0:
                    thought.intensity = 1.0
                else:
                    thought.intensity = 1.0 - stack_coefficient

                if max_stack == 1:
                    # 单 instance 语义：刷新现有 thought，不创建新 instance（保持 ID 稳定）
                    if same_act_count >= 1:
                        old = same_act_thoughts[0]
                        old.expire_time = thought.expire_time
                        old.create_time = thought.create_time
                        old.desc = thought.desc
                        old.intensity = thought.intensity
                        logger.debug(f"max_stack=1 刷新：act='{thought.act}' 续期 id={old.id}")
                        await self._emit("thought_refreshed", old)
                        await self._cleanup_expired()
                        return
                elif max_stack > 1:
                    # 多 instance 语义：FIFO 替换
                    if same_act_count >= max_stack:
                        oldest = min(same_act_thoughts, key=lambda t: t.create_time)
                        self.thoughts.remove(oldest)
                        logger.debug(f"FIFO 替换：act='{thought.act}' 移除最早 thought id={oldest.id}")
                        await self._emit("thought_replaced", oldest)
            else:
                # act 未注册（如最终评价词）：intensity 保持传入值（默认 1.0）；
                # 显式传入 max_stack_override 时按该上限 FIFO 封顶（2026-09-24：
                # 所有想法统一堆叠上限 3）
                if max_stack_override is not None:
                    same_act_thoughts = [t for t in self.thoughts if t.act == thought.act]
                    if len(same_act_thoughts) >= max_stack_override:
                        oldest = min(same_act_thoughts, key=lambda t: t.create_time)
                        self.thoughts.remove(oldest)
                        logger.debug(f"FIFO 替换（未注册 act 封顶 {max_stack_override}）：act='{thought.act}' 移除最早 id={oldest.id}")
                        await self._emit("thought_replaced", oldest)
                thought.intensity = thought.intensity if thought.intensity else 1.0

            self.thoughts.append(thought)
            await self._emit("thought_added", thought)
            await self._cleanup_expired()

    def has_thought_with_id(self, thought_id: str) -> bool:
        """判断当前 thoughts 列表中是否已存在指定 ID 的 thought（无论是否过期都视为存在）"""
        return any(t.id == thought_id for t in self.thoughts)

    def remove_thoughts_by_act(self, act: str) -> int:
        """移除所有 act 等于指定值的 thought。返回移除条数。
        用于迁移：清理旧版本中以普通 act 形式存储的系统 thought。
        """
        before = len(self.thoughts)
        self.thoughts = [t for t in self.thoughts if t.act != act]
        return before - len(self.thoughts)

    async def ensure_system_thought(self, act_name: str, thought_id: str, desc: str, duration: int) -> bool:
        """保证指定系统 act 在 thought_cache 中存在且唯一。

        行为：
        1. 检查 thought_id 是否已存在；存在 → 跳过
        2. 不存在 → 创建 Thought，append 到列表，持久化

        返回 True 表示本次新注入了 thought；False 表示已存在。
        """
        async with self._lock:
            await self._cleanup_expired()
            if self.has_thought_with_id(thought_id):
                return False
            now = int(time.time())
            thought = Thought(
                id=thought_id,
                create_time=now,
                desc=desc,
                act=act_name,
                expire_time=now + duration,
            )
            self.thoughts.append(thought)
            await self._emit("thought_added", thought)
            self.save_to_file()
            logger.info(f"注入系统内置 thought: act='{act_name}', id='{thought_id}'")
            return True

    async def create_thought_debug(
        self,
        act: str,
        desc: str,
        intensity: float = 1.0,
        duration: Optional[int] = None,
        create_time: Optional[int] = None,
    ) -> Thought:
        """（调试/测试专用）直接在想法池新建一条 thought，绕过 add() 的 stacking/强度推导。

        供 web 端想法池编辑器使用。system 类 act 拒绝直建（由 ensure_system_thought 管理）。
        """
        if ACTEffects.category_of(act) == "system":
            raise ValueError(f"act '{act}' 属于系统内置类，禁止直接创建（由 ensure_system_thought 管理）")
        async with self._lock:
            await self._cleanup_expired()
            now = int(time.time())
            thought = Thought(
                id=generate_timestamp_id(),
                create_time=create_time if create_time is not None else now,
                desc=desc,
                act=act,
                expire_time=now + (duration if duration is not None else THOUGHT_DURATION),
                intensity=clamp(float(intensity), 0.0, 10.0),
            )
            self.thoughts.append(thought)
            await self._emit("thought_added", thought)
            return thought

    async def update_thought_debug(
        self,
        thought_id: str,
        *,
        desc: Optional[str] = None,
        act: Optional[str] = None,
        intensity: Optional[float] = None,
        expire_time: Optional[int] = None,
    ) -> Thought:
        """（调试/测试专用）修改想法池中一条 thought 的字段；id 不存在抛 ValueError。

        仅允许编辑非 system 类 thought：system 类由 bootstrap 管理，
        改 act 会破坏其唯一语义，故禁止改 act（desc/强度/时长可改）。
        """
        async with self._lock:
            target = next((t for t in self.thoughts if t.id == thought_id), None)
            if target is None:
                raise ValueError(f"thought '{thought_id}' 不存在")
            if act is not None:
                if ACTEffects.category_of(act) == "system":
                    raise ValueError(f"act '{act}' 属于系统内置类，禁止改为系统 act")
                target.act = act
            if desc is not None:
                target.desc = desc
            if intensity is not None:
                target.intensity = clamp(float(intensity), 0.0, 10.0)
            if expire_time is not None:
                target.expire_time = int(expire_time)
            await self._emit("thought_updated", target)
            return target

    async def delete_thought(self, thought_id: str) -> None:
        """（调试/测试专用）从想法池中删除一条 thought；id 不存在抛 ValueError。"""
        async with self._lock:
            target = next((t for t in self.thoughts if t.id == thought_id), None)
            if target is None:
                raise ValueError(f"thought '{thought_id}' 不存在")
            self.thoughts.remove(target)
            await self._emit("thought_removed", target)

    async def get_active_thoughts(self) -> List[Thought]:
        async with self._lock:
            await self._cleanup_expired()
            return self.thoughts.copy()

    async def get_active_thought_ids(self) -> set:
        """返回当前所有未过期 thought 的 ID 集合。
        用于 input_manager 同步"已输出"缓存与 thought_cache 现状。
        """
        async with self._lock:
            await self._cleanup_expired()
            return {t.id for t in self.thoughts}

    async def calculate_mood_snapshot(self) -> MoodSnapshot:
        """计算当前情绪快照。

        每个 thought 贡献 = intensity * act 的 ne/da delta。
        所有 thought 贡献加性累加，最后 clamp 到 [-100, 100]。

        thought 的 intensity 在 add() 时已根据"是否该 act 池已有 thought"确定。
        """
        async with self._lock:
            before_count = len(self.thoughts)
            await self._cleanup_expired()
            if before_count != len(self.thoughts):
                self.save_to_file()
            if not self.thoughts:
                return MoodSnapshot(
                    norepinephrine=BASE_VALUE,
                    dopamine=BASE_VALUE,
                    top_thoughts=[],
                )

            total_norepinephrine = 0.0
            total_dopamine = 0.0

            for t in self.thoughts:
                effect = ACTEffects.lookup(t.act)
                if not effect:
                    continue
                total_norepinephrine += t.intensity * effect["ne_delta"]
                total_dopamine += t.intensity * effect["da_delta"]

            norepinephrine = clamp(total_norepinephrine, MIN_VALUE, MAX_VALUE)
            dopamine = clamp(total_dopamine, MIN_VALUE, MAX_VALUE)

            # 按时间排序取前 3 条
            sorted_thoughts = sorted(self.thoughts, key=lambda x: x.create_time, reverse=True)
            selected_thoughts = sorted_thoughts[:3]

            return MoodSnapshot(
                norepinephrine=norepinephrine,
                dopamine=dopamine,
                top_thoughts=selected_thoughts,
            )


def calculate_duration(act: str = None) -> int:
    """计算 thought 持续时间（秒）。
    优先使用 act 表中的 duration 字段；缺省时回退到 THOUGHT_DURATION。
    """
    if act:
        effect = ACTEffects.lookup(act)
        if effect and "duration" in effect:
            return int(effect["duration"])
    return THOUGHT_DURATION
