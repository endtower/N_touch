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
- query_address_book:        查看通讯录（对齐 PFCA_tool_manager._query_address_book）
- query_conversation_history: 查看一个会话的聊天记录（对齐 PFCA_tool_manager.query_history）
- update_long_term_event:    受限更新长期事件（仅 update；用于推迟唤醒时间，详见工具 docstring）
- skip_proactive:            主动跳过本次任务（终结工具）

暂未暴露给 LLM 的工具（实现 + 注册保留，仅从 TOOL_SCHEMAS 中隐藏，避免 LLM 发现/误调）：
- memory_agent:              调整记忆（实现仍保留在 _tool_memory_adjustment）
- event_register_agent:      调整事件（实现仍保留在 _tool_event_judgement；与 update_long_term_event
                             不同：event_register_agent 走 EventRegisterAgent.run 的多轮 LLM 决策，
                             可 create/update/delete；proactive 不开放给 LLM）
"""

import json
import re
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from utils.logger import get_logger
from brain.base import ToolManager

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
                "PFCA 收到的输入是三个字段：任务信息（含根源 + 时机）+ 目标会话 id + 本次要处理的长期任务标题。"
                "若本次主动唤醒是因为【当前已到达计划再处理时间的未完成事件】触发的，"
                "target_long_term_event_title 必须填写该事件的标题（必须能在事件表里精确匹配）；"
                "否则（无长期事件来源的主动任务，如基于情绪/观察的临时任务）传空字符串 ''。"
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
                    "target_long_term_event_title": {
                        "type": "string",
                        "description": (
                            "本次要处理的长期任务（长期事件）的标题。"
                            "若本次主动唤醒是因为【当前已到达计划再处理时间的未完成事件】触发的，"
                            "必须填写该事件的标题（须能在事件表中精确匹配一条 ongoing 长期事件）；"
                            "否则传空字符串 ''。"
                        ),
                    },
                },
                "required": ["task_info"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_address_book",
            "description": "查看自己都可以联系到谁或在哪些群聊中。chat_type 选'私聊'/'群聊'/'全部'，一般选全部。",
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_type": {
                        "type": "string",
                        "enum": ["私聊", "群聊", "全部"],
                        "description": "聊天类型筛选",
                    },
                },
                "required": ["chat_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_conversation_history",
            "description": (
                "查询某个会话的历史消息。用于了解特定会话的上下文。"
                "如不知道 session_id 请先调用 query_address_book 查通讯录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "目标会话的id，格式为'type_id'，例如'private_1234567890'或'group_198431299'。如没有这些信息，请先查询通讯录。",
                    },
                    "search_range": {
                        "type": "string",
                        "description": "格式为'last_n'，其中n为要返回的消息条数，例如'last_10'表示返回最后10条历史聊天消息。",
                    },
                },
                "required": ["session_id", "search_range"],
            },
        },
    },
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
    {
        "type": "function",
        "function": {
            "name": "update_long_term_event",
            "description": (
                "更新一条【已存在】的长期事件。\n"
                "重要限制（proactive 上下文）：\n"
                "  - 本工具**只能更新已存在的长期事件**，禁止创建新事件、禁止删除事件。\n"
                "  - event_title 必须能在事件表里精确匹配到一条已存在的长期事件。\n"
                "  - 不确定事件是否存在时，请先用 query_conversation_history 等工具确认，"
                "或直接放弃更新（不要靠模糊标题硬调）。\n"
                "【典型用途：推迟再次唤醒时间】\n"
                "  当你审视当前被唤醒的输入，发现它来自一条已经到期的长期事件，"
                "但此刻又不想/不适宜执行（或条件不满足、或暂时不感兴趣），"
                "**必须**调用本工具把这条事件的 next_occurrence 推迟到一个更晚的时间点，"
                "以避免系统短期重复唤起——这是防止重复唤起的唯一受支持手段。\n"
                "  - 把 event_type 设为 'ongoing'，并在 next_occurrence 里填入新的期望时间（ISO 8601）。\n"
                "  - summary 里要说明为什么这次不处理 / 推迟到何时。\n"
                "也可以用于：(1) 给已有事件追加新进展；(2) 把一条 ongoing 事件标记为 completed（结束）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_title": {
                        "type": "string",
                        "description": (
                            "要更新的事件标题，必须能在事件表中精确匹配到一条已存在的长期事件。"
                        ),
                    },
                    "event_type": {
                        "type": "string",
                        "enum": ["ongoing", "completed"],
                        "description": (
                            "更新后的事件类型。"
                            "ongoing=继续关注，必须同时提供 next_occurrence；"
                            "completed=标记为完成（事件不再触发主动任务）。"
                        ),
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "更新后的事件概况，将覆盖旧的概况。"
                            "若本次为推迟唤醒时间，请在这里说明推迟原因（例如：时机不成熟、缺关键信息等）。"
                        ),
                    },
                    "next_occurrence": {
                        "type": "string",
                        "description": (
                            "预计再次发生时间。event_type='ongoing' 时必填，ISO 8601 格式，"
                            "例如 '2024-01-20T10:00:00'。"
                            "推迟唤醒时必须填一个比当前时间更晚的时间点，否则系统会立刻再次唤醒。"
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "本次更新的原因（给 history/日志留痕），例如：'主动推迟，理由：时机不成熟'。"
                        ),
                    },
                },
                "required": ["event_title", "event_type", "summary"],
            },
        },
    },
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

    _instance: Optional["ProactiveToolRegistry"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._functions: Dict[str, Callable[..., Awaitable[Any]]] = {
            "launch_pfca": _tool_launch_pfca,
            "query_address_book": _tool_query_address_book,
            "query_conversation_history": _tool_query_conversation_history,
            "memory_adjustment": _tool_memory_adjustment,
            "event_judgement": _tool_event_judgement,
            "skip_proactive": _tool_skip_proactive,
            "update_long_term_event": _tool_update_long_term_event,
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
# 对齐 PFCA_tool_manager 中同类工具的实现
# ============================================================

async def _tool_launch_pfca(
    task_info: str,
    target_session_id: str = "",
    target_long_term_event_title: str = "",
) -> str:
    """提交 PFCA 启动决策（不直接执行）。

    设计：proactive 只负责"选择做什么"，实际启动 PFCA / 事件链 / conversation
    的动作交给 station 统一调度（与 ongoing_truck 的"只决策不执行"风格一致）。

    Args:
        task_info: 任务信息 str，含根源 + 时机 + 意图
        target_session_id: 目标会话 id（'private_xxx' / 'group_xxx' / 空字符串）
        target_long_term_event_title: 本次要处理的长期事件标题。
            若本次主动唤醒源于一条已到达 next_occurrence 的长期事件，必须填其标题（与事件表精确匹配）；
            若无长期事件来源（如基于情绪/观察的临时主动任务），传空字符串 ''。

    Returns:
        JSON 字符串：
        ``{"status": "queued", "task_info": ..., "target_session_id": ..., "target_long_term_event_title": ...}``
        由 ongoing_truck 在 decision_result 里透传给 station，station 启动事件链
        并调 PFCA 时会把这两段拼成 PFCA 的 user content。
    """
    logger.info(
        f"proactive 提交 PFCA 启动决策 (不直接执行), task_info 长度={len(task_info)}, "
        f"target={target_session_id or '(无)'}, "
        f"long_term_event_title={target_long_term_event_title or '(无)'}"
    )
    payload = {
        "status": "queued",
        "task_info": task_info,
        "target_session_id": target_session_id or "",
        "target_long_term_event_title": target_long_term_event_title or "",
    }
    return json.dumps(payload, ensure_ascii=False)


async def _tool_query_address_book(chat_type: str) -> str:
    """查询通讯录。

    对齐 PFCA_tool_manager._query_address_book（pfca_tools.py:541）。
    Args:
        chat_type: '私聊' | '群聊' | '全部'
    """
    logger.debug(f"proactive 执行 query_address_book, chat_type={chat_type}")

    from memory.address_book import get_address_book
    address_book = get_address_book()

    if chat_type == '私聊':
        return await address_book.get_private_contacts()
    elif chat_type == '群聊':
        return await address_book.get_group_contacts()
    elif chat_type == '全部':
        return await address_book.get_all_contacts()
    else:
        return f"未知的聊天类型: {chat_type}"


async def _tool_query_conversation_history(session_id: str, search_range: str) -> str:
    """查询指定会话的历史聊天记录。

    对齐 PFCA_tool_manager.query_history（pfca_tools.py:377）。
    Args:
        session_id:  会话标识，格式为 "type_id"，如 "private_1234567890" / "group_198431299"
        search_range: 历史记录查询范围，格式为 'last_n'，例如 'last_10'
    """
    logger.debug(f"proactive 执行 query_conversation_history, session={session_id}, range={search_range}")

    from brain.hc.global_session_history import (
        get_global_session_history,
        FetchOptions,
        MessageFormatOptions,
        FetchBehavior,
    )

    history = get_global_session_history()

    message_count = 10
    if search_range:
        last_n_match = re.match(r'last_(\d+)', search_range)
        if last_n_match:
            message_count = int(last_n_match.group(1))
        else:
            logger.error(f"消息数量格式错误: {search_range}")
            return "消息数量格式错误，请使用正确的格式: 'last_n'，其中n为要返回的消息条数，例如'last_10'表示返回最后10条消息。"

    end_time = datetime.now().timestamp()
    start_time = 0
    result = await history.fetch(
        session_id, "",
        start_time=start_time,
        end_time=end_time,
        options=FetchOptions(
            behavior=FetchBehavior(message_limit=message_count),
            message_format=MessageFormatOptions(
                show_new_marker=False,
                show_timestamp=True,
            ),
        ),
    )
    messages = "\n".join(result.formatted_lines) if result.formatted_lines else ""
    return messages if messages else "（没有历史消息）"


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


async def _tool_event_judgement(content: str, operation_type: str) -> str:
    """事件调整，根据 PFCA 的请求对事件进行记录、更新或归档。

    对齐 PFCA_tool_manager.event_judgement（pfca_tools.py:675）。

    Args:
        content: 具体情况的详细描述
        operation_type: 操作类型
            - 'create_ongoing': 创建新的持续关注事件
            - 'update_ongoing': 修改或完成一个持续关注事件
            - 'create_completed': 创建一个独立的完成事件

    Returns:
        str: EventRegisterAgent 的最终文本回复（未使用工具的轮次）
    """
    logger.info(f"执行event_judgement操作，operation_type={operation_type}，内容: {content}")

    from brain.hc.global_session_state import get_global_session_state
    from brain.base.light_agent.event_register_agent import EventRegisterAgent

    focus_state = get_global_session_state()
    action_desc = {
        "create_ongoing": f"新增待办任务：{content}",
        "update_ongoing": f"更新待办任务：{content}",
        "create_completed": f"归档完成事件：{content}",
    }.get(operation_type, f"记录待办任务：{content}")
    focus_state.manager_pfca_actions('add', action_desc)

    agent = EventRegisterAgent()
    result = await agent.run(additional_content=content, operation_type=operation_type)

    if isinstance(result, dict) and result.get('formatted_output'):
        return result['formatted_output']
    return str(result) if result else "任务已记录"


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


async def _tool_update_long_term_event(
    event_title: str,
    event_type: str,
    summary: str,
    next_occurrence: str = "",
    reason: str = "",
) -> str:
    """受限的长期事件更新工具（proactive 专用）。

    与 EventRegisterAgent 暴露的 update_ongoing_event 不同，这里**直接复用底层
    _update_ongoing_event_impl**（绕过 EventRegisterAgent.run 的多轮 LLM 决策层），
    因此只能 update、不能 create、不能 delete。

    Args:
        event_title: 事件标题（必须能在事件表里精确匹配到一条已存在的长期事件）
        event_type: 'ongoing' | 'completed'
        summary: 更新后的事件概况
        next_occurrence: 预计再次发生时间（ISO 8601，event_type='ongoing' 时必填）
        reason: 本次更新原因（仅日志/留痕，不影响行为）

    Returns:
        JSON 字符串，包含 success / event_title / event_type / next_occurrence / reason
    """
    if not event_title:
        return json.dumps(
            {"success": False, "error": "event_title 不能为空"},
            ensure_ascii=False,
        )
    if event_type not in ("ongoing", "completed"):
        return json.dumps(
            {"success": False, "error": f"event_type 必须是 'ongoing' 或 'completed'，收到: {event_type!r}"},
            ensure_ascii=False,
        )
    if not summary:
        return json.dumps(
            {"success": False, "error": "summary 不能为空"},
            ensure_ascii=False,
        )
    if event_type == "ongoing" and not next_occurrence:
        return json.dumps(
            {"success": False, "error": "event_type='ongoing' 时必须提供 next_occurrence"},
            ensure_ascii=False,
        )

    logger.info(
        f"proactive 更新长期事件: event_title={event_title!r}, "
        f"event_type={event_type}, next_occurrence={next_occurrence!r}, reason={reason!r}"
    )

    # 懒导入：避免与 event_register_agent 形成循环 / 重
    from brain.base.light_agent.event_register_agent import _update_ongoing_event_impl

    result = await _update_ongoing_event_impl(
        event_title=event_title,
        event_type=event_type,
        summary=summary,
        next_occurrence=next_occurrence or None,
    )

    # 记录 reason 到历史/日志（不影响 update 行为本身）
    if reason:
        try:
            from brain.hc.global_session_state import get_global_session_state
            focus_state = get_global_session_state()
            focus_state.manager_pfca_actions(
                'add',
                f"更新长期事件「{event_title}」：{reason}",
            )
        except Exception as e:
            logger.debug(f"记录 reason 到 pfca_actions 失败（不影响主流程）: {e}")

    # 包装返回：补一个 reason 字段供 LLM 校对
    result.setdefault("reason", reason)
    return json.dumps(result, ensure_ascii=False, default=str)