# -*- coding: utf-8 -*-
"""
proactive 工具模块

类比 pfca_tools（pfca_tool_manager）：
- 工具 schema 在最前面（OpenAI function call 格式）
- 提供工具管理器（ProactiveToolRegistry）单例
- 提供 handle_tool_call(tool_call) 解析执行
- 工具实现是独立 async 函数，定义在类后面

已注册工具：
- launch_pfca:               启动 PFCA（终结工具：调完 session 退出）
- query_address_book:        查看通讯录（ToolSocial 通用实现，对齐 PFCA_tool_manager）
- query_history:             查看一个会话的聊天记录（ToolSocial 通用实现，对齐 PFCA_tool_manager）
- quest_agent:               登记一条待接续事项（委托 ToolTask，交给 Quest 查重后登记）
- skip_proactive:            主动跳过本次任务（终结工具）

暂未暴露给 LLM 的工具（实现 + 注册保留，仅从 TOOL_SCHEMAS 中隐藏，避免 LLM 发现/误调）：
- memory_agent:              调整记忆（实现仍保留在 _tool_memory_adjustment）
"""

import json
from functools import partial
from typing import Any, Awaitable, Callable, Dict, List, Optional

from utils.logger import get_logger
from brain.base import ToolManager, ToolSocial, ToolTask

logger = get_logger("ProactiveTools", "DEBUG")


# ============================================================
# 工具 schema（OpenAI function call 格式）
# ============================================================

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "launch_pfca",
            "description": (
                "启动 PFCA 执行主动任务（终结工具）。"
                "调用完成后本次 proactive 会话即关闭。"
                "PFCA 收到的输入是三个字段：任务信息（含根源 + 时机）+ 目标会话 id + 本次要处理的任务标题。"
                "若本次主动唤醒是因为【当前已到达计划再处理时间的未完成任务】触发的，"
                "target_task_title 必须填写该任务的标题（必须能在任务表里精确匹配）；"
                "否则（无任务来源的主动任务，如基于情绪/观察的临时任务）传空字符串 ''。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_info": {
                        "type": "string",
                        "description": (
                            "任务信息。包含两部分：根源 + 时机"
                            "例：'我被某xxx群聊的某人激怒，恰好看到xxxx群有人讨论画作分享经验，"
                        ),
                    },
                    "target_session_id": {
                        "type": "string",
                        "description": (
                            "目标会话 id，格式为 'type_id'，如 'private_xxx' / 'group_xxx'。"
                            "若该任务无具体目标对象（如单纯自我情绪宣泄），"
                            "传空字符串 ''。"
                        ),
                    },
                    "target_task_title": {
                        "type": "string",
                        "description": (
                            "本次要处理的任务标题。"
                            "若本次主动唤醒是因为【当前已到达计划再处理时间的未完成任务】触发的，"
                            "必须填写该任务的标题（须能在任务表中精确匹配一条未完成任务）；"
                            "否则传空字符串 ''。"
                        ),
                    },
                },
                "required": ["task_info"],
            },
        },
    },
    ToolSocial.get_query_address_book_tool_schema(),
    ToolSocial.get_query_history_tool_schema(),
    {
        "type": "function",
        "function": {
            "name": "skip_proactive",
            "description": (
                "跳过本次主动任务。LLM 经过判断后，如果认为不需要主动推进"
                "（比如 reason 不充分、当前有未读消息应让位、与目标用户关系冲突、时机不恰当等），"
                "调用本工具让 session 立即退出。"
                "与 launch_pfca 一样，是终结工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "跳过原因（必填），给 history / 日志留痕",
                        "default": "no_action_needed",
                    },
                },
                "required": [],
            },
        },
    },
    # quest_agent 联系通道（2026-10-11 接入：原 update_long_term_task 的更新语义已随任务
    # 系统收敛移除——任务触发即过期、续办靠新建。此刻不宜执行时，交给 Quest 查重后
    # 登记一条更晚唤醒的同类事项，而非就地更新原任务）
    ToolTask.get_quest_agent_tool_schema(),
]


# ============================================================
# 工具注册表 / 工具管理器（类比 PFCA_tool_manager）
# ============================================================

