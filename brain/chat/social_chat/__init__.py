# -*- coding: utf-8 -*-
"""social_chat 模块 - 由 social_agent 调起的 chat agent。

与 conversation 平级：
- 主人会话：主人私聊会话（master_session_id）
- 工作会话：social_agent 指定的工作会话（working_session_id），消息实际发出地
- 工具集：见 social_chat_tool_manager
"""