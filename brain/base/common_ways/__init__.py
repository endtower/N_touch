# -*- coding: utf-8 -*-
"""通用方法（common_ways）

存放跨模块共用的通用方法，按类别分文件：
- input_helpers：input 通用方法（如 build_session_environment）+ 共享消费者游标常量
- tool_handlers：tool 通用方法基类（ToolHandlers：wait_and_observe / peek_unread）
- tool_conversation：会话发送类通用工具（ToolConversation：send_message / send_image 及发送原语）
- tool_vision：vision 通用工具（ToolVision：识图 / 表情包管理）
- tool_hc：hc 通用工具（ToolHC：灵光一现记事本 列出/查询/记录）
- tool_social：社交环境查询工具（ToolSocial：通讯录 / 会话历史，以 PFCA 实现为基准）
- tool_persistent：持久化工具（ToolPersistent：memory_agent 印象记忆 / task_register_agent
  任务登记；TaskRegisterAgent 实例进程级持久持有，原 PFCA 实现 2026-09-15 收口于此）
- tool_bequest：经验嘱托工具（ToolBequest：upsert_bequest / delete_bequest；
  原 bequest_agent 实现 2026-09-15 收口于此）
- tool_task：任务管理工具（ToolTask：create_task / update_task_status / query_task_status；
  2026-09-15 由事件系统重做为任务系统）
"""

from brain.base.common_ways.input_helpers import (
    CONVERSATION_CONSUMER_ID,
    build_session_environment,
    build_social_chat_environment,
)
from brain.base.common_ways.tool_handlers import ToolHandlers
from brain.base.common_ways.tool_conversation import ToolConversation
from brain.base.common_ways.tool_vision import ToolVision
from brain.base.common_ways.tool_hc import ToolHC
from brain.base.common_ways.tool_social import ToolSocial
from brain.base.common_ways.tool_persistent import ToolPersistent
from brain.base.common_ways.tool_bequest import ToolBequest
from brain.base.common_ways.tool_task import ToolTask

__all__ = [
    'CONVERSATION_CONSUMER_ID',
    'build_session_environment',
    'build_social_chat_environment',
    'ToolHandlers',
    'ToolConversation',
    'ToolVision',
    'ToolHC',
    'ToolSocial',
    'ToolPersistent',
    'ToolBequest',
    'ToolTask',
]
