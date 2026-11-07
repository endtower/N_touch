# -*- coding: utf-8 -*-
"""通用提示词辅助方法

存放 conversation / social_chat 共用的 system prompt 构造辅助常量。
"""

# 工具调用硬约束（conversation / social_chat 的 system prompt 共用，逐字一致，勿在模块内复制）
TOOL_CALL_CONSTRAINT = (
    "你的每一步必须调用工具，并通过工具调用与外界沟通。"
    "你可以将思维过程写在调用工具时的content中。"
    "若直接输出，不调用工具会导致系统崩溃"
)
