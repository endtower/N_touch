# -*- coding: utf-8 -*-

"""
pfca模块核心功能实现

负责处理注意力相关的核心逻辑，作为pfca模块的主要入口。
主要职责：
1. LLM客户端管理
2. Agent模式的核心处理逻辑
3. 注意力信息的处理
4. 与工具模块的交互接口
配置从settings.py中统一获取。
"""

import asyncio
from typing import Dict, Any, Optional

from config.settings import pfca_config
from utils.error_handling import async_handle_errors
from utils.logger import get_logger
from brain.pfc.pfca.pfca_session import PFCASession
from brain.pfc.pfca.pfca_history_manager import get_pfca_history_manager
from brain.pfc.pfca.pfca_input_manager import get_pfca_input_manager, FirstMessageType

# 获取日志记录器
logger = get_logger('PFCACore', 'INFO')

class PFCACore:
    """pfca_core模块的核心类，负责处理注意力相关的核心逻辑和LLM Agent功能"""

    def __init__(self):
        """初始化pfca_core模块"""
        logger.info("初始化PFCACore模块")
        self.current_task = None
        self.is_task_running = False
        self.model_name = pfca_config.MODEL_NAME
        self.should_stop = False
        self.current_session: PFCASession | None = None
        # 在本次 PFCA 会话（即一条事件链）期间持久化的事件登记 agent。
        # 设计：pfca_tools.event_judgement 会从本字段获取同一个 agent 实例，
        # 这样多次调用 event_judgement 时 agent 上下文（messages / tools_called）是连续的，
        # 保证 PFCA 与 event_register_agent 之间的多轮通讯不会丢上下文。
        # 本次会话结束时由 stop()/start() 负责清空，避免跨链泄漏。
        self.event_register_agent = None

    async def stop(self):
        """
        立即停止当前正在运行的 PFCA Session
        """
        logger.info("设置PFCA Core退出标志，停止当前 Session")
        self.should_stop = True
        if self.current_session is not None:
            await self.current_session.stop()
            logger.info("已停止当前 PFCA Session")

    @async_handle_errors()
    async def start(
        self,
        purpose: str,
        current_session: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        启动 PFCA Session（输入显式传入，内部准备首条消息）。

        统一入口约定（对齐 HeavyCore.start 的输入参数化）：
        - 输入（purpose / current_session）由调用方显式传入，不再依赖外部隐式
          prepare_first_message。
        - 内部调用 input_manager.prepare_first_message 缓存首条请求，
          PFCASession 在 _inject_first_user_message 中消费。

        Args:
            purpose: 本次 PFCA 会话的目的（已拼接好的消息字符串）。
            current_session: 会话上下文（含 session_id），无会话上下文时传 None。

        Returns:
            Dict[str, Any]: LLM处理的最终结果
        """
        get_pfca_input_manager().prepare_first_message(
            FirstMessageType.NORMAL_STARTUP,
            current_session,
            message=purpose,
        )

        self.is_task_running = True
        self.current_task = asyncio.current_task()

        pfca_history_manager = get_pfca_history_manager()
        pfca_history_manager.archive_context()
        logger.info("PFCA Core 启动，已归档 PFCA History")

        # 新事件链开始：清空跨链的事件登记 agent 上下文，避免上一条事件链的 backlog / 工具记录泄漏。
        # agent 实例本身允许为 None，下游按需懒加载。
        if self.event_register_agent is not None:
            try:
                self.event_register_agent.reset()
            except Exception as e:
                logger.warning(f"重置 event_register_agent 失败（继续）: {e}")
            self.event_register_agent = None
            logger.debug("PFCA Core 启动：已清空上一事件链的 event_register_agent 上下文")

        logger.info("执行 PFCASession")
        session = PFCASession()
        self.current_session = session
        session_result = await session.start()
        self.current_session = None

        logger.info(f"PFCA Core 执行完成")

        self.is_task_running = False
        self.should_stop = False
        return session_result


# 全局PFCA实例，延迟初始化
_global_pfca_instance = None

# 获取PFCA实例的方法

def get_pfca_instance() -> PFCACore:
    """
    获取PFCA实例的方法，实现单例模式

    Returns:
        PFCACore: PFCA实例
    """
    global _global_pfca_instance
    if _global_pfca_instance is None:
        _global_pfca_instance = PFCACore()
    return _global_pfca_instance

# 导出强制中止start方法

def stop():
    """
    强行中止当前正在执行的start任务
    使用asyncio.Task.cancel()实现真正的异步任务中断

    Returns:
        bool: 中断是否成功设置
    """
    pfca_instance = get_pfca_instance()
    return pfca_instance.stop()