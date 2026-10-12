# -*- coding: utf-8 -*-
"""Quest：接续事项的查重登记器。

唯一职责：判断调用方给出的这条事项是否与已有待接续事项重复。不重复则登记
（create_task），重复则直接说明已存在、不登记。不做更新、不做阶段推进。

每次调用使用独立工作上下文和独立日志文件；长期接续依靠数据库记录，不依靠
累积旧对话，故全程单轮：一次 LLM 决策即收尾。
"""

import json
from typing import List, Dict, Any, Optional

from brain.base.light_agent.lightweight_agent import LightweightAgent
from brain.base.light_agent.light_history_manager import new_light_history_manager
from utils.logger import get_logger
from utils.llm_client_manager import extract_reasoning
from utils.error_handling import async_handle_errors
from brain.hc.database.task import TaskOperations

logger = get_logger(__name__, "DEBUG")


class QuestAgent(LightweightAgent):
    """接续事项的查重与登记 agent（单轮）。"""

    # ===================== 记录写入级工具（私有） =====================
    @classmethod
    def get_create_task_tool_schema(cls) -> Dict[str, Any]:
        """返回 create_task 工具的 schema（记录写入级，私有于本类）。"""
        return {
            "type": "function",
            "function": {
                "name": "create_task",
                "description": (
                    "登记一条新的待接续事项。仅在确认与已有待接续事项不重复后调用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "任务标题，简短概括这条事项。",
                        },
                        "context": {
                            "type": "string",
                            "description": (
                                "要传递给下次启动时的上下文：这条事项的目的、"
                                "已确认进展、人物与会话标识、材料线索、下次要处理什么。"
                            ),
                        },
                        "next_occurrence": {
                            "type": "string",
                            "description": (
                                "下次唤醒时间：ISO 8601（如 2024-01-20T10:00:00），"
                                "或「尽快」表示现在执行。"
                            ),
                        },
                    },
                    "required": ["title", "context", "next_occurrence"],
                },
            },
        }

    # ===================== 工具表拼装 =====================
    def _initialize_tool_functions(self) -> Dict[str, Any]:
        return {
            "create_task": QuestAgent._create_task_impl,
        }

    def __init__(self):
        super().__init__()
        # 仅本次 run 的工作状态；每次调用重建，长期信息保存在任务记录中。
        self._messages: List[Dict[str, Any]] = []
        self._tools_called: List[Dict[str, Any]] = []
        self._final_text: str = ""
        self._history = None

    def reset(self) -> None:
        """清空本次工作状态；原日志保留供追溯。"""
        self._messages = []
        self._tools_called = []
        self._final_text = ""
        if self._history is not None:
            self._history.archive_context()
            self._history = None
        logger.debug("[quest] 上下文已 reset")

    def _push(self, message: Dict[str, Any]) -> None:
        """追加一条消息：内存 LLM 上下文（self._messages）+ 事件链上下文
        管理器双写（链内落盘到 event_chains/{chain_id}/quest_{HHMMSS}.json）。"""
        self._messages.append(message)
        self._history.append(dict(message))

    def get_available_tools(self) -> List[Dict[str, Any]]:
        return [QuestAgent.get_create_task_tool_schema()]

    def get_system_prompt(self) -> str:
        return """
【职责】
你负责判断调用方给出的这条事项是否与已有待接续事项重复。这是你唯一的职责。
- 不重复：调用 create_task 登记，原样承载调用方给的标题、上下文与唤醒时间。
- 重复（已有同一件事的待接续安排）：直接文字回复说明已存在，不调用 create_task。
不做更新、不拆分阶段、不评价是否值得立项，也不改动已有事项。

【判断依据】
只依据调用方给的事项与下方注入的【已有待接续事项】。标题相近不等于重复；
要看上下文是否指向同一件待办。无法确定是否重复时，简短说明你的判断依据。

【输出】
登记成功后简短说明登记了什么及下次唤醒时间；未成功落库不得声称已登记。
直接以文字回复结束本次调用。
"""

    # ===================== LLM 调用 =====================
    async def _call_llm(self, messages: list, *, level: str = "flash",
                        system_prompt: Optional[str] = None, **kwargs):
        """统一 LLM 调用：走 call_llm。

        Quest 自建循环（不走父类 ``_llm_loop``），故在此单点声明 agent 身份，
        保证 token 统计归到 QuestAgent 而非外层会话。
        """
        from utils.llm_client_manager import call_llm, agent_scope

        sp = system_prompt if system_prompt is not None else self.get_system_prompt()
        full_messages = [{"role": "system", "content": sp}] + messages
        with agent_scope(self.agent_name or type(self).__name__):
            return await call_llm(
                level=level,
                messages=full_messages,
                tools=self.get_available_tools(),
            )

    # ===================== 五段式骨架（run 自实现单轮循环，其余给默认实现） =====================
    def _preprocess(self, query: str) -> Any:
        return query

    def _should_continue(self, response_message, results: List[Dict[str, Any]]) -> bool:
        return bool(response_message.tool_calls)

    def _postprocess(self, content: str, extra_info: Any) -> Dict[str, Any]:
        return {"content": content, "extra_info": extra_info}

    # ===================== 工具实现 =====================
    @staticmethod
    def _resolve_next_occurrence(next_occurrence: Optional[str]) -> str:
        """解析 next_occurrence 的「尽快」快捷语义。

        「尽快」＝现在就执行：把时间置为当前时间——task_truck 下一轮周期检测
        即视为已到期并触发 PFCA。ISO 8601 时间原样规整返回。
        """
        if not next_occurrence:
            return ""
        from datetime import datetime
        if "尽快" in next_occurrence:
            return datetime.now().isoformat(timespec="seconds")
        parsed = datetime.fromisoformat(next_occurrence.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed.isoformat(timespec="seconds")

    @staticmethod
    async def _create_task_impl(
        title: str,
        context: str,
        next_occurrence: Optional[str] = None,
    ) -> Dict[str, Any]:
        """登记一条新的待接续事项。

        Returns:
            {"task_id": str, "title": str, "context": str, "next_occurrence": str}
        """
        next_occurrence = QuestAgent._resolve_next_occurrence(next_occurrence)
        if not next_occurrence:
            raise ValueError("登记接续事项必须提供下次唤醒时间；缺少时间依据时请先向调用方澄清。")

        logger.info(
            f"创建任务: title={title}, next_occurrence={next_occurrence}"
        )

        task_ops = TaskOperations()
        task_id = await task_ops.create_task(
            title=title,
            context=context,
            next_occurrence=next_occurrence,
        )

        logger.info(f"任务创建成功: task_id={task_id}")

        return {
            "task_id": task_id,
            "title": title,
            "context": context,
            "next_occurrence": next_occurrence,
        }

    # ===================== 单轮入口 =====================
    @async_handle_errors()
    async def run(self, title: str, context: str, next_occurrence: str = "") -> Dict[str, Any]:
        """独立处理一次登记请求；单轮决策后收尾。

        参数错误（缺时间等）回给 LLM 自纠；其余异常上抛崩溃阻断。
        """
        self.reset()
        self._history = new_light_history_manager("quest")
        system_prompt = self.get_system_prompt()
        self._history.append({"role": "system", "content": system_prompt})

        directory = await TaskOperations().format_active_task_overview(limit=20)
        self._push({
            "role": "user",
            "content": self._build_user_message(title, context, next_occurrence, directory),
        })

        created_tasks: List[Dict[str, Any]] = []
        response = await self._call_llm(
            self._messages, level='flash', system_prompt=system_prompt,
        )
        response_message = response.choices[0].message
        last_assistant_text: str = response_message.content or ""

        if not response_message.tool_calls:
            self._push({"role": "assistant", "content": last_assistant_text})
            self._final_text = last_assistant_text
            logger.info("[quest] LLM 直接文字回复，未登记")
        else:
            self._push(self._serialize_assistant_message(response_message))
            for tool_call in response_message.tool_calls:
                function_name = tool_call.function.name
                try:
                    function_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    function_args = {}

                logger.info(f"[quest] tool={function_name} args={function_args}")

                if function_name not in self.tool_functions:
                    tool_result = {"error": f"未知工具: {function_name}"}
                else:
                    try:
                        tool_result = await self.tool_functions[function_name](**function_args)
                    except (ValueError, TypeError, KeyError) as e:
                        # 参数错误：回给 LLM 自纠（可恢复）
                        tool_result = {"error": f"{type(e).__name__}: {e}"}
                    # 其余异常（模块未运行 / IO 错等）上抛崩溃阻断，不吞
                self._tools_called.append(
                    {"tool": function_name, "args": function_args, "result": tool_result}
                )
                if (function_name == "create_task" and isinstance(tool_result, dict)
                        and not tool_result.get("error") and tool_result.get("task_id")):
                    created_tasks.append(dict(tool_result))

                self._push({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                })

        final_text = self._final_text or last_assistant_text
        if not final_text and created_tasks:
            # 仅调用工具（无文字回复）时给出可回报的回执，避免上游拿到空串。
            final_text = "已登记待接续事项：" + "、".join(
                str(t.get("title") or "未命名") for t in created_tasks
            )
        return {
            "status": "finished",
            "tools_called": list(self._tools_called),
            "created_tasks": created_tasks,
            "formatted_output": final_text,
        }

    def _build_user_message(self, title: str, context: str, next_occurrence: str,
                            directory: str) -> str:
        from datetime import datetime
        parts = [
            f"【本次事项】\n标题：{title}\n上下文：{context}\n下次唤醒时间：{next_occurrence}",
            f"【当前时间】{datetime.now().isoformat(timespec='seconds')}",
            f"【已有待接续事项】\n{directory or '（当前没有待接续事项）'}",
        ]
        parts.append("请判断本次事项是否与已有事项重复：不重复则调用 create_task 登记，重复则直接说明。")
        return "\n\n".join(parts)

    def _serialize_assistant_message(self, response_message) -> Dict[str, Any]:
        """把 LLM 返回的 assistant 消息序列化成可回灌 messages 的 dict。"""
        return {
            "role": "assistant",
            "content": response_message.content or "",
            "reasoning_content": extract_reasoning(response_message),
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
