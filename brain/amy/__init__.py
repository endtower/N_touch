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
- emotional_interface: 对外统一接口（build_thought 根方法 + build_conversation_thought 适配层 + get_amy_description 统一出口）

API 文档：docs/api/amy.md —— 修改本包对外接口后必须同步更新该文档
（校验：python scripts/check_api_docs.py）。
"""

from brain.amy.constants import (
    # ACT 表
    ExternalInteractionAct, EvaluationAct, SelfActionAct, SystemBuiltInAct, ACTEffects,
    Thought, RelationEvent, EventBase, BuildThoughtInput,
    ALL_ACT_ENTRIES, ACT_EFFECTS_BY_CATEGORY, ACT_EFFECTS,
    generate_timestamp_id, clamp,
    DOPAMINE_DESCRIPTIONS, NOREPINEPHRINE_DESCRIPTIONS,
    BASE_VALUE, MIN_VALUE, MAX_VALUE,
)
from brain.amy.emotional_interface import EmotionalInterface, get_emotional_interface, reset_emotional_interface
from brain.amy.event_log import EventLog

__all__ = [
    # ACT 表（四个独立类 + 合并视图）
    "ExternalInteractionAct", "EvaluationAct", "SelfActionAct", "SystemBuiltInAct", "ACTEffects",
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