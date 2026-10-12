#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""AMY 门面。表现侧沿用 NE/DA；决策侧提供效能感、归属感及可追溯变动。
PFCA 确认收尾时结算两轴；旧 feedback_state value 和欠账台账保持兼容。
"""

from paths import data_root
import time
from pathlib import Path
from typing import Optional, Tuple, List

from brain.amy.constants import (
    ACT_EFFECTS, ACTEffects,
    SystemBuiltInAct,
    Thought, MoodSnapshot, generate_timestamp_id, THOUGHT_DURATION,
    FinalEvaluationAct, TaskResultAct, FINAL_EVALUATION_THOUGHT_DURATION,
    REFLECTION_VERIFICATION_COMPENSATION,
    REFLECTION_SOCIAL_COMPENSATION,
)
from brain.amy.thought_cache import ThoughtCache, calculate_duration
from brain.amy.affinity import AffinityStore
from brain.amy.event_log import EventLog
from brain.amy.decision_feelings import AXIS_NAMES, build_decision_feedback_prompt
from brain.amy.feedback_state import get_feedback_state, reset_feedback_state
from brain.amy.debt_ledger import get_debt_ledger, reset_debt_ledger
from brain.amy.prompt_engine import (
    get_amy_description,
    build_language_mood_prompt,
)
from utils.logger import get_logger

logger = get_logger('amy_interface', 'INFO')


class EmotionalInterface:
    """情绪系统对外接口（普通类，单例由模块级 get_emotional_interface 持有）。"""

    def __init__(self, persistence_dir: Optional[Path] = None):
        if persistence_dir is None:
            persistence_dir = data_root() / "brain" / "amy" / "persistence"
        persistence_dir.mkdir(parents=True, exist_ok=True)
        # 情绪事件日志（内存实现），注入给 thought_cache 写事件
        self.event_log = EventLog()
        self.thought_cache = ThoughtCache(persistence_dir, event_log=self.event_log)
        self.affinity_store = AffinityStore(persistence_dir)
        # 决策侧双轴；旧归属感数据保留，新效能感默认 50
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

    async def apply_reflection_compensation(self, title: str, reason: str,
                                            axis: str = "competence") -> str:
        """安排自修正验证补偿效能感、安排主动社交补偿归属感，均为 +30。

        由 Reflection 的发布入口调用，每项每轮只补偿一次由会话工具管理器保证。
        仅修改指定轴，不生成表达侧想法，不改变 NE/DA 或欠账台账。
        """
        if not isinstance(title, str) or not title.strip():
            raise ValueError("反思补偿必须提供事件标题")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("反思补偿必须提供安排行动的依据")
        if axis == "competence":
            label, delta = "已安排自修正验证", REFLECTION_VERIFICATION_COMPENSATION
        elif axis == "recognition":
            label, delta = "已安排主动社交", REFLECTION_SOCIAL_COMPENSATION
        else:
            raise ValueError("反思补偿轴必须为 competence 或 recognition")
        self.feedback_state.settle(title.strip(), [
            (axis, label, delta, reason.strip()),
        ])
        return await self.get_reflection_description()

    # ===== 最终评价登记 =====

    async def apply_final_evaluation(self, final_evaluation: str, title: str,
                                     task_result: str = TaskResultAct.NEUTRAL,
                                     task_reason: str = "", evaluation_reason: str = "") -> None:
        """确认收尾时结算效能感与归属感。成功/失败都必须给任务原因（通用条目）；不影响 NE/DA。"""
        TaskResultAct.validate(task_result, task_reason)
        if not isinstance(evaluation_reason, str):
            raise ValueError("评价依据必须是文本")
        if not FinalEvaluationAct.contains(final_evaluation):
            allowed = "、".join(FinalEvaluationAct.names())
            raise ValueError(f"未知最终评价: '{final_evaluation}'，允许词表: {allowed}")
        if not title or not title.strip():
            raise ValueError("最终评价必须提供事件标题 title（≤15 字）")

        title = title.strip()
        final_evaluation = FinalEvaluationAct.normalize(final_evaluation)
        now = int(time.time())

        # 任务结果与外部评价分别结算，两个值不相互抵扣。
        delta = FinalEvaluationAct.feedback_delta(final_evaluation)
        self.feedback_state.settle(title, [
            ("competence", task_result, TaskResultAct.deltas[task_result],
             task_reason.strip() or f"PFCA 收尾判断：{task_result}，详见笔记"),
            ("recognition", final_evaluation, delta,
             evaluation_reason.strip() or f"PFCA 根据本轮外部反馈判断：{final_evaluation}，详见笔记"),
        ], task_result=task_result)
        trend = "上升" if delta > 0 else "降低"

        # 欠账台账（系统级共享）：负向评价记一笔明细；正向评价作为额度从最旧欠账开始抵扣。
        if delta < 0:
            self.debt_ledger.record_debt(title, final_evaluation, delta)
            self.debt_ledger.save_to_file()
        elif self.debt_ledger.settle_with_credit(delta) > 0:
            self.debt_ledger.save_to_file()

        # 最终评价计入想法池：act=评价词（前端归为 final 分类），
        # desc 包含事件标题、任务结果与任务原因（成功/失败对称），
        # desc_extra 挂该评价产生时对应笔记的标题，供 LLM 用 query_note 回溯原笔记。
        # 时长 4 小时；遵守统一堆叠上限 3（2026-09-24：池满 FIFO 替换最早）。
        thought = Thought(
            id=generate_timestamp_id(),
            create_time=now,
            desc=f"{title}：{AXIS_NAMES['recognition']}{trend}；任务结果：{task_result}" + (f"；任务原因：{task_reason.strip()}" if task_reason.strip() else ""),
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
        """决策侧读取两轴感受与各自最近五条变动；不注入 NE/DA 表达情绪。"""
        return build_decision_feedback_prompt(self.feedback_state.snapshot())

    async def get_reflection_description(self) -> str:
        """Reflection 读取两轴与各自最近五条变动；不新增低值唤醒或规定补偿行动。"""
        return build_decision_feedback_prompt(self.feedback_state.snapshot())

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
