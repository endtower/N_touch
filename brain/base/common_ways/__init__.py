# -*- coding: utf-8 -*-
"""通用方法（common_ways）

存放跨模块共用的通用方法，按类别分文件：
- input_helpers：input 通用方法（如 build_session_environment）
- tool_handlers：tool 通用方法基类（ToolHandlers：send_message / wait_and_observe）
- tool_vision：vision 通用工具（ToolVision：识图 / 表情包管理）
"""

from brain.base.common_ways.input_helpers import build_session_environment
from brain.base.common_ways.tool_handlers import ToolHandlers
from brain.base.common_ways.tool_vision import ToolVision

__all__ = [
    'build_session_environment',
    'ToolHandlers',
    'ToolVision',
]
