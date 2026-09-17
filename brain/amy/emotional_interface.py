#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 情绪系统核心接口

提供两个核心方法：
- build_thought: 根方法，不约束 act 输入，调用方全责（唯一写入入口）
- build_conversation_thought: 适配层（薄），用于 conversation tool 路径，内部调 build_thought
- get_language_description: 表现侧出口（仅 NE/DA），conversation / social_chat 专用
- get_decision_description: 决策侧出口（仅 SC），pfca / proactive / light_agent 专用
- get_amy_description: 完整视图出口（三行），仅供 adapt/web/amy_api 前端展示
- get_mood_snapshot: 统一快照出口，供需要数值快照的消费者（reorganize/strategy_check/amy_api）使用
- apply_final_evaluation: PFCA 收尾最终评价登记，按评价类别刷新 SC 常驻标量并持久化

情绪体系隔离（2026-09-13）：
- 表现侧（NE/DA）与决策侧（SC）完全隔离：
  conversation / social_chat 只见 NE/DA 两行 + 非最终评价类想法原因；
  pfca / proactive / light_agent 只见 SC 行动行 + 最终评价类想法原因。
- 切分点：act 是否命中 FinalEvaluationAct 词表（prompt_engine._split_thoughts）。

情绪消费者游标：
- 用"事件日志 + 消费者游标"替代"记录上次情绪值"去重逻辑
- get_unread_events(consumer_id) / advance_cursor(consumer_id, seq)

