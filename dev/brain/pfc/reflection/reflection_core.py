# -*- coding: utf-8 -*-

"""
reflection模块核心功能实现

负责处理反思相关的核心逻辑，作为reflection模块的主要入口。
主要职责：
1. LLM客户端管理
2. 反思模式的核心处理逻辑
3. 与工具模块的交互接口
配置从settings.py中统一获取。
"""

import asyncio
from typing import Dict, Any, Optional, Literal

from config.settings import reflection_config
from utils.error_handling import async_handle_errors
from utils.logger import get_logger
from brain.pfc.reflection.reflection_session import ReflectionSession
from brain.pfc.reflection.reflection_history_manager import get_reflection_history_manager
from brain.pfc.reflection.reflection_input_manager import get_reflection_input_manager, FirstMessageType

# 获取日志记录器
logger = get_logger('ReflectionCore', 'INFO')

class ReflectionCore:
    """reflection_core模块的核心类，负责处理反思相关的核心逻辑和LLM Agent功能"""

    def __init__(self):
        """初始化reflection_core模块"""
        logger.info("初始化ReflectionCore模块")
        self.current_task = None
        self.is_task_running = False
        self.model_name = reflection_config.MODEL_NAME
        self.should_stop = False
        self.current_session: ReflectionSession | None = None

    async def stop(self, exit_reason: Literal["reflection_done", "external_stop"] | None = None):
        """
        立即停止当前正在运行的 Reflection Session

        Args:
            exit_reason: 退出原因，透传给 ReflectionSession
                （reflection_done=反思完成；external_stop=外部强制停止；None=未指定）
        """
        logger.info(f"设置Reflection Core退出标志，停止当前 Session（原因: {exit_reason}）")
        self.should_stop = True
        if self.current_session is not None:
            await self.current_session.stop(exit_reason)
            logger.info("已停止当前 Reflection Session")

    @async_handle_errors()
    async def start(
        self,
        purpose: str,
        current_session: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        启动 Reflection Session（输入显式传入，内部准备首条消息）。

        统一入口约定（对齐 pfca_core.start 的输入参数化）：
        - 输入（purpose / current_session）由调用方显式传入，不再依赖外部隐式
          prepare_first_message。
        - 内部调用 input_manager.prepare_first_message 缓存首条请求，
          ReflectionSession 在 _inject_first_user_message 中消费。

        Args:
            purpose: 本次反思会话的目的（已拼接好的消息字符串，通常为待反思的事件/对话摘要）。
            current_session: 会话上下文（含 session_id），无会话上下文时传 None。

        Returns:
            Dict[str, Any]: LLM处理的最终结果
        """
        input_manager = get_reflection_input_manager()
        # 新一轮 Reflection：先复位单例状态（_first_activation / 残留队列请求），再缓存首条。
        # 单例跨运行共享，不复位会导致下一次运行的首条消息走增量路径而静默丢失。
        input_manager.reset()
        input_manager.prepare_first_message(
            FirstMessageType.NORMAL_STARTUP,
            current_session,
            message=purpose,
        )

        self.is_task_running = True
        self.current_task = asyncio.current_task()

        reflection_history_manager = get_reflection_history_manager()
        reflection_history_manager.archive_context()
        logger.info("Reflection Core 启动，已归档 Reflection History")

        logger.info("执行 ReflectionSession")
        session = ReflectionSession()
        self.current_session = session
        try:
            session_result = await session.start()
        finally:
            self.current_session = None
            # 会话结束统一收尾：反思脱离事件链，工具调用广播的 activity 必须在此
            # 复位（含异常路径），否则前端残留 "LLM xxx" 假激活显示（事件链实际未激活）
            from brain.hc.global_session_state import get_global_session_state
            get_global_session_state().end_session_activity(source="reflection")

        logger.info("Reflection Core 执行完成")

        self.is_task_running = False
        self.should_stop = False
        return session_result


# 全局Reflection实例，延迟初始化
_global_reflection_instance = None

# 获取Reflection实例的方法

def get_reflection_instance() -> ReflectionCore:
    """
    获取Reflection实例的方法，实现单例模式

    Returns:
        ReflectionCore: Reflection实例
    """
    global _global_reflection_instance
    if _global_reflection_instance is None:
        _global_reflection_instance = ReflectionCore()
    return _global_reflection_instance

# 导出强制中止start方法

def stop():
    """
    强行中止当前正在执行的start任务
    使用asyncio.Task.cancel()实现真正的异步任务中断

    Returns:
        bool: 中断是否成功设置
    """
    reflection_instance = get_reflection_instance()
    return reflection_instance.stop()