class ProactiveToolRegistry(ToolManager):
    """proactive 工具注册表/管理器（单例）

    类比 PFCA_tool_manager：
    - get_tools()            -> 返回 OpenAI function call schema
    - handle_tool_call(call) -> 解析 tool_call 并执行
    """

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._functions: Dict[str, Callable[..., Awaitable[Any]]] = {
            "launch_pfca": _tool_launch_pfca,
            "query_address_book": ToolSocial.query_address_book,
            # consumer_id 显式传 "pfca"：LLM 已读到的消息推进 pfca 游标，truck 不再重复触发
            "query_history": partial(ToolSocial.query_history, consumer_id="pfca"),
            "memory_adjustment": _tool_memory_adjustment,
            "skip_proactive": _tool_skip_proactive,
            "quest_agent": ToolTask.quest_agent,
        }
        self._initialized = True
        logger.debug("ProactiveToolRegistry 初始化完成")

    def get_tools(self) -> List[Dict[str, Any]]:
        """获取 OpenAI function call 格式的工具 schema"""
        return TOOL_SCHEMAS

    def get_function(self, name: str) -> Optional[Callable[..., Awaitable[Any]]]:
        return self._functions.get(name)

    def list_names(self) -> List[str]:
        return list(self._functions.keys())

    async def handle_tool_call(self, tool_call) -> Dict[str, Any]:
        """处理一个 tool_call，解析 arguments 并执行函数

        Args:
            tool_call: LLM 返回的 tool_call 对象，包含 id / function.name / function.arguments

        Returns:
            Dict: {
                'role': 'tool',
                'tool_call_id': str,
                'content': str  # 函数返回值（序列化为字符串）
            }
        """
        parsed = self._parse_tool_call(tool_call)
        if parsed is None:
            return self._tool_error_response(
                getattr(tool_call, "id", None) if hasattr(tool_call, "id") else None,
                "<unknown>",
                ValueError(f"无法解析工具调用格式: {tool_call}"),
            )
        fn_name = parsed["name"]
        tool_call_id = parsed["id"]

        try:
            fn_args = self._parse_arguments(parsed["arguments"])
        except Exception as e:
            logger.warning(f"工具参数解析失败 {fn_name}: {e}")
            return self._tool_error_response(tool_call_id, fn_name, e)

        # 前端同步: 把 proactive 的工具调用广播给 web 通道, IdentityBar 显示 "proactive 调用 xxx"
        # (后端维护单一 activity 状态, source='proactive' 让前端显示不同前缀)
        from brain.hc.global_session_state import get_global_session_state
        get_global_session_state().set_activity(
            source="proactive", tool_name=fn_name, tool_args=fn_args
        )

        fn = self._functions.get(fn_name)
        if fn is None:
            return self._tool_error_response(
                tool_call_id, fn_name, ValueError(f"未知工具: {fn_name}")
            )

        # 统一错误处理（参数错误回 LLM，其余异常上抛阻断）
        result = await self._safe_execute(fn, tool_call_id, fn_name, **fn_args)
        if isinstance(result, dict) and result.get("role") == "tool":
            # 出错时 _safe_execute 已返回 role=tool 的错误消息，直接透传
            return result

        # 工具返回值：字符串直接用，dict/list 序列化为字符串
        if isinstance(result, str):
            content = result
        else:
            content = json.dumps(result, ensure_ascii=False)
        logger.info(f"工具 {fn_name} 执行成功")
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        }

def get_proactive_tool_registry() -> ProactiveToolRegistry:
    """获取 ProactiveToolRegistry 单例"""
    return ProactiveToolRegistry()


# ============================================================
# 工具实现（async functions）
# query_address_book / query_history 统一走 ToolSocial（common_ways）
# ============================================================

