# -*- coding: utf-8 -*-
"""任务整理登记Agent - 负责整理任务、创建/更新任务状态

设计要点：
- 多轮执行：每轮先拉取"全景"（进行中任务 + 本轮 backlog），让 agent 自己规划，
  再调工具。本轮可多次创建/更新；当所有 backlog 已处理、任务表已整理得当时，
  直接以自然语言回复即视为结束本轮。
- 拒绝合并：当 backlog 含多个明显不同主题的任务时，必须为每个独立主题分别建任务；
  只能合并语义上确实同一件事的子项。
- 全异步：复用既有 TaskOperations（已经是 async），本文件不再引入同步 IO。

按 `lightweight_agent.LightweightAgent` 的五段式骨架订正：
本 agent 的多轮循环为自实现（维护跨 run 的 messages / tools_called），
仅复用父类的 `_call_llm` / `_initialize_tool_functions` / `get_available_tools` / `get_system_prompt`。

任务工具（create_task / update_task_status / query_task_status）的 schema 与实现
收口至 ToolTask（common_ways/tool_task.py），本类仅做委托拼装。
（2026-09-15 由 event_register_agent 事件系统重做为任务系统）
"""

import json
from typing import List, Dict, Any, Optional

from brain.base.light_agent.lightweight_agent import LightweightAgent
from brain.base.common_ways.tool_task import ToolTask
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from config.role_settings import role_config
from brain.hc.database.task import TaskOperations

logger = get_logger(__name__, "DEBUG")


# 单次 run() 最多允许多少轮 LLM 决策（防止死循环把 token 烧光）
MAX_TURNS = 8

# 第一条 user 输入中额外展示的最近完结任务数量
RECENT_COMPLETED_LIMIT = 3


