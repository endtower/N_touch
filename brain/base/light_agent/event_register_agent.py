# -*- coding: utf-8 -*-
"""事件整理登记Agent - 负责整理事件、创建/更新/删除长期事件

设计要点：
- 多轮执行：每轮先拉取"全景"（ongoing 事件 + 本轮 backlog），让 agent 自己规划，
  再调工具。本轮可多次创建/更新/删除；当所有 backlog 已处理、事件表已整理得当时，
  直接以自然语言回复即视为结束本轮。
- 拒绝合并：当 backlog 含多个明显不同主题的任务时，必须为每个独立主题分别建事件；
  只能合并语义上确实同一件事的子项。
- 主动整理：agent 可以删除冗余/已无意义的事件，目标是让 ongoing 事件表保持"有条理"。
- 全异步：复用既有 EventOperations（已经是 async），本文件不再引入同步 IO。

按 `lightweight_agent.LightweightAgent` 的五段式骨架订正：
本 agent 的多轮循环为自实现（维护跨 run 的 messages / tools_called），
仅复用父类的 `_call_llm` / `_initialize_tool_functions` / `get_available_tools` / `get_system_prompt`。
"""

import json
from typing import List, Dict, Any, Optional
from datetime import datetime

from brain.base.light_agent.lightweight_agent import LightweightAgent
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from config.role_settings import role_config
from brain.hc.database.event import EventOperations

logger = get_logger(__name__, "DEBUG")


# 单次 run() 最多允许多少轮 LLM 决策（防止死循环把 token 烧光）
MAX_TURNS = 8

# 第一条 user 输入中额外展示的最近 completed 事件数量
RECENT_COMPLETED_LIMIT = 3


