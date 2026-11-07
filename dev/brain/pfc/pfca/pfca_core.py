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
from brain.hc.global_session_state import get_global_session_state

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
        input_manager = get_pfca_input_manager()
        # 新一轮 PFCA：先复位单例状态（_first_activation / 残留队列请求），再缓存首条。
        # 单例跨运行共享，不复位会导致下一次运行的首条消息走增量路径而静默丢失。
        input_manager.reset()
        input_manager.prepare_first_message(
            FirstMessageType.NORMAL_STARTUP,
            current_session,
            message=purpose,
        )

        self.is_task_running = True
        self.current_task = asyncio.current_task()

        pfca_history_manager = get_pfca_history_manager()
        pfca_history_manager.archive_context()
        logger.info("PFCA Core 启动，已归档 PFCA History")

        logger.info("执行 PFCASession")
        session = PFCASession()
        self.current_session = session
        try:
            session_result = await session.start()
        finally:
            self.current_session = None
            # terminate 退出兜底（docs/已完成.md #9）：会话若未经"第二次确认调用"结束
            # （token 预算熔断 / 外部 stop 等），首次调用暂存的总结
            # 在此补写入临时记忆——保证"最后一次 terminate 的总结"不丢。
            # 正常确认路径已在 terminate 内弹出暂存，此处为 no-op。
            # 写入失败直接上抛（硬约定 #4），不吞。
            pending = get_global_session_state()._data.pop("pending_terminate", None)
            if pending and pending.get("summary"):
                from brain.hc.temporary_memory import get_temporary_memory
                get_temporary_memory().add(pending["summary"])
                logger.info("会话退出兜底：已将未确认的 terminate 总结写入短期记忆")

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