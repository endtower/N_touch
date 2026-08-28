#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 情绪系统

去甲肾上腺素与多巴胺双值情绪系统

核心组件：
- constants: ACT 表（ConversationAct + InternalAct）、状态枚举、崩溃参数
- thought_cache: 短期情绪想法缓存 + 快照计算
- affinity: 亲密度存储
- crash: 崩溃状态机（三态单槽位）
- crash_agent: 崩溃判定 Agent（同步阻塞调 LLM）
- prompt_engine: 情绪快照 -> 提示词文本
- emotional_interface: 对外统一接口（build_thought 根方法 + build_conversation_thought 适配层 + get_amy_description 统一出口）
"""

from brain.amy.constants import (
    # ACT 表
    ExternalInteractionAct, EvaluationAct, SelfActionAct, SystemBuiltInAct, ACTEffects,
    SystemState, CrashInfo, SystemStateData, MoodSnapshot,
    Thought, RelationEvent, EventBase, BuildThoughtInput,
    ALL_ACT_ENTRIES, ACT_EFFECTS_BY_CATEGORY, ACT_EFFECTS,
    generate_timestamp_id, clamp,
    DOPAMINE_DESCRIPTIONS, NOREPINEPHRINE_DESCRIPTIONS,
    BASE_VALUE, MIN_VALUE, MAX_VALUE,
    CRASH_LONG_DURATION, NOT_CRASHED_SHORT_DURATION,
    CRASH_POST_PROTECT_DURATION,
    CRASH_INDEX_THRESHOLD,
    DEFAULT_CRASH_DESCRIPTION, DEFAULT_NOT_CRASHED_DESCRIPTION,
    DEFAULT_CRASH_DESCRIPTION_NE_HIGH,
)
from brain.amy.emotional_interface import EmotionalInterface, get_emotional_interface
from brain.amy.crash import CrashSystem, CrashMode
from brain.amy.event_log import EventLog

__all__ = [
    # ACT 表（四个独立类 + 合并视图）
    "ExternalInteractionAct", "EvaluationAct", "SelfActionAct", "SystemBuiltInAct", "ACTEffects",
    "ALL_ACT_ENTRIES", "ACT_EFFECTS_BY_CATEGORY", "ACT_EFFECTS",
    # 状态与数据结构
    "SystemState", "CrashInfo", "SystemStateData", "MoodSnapshot",
    "Thought", "RelationEvent", "EventBase", "BuildThoughtInput",
    # 工具函数
    "generate_timestamp_id", "clamp",
    # 描述表
    "DOPAMINE_DESCRIPTIONS", "NOREPINEPHRINE_DESCRIPTIONS",
    # 基础参数
    "BASE_VALUE", "MIN_VALUE", "MAX_VALUE",
    # 崩溃参数
    "CRASH_LONG_DURATION", "NOT_CRASHED_SHORT_DURATION",
    "CRASH_POST_PROTECT_DURATION",
    "CRASH_INDEX_THRESHOLD",
    "DEFAULT_CRASH_DESCRIPTION", "DEFAULT_NOT_CRASHED_DESCRIPTION",
    "DEFAULT_CRASH_DESCRIPTION_NE_HIGH",
    # 崩溃模式
    "CrashMode",
    # 核心类
    "EmotionalInterface", "get_emotional_interface", "CrashSystem", "EventLog",
]