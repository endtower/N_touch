# -*- coding: utf-8 -*-
"""内心人格模块 - LLM 多重人格机制（取代 advance agent 系统）。

组成：
- persona_registry：人格定义（PersonaDef）与注册表，内置 assistant / normal /
  task 三个统合型人格，外加一条平行的基本人格条目（persona_name="common"，
  先于各人格定义注入，供未来反思系统修改与扩展）。
- persona_agent：人格自荐 agent，按 persona_name 注入人格内核 system prompt，
  让 LLM 思考链代入角色，产出该人格的参与度评分（内荐机制核心）。
  基本人格不参与内荐。

人格为逐会话显式指定（2026-09-29 起）：PFCA 不再持有 / 注入"当前人格"，
由 conversation_agent / social_chat_agent 的 persona_name 入参指定，缺省用
persona_registry.DEFAULT_PERSONA_MODE。
"""
from brain.base.persona.persona_registry import (
    PersonaDef,
    register_persona,
    get_persona,
    list_personas,
)
from brain.base.persona.persona_agent import PersonaAgent

__all__ = [
    'PersonaDef',
    'register_persona',
    'get_persona',
    'list_personas',
    'PersonaAgent',
]
