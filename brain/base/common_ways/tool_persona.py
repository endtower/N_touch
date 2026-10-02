# -*- coding: utf-8 -*-
"""通用工具：ToolPersona（内心人格工具：内荐 schema + 人格定义修改）

内心人格内荐工具 schema 的唯一实现，收口于本模块（pfca 工具表引用）：
执行侧（并行自荐评分 + 裁决激活）在 pfca_tools.summon_personas；本类负责
各工具的 schema，并实现人格定义修改。

内荐机制（2026-09-28）：PFCA 不再点名选人，只给现状；全部已注册人格
并行自荐（各给参与度评分），评分最高者上场。故内荐 schema 不再暴露 persona
清单，注册新人格后自动参与内荐，无需改 pfca_tools。

人格工具（2026-09-29；2026-10-01 起分层冻结，方案一「common 降格为基本
说话风格」）：
- list_personas：列出目前所有的人格（含基本人格），返回 人格名 -> 场景梗概。
- query_persona：查询一个人格的场景梗概与人格定义。
- update_persona：**仅基本人格（common）可改**——其 kernel_prompt 语义已收窄为
  「基本说话风格」（说人话式普适要求，硬预算 COMMON_STYLE_BUDGET 字符，
  超限拒绝）；普通人格的 kernel_prompt 已冻结，传入一律 ValueError 硬拦
  （软警告实测会被 LLM 忽略，必须硬拦）。覆盖写入前自动留档旧版
  （persona_registry.history/），可回滚。
  经验挂载链路（query_persona_status / mount_persona_experience）已删除：
  kernel_prompt 是人格定义的唯一来源，人格 system prompt 只取该字段，
  挂载列表无任何运行时注入与消费方，属纯死代码。

数据源见 brain.base.persona.persona_registry（PersonaDef / get_persona /
set_persona_kernel_prompt / compose_persona_prompt / persona_exists /
list_personas / list_roster_personas）。
"""

from typing import Any, Callable, Dict, List

from utils.logger import get_logger

logger = get_logger('ToolPersona', "INFO")