class EventRegisterAgent(LightweightAgent):
    """事件整理登记Agent - 专门负责事件整理和长期事件管理"""

    def _initialize_tool_functions(self) -> Dict[str, Any]:
        return {
            "create_event": self._create_event,
            "update_ongoing_event": self._update_ongoing_event,
            "delete_event": self._delete_event,
        }

    def __init__(self):
        super().__init__()
        # 跨 run() 持久化的对话状态。
        # 设计：在一个事件链（一个 PFCA 会话）中，PFCA 可能会多次呼叫本 agent，
        # 每次调用都应该延续上一次的上下文（messages / backlog / tools_called），
        # 保证两个 agent 之间的多轮通讯是连续的，而不是每次都从零开始。
        # 调用方需要在事件链结束时显式调用 reset() 清空。
        self._messages: List[Dict[str, Any]] = []
        self._tools_called: List[Dict[str, Any]] = []
        self._last_backlog_text: str = ""
        self._final_text: str = ""
        self._is_initialized: bool = False

    def reset(self) -> None:
        """清空跨 run() 的对话状态。"""
        self._messages = []
        self._tools_called = []
        self._last_backlog_text = ""
        self._final_text = ""
        self._is_initialized = False
        logger.debug("[event_register] 上下文已 reset")

    def get_available_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "create_event",
                    "description": (
                        "为一个独立主题创建一个新事件。\n"
                        "【不要把多个明显不同的任务合并到同一个事件里】——"
                        "如果 backlog 含 A/B 两个不同主题，必须为 A 调一次 create_event、为 B 再调一次。\n"
                        "只有语义上确实是同一件事的子项，才允许合并。\n"
                        "在调用前，请先核对【当前ongoing事件】，避免创建与已有 ongoing 重复或重叠的事件。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "事件标题，简短概括事件",
                            },
                            "event_type": {
                                "type": "string",
                                "enum": ["ongoing", "completed"],
                                "description": "事件类型。ongoing=需要继续关注/跟进，completed=已完结",
                            },
                            "summary": {
                                "type": "string",
                                "description": "事件概况，详细描述事件内容（少于100个字符）",
                            },
                            "next_occurrence": {
                                "type": "string",
                                "description": (
                                    "预计再次发生时间。ongoing时必填，ISO 8601格式，如：2024-01-20T10:00:00"
                                ),
                            },
                        },
                        "required": ["title", "event_type", "summary"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_ongoing_event",
                    "description": (
                        "修订一个正在进行的长期事件。\n"
                        "用于：(1) 给已有事件追加进展；(2) 把一个 ongoing 事件标记为 completed；"
                        "(3) 修正之前写错的标题/概况。\n"
                        "严禁用 update 把两个不同主题硬塞进同一个事件——那种情况请分别 create_event。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "event_title": {
                                "type": "string",
                                "description": "要修订的事件标题，用于定位事件",
                            },
                            "event_type": {
                                "type": "string",
                                "enum": ["ongoing", "completed"],
                                "description": "修订后的事件类型。ongoing=继续关注，completed=标记为完成",
                            },
                            "summary": {
                                "type": "string",
                                "description": "修订后的事件概况，描述事件当前状态或新进展，将会覆盖旧的概况",
                            },
                            "next_occurrence": {
                                "type": "string",
                                "description": "预计再次发生时间。ongoing时必填，ISO 8601格式",
                            },
                        },
                        "required": ["event_title", "event_type", "summary"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_event",
                    "description": (
                        "删除一个事件。\n"
                        "适用场景：重复创建的事件、已经过期且无后续的 ongoing、标题拼写错误、"
                        "或与已有事件重叠且应被合并掉的冗余项。\n"
                        "删除前请确认该事件不会再被需要。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "event_title": {
                                "type": "string",
                                "description": "要删除的事件的标题",
                            },
                            "reason": {
                                "type": "string",
                                "description": "删除原因（简短一句话），用于审计/日志",
                            },
                        },
                        "required": ["event_title"],
                    },
                },
            },
        ]

    def get_system_prompt(self) -> str:
        return f"""
你是{role_config.ASSISTANT_NAME}，一个事件执行agent。

【系统定位】
你是复杂类人agent系统中的一员。PFCA agent 会向你下发"长期事件"的整理请求，
你的回复会以工具返回结果的形式回流给 PFCA，由它继续后续决策与对话。
因此你与 PFCA 之间是协作关系——你的输出应当包含足够的信息，让 PFCA 知道本轮做了什么、还有什么悬而未决。

【核心职责】
- 维护"长期事件表"：按 PFCA 给的 backlog，对事件进行 create / update / delete。
- backlog 可能出错或不完整，你要按实际情况判断；与已有事件相悖或递进的，应更新或标记完成。
- 目标是让事件表【有条理】，而不是机械翻译 backlog：无重复、无冗余，必要时主动 delete。

【重要约束】
- 信息不完整时：不要瞎猜。在最终文字回复里**明确反馈给 PFCA**：缺什么、哪里矛盾、建议 PFCA 怎么补齐。
- 对 ongoing 类事件（会触发下一次系统主动激活）：
  - 该事件代表着"下次系统被自动唤醒并处理该事件"的时间点。
  - 你应当向 PFCA 确认：这条 ongoing 的安排（next_occurrence、summary）是否已经得到主人确认？
    若 PFCA 的输入中没有"主人已确认"的迹象，应在回复中明确提醒 PFCA，让它去和主人确认后再落实。
- 严禁合并明显不同的主题；只在语义确属同一件事时合并。
- 一轮内对同一 title 不要重复 create_event；要新建前先 delete 旧的。
- 所有独立主题都处理完后，用自然语言回复（不调工具）结束本轮；回复用"你"自指，避免"我"。"""

    # ===================== 五段式骨架（run 自实现多轮循环，其余给默认实现） =====================
    def _preprocess(self, query: str) -> Any:
        return query

    def _should_continue(self, response_message, results: List[Dict[str, Any]]) -> bool:
        return bool(response_message.tool_calls)

    def _postprocess(self, content: str, extra_info: Any) -> Dict[str, Any]:
        return {"content": content, "extra_info": extra_info}

    async def _call_llm(self, messages: list, *, mode: str = "basic-reason",
                        tool_choice: Optional[str] = None,
                        system_prompt: Optional[str] = None, **kwargs):
        """统一 LLM 调用：走 call_llm。"""
        from utils.llm_client_manager import call_llm

        sp = system_prompt if system_prompt is not None else self.get_system_prompt()
        full_messages = [{"role": "system", "content": sp}] + messages
        return await call_llm(
            mode=mode,
            messages=full_messages,
            tools=self.get_available_tools(),
        )

    # ===================== 工具实现 =====================
    async def _create_event(
        self,
        title: str,
        event_type: str,
        summary: str,
        next_occurrence: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await _create_event_impl(title, event_type, summary, next_occurrence)

    async def _update_ongoing_event(
        self,
        event_title: str,
        event_type: str,
        summary: str,
        next_occurrence: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await _update_ongoing_event_impl(event_title, event_type, summary, next_occurrence)

    async def _delete_event(self, event_title: str, reason: Optional[str] = None) -> Dict[str, Any]:
        return await _delete_event_impl(event_title, reason)

    # ===================== 多轮入口 =====================
    @async_handle_errors()
    async def run(
        self,
        additional_content: str = None,
        operation_type: str = None,
    ) -> Dict[str, Any]:
        """多轮执行：每轮 LLM 决策 → 工具执行 → 刷新事件全景，
        直到 LLM 直接给文字回复（视为结束）或达到 MAX_TURNS。"""
        backlog_lines = []
        if additional_content:
            backlog_lines.append(f"待处理内容：{additional_content}")
        if operation_type:
            backlog_lines.append(f"操作类型：{operation_type}")
        if not backlog_lines:
            backlog_lines.append("（无显式 backlog，请主动审视当前事件表并整理）")
        backlog_text = "\n".join(backlog_lines)

        system_prompt = self.get_system_prompt()

        if not self._is_initialized:
            initial_ongoing = await self._fetch_ongoing_events_text()
            initial_recent_completed = await self._fetch_recent_completed_events_text()
            initial_user = self._build_user_message(
                backlog_text,
                turn_index=0,
                ongoing_text=initial_ongoing,
                recent_completed_text=initial_recent_completed,
                continuation_note=None,
            )
            self._messages.append({"role": "user", "content": initial_user})
            self._is_initialized = True
        else:
            refreshed_ongoing = await self._fetch_ongoing_events_text()
            continuation_note = (
                "PFCA 又发来一次请求，请基于你之前的整理结果继续处理。\n"
                "下面的 backlog 是本次新增/修订的内容。"
            )
            self._messages.append({
                "role": "user",
                "content": self._build_user_message(
                    backlog_text,
                    turn_index=0,
                    ongoing_text=refreshed_ongoing,
                    recent_completed_text=None,
                    continuation_note=continuation_note,
                ),
            })

        self._last_backlog_text = backlog_text

        finished = False
        last_assistant_text: str = self._final_text or ""

        for turn in range(1, MAX_TURNS + 1):
            logger.info(f"[event_register] turn {turn}/{MAX_TURNS}")

            response = await self._call_llm(
                self._messages,
                mode='basic-reason',
                tool_choice="auto",
                system_prompt=system_prompt,
            )
            response_message = response.choices[0].message
            last_assistant_text = response_message.content or ""

            # 1) 没有 tool_calls：LLM 直接给文字回复 → 视为本轮收尾
            if not response_message.tool_calls:
                self._messages.append({
                    "role": "assistant",
                    "content": last_assistant_text,
                })
                finished = True
                self._final_text = last_assistant_text
                logger.info(
                    f"[event_register] turn {turn} LLM 直接文字回复，结束本轮"
                )
                break

            # 2) 把 assistant 消息原样塞回 messages（带 tool_calls）
            self._messages.append(self._serialize_assistant_message(response_message))

            # 3) 逐个执行工具，把结果以 role=tool 灌回
            for tool_call in response_message.tool_calls:
                function_name = tool_call.function.name
                try:
                    function_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    function_args = {}

                logger.info(
                    f"[event_register] turn {turn} tool={function_name} args={function_args}"
                )

                if function_name not in self.tool_functions:
                    tool_result = {"error": f"未知工具: {function_name}"}
                else:
                    try:
                        tool_result = await self.tool_functions[function_name](**function_args)
                    except Exception as e:
                        logger.exception(f"[event_register] 工具 {function_name} 执行异常")
                        tool_result = {"error": f"{type(e).__name__}: {e}"}

                self._tools_called.append({"tool": function_name, "args": function_args, "result": tool_result})

                self._messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                })

            # 4) 把"新一轮看到的全貌"作为 user 提示塞回去
            next_ongoing = await self._fetch_ongoing_events_text()
            next_user = self._build_user_message(backlog_text, turn_index=turn, ongoing_text=next_ongoing)
            self._messages.append({"role": "user", "content": next_user})

        if not finished:
            logger.warning(
                f"[event_register] 达到 MAX_TURNS={MAX_TURNS} 仍未自然结束，强制收尾"
            )

        return {
            "status": "finished" if finished else "max_turns_reached",
            "turns": turn if not finished else turn,
            "tools_called": list(self._tools_called),
            "formatted_output": self._final_text if finished else last_assistant_text,
        }

    # ===================== 构造 user 消息 =====================
    def _build_user_message(
        self,
        backlog_text: str,
        turn_index: int,
        ongoing_text: str,
        recent_completed_text: Optional[str] = None,
        continuation_note: Optional[str] = None,
    ) -> str:
        """每轮 user 消息：backlog + 当前事件全景 + 轮次提示。"""
        from datetime import datetime
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if continuation_note:
            head = f"{continuation_note}\n\n【本次 backlog】\n{backlog_text}"
        elif turn_index == 0:
            head = "【本轮 backlog】\n" + backlog_text
        else:
            head = (
                f"【本轮 backlog（未变）】\n{backlog_text}\n\n"
                f"你已经执行了 {turn_index} 轮工具调用。"
                "请继续：处理剩余 backlog / 整理事件表，"
                "完成后直接以文字回复即可结束本轮。"
            )

        parts = [head, f"【当前事件全景（ongoing）】\n{ongoing_text}"]

        if turn_index == 0 and recent_completed_text:
            parts.append(
                f"【参考：最近创建的 completed 事件（最多 {RECENT_COMPLETED_LIMIT} 条）】\n"
                f"{recent_completed_text}"
            )

        parts.append(f"【当前时间】{current_time}")

        return "\n\n".join(parts)

    async def _fetch_ongoing_events_text(self) -> str:
        """异步拉取当前所有 ongoing 事件，格式化为字符串。"""
        try:
            event_ops = EventOperations()
            ids = await event_ops.get_ongoing_events()
            if not ids:
                return "（无正在进行的长期事件）"
            lines = []
            for i, eid in enumerate(ids[:20], 1):
                formatted = await event_ops.format_event_by_id(eid)
                if not formatted.startswith("未找到"):
                    lines.append(f"{i}. {formatted}")
            return "\n".join(lines) if lines else "（无正在进行的长期事件）"
        except Exception as e:
            logger.warning(f"拉取 ongoing 事件失败: {e}")
            return "（拉取 ongoing 事件失败，请谨慎操作）"

    async def _fetch_recent_completed_events_text(self) -> str:
        """异步拉取最近创建的 completed 事件，格式化为字符串。"""
        try:
            event_ops = EventOperations()
            ids = await event_ops.get_recent_completed_events(limit=RECENT_COMPLETED_LIMIT)
            if not ids:
                return "（暂无已完成的长期事件）"
            lines = []
            for i, eid in enumerate(ids, 1):
                formatted = await event_ops.format_event_by_id(eid)
                if not formatted.startswith("未找到"):
                    lines.append(f"{i}. {formatted}")
            return "\n".join(lines) if lines else "（暂无已完成的长期事件）"
        except Exception as e:
            logger.warning(f"拉取最近 completed 事件失败: {e}")
            return "（拉取最近 completed 事件失败，忽略该参考）"

    def _serialize_assistant_message(self, response_message) -> Dict[str, Any]:
        """把 LLM 返回的 assistant 消息序列化成可回灌 messages 的 dict。"""
        return {
            "role": "assistant",
            "content": response_message.content or "",
            "reasoning_content": getattr(response_message, 'reasoning_content', None),
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in response_message.tool_calls
            ],
        }

    # ===================== 回调（保留空实现，兼容父类约定） =====================
    def _on_tool_result(
        self,
        tool_name: str,
        result: Any,
        results: Dict[str, Any],
    ) -> None:
        pass


