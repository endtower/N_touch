# -*- coding: utf-8 -*-
"""内心人格模块 - LLM 多重人格机制（取代 advance agent 系统）。

组成：
- persona_registry：人格定义（PersonaDef）与注册表，内置助手身份 / 平常身份 /
  攻角 三个统合型人格，供未来反思系统修改与扩展。
- persona_agent：人格召唤 agent，按 persona_id 注入人格内核 system prompt，
  让 LLM 思考链代入角色，产出该人格视角的思考结论。
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