class ToolPersona:
    """内心人格通用工具：内荐 schema + 人格定义修改。"""

    # ============ 内荐工具 schema ============

    @classmethod
    def get_inner_persona_tool_schema(cls) -> Dict[str, Any]:
        """生成 PFCA 用的内心人格内荐工具 schema。

        PFCA 只需给出当前现状；参与内荐的人格集合由系统（注册表）决定，
        不在此暴露，避免 PFCA 直接点名选人。
        """
        return {
            "type": "function",
            "function": {
                "name": "inner_persona_agent",
                "description": (
                    "调用内心人格内荐代理，仔细分析当前现状，以适应新的情况。人格将自动判定并且给出自己的建议与理由。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "current_summary": {
                            "type": "string",
                            "description": "当前现状总结：前因后果、状态、问题、已尝试方法。需要写的尽量宽泛，不应具体到可能的选项。"
                        }
                    },
                    "required": ["current_summary"]
                }
            }
        }

    # ============ 人格定义修改工具 schema ============

    @classmethod
    def get_update_persona_tool_schema(cls) -> Dict[str, Any]:
        """返回 update_persona 工具的 schema（2026-09-29 新增）。

        2026-10-01 起仅基本人格（common）可改，kernel_prompt 语义收窄为
        「基本说话风格」（硬预算，超限拒绝）；普通人格内核已冻结。
        """
        return {
            "type": "function",
            "function": {
                "name": "update_persona",
                "description": (
                    "修改基本人格（common）的基本说话风格。"
                    "只有 common 可改，其他人格的内核已冻结、传了也会被拒绝。"
                    "common 拼接到所有会话首条消息最前段，这里只写"
                    "「说人话」式的普适性说话要求——让说话自然的那几条底线"
                    "（语气、句式、用词、发言节奏），硬预算 300 字，超出会被拒绝；"
                    "写之前先 query_persona 看现状，想加新内容先掂量删掉哪条旧的。"
                    "不写逐字逐词的细则，不写待人策略，只管怎么说话自然。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "persona_name": {
                            "type": "string",
                            "description": "人格名称，只能传 common",
                        },
                        "kernel_prompt": {
                            "type": "string",
                            "description": (
                                "新的基本说话风格全文（覆盖式替换旧的，"
                                "≤300 字，超出将被拒绝）。"
                                "每条都应是可观察的说话行为约束，"
                                "不要写「保持自然」这类抽象品质词。"
                            ),
                        },
                    },
                    "required": ["persona_name", "kernel_prompt"],
                },
            },
        }

    @classmethod
    def get_list_personas_tool_schema(cls) -> Dict[str, Any]:
        """返回 list_personas 工具的 schema（2026-09-29 新增，无参）。"""
        return {
            "type": "function",
            "function": {
                "name": "list_personas",
                "description": (
                    "列出目前所有的人格，包括基本人格。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        }

    @classmethod
    def get_query_persona_tool_schema(cls) -> Dict[str, Any]:
        """返回 query_persona 工具的 schema（2026-09-29 新增）。"""
        return {
            "type": "function",
            "function": {
                "name": "query_persona",
                "description": (
                    "查询一个人格的梗概与人格定义：返回该人格的适配场景与内核提示词全文。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "persona_name": {
                            "type": "string",
                            "description": "要查询的人格名称",
                        },
                    },
                    "required": ["persona_name"],
                },
            },
        }

    @classmethod
    def get_persona_tool_schemas(cls) -> List[Dict[str, Any]]:
        """返回人格工具 schema 列表（供工具表直接拼装）。"""
        return [
            cls.get_list_personas_tool_schema(),
            cls.get_query_persona_tool_schema(),
            cls.get_update_persona_tool_schema(),
        ]

    # ============ 工具实现 ============

    @classmethod
    def _require_persona(cls, persona_name: str):
        """按人格名称定位人格定义；不存在抛 ValueError 走 LLM 自纠通道。"""
        from brain.base.persona.persona_registry import list_personas

        key = (persona_name or "").strip()
        for persona in list_personas():
            if key == persona.persona_name:
                return persona
        valid = "、".join(p.persona_name for p in list_personas())
        raise ValueError(f"人格不存在: {persona_name!r}。可用人格: {valid}。")

    @classmethod
    def list_personas(cls) -> Dict[str, Any]:
        """列出全部人格（含基本人格）：人格名 -> 场景梗概。

        Returns:
            {"personas": {persona_name: scene, ...}, "total": int}
        """
        from brain.base.persona.persona_registry import list_personas

        personas = list_personas()
        return {
            "personas": {p.persona_name: p.scene for p in personas},
            "total": len(personas),
        }

    @classmethod
    def query_persona(cls, persona_name: str) -> Dict[str, Any]:
        """查询一个人格的场景梗概与人格定义。

        人格不存在抛 ValueError 走 LLM 自纠通道。

        Returns:
            {"persona_name": str, "scene": str, "kernel_prompt": str}
        """
        persona = cls._require_persona(persona_name)
        return {
            "persona_name": persona.persona_name,
            "scene": persona.scene,
            "kernel_prompt": persona.kernel_prompt,
        }

    @classmethod
    async def update_persona(cls, persona_name: str, kernel_prompt: str) -> Dict[str, Any]:
        """覆盖式更新基本人格（common）的说话风格区，并回写人格文件。

        2026-10-01 起硬性限制：persona_name 非 common 直接 ValueError
        （普通人格内核对 LLM 冻结）；kernel_prompt 超出
        COMMON_STYLE_BUDGET 同样 ValueError。人格名称、人格定义为空抛
        ValueError 走 LLM 自纠通道；人格不存在同样抛 ValueError。
        覆盖写入前由 set_persona_kernel_prompt 自动留档旧版。

        Returns:
            {"persona_name": str, "kernel_prompt": str}
        """
        from brain.base.persona.persona_registry import (
            COMMON_PERSONA_NAME,
            COMMON_STYLE_BUDGET,
            set_persona_kernel_prompt,
        )

        if not (persona_name or "").strip():
            raise ValueError("人格名称不能为空。")
        if not (kernel_prompt or "").strip():
            raise ValueError("人格定义不能为空。")

        persona = cls._require_persona(persona_name)
        if persona.persona_name != COMMON_PERSONA_NAME:
            raise ValueError(
                f"人格 {persona.persona_name!r} 的内核已冻结，不可经工具修改；"
                f"唯一可改的是基本人格（{COMMON_PERSONA_NAME}）的基本说话风格区。"
            )
        if len(kernel_prompt) > COMMON_STYLE_BUDGET:
            raise ValueError(
                f"超出基本说话风格预算：当前 {len(kernel_prompt)} 字，"
                f"上限 {COMMON_STYLE_BUDGET} 字。请精简后再提交——"
                "每条保留可观察的说话行为约束，删掉抽象描述与一次性格外教训。"
            )
        updated = await set_persona_kernel_prompt(persona.persona_name, kernel_prompt)
        return {
            "persona_name": updated.persona_name,
            "kernel_prompt": updated.kernel_prompt,
        }

    # ============ 分发表 ============

    @classmethod
    def get_tool_functions(cls) -> Dict[str, Callable]:
        """返回工具名 -> 实现的分发表（供 agent 的 _initialize_tool_functions 直接使用）。"""
        return {
            "list_personas": cls.list_personas,
            "query_persona": cls.query_persona,
            "update_persona": cls.update_persona,
        }
