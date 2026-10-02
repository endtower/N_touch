#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 情绪系统核心接口

提供两个核心方法：
- build_thought: 根方法，不约束 act 输入，调用方全责（唯一写入入口）
- build_conversation_thought: 适配层（薄），用于 conversation tool 路径，内部调 build_thought
- get_language_description: 表现侧出口（仅 NE/DA），conversation / social_chat 专用
- get_decision_description: 决策侧出口（仅最终评价原因段），pfca / proactive / light_agent 专用
- get_amy_description: 完整视图出口，仅供 adapt/web/amy_api 前端展示
- get_mood_snapshot: 统一快照出口，供需要数值快照的消费者（strategy_check/amy_api）使用
- apply_final_evaluation: PFCA 收尾最终评价登记，计入想法池并落库记事本
- feedback_state: 存在感常驻标量（0-100，默认 50，原"反馈感" 2026-09-29 改名），评价时按格子步进结算
- debt_ledger: 欠账台账（系统级共享单例），负向评价记账 / 正向评价按分数值销账；
  有未偿欠账时决策侧出口条件输出「存在感 + 原因」归因行（proactive 据此补偿，不没事找事）

情绪体系隔离（2026-09-13；2026-09-27 SC 轴剔除）：
- 表现侧（NE/DA）与决策侧完全隔离：
  conversation / social_chat 只见 NE/DA 两行 + 非最终评价类想法原因；
  pfca / proactive / light_agent 只见最终评价类想法原因（SC 轴已剔除）。
- 切分点：act 是否命中 FinalEvaluationAct 词表（prompt_engine._split_thoughts）。