async def _tool_launch_pfca(
    task_info: str,
    target_session_id: str = "",
    target_task_title: str = "",
) -> str:
    """提交 PFCA 启动决策（不直接执行）。

    设计：proactive 只负责"选择做什么"，实际启动 PFCA / 任务链 / conversation
    的动作交给 station 统一调度（与 ongoing_truck 的"只决策不执行"风格一致）。

    Args:
        task_info: 任务信息 str，含根源 + 时机 + 意图
        target_session_id: 目标会话 id（'private_xxx' / 'group_xxx' / 空字符串）
        target_task_title: 本次要处理的任务标题。
            若本次主动唤醒源于一条已到达 next_occurrence 的未完成任务，必须填其标题（与任务表精确匹配）；
            若无任务来源（如基于情绪/观察的临时主动任务），传空字符串 ''。

    Returns:
        JSON 字符串：
        ``{"status": "queued", "task_info": ..., "target_session_id": ..., "target_task_title": ...}``
        由 ongoing_truck 在 decision_result 里透传给 station，station 启动任务链
        并调 PFCA 时会把这两段拼成 PFCA 的 user content。
    """
    logger.info(
        f"proactive 提交 PFCA 启动决策 (不直接执行), task_info 长度={len(task_info)}, "
        f"target={target_session_id or '(无)'}, "
        f"task_title={target_task_title or '(无)'}"
    )
    payload = {
        "status": "queued",
        "task_info": task_info,
        "target_session_id": target_session_id or "",
        "target_task_title": target_task_title or "",
    }
    return json.dumps(payload, ensure_ascii=False)


async def _tool_memory_adjustment(
    session_id: str,
    modification_type: str,
    content: str,
    extra_name: str = "",
) -> str:
    """调整或创建记忆。

    对齐 PFCA_tool_manager.memory_adjustment（pfca_tools.py:499）。

    Args:
        session_id: 发生的会话ID（在哪一个会话的会谈中被提及的）。
                    格式为 'type_id'，例如 'private_1234567890' / 'group_198431299'。
                    系统会根据该 ID 自动判定是群聊（Group）还是私聊（Session/Character）。
        modification_type: 修改类型。
                    'session'：修改本会话印象（兼容群聊/私聊）。
                    'mentioned_person'：创建一个在该会话中被提及的重要人物。
        content: 自然语言描述修改的值。
        extra_name: 仅当 modification_type='mentioned_person' 时必填，被提及人物的名字。

    Returns:
        str: Agent 的文本回复
    """
    # 参数校验
    if not session_id:
        return "[错误] memory_adjustment 缺少必填参数 session_id"
    if modification_type not in ("session", "mentioned_person"):
        return f"[错误] memory_adjustment 的 modification_type 必须是 'session' 或 'mentioned_person'，收到: {modification_type}"
    if not content:
        return "[错误] memory_adjustment 缺少必填参数 content"
    if modification_type == "mentioned_person" and not extra_name:
        return "[错误] modification_type='mentioned_person' 时必须提供 extra_name（被提及人物的名字）"

    logger.info(
        f"执行memory_adjustment操作: session_id={session_id}, "
        f"modification_type={modification_type}, content={content}, extra_name={extra_name}"
    )

    from brain.hc.global_session_state import get_global_session_state
    from brain.base.light_agent.memory_agent import MemoryAgent

    # 事件链由 station 在调 async_process_from_att_factory 前统一启动，
    # 这里不再启动事件链（与 station 的事件链生命周期对齐）。
    focus_state = get_global_session_state()

    # 记录到行动日志
    if modification_type == "mentioned_person":
        pfca_action = f"记录被提及人物「{extra_name}」（来源会话 {session_id}）：{content}"
    else:
        pfca_action = f"调整会话「{session_id}」印象：{content}"
    focus_state.manager_pfca_actions('add', pfca_action)
    logger.info(f"添加pfca action: {pfca_action}")

    agent = MemoryAgent()
    result = await agent.run(
        session_id=session_id,
        modification_type=modification_type,
        content=content,
        extra_name=extra_name or None,
    )
    return str(result)


async def _tool_skip_proactive(reason: str = "no_action_needed") -> str:
    """主动任务跳过/退出工具。

    LLM 经过判断后，如果认为不需要主动推进（比如：
    - reason 不充分
    - 当前已有未读消息排队，主动任务应让位
    - 与目标用户画像/关系冲突
    - 时机不恰当
    ），可以调用本工具主动跳过，session 立即退出。

    Args:
        reason: 跳过原因（给 history/日志留痕）
    """
    logger.info(f"proactive LLM 决定跳过: {reason}")
    return f"已跳过本次主动任务, reason={reason}"