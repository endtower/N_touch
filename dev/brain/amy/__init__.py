#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 情绪系统

去甲肾上腺素与多巴胺双值情绪系统

核心组件：
- constants: ACT 表（ConversationAct + InternalAct）、状态枚举、崩溃参数
- thought_cache: 短期情绪想法缓存 + 快照计算
- affinity: 亲密度存储
- prompt_engine: 情绪快照 -> 提示词文本
- emotional_interface: 对外统一接口（build_thought 根方法 + build_conversation_thought 适配层
  + get_language_description 表现侧出口（仅 NE/DA，conversation/social_chat 专用）
  + get_decision_description 决策侧出口（仅最终评价原因段，pfca/proactive/light_agent 专用）
  + get_reflection_description Reflection 专用出口（反馈数值状态行，不显示想法）
  + get_amy_description 完整视图出口（仅供 adapt/web/amy_api 前端展示））

API 文档：docs/api/amy.md —— 修改本包对外接口后必须同步更新该文档
（校验：python scripts/check_api_docs.py）。
"""

from brain.amy.constants import (
    # ACT 表
    ExternalInteractionAct, EvaluationAct, SelfActionAct, PfcaRegulationAct,
    SystemBuiltInAct,
    FinalEvaluationAct, TaskResultAct, ACTEffects,
    Thought, RelationEvent, EventBase, BuildThoughtInput, MoodSnapshot,
    ALL_ACT_ENTRIES, ACT_EFFECTS_BY_CATEGORY, ACT_EFFECTS,
    generate_timestamp_id, clamp,
    DOPAMINE_DESCRIPTIONS, NOREPINEPHRINE_DESCRIPTIONS,
    BASE_VALUE, MIN_VALUE, MAX_VALUE,
)
from brain.amy.emotional_interface import EmotionalInterface, get_emotional_interface, reset_emotional_interface
from brain.amy.event_log import EventLog

__all__ = [
    # ACT 表（词表类 + 合并视图）
    "ExternalInteractionAct", "EvaluationAct", "SelfActionAct",
    "PfcaRegulationAct", "SystemBuiltInAct",
    "FinalEvaluationAct", "TaskResultAct", "ACTEffects",
    "ALL_ACT_ENTRIES", "ACT_EFFECTS_BY_CATEGORY", "ACT_EFFECTS",
    # 状态与数据结构
    "MoodSnapshot",
    "Thought", "RelationEvent", "EventBase", "BuildThoughtInput",
    # 工具函数
    "generate_timestamp_id", "clamp",
    # 描述表
    "DOPAMINE_DESCRIPTIONS", "NOREPINEPHRINE_DESCRIPTIONS",
    # 基础参数
    "BASE_VALUE", "MIN_VALUE", "MAX_VALUE",
    # 核心类
    "EmotionalInterface", "get_emotional_interface", "reset_emotional_interface",
    "EventLog",
]