# ============================================================
# 工具底层实现（顶层函数，async，统一 await）
# ============================================================

@async_handle_errors()
async def _create_event_impl(
    title: str,
    event_type: str,
    summary: str,
    next_occurrence: Optional[str] = None,
) -> Dict[str, Any]:
    logger.info(f"创建事件: title={title}, type={event_type}")

    if event_type not in ["ongoing", "completed"]:
        event_type = "completed"

    if event_type == "ongoing" and not next_occurrence:
        logger.warning("ongoing事件缺少next_occurrence，将设置为明天")
        next_occurrence = (datetime.now().replace(hour=23, minute=59, second=59)).isoformat()

    event_ops = EventOperations()
    event_id = await event_ops.create_event(
        title=title,
        event_type=event_type,
        summary=summary,
        next_occurrence=next_occurrence,
    )

    logger.info(f"事件创建成功: event_id={event_id}")

    return {
        "event_id": event_id,
        "title": title,
        "event_type": event_type,
        "summary": summary,
        "next_occurrence": next_occurrence,
    }


@async_handle_errors()
async def _update_ongoing_event_impl(
    event_title: str,
    event_type: str,
    summary: str,
    next_occurrence: Optional[str] = None,
) -> Dict[str, Any]:
    logger.info(f"更新事件: event_title={event_title}, type={event_type}")

    if event_type not in ["ongoing", "completed"]:
        event_type = "completed"

    if event_type == "ongoing" and not next_occurrence:
        logger.warning("ongoing事件缺少next_occurrence，将设置为明天")
        next_occurrence = (datetime.now().replace(hour=23, minute=59, second=59)).isoformat()

    event_ops = EventOperations()
    success = await event_ops.update_event(
        event_title,
        summary=summary,
        event_type=event_type,
        next_occurrence=next_occurrence,
    )

    if success:
        logger.info(f"事件更新成功: {event_title}")
    else:
        logger.warning(f"事件更新失败，可能找不到: {event_title}")

    return {
        "event_title": event_title,
        "event_type": event_type,
        "summary": summary,
        "next_occurrence": next_occurrence,
        "success": success,
    }


@async_handle_errors()
async def _delete_event_impl(event_title: str, reason: Optional[str] = None) -> Dict[str, Any]:
    """通过 title 定位并删除事件。"""
    logger.info(f"删除事件: event_title={event_title}, reason={reason!r}")

    event_ops = EventOperations()
    success = await event_ops.delete_event_by_id(event_title)

    if success:
        logger.info(f"事件删除成功: {event_title}")
    else:
        logger.warning(f"事件删除失败，未找到: {event_title}")

    return {
        "event_title": event_title,
        "reason": reason,
        "success": success,
    }