class TaskRegisterAgent(LightweightAgent):
    """任务整理登记Agent - 专门负责任务整理和任务管理"""

    # schema 与实现已收口至 ToolTask，本类仅做委托拼装
    def _initialize_tool_functions(self) -> Dict[str, Any]:
        return ToolTask.get_tool_functions()

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
        logger.debug("[task_register] 上下文已 reset")

    def get_available_tools(self) -> List[Dict[str, Any]]:
        return ToolTask.get_task_tool_schemas()

    def get_system_prompt(self) -> str:
        return f"""
你是{role_config.ASSISTANT_NAME}，一个任务执行agent。

【系统定位】
你是复杂类人agent系统中的一员。PFCA agent 会向你下发"任务"的整理请求，
你的回复会以工具返回结果的形式回流给 PFCA，由它继续后续决策与对话。
因此你与 PFCA 之间是协作关系——你的输出应当包含足够的信息，让 PFCA 知道本轮做了什么、还有什么悬而未决。

【核心职责】
- 维护"任务表"：按 PFCA 给的 backlog，对任务进行 create / update。
- backlog 可能出错或不完整，你要按实际情况判断；与已有任务相悖或递进的，应更新（覆盖概况）或标记完结。
- 目标是让任务表【有条理】，而不是机械翻译 backlog：无重复、无冗余。

【工具说明】
- create_task：为独立主题创建新任务（标题 + 概况 + 下次发生时间）。
- update_task_status：修改任务状态——用新的完整概况覆盖旧的概况，并更新下次发生时间；
  下次发生时间输入「完结」即标记该任务已经完成。
- query_task_status：查询任务状态（所有未完成任务列表 / 某一具体任务细节）。

【重要约束】
- 信息不完整时：不要瞎猜。在最终文字回复里**明确反馈给 PFCA**：缺什么、哪里矛盾、建议 PFCA 怎么补齐。
- 对进行中的任务（next_occurrence 会触发下一次系统主动激活）：
  - 该任务代表着"下次系统被自动唤醒并处理该任务"的时间点。
  - 你应当向 PFCA 确认：这条任务的安排（next_occurrence、summary）是否已经得到主人确认？
    若 PFCA 的输入中没有"主人已确认"的迹象，应在回复中明确提醒 PFCA，让它去和主人确认后再落实。
- 严禁合并明显不同的主题；只在语义确属同一件事时合并。
- 一轮内对同一 title 不要重复 create_task；已有任务的变化用 update_task_status。
- 所有独立主题都处理完后，用自然语言回复（不调工具）结束本轮；回复用"你"自指，避免"我"。"""

    # ===================== 五段式骨架（run 自实现多轮循环，其余给默认实现） =====================
    def _preprocess(self, query: str) -> Any:
        return query

    def _should_continue(self, response_message, results: List[Dict[str, Any]]) -> bool:
        return bool(response_message.tool_calls)

    def _postprocess(self, content: str, extra_info: Any) -> Dict[str, Any]:
        return {"content": content, "extra_info": extra_info}

    async def _call_llm(self, messages: list, *, level: str = "flash",
                        tool_choice: Optional[str] = None,
                        system_prompt: Optional[str] = None, **kwargs):
        """统一 LLM 调用：走 call_llm。"""
        from utils.llm_client_manager import call_llm

        sp = system_prompt if system_prompt is not None else self.get_system_prompt()
        full_messages = [{"role": "system", "content": sp}] + messages
        return await call_llm(
            level=level,
            messages=full_messages,
            tools=self.get_available_tools(),
        )

    # ===================== 工具实现 =====================
    # create_task / update_task_status / query_task_status 已迁至 ToolTask（common_ways），
    # 经 get_tool_functions() 分发表直接拼装，本类不再重复实现

    # ===================== 多轮入口 =====================
    @async_handle_errors()
    async def run(
        self,
        additional_content: str = None,
        operation_type: str = None,
    ) -> Dict[str, Any]:
        """多轮执行：每轮 LLM 决策 → 工具执行 → 刷新任务全景，
        直到 LLM 直接给文字回复（视为结束）或达到 MAX_TURNS。"""
        backlog_lines = []
        if additional_content:
            backlog_lines.append(f"待处理内容：{additional_content}")
        if operation_type:
            backlog_lines.append(f"操作类型：{operation_type}")
        if not backlog_lines:
            backlog_lines.append("（无显式 backlog，请主动审视当前任务表并整理）")
        backlog_text = "\n".join(backlog_lines)

        system_prompt = self.get_system_prompt()

        if not self._is_initialized:
            initial_active = await self._fetch_active_tasks_text()
            initial_recent_completed = await self._fetch_recent_completed_tasks_text()
            initial_user = self._build_user_message(
                backlog_text,
                turn_index=0,
                active_text=initial_active,
                recent_completed_text=initial_recent_completed,
                continuation_note=None,
            )
            self._messages.append({"role": "user", "content": initial_user})
            self._is_initialized = True
        else:
            refreshed_active = await self._fetch_active_tasks_text()
            continuation_note = (
                "PFCA 又发来一次请求，请基于你之前的整理结果继续处理。\n"
                "下面的 backlog 是本次新增/修订的内容。"
            )
            self._messages.append({
                "role": "user",
                "content": self._build_user_message(
                    backlog_text,
                    turn_index=0,
                    active_text=refreshed_active,
                    recent_completed_text=None,
                    continuation_note=continuation_note,
                ),
            })

        self._last_backlog_text = backlog_text

        finished = False
        last_assistant_text: str = self._final_text or ""

        for turn in range(1, MAX_TURNS + 1):
            logger.info(f"[task_register] turn {turn}/{MAX_TURNS}")

            response = await self._call_llm(
                self._messages,
                level='flash',
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
                    f"[task_register] turn {turn} LLM 直接文字回复，结束本轮"
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
                    f"[task_register] turn {turn} tool={function_name} args={function_args}"
                )

                if function_name not in self.tool_functions:
                    tool_result = {"error": f"未知工具: {function_name}"}
                else:
                    try:
                        tool_result = await self.tool_functions[function_name](**function_args)
                    except Exception as e:
                        logger.exception(f"[task_register] 工具 {function_name} 执行异常")
                        tool_result = {"error": f"{type(e).__name__}: {e}"}

                self._tools_called.append({"tool": function_name, "args": function_args, "result": tool_result})

                self._messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                })

            # 4) 把"新一轮看到的全貌"作为 user 提示塞回去
            next_active = await self._fetch_active_tasks_text()
            next_user = self._build_user_message(backlog_text, turn_index=turn, active_text=next_active)
            self._messages.append({"role": "user", "content": next_user})

        if not finished:
            logger.warning(
                f"[task_register] 达到 MAX_TURNS={MAX_TURNS} 仍未自然结束，强制收尾"
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
        active_text: str,
        recent_completed_text: Optional[str] = None,
        continuation_note: Optional[str] = None,
    ) -> str:
        """每轮 user 消息：backlog + 当前任务全景 + 轮次提示。"""
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
                "请继续：处理剩余 backlog / 整理任务表，"
                "完成后直接以文字回复即可结束本轮。"
            )

        parts = [head, f"【当前任务全景（进行中）】\n{active_text}"]

        if turn_index == 0 and recent_completed_text:
            parts.append(
                f"【参考：最近完结的任务（最多 {RECENT_COMPLETED_LIMIT} 条）】\n"
                f"{recent_completed_text}"
            )

        parts.append(f"【当前时间】{current_time}")

        return "\n\n".join(parts)

    async def _fetch_active_tasks_text(self) -> str:
        """异步拉取当前所有进行中的任务，格式化为字符串。

        拉取失败直接上抛崩溃阻断（硬约定），不吞错。"""
        task_ops = TaskOperations()
        ids = await task_ops.get_active_tasks()
        if not ids:
            return "（当前没有进行中的任务）"
        lines = []
        for i, tid in enumerate(ids[:20], 1):
            formatted = await task_ops.format_task_by_id(tid)
            if not formatted.startswith("未找到"):
                lines.append(f"{i}. {formatted}")
        return "\n".join(lines) if lines else "（当前没有进行中的任务）"

    async def _fetch_recent_completed_tasks_text(self) -> str:
        """异步拉取最近完结的任务，格式化为字符串。

        拉取失败直接上抛崩溃阻断（硬约定），不吞错。"""
        task_ops = TaskOperations()
        ids = await task_ops.get_recent_completed_tasks(limit=RECENT_COMPLETED_LIMIT)
        if not ids:
            return "（暂无已完结的任务）"
        lines = []
        for i, tid in enumerate(ids, 1):
            formatted = await task_ops.format_task_by_id(tid)
            if not formatted.startswith("未找到"):
                lines.append(f"{i}. {formatted}")
        return "\n".join(lines) if lines else "（暂无已完结的任务）"

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
