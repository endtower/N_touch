#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 提示词拼接引擎

将情绪状态转换为可注入到 LLM 的提示词文本
去甲肾上腺素与多巴胺双值系统

情绪体系隔离（2026-09-13；2026-09-27 SC 轴剔除；2026-09-28 新增反馈数值视图）：
- 表现侧（NE/DA）：conversation / social_chat 专用，build_language_mood_prompt
- 旧决策侧想法渲染：build_decision_mood_prompt 保留兼容；当前双轴转到 decision_feelings
- 完整视图       ：仅供 adapt/web/amy_api 前端展示，build_mood_prompt
- 旧单轴视图：build_feedback_prompt / build_presence_attribution 保留兼容
- 当前决策与 Reflection：decision_feelings.build_decision_feedback_prompt
两侧通过 FinalEvaluationAct 类别切分想法原因段，实现情绪体系互不可见。
"""

from brain.amy.decision_feelings import AXIS_NAMES
from typing import List
from utils.logger import get_logger
from brain.amy.constants import (
    MoodSnapshot, Thought,
    DOPAMINE_DESCRIPTIONS, NOREPINEPHRINE_DESCRIPTIONS,
    FEEDBACK_DESCRIPTIONS, FEEDBACK_MAX,
    FinalEvaluationAct,
)
from brain.amy.debt_ledger import DebtEntry

logger = get_logger('amy_prompt', 'INFO')

def get_dopamine_desc(value: float) -> str:
    for low, high, desc in DOPAMINE_DESCRIPTIONS:
        if low <= value < high:
            return desc
    if value >= 90:
        return DOPAMINE_DESCRIPTIONS[0][2]
    return DOPAMINE_DESCRIPTIONS[-1][2]

def get_norepinephrine_desc(value: float) -> str:
    for low, high, desc in NOREPINEPHRINE_DESCRIPTIONS:
        if low <= value < high:
            return desc
    if value >= 90:
        return NOREPINEPHRINE_DESCRIPTIONS[0][2]
    return NOREPINEPHRINE_DESCRIPTIONS[-1][2]

def get_feedback_desc(value: float) -> str:
    """把反馈维度数值映射到 5 档语义词（贫乏 / 较低 / 正常 / 较高 / 充盈）。"""
    for low, high, desc in FEEDBACK_DESCRIPTIONS:
        if low <= value < high:
            return desc
    return FEEDBACK_DESCRIPTIONS[0][2] if value >= FEEDBACK_MAX else FEEDBACK_DESCRIPTIONS[-1][2]


def build_thought_summary(top_thoughts: List[Thought], prefix: str = "原因：") -> str:
    """把 thoughts 列表渲染为"编号 + 聚合"的多行文本，供所有模块统一复用。
    格式：
        {prefix}
        1. {desc}（{desc_extra}）   ← 如果 thought.desc_extra 非空，附加在 desc 后
        2. {desc}（×{count}）
        3. {desc}

    desc_extra 是 act 字典里登记的"附加描述"，仅在本渲染函数中使用；
    普通业务（emotional_judgment 工具返回、amy 内部情绪合成）不会读它。

    Args:
        top_thoughts: Thought 列表（通常传全部活跃 thoughts，不必限定 top3）
        prefix: 标题行，传空字符串则不输出标题行（仅返回列表内容）

    Returns:
        拼接好的多行字符串；thoughts 为空时返回空字符串。
    """
    if not top_thoughts:
        return ""

    desc_counts: dict[str, int] = {}
    desc_extras: dict[str, str] = {}
    for thought in top_thoughts:
        desc = getattr(thought, "desc", "") or ""
        if not desc:
            continue
        desc_counts[desc] = desc_counts.get(desc, 0) + 1
        # 聚合时取首个非空 desc_extra 作为该 desc 的附加描述
        extra = (getattr(thought, "desc_extra", "") or "").strip()
        if extra and desc not in desc_extras:
            desc_extras[desc] = extra

    if not desc_counts:
        return ""

    def _render_desc(desc: str) -> str:
        extra = desc_extras.get(desc, "")
        if extra:
            return f"{desc}（{extra}）"
        return desc

    lines = []
    for idx, (desc, count) in enumerate(desc_counts.items(), 1):
        rendered = _render_desc(desc)
        if count > 1:
            lines.append(f"{idx}. {rendered}（×{count}）")
        else:
            lines.append(f"{idx}. {rendered}")

    body = "\n".join(lines)
    return f"{prefix}\n{body}" if prefix else body

def _split_thoughts(top_thoughts: List[Thought], final_only: bool) -> List[Thought]:
    """按 act 类别切分想法列表（情绪体系隔离的切分点）。

    act 命中 FinalEvaluationAct 词表 ⇔ 属最终评价类想法（决策侧专属）。
    final_only=True  仅保留最终评价类想法（决策侧原因段）；
    final_only=False 仅保留非最终评价类想法（表现侧原因段：系统 act /
    conversation·social_chat 的情感遭遇等）。
    """
    kept: List[Thought] = []
    for thought in top_thoughts:
        act = getattr(thought, "act", "") or ""
        is_final = FinalEvaluationAct.contains(act)
        if is_final == final_only:
            kept.append(thought)
    return kept


def build_mood_prompt(snapshot: MoodSnapshot, with_reason: bool = False) -> str:
    """完整视图：两行（NE 描述 + DA 描述），仅供 adapt/web/amy_api 前端展示。

    消费方禁用本视图：表现侧（conversation/social_chat）走 build_language_mood_prompt，
    决策侧（pfca/proactive/light_agent）走 build_decision_mood_prompt，
    保证 pfca 与 conversation 情绪体系隔离（见 docs/api/amy.md）。

    原因段仅在 with_reason=True 时追加在末尾（全部想法，不分区）。
    """
    ne_desc = get_norepinephrine_desc(snapshot.norepinephrine)
    da_desc = get_dopamine_desc(snapshot.dopamine)

    parts = [ne_desc, da_desc]
    if with_reason:
        thought_summary = build_thought_summary(snapshot.top_thoughts)
        if thought_summary:
            parts.append(thought_summary)
    return "\n".join(parts)

def build_language_mood_prompt(snapshot: MoodSnapshot, with_reason: bool = False) -> str:
    """表现侧视图（仅 NE/DA）：conversation / social_chat 专用。

    输出两行激素描述（NE 描述 + DA 描述）；
    原因段（with_reason=True 时）仅渲染非最终评价类想法
    （系统 act / 情感遭遇），与决策侧（PFCA）完全隔离。
    """
    ne_desc = get_norepinephrine_desc(snapshot.norepinephrine)
    da_desc = get_dopamine_desc(snapshot.dopamine)

    parts = [ne_desc, da_desc]
    if with_reason:
        thought_summary = build_thought_summary(
            _split_thoughts(snapshot.top_thoughts, final_only=False)
        )
        if thought_summary:
            parts.append(thought_summary)
    return "\n".join(parts)

def build_decision_mood_prompt(snapshot: MoodSnapshot, with_reason: bool = False) -> str:
    """决策侧视图：pfca / proactive / light_agent 专用。

    不含 NE/DA 行；原因段（with_reason=True 时）仅渲染最终评价类想法
    （FinalEvaluationAct 词表，PFCA 收尾自我评价），与表现侧完全隔离。
    """
    parts: List[str] = []
    if with_reason:
        thought_summary = build_thought_summary(
            _split_thoughts(snapshot.top_thoughts, final_only=True)
        )
        if thought_summary:
            parts.append(thought_summary)
    return "\n".join(parts)

def build_feedback_prompt(value: float) -> str:
    """旧归属感单轴兼容视图，Reflection 当前读取双轴。

    直接显示归属感数值状态，**不显示任何想法**。
    格式：归属感 {value}/{FEEDBACK_MAX} {词档}
    例：归属感 50/100 正常（词档：贫乏 / 较低 / 正常 / 较高 / 充盈）。
    """
    return f"{AXIS_NAMES['recognition']} {int(round(value))}/{int(FEEDBACK_MAX)} {get_feedback_desc(value)}"


def build_debt_attribution(entries: List[DebtEntry]) -> str:
    """未偿欠账的"原因："归因列表（每笔一行）；当前仅由前端页面视图（build_presence_attribution）消费。

    格式：
        原因：{事件标题} {事件评价} {数值减益}
    无欠账时返回空字符串。
    """
    return "\n".join(f"原因：{entry.title} {entry.kind} {int(entry.score)}" for entry in entries)


def build_presence_attribution(value: float, entries: List[DebtEntry]) -> str:
    """旧归属感归因块（页面兼容）；决策与人格当前读取双轴。

    归属感行**常显**（PFCA / 人格无条件可见自身数值）：
        归属感：{当前值}/100
    存在未偿欠账（负面记录）时追加每笔一行归因：
        原因：{事件标题} {事件评价} {数值减益}
    无欠账时仅返回归属感行。
    """
    lines = [f"{AXIS_NAMES['recognition']}：{int(round(value))}/{int(FEEDBACK_MAX)}"]
    attribution = build_debt_attribution(entries)
    if attribution:
        lines.append(attribution)
    return "\n".join(lines)


def get_amy_description(snapshot: MoodSnapshot, with_reason: bool = False) -> str:
    """完整视图包装（仅供 adapt/web/amy_api 前端展示，见 build_mood_prompt）。"""
    return build_mood_prompt(snapshot, with_reason=with_reason)