# -*- coding: utf-8 -*-
"""通用方法（common_ways）

存放跨模块共用的通用方法，按类别分文件：
- input_helpers：input 通用方法（如 build_session_environment）+ 共享消费者游标常量
- tool_handlers：tool 通用方法基类（ToolHandlers：wait_and_observe / peek_unread）
- tool_conversation：会话发送类通用工具（ToolConversation：send_message / send_image 及发送原语）
- tool_vision：vision 通用工具（ToolVision：vision_agent 查询 / vision_agent_adjust 表情包管理）
- tool_hc：hc 通用工具（ToolHC：灵光一现记事本 列出/查询/记录）
- tool_social：社交环境查询工具（ToolSocial：通讯录 / 会话历史，以 PFCA 实现为基准）
- tool_persistent：持久化工具（ToolPersistent：memory_agent 印象记忆 / quest_agent
  任务管理；QuestAgent 实例进程级持久持有，原 PFCA 实现 2026-09-15 收口于此，
  2026-09-17 由 task_register_agent 任务登记升级为任务管理）
- tool_bequest：经验工具（ToolBequest：query_bequest 话术检索 / query_decision_bequest
  决策检索 / upsert_bequest / delete_bequest；2026-09-15 收口于此，原 bequest_agent
  已于 2026-09-24 随遗留链路删除。2026-09-27 起决策经验入口从 LLM 视野移除，
  通用实现保留，供程序侧直接调用）
- tool_task：任务管理工具（ToolTask：create_task / update_task_status / query_task_status；
  2026-09-15 由事件系统重做为任务系统）
- tool_persona：内心人格工具（ToolPersona：内荐 schema + 人格定义修改，
  2026-09-19 从 persona_agent 收口于此；2026-09-29 移除经验挂载链路）
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
from brain.base.common_ways.tool_persona import ToolPersona

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
    'ToolPersona',
]
