#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 提示词拼接引擎

将情绪状态转换为可注入到 LLM 的提示词文本
去甲肾上腺素与多巴胺双值系统
"""

from typing import List
from utils.logger import get_logger
from brain.amy.constants import (
    MoodSnapshot, Thought,
    DOPAMINE_DESCRIPTIONS, NOREPINEPHRINE_DESCRIPTIONS,
)

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

def build_mood_prompt(snapshot: MoodSnapshot, with_reason: bool = False) -> str:
    """生成 amy 情绪描述。

    默认只输出"你当前的情绪：ne_desc。da_desc"这一行（不含原因）。
    参数 with_reason=True 时追加原因段（仅 amy 内部或非 PFCA 调用方使用）。

    为什么不默认带原因：
    - PFCA 的 user 消息里已经自己挑 thought 拼"因为{desc}，你的心情变为：{新感受}"
    - 若 amy 这里再输出一份"原因：\n 在{desc}"，会出现同一 thought 被描述两次的冗余
    """
    ne_desc = get_norepinephrine_desc(snapshot.norepinephrine)
    da_desc = get_dopamine_desc(snapshot.dopamine)

    parts = [f"【当前情绪】{ne_desc}{da_desc}"]
    if with_reason:
        thought_summary = build_thought_summary(snapshot.top_thoughts)
        if thought_summary:
            parts.append(thought_summary)
    return "\n".join(parts)

def get_amy_description(snapshot: MoodSnapshot, with_reason: bool = False) -> str:
    return build_mood_prompt(snapshot, with_reason=with_reason)