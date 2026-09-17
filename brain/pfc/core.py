# -*- coding: utf-8 -*-

"""
前额叶处理核心模块
负责处理大脑前额叶相关的逻辑，是整个注意力系统的核心。
"""

import asyncio
from typing import Any, Dict, Optional
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from brain.pfc.pfca.pfca_core import get_pfca_instance
from brain.hc.global_session_state import get_global_session_state

logger = get_logger(__name__, "INFO")


@async_handle_errors()
async def async_process_from_att_factory(
    message: str,
    session_id: Optional[str] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    供 att 工厂调用的异步接口。

    输入契约（三项，可选者给出即可）：
    - ``message``: 已拼接好的字符串消息（必填）
    - ``session_id``: 会话标识，格式 ``type_id``（如 ``private_xxx`` / ``group_xxx``）；
      proactive / 无会话上下文场景可不传。
    - ``name``: 触发本轮 PFCA 的那条消息所属会话的人类可读名称（可选），
      例如私聊触发 → ``<master_name>``、群聊触发 → ``未定义开发社3群``。
      定义：仅由"消息类型触发的会话"提供该会话的 ``session_name``；
      其他没有真实消息会话来源的场景（ongoing、scheduled_proactive
      等纯事件/计划任务）须传 ``None``，不得伪造占位串，否则
      ``first_session_impression_search`` 会按错误名称误命中/新建记忆数据库节点。
      未传时 ``add_pfca_session`` 内部可从消息存储兑底。

    流程：
    1. 初始化和状态检查
    2. 挂载会话（若提供 session_id）
    3. 准备首次消息到队列
    4. 运行 pfca 实例
    5. 返回结果

    注：bequest 经验登记已移至事件链收尾（end_event_chain），不再由本流程负责。

    Args:
        message: 来自 att 工厂的字符串消息。
        session_id: 来自 att 工厂的会话标识，可选。
        name: 来自 att 工厂的会话名称，可选。

    Returns:
        Dict[str, Any]: 处理结果，包含 success 和 processing_time。
    """
    start_time = asyncio.get_event_loop().time()

    # 1. 初始化和状态检查
    logger.info("开始处理新任务")
    pfca_instance = get_pfca_instance()
    if pfca_instance.is_task_running:
        logger.warning("已有pfca任务在运行，拒绝新任务请求")
        raise RuntimeError("已有pfca任务在运行，拒绝新任务请求")

    focus_state = get_global_session_state()

    # 2. 挂载会话（按需）
    if session_id:
        await focus_state.add_pfca_session(
            session_id,
            session_name=(name or ""),
            search_impression=True,
        )
    else:
        logger.info("无 session_id，跳过会话上下文挂载（proactive 或纯 message 场景）")

    # 3. 构造会话上下文（有 session_id 时作为最小上下文传入，PFCA 仅作占位，
    #    首条 content 真正读的是 message；无 session_id 时传 None）
    session_context = {"session_id": session_id} if session_id else None

    # 4. 运行 pfca 实例（输入显式传入，首条消息由 start() 内部准备）
    #    is_task_running 必须 try/finally 复位：任何异常/取消路径残留 True，
    #    会让后续 PFCA 请求被"已有pfca任务在运行"永久拒绝（致命静默）。
    pfca_instance.is_task_running = True
    try:
        processed_result = await pfca_instance.start(message, session_context)
    finally:
        pfca_instance.is_task_running = False

    if processed_result is None:
        logger.error("致命错误:PFCA处理结果为None")
        raise RuntimeError("PFCA处理结果为None")

    # 5. 返回结果
    processing_time = asyncio.get_event_loop().time() - start_time
    return {
        'success': True,
        'processing_time': processing_time
    }
