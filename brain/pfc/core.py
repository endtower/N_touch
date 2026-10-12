# -*- coding: utf-8 -*-

"""
前额叶处理核心模块
负责处理大脑前额叶相关的逻辑，是整个注意力系统的核心。
"""

import asyncio
from typing import Any, Dict, Optional
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from utils.chain_context import (
    generate_chain_id,
    set_chain_context,
    reset_chain_context,
)
from brain.pfc.pfca.pfca_core import get_pfca_instance
from brain.hc.global_session_state import get_global_session_state

logger = get_logger(__name__, "INFO")


@async_handle_errors()
async def run_pfc_cycle(
    initial_state: str,
    initial_purpose: str,
    current_session: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """PFCA(行动) ↔ Reflection(反思) 链式接力协调器（2026-09-23 接入）。

    允许两类双向接力：
    - PFCA `terminate` 第二次确认调用时可带 enter_reflection=true + reflection_purpose，
      结束本轮行动后自动进入反思腿；
    - Reflection `finalize_reflection` 收尾时可带 enter_action=true + action_purpose，
      结束本轮反思后自动进入行动腿（新一轮 PFCA）。

    接力请求经 GlobalSessionState._data["pfc_handoff"] 传递：工具写入、
    本协调器在每段会话结束后消费，实现 "行动 → 反思 → 行动 → …" 任意链长。

    事件链管理（保守思路，2026-09-23）：
    - 每条 action 腿 = 一条独立事件链（全新 chain_id + 切换 contextvar），
      杜绝"同一事件链被多次 finalize_chain"的笔记混乱；
    - 首段 action 腿若由 station 预开链（PFCA 首触发），复用该链、不代管收尾；
      其余接力 action 腿一律先收尾残留旧链，再新开独立链并在腿结束后收尾；
    - reflection 腿不依赖事件链（反思笔记不经过事件链缓存，与事件链解耦），
      与 reflection truck 行为一致。

    Args:
        initial_state: 首段会话类型，"action"（PFCA）或 "reflection"（Reflection）。
        initial_purpose: 首段会话的目的。
        current_session: 首段 action 腿的会话上下文（含 session_id）；接力腿无上下文传 None。

    Returns:
        最后一段会话的 session_result。
    """
    from brain.pfc.reflection.reflection_core import get_reflection_instance

    focus_state = get_global_session_state()

    state = initial_state
    purpose = initial_purpose
    session_ctx = current_session
    result = None
    leg_index = 0
    # 接力深度护栏（2026-09-23）：防止 LLM 病理性地在 行动↔反思 间无限循环，
    # 挂死 station 处理队列。超限属状态异常，直接上抛崩溃阻断（硬约定 #4）。
    max_legs = 10

    while state:
        max_legs -= 1
        if max_legs < 0:
            raise RuntimeError(
                "PFC 链式接力超过上限（行动↔反思循环疑似失控），崩溃阻断"
            )
        if state == "action":
            # action 腿：PFCA terminate 落库依赖记事本缓存文档，必须处于活动事件链内。
            # - 首腿且链已活动（station 预开）：复用，不代管；
            # - 无活动链（reflection truck 接力路径）：独立新开链；
            # - 接力腿且残留 station 首腿链：先收尾旧链再新开独立链，
            #   保证每条 action 腿各自 finalize，互不复用缓存文档。
            chain_owned_by_cycle = False
            chain_token = None
            if not focus_state.is_event_chain_active():
                new_chain_id = generate_chain_id()
                chain_token = set_chain_context(new_chain_id)
                focus_state.start_new_event_chain(
                    purpose or "",
                    chain_id=new_chain_id,
                )
                chain_owned_by_cycle = True
            elif leg_index > 0:
                focus_state.end_event_chain()
                new_chain_id = generate_chain_id()
                chain_token = set_chain_context(new_chain_id)
                focus_state.start_new_event_chain(
                    purpose or "",
                    chain_id=new_chain_id,
                )
                chain_owned_by_cycle = True
            try:
                result = await get_pfca_instance().start(
                    purpose=purpose, current_session=session_ctx
                )
            finally:
                # 本协调器新开的事件链在腿结束后收尾（未真正结束路径缓存丢弃）
                try:
                    if chain_owned_by_cycle and focus_state.is_event_chain_active():
                        focus_state.end_event_chain()
                finally:
                    if chain_token is not None:
                        reset_chain_context(chain_token)
            session_ctx = None  # 接力腿无原始会话上下文
        elif state == "reflection":
            result = await get_reflection_instance().start(
                purpose=purpose, current_session=None
            )
        else:
            raise ValueError(f"未知 PFC 接力状态: {state!r}")

        leg_index += 1
        # 消费接力请求（terminate / finalize_reflection 写入）；无则链结束
        handoff = focus_state._data.pop("pfc_handoff", None)
        if not handoff:
            break
        state = handoff["state"]
        purpose = handoff["purpose"]

    return result


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
    #    经 run_pfc_cycle 协调器运行：PFCA terminate 可选接力进入反思（enter_reflection），
    #    反思 finalize 可选接力回到行动（enter_action），构成 行动↔反思 链式会话。
    pfca_instance.is_task_running = True
    try:
        processed_result = await run_pfc_cycle(
            initial_state="action",
            initial_purpose=message,
            current_session=session_context,
        )
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