存在感（2026-09-28 上线为"反馈感"，2026-09-29 改名为"存在感"；与 AMY 原设计冲突的替代实现，随时可能推翻）：
- 独立标量 feedback_state（0-100，默认 50），**不参与** NE/DA 快照计算；
- 仅在 apply_final_evaluation 单点按格子结算：优秀 +20 / 尚可 +10 / 无评价 -10 / 欠佳 -20；
- 决策侧提示词暂不输出该维度任何描述文本（见 docs/未来改进.md 第一节 1.1）。

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
)
from brain.amy.thought_cache import ThoughtCache, calculate_duration
from brain.amy.affinity import AffinityStore
from brain.amy.event_log import EventLog
from brain.amy.feedback_state import get_feedback_state, reset_feedback_state
from brain.amy.debt_ledger import get_debt_ledger, reset_debt_ledger
from brain.amy.prompt_engine import (
    get_amy_description,
    build_language_mood_prompt,
    build_decision_mood_prompt,
    build_feedback_prompt,
    build_presence_attribution,
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
        # 反馈维度常驻标量（与 AMY 原设计冲突的替代实现，见 docs/未来改进.md 第一节 1.1）
        self.feedback_state = get_feedback_state(persistence_dir)
        # 欠账台账（系统级共享单例）：负向评价记账，正向评价按分数值销账
        self.debt_ledger = get_debt_ledger(persistence_dir)
        logger.info("EmotionalInterface initialized")

    async def _bootstrap_system_acts(self) -> None:
        """启动时初始化所有系统内置 act（async，由 get_emotional_interface 首次创建后 await）。

        行为：
        1. 迁移：清理所有 act="长期正面心情" 的旧 thought
           （该 act 已改名"长期基本想法"，按旧 act 名清理遗留，避免旧 ID thought 残留）
        2. 按 SystemBuiltInAct.items 遍历，ensure_system_thought() 保证每个 act 唯一存在
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

    # ===== 最终评价登记 =====

    async def apply_final_evaluation(self, final_evaluation: str, title: str) -> None:
        """PFCA 收尾最终评价登记：把本次评价计入想法池，并结算反馈维度。

        把本次评价作为 thought 入池（act=评价词，
        desc=「因为 {title} 任务中，导致的{上升/降低}」，时长 4 小时），
        并挂上该评价产生时对应笔记的标题（desc_extra），供 LLM 后续用 query_note 追溯。
        不产生 NE/DA 增量（final 词表不注册进 ACTEffects）。

        同时按**格子步进**结算反馈维度（feedback_state）：
        优秀 +20 / 尚可 +10 / 无评价 -10 / 欠佳 -20（四档对称），clamp 到 [0,100]。
        这是与 AMY 原设计的冲突点，暂以替代办法实现，随时可能推翻
        （见 docs/未来改进.md 第一节 1.1）。

        评价词未命中词表直接抛 ValueError（不吞异常，关键失败直接阻断上抛）；
        title 为空直接抛 ValueError（最终评价想法必须绑定事件标题）。
        """
        if not FinalEvaluationAct.contains(final_evaluation):
            allowed = "、".join(FinalEvaluationAct.names())
            raise ValueError(f"未知最终评价: '{final_evaluation}'，允许词表: {allowed}")
        if not title or not title.strip():
            raise ValueError("最终评价必须提供事件标题 title（≤15 字）")

        title = title.strip()
        now = int(time.time())

        # 反馈维度格子结算（不对称）：好评价上升，其余两词均下降。
        delta = FinalEvaluationAct.feedback_delta(final_evaluation)
        self.feedback_state.apply_delta(delta)
        self.feedback_state.save_to_file()
        trend = "上升" if delta > 0 else "降低"

        # 欠账台账（系统级共享）：负向评价记一笔明细；正向评价作为额度从最旧欠账开始抵扣。
        if delta < 0:
            self.debt_ledger.record_debt(title, final_evaluation, delta)
            self.debt_ledger.save_to_file()
        elif self.debt_ledger.settle_with_credit(delta) > 0:
            self.debt_ledger.save_to_file()

        # 最终评价计入想法池：act=评价词（前端归为 final 分类），
        # desc 采用可追溯形式（因为 {title} 任务中，导致的{上升/降低}），
        # desc_extra 挂该评价产生时对应笔记的标题，供 LLM 用 query_note 回溯原笔记。
        # 时长 4 小时；遵守统一堆叠上限 3（2026-09-24：池满 FIFO 替换最早）。
        thought = Thought(
            id=generate_timestamp_id(),
            create_time=now,
            desc=f"因为 {title} 任务中，导致的{trend}",
            act=final_evaluation,
            expire_time=now + FINAL_EVALUATION_THOUGHT_DURATION,
            desc_extra=f"笔记：{title}",
        )
        await self.thought_cache.add(thought, max_stack_override=3)
        self.thought_cache.save_to_file()

        # 刷新决策侧情绪描述（消费者各自经门面拉取，此处仅确认可生成）
        await self.get_decision_description()

    # ===== 统一出站接口（情绪体系隔离：表现侧 / 决策侧 / 完整视图） =====

    async def get_language_description(self, with_reason: bool = True) -> str:
        """表现侧出口（仅 NE/DA）：conversation / social_chat 专用。

        两行激素描述（NE 描述 + DA 描述）+ 原因（仅非最终评价类想法，with_reason=True 时），
        不含最终评价类原因——决策侧（PFCA）对此完全不可见，反之亦然。

        2026-09-29：表现侧展示块暂时隐藏原因（传 with_reason=False，仅供展示）；
        经验检索的心情通道 query 仍用带原因原串（with_reason=True），检索行为不变。
        """
        snapshot = await self.thought_cache.calculate_mood_snapshot()
        return build_language_mood_prompt(snapshot, with_reason=with_reason)

    async def get_decision_description(self) -> str:
        """决策侧出口：pfca / proactive / light_agent 专用。

        仅原因（仅最终评价类想法），不含 NE/DA 行——表现侧（conversation/social_chat）
        对此完全不可见，反之亦然。SC 轴已于 2026-09-27 剔除，此处不再输出行动行。

        存在感归因（2026-09-29）：末尾追加存在感行（**常显**，PFCA 无条件可见自身数值）——
        存在感：{当前值}/100
        存在未偿欠账（负面记录）时追加每笔一行归因：
        原因：{事件标题} {事件评价} {数值减益}
        无欠账仅存在感行，系统不会凭空行动（"找点事做不能没事找事"）。
        """
        snapshot = await self.thought_cache.calculate_mood_snapshot()
        base = build_decision_mood_prompt(snapshot, with_reason=True)
        segments = [base] if base else []

        attribution = build_presence_attribution(
            self.feedback_state.read_value(), self.debt_ledger.list_debts()
        )
        if attribution:
            segments.append(attribution)

        return "\n".join(segments)

    async def get_reflection_description(self) -> str:
        """Reflection 专用出口：存在感数值视图（不显示任何想法）。

        仅输出存在感数值状态行（如「存在感 50/100 正常」），
        不含 NE/DA 行、不含任何想法原因——与决策侧出口
        get_decision_description（pfca / proactive / light_agent 仍走该出口）
        相互独立，仅 reflection 使用本出口。
        """
        return build_feedback_prompt(self.feedback_state.read_value())

    async def get_amy_description(self) -> str:
        """完整视图出口（激素描述 + 全部想法原因）：
        仅供 adapt/web/amy_api 前端展示。业务消费方必须改走
        get_language_description（表现侧）/ get_decision_description（决策侧）。
        """
        snapshot = await self.thought_cache.calculate_mood_snapshot()
        return get_amy_description(snapshot, with_reason=True)

    async def get_mood_snapshot(self) -> MoodSnapshot:
        """统一快照出口：供需要数值快照的消费者（strategy_check/amy_api）使用。"""
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
    reset_feedback_state()
    reset_debt_ledger()