注：崩溃/覆盖机制已于 2026-08-30 移除（拍板记录见 docs/amy情绪系统设计思路.md），
get_amy_description / build_thought 不再包含覆盖态分支与崩溃到期检查。
"""

import time
from pathlib import Path
from typing import Optional, Tuple, List

from brain.amy.constants import (
    ACT_EFFECTS, ACTEffects,
    SystemBuiltInAct,
    Thought, MoodSnapshot, generate_timestamp_id, THOUGHT_DURATION,
    FinalEvaluationAct, FINAL_EVALUATION_THOUGHT_DURATION,
    SC_DEFAULT, SC_EFFECT_GOOD, SC_EFFECT_BAD,
)
from brain.amy.thought_cache import ThoughtCache, calculate_duration
from brain.amy.sc_state import ScState, reset_sc_state
from brain.amy.smoothing import (
    SMOOTH_TARGET, SMOOTH_STEP,
    pull_toward_value,
)
from brain.amy.affinity import AffinityStore
from brain.amy.event_log import EventLog
from brain.amy.prompt_engine import (
    get_amy_description,
    build_language_mood_prompt,
    build_decision_mood_prompt,
)
from utils.logger import get_logger

logger = get_logger('amy_interface', 'INFO')


class EmotionalInterface:
    """情绪系统对外接口（普通类，单例由模块级 get_emotional_interface 持有）。"""

    def __init__(self, persistence_dir: Optional[Path] = None):
        if persistence_dir is None:
            persistence_dir = Path(__file__).resolve().parent / "persistence"
        persistence_dir.mkdir(exist_ok=True)
        # 情绪事件日志（内存实现），注入给 thought_cache 写事件
        self.event_log = EventLog()
        self.thought_cache = ThoughtCache(persistence_dir, event_log=self.event_log)
        self.affinity_store = AffinityStore(persistence_dir)
        # SC（自我一致性/认知自信轴）常驻标量（同一目录持久化），绑定给 thought_cache 写快照
        self.sc_state = ScState(persistence_dir)
        self.thought_cache.bind_sc_state(self.sc_state)
        logger.info("EmotionalInterface initialized")

    async def _bootstrap_system_acts(self) -> None:
        """启动时初始化所有系统内置 act（async，由 get_emotional_interface 首次创建后 await）。

        行为：
        1. 迁移：清理所有 act="长期正面心情" 的旧 thought
           （该 act 已改名"长期基本想法"，按旧 act 名清理遗留，避免旧 ID thought 残留）
        2. 按 SystemBuiltInAct.items 遍历，ensure_system_thought() 保证每个 act 唯一存在；
           条目声明 sc_value 时，仅首启（无 SC 持久化文件）把 SC 锚定到该基线值
        """
        # 迁移：清理旧版"长期正面心情" thought（改名为"长期基本想法"前的遗留）
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
            # SC 锚定：仅首启（无 SC 持久化文件）生效，不覆盖既有评价结果；
            # 存量低值（如历史塌陷的 0）由惰性衰减朝基线 SC_DECAY_TARGET 回升自愈
            sc_value = entry.get("sc_value")
            if sc_value is not None and not self.sc_state.persistence_file.exists():
                self.sc_state.set_value(sc_value)
                logger.info(f"首启锚定 SC={sc_value}（系统内置 act '{entry['name']}'）")

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

        # 表现侧出口（仅 NE/DA + 非最终评价类原因）：本 prompt 经
        # build_conversation_thought 返回给 conversation / social_chat 的
        # emotional_judgment 工具，与决策侧（PFCA）情绪体系隔离。
        prompt = build_language_mood_prompt(snapshot, with_reason=True)

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

    # ===== SC 最终评价登记 =====

    async def apply_final_evaluation(self, final_evaluation: str, title: str) -> None:
        """PFCA 收尾最终评价登记：按评价类别刷新 SC 常驻标量并持久化，同时计入想法池。

        规则：
        - good → 当前 SC + SC_EFFECT_GOOD
        - bad → 当前 SC + SC_EFFECT_BAD
        - none → pull_toward_value 朝 SMOOTH_TARGET（40）单点抚平 SMOOTH_STEP
        写盘后，把本次评价作为 thought 入池（act=评价词，desc=「在事件{title} 中，{评价词}」，
        时长 4 小时），供给情绪快照 / 描述合成消费（决策侧专属原因段）。
        最后经决策侧出口 get_decision_description() 刷新情绪描述。
        评价词未命中词表直接抛 ValueError（不吞异常，关键失败直接阻断上抛）；
        title 为空直接抛 ValueError（最终评价想法必须绑定事件标题）。
        """
        kind = FinalEvaluationAct.kind_of(final_evaluation)
        if kind is None:
            allowed = "、".join(FinalEvaluationAct.names())
            raise ValueError(f"未知最终评价: '{final_evaluation}'，允许词表: {allowed}")
        if not title or not title.strip():
            raise ValueError("最终评价必须提供事件标题 title（≤15 字）")

        current = self.sc_state.read_value()
        if kind == "good":
            new_value = current + SC_EFFECT_GOOD
        elif kind == "bad":
            new_value = current + SC_EFFECT_BAD
        else:  # "none"：无评价抚平，朝目标点 40 收敛（不再使用区间语义）
            new_value = pull_toward_value(
                current, SMOOTH_TARGET, SMOOTH_STEP
            )

        self.sc_state.set_value(new_value)
        self.sc_state.save_to_file()  # 保存前自动再结算一次惰性衰减

        # 最终评价计入想法池：act=评价词（前端归为 final 分类），desc 带事件标题，时长 4 小时。
        # 不产生 NE/DA 增量（final 词表不注册进 ACTEffects，快照计算自然跳过边际贡献）。
        title = title.strip()
        now = int(time.time())
        thought = Thought(
            id=generate_timestamp_id(),
            create_time=now,
            desc=f"在事件{title} 中，{final_evaluation}",
            act=final_evaluation,
            expire_time=now + FINAL_EVALUATION_THOUGHT_DURATION,
        )
        await self.thought_cache.add(thought)
        self.thought_cache.save_to_file()

        # 刷新决策侧情绪描述（消费者各自经门面拉取，此处仅确认可生成）
        await self.get_decision_description()

    # ===== 统一出站接口（情绪体系隔离：表现侧 / 决策侧 / 完整视图） =====

    async def get_language_description(self) -> str:
        """表现侧出口（仅 NE/DA）：conversation / social_chat 专用。

        两行激素描述（NE 描述 + DA 描述）+ 原因（仅非最终评价类想法），
        不含 SC 行动行——决策侧（PFCA）对此完全不可见，反之亦然。
        """
        snapshot = await self.thought_cache.calculate_mood_snapshot()
        return build_language_mood_prompt(snapshot, with_reason=True)

    async def get_decision_description(self) -> str:
        """决策侧出口（仅 SC）：pfca / proactive / light_agent 专用。

        仅一行 SC 行动对应（sc_act_line）+ 原因（仅最终评价类想法），
        不含 NE/DA 行——表现侧（conversation/social_chat）对此完全不可见，反之亦然。
        """
        snapshot = await self.thought_cache.calculate_mood_snapshot()
        return build_decision_mood_prompt(snapshot, with_reason=True)

    async def get_amy_description(self) -> str:
        """完整视图出口（三行激素描述 + 全部想法原因）：
        仅供 adapt/web/amy_api 前端展示。业务消费方必须改走
        get_language_description（表现侧）/ get_decision_description（决策侧）。
        """
        snapshot = await self.thought_cache.calculate_mood_snapshot()
        return get_amy_description(snapshot, with_reason=True)

    async def get_mood_snapshot(self) -> MoodSnapshot:
        """统一快照出口：供需要数值快照的消费者（reorganize/strategy_check/amy_api）使用。"""
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

    async def get_affinity(self, user_id: str) -> float:
        """获取好感度（async，返回 float）。"""
        return await self.affinity_store.get_affinity(user_id)


_emotional_interface: Optional[EmotionalInterface] = None


async def get_emotional_interface(persistence_dir: Optional[Path] = None) -> EmotionalInterface:
    """获取 amy 单例。首次创建时完成系统 act 引导。

    注意：persistence_dir 仅在首次创建时生效；换目录走 reset_emotional_interface()
    后重建（web 预设 / 测试隔离流程依赖该语义）。
    """
    global _emotional_interface
    if _emotional_interface is None:
        _emotional_interface = EmotionalInterface(persistence_dir)
        await _emotional_interface._bootstrap_system_acts()
    return _emotional_interface


async def reset_emotional_interface() -> None:
    """清除单例（测试/预设环境用）。下次 get_emotional_interface 重建。"""
    global _emotional_interface
    _emotional_interface = None
    reset_sc_state()
