# -*- coding: utf-8 -*-
"""通用工具：ToolPersona（内心人格召唤工具）

内心人格召唤工具 schema 的唯一实现，收口于本模块（pfca 工具表引用）：
enum / description 从人格注册表动态生成——反思系统未来注册新人格后
schema 自动跟上，无需改 pfca_tools。执行侧（召唤 LLM 跑思考链）仍在
pfca_tools.summon_persona，本类只负责 schema。

数据源见 brain.base.persona.persona_registry（PersonaDef / register_persona）。
"""

from typing import Any, Dict

from utils.logger import get_logger

logger = get_logger('ToolPersona', "INFO")


class ToolPersona:
    """内心人格召唤通用工具：从人格注册表动态生成工具 schema。"""

    @classmethod
    def get_inner_persona_tool_schema(cls) -> Dict[str, Any]:
        """生成 PFCA 用的内心人格召唤工具 schema。

        enum 与 description 从注册表动态生成：注册新人格后自动跟上，
        无需改 pfca_tools（schema 与实现收口于通用层，避免复制漂移）。
        """
        # 延迟导入：persona_agent 反向依赖 brain.base，模块级导入会造成循环
        from brain.base.persona.persona_registry import list_personas

        personas = list_personas()
        scene_text = "；".join(f"{p.persona_id}={p.scene}" for p in personas)
        return {
            "type": "function",
            "function": {
                "name": "inner_persona_agent",
                "description": "召唤一个或多个内心人格并行思考，各自以自己的人格立场审视现状，统一返回各视角的思考结论。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "persona_ids": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [p.persona_id for p in personas],
                            },
                            "minItems": 1,
                            "maxItems": len(personas),
                            "description": f"要召唤的内心人格（可多选，并行思考）。可选：{scene_text}。"
                        },
                        "current_summary": {
                            "type": "string",
                            "description": "当前现状总结：前因后果、状态、问题、已尝试方法。"
                        }
                    },
                    "required": ["persona_ids"]
                }
            }
        }
