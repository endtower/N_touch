# -*- coding: utf-8 -*-
"""代理框架基类（HeavyCore / HeavySession / ToolManager 等）。

API 文档：docs/api/infrastructure.md —— 修改本包对外接口后必须同步更新该文档
（校验：python scripts/check_api_docs.py）。"""
from brain.base.heavy_agent.heavy_session import HeavySession
from brain.base.heavy_agent.heavy_core import HeavyCore
from brain.base.heavy_agent.tool_manager import ToolManager
from brain.base.common_ways import (
    CONVERSATION_CONSUMER_ID,
    build_session_environment,
    build_social_chat_environment,
    ToolHandlers,
    ToolConversation,
    ToolStickers,
    ToolVision,
    ToolHC,
    ToolSocial,
    ToolPersistent,
    ToolBequest,
    ToolTask,
    ToolPersona,
    ToolJinnang,
)

__all__ = [
    'HeavySession',
    'HeavyCore',
    'ToolManager',
    'CONVERSATION_CONSUMER_ID',
    'build_session_environment',
    'build_social_chat_environment',
    'ToolHandlers',
    'ToolConversation',
    'ToolStickers',
    'ToolVision',
    'ToolHC',
    'ToolSocial',
    'ToolPersistent',
    'ToolBequest',
    'ToolTask',
    'ToolPersona',
    'ToolJinnang',
]
