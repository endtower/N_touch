# -*- coding: utf-8 -*-
"""Quest：接续事项的查重登记与任务笔记整理器。

职责：判断调用方给出的这条事项是否与已有待接续事项重复。不重复则登记
（create_task），同一事项有新时间或进展则更新原记录，无变化才直接说明已存在；调用方
明确要求时把某条事项标记为已完成、不再接续（complete_task）。同时把「任务相关笔记」槽位里
累积的进展并入对应事项（update_task 改写其上下文），无用的笔记丢弃；
本次调用结束后由运行时代码自动清空该槽位（不依赖 LLM 决策）。

每次调用使用独立工作上下文和独立日志文件；长期接续依靠数据库记录，不依靠
累积旧对话。一次调用内 LLM 可多轮推进（查重、合并 / 腾位置、登记、整理笔记），
由 LLM 自己调用 finish 工具收尾；未调用 finish 本次工作不算结束。
"""

import json
from typing import List, Dict, Any, Optional

from brain.base.light_agent.lightweight_agent import LightweightAgent
from brain.base.light_agent.light_history_manager import new_light_history_manager
from utils.logger import get_logger
from utils.llm_client_manager import extract_reasoning
from brain.hc.database.task import (
    TaskOperations,
    TASK_STATUS_ACTIVE,
    TASK_STATUS_EXPIRED,
)

logger = get_logger(__name__, "DEBUG")


class QuestAgent(LightweightAgent):
    """接续事项的查重登记与任务笔记整理 agent（多轮，finish 工具收尾）。"""

    # 待接续事项的容量上限：超出时必须先合并同类事项或提前完成旧事项。
    MAX_ACTIVE_TASKS = 5

    # 一次调用内允许的最大轮次；用尽仍未 finish 视为失控，直接崩溃阻断。
    MAX_ROUNDS = 8

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
                    f"最多 {cls.MAX_ACTIVE_TASKS} 条，已满时须先合并同类事项（update_task）"
                    "或提前完成旧事项（complete_task），否则登记会被拒绝。"
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
                                "须写到点无人回应也能推进的兜底动作，"
                                "禁止写「无人回应就什么都不做 / 到此为止」这类终止条件。"
                            ),
                        },
                        "next_occurrence": {
                            "type": "string",
                            "description": (
                                "下次唤醒时间：ISO 8601（如 2024-01-20T10:00:00），"
                                "或「尽快」表示现在执行。kind=onetime 时必填；"
                                "kind=ongoing 时省略（持续任务没有时间点）。"
                            ),
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["onetime", "ongoing"],
                            "description": (
                                "事项类型：onetime=到点执行一次的一次性事项（默认）；"
                                "ongoing=没有时间点的持续任务（如长期融入某个群、慢慢建立关系），"
                                "登记后由契机巡检长期挂起，不会被消耗掉。"
                            ),
                        },
                    },
                    "required": ["title", "context"],
                },
            },
        }

    @classmethod
    def get_update_task_tool_schema(cls) -> Dict[str, Any]:
        """返回 update_task 工具的 schema（记录写入级，私有于本类）。

        供已有事项改期或合并任务进展；只作用于 active 事项。
        """
        return {
            "type": "function",
            "function": {
                "name": "update_task",
                "description": (
                    "修改已有待接续事项的唤醒时间或合并上下文。"
                    "同一事项再次交付且指定了新时间时，修改原记录，不另建重复任务。"
                    "仅改期时只传 task_id 与 next_occurrence，原上下文完整保留；"
                    "有新进展或相关笔记时再传整合后的完整 context。仅用于 status=active 的事项。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "要改写的事项编号，取自【已有待接续事项】目录行括号内的编号。",
                        },
                        "context": {
                            "type": "string",
                            "description": (
                                "可选，改写后的完整上下文：把该事项原有的目的 / 已确认进展与"
                                "本次笔记整合成一份新的上下文，供下次启动时使用。"
                                "不要原样附加而要与原文合并重写；仅改期时省略，保留原文。"
                                "合并后仍须满足 create_task 的上下文要求（禁止零动作终止条件）。"
                            ),
                        },
                        "next_occurrence": {
                            "type": "string",
                            "description": (
                                "可选：新的下次唤醒时间，ISO 8601（如 2024-01-20T10:00:00）"
                                "或「尽快」表示现在执行；不需要调整时留空。"
                            ),
                        },
                    },
                    "required": ["task_id"],
                },
            },
        }

    @classmethod
    def get_complete_task_tool_schema(cls) -> Dict[str, Any]:
        """返回 complete_task 工具的 schema（记录写入级，私有于本类）。

        供调用方在事项已实际办妥后，把它标记为完成、不再接续。
        """
        return {
            "type": "function",
            "function": {
                "name": "complete_task",
                "description": (
                    "把一条已有待接续事项标记为已完成：事项已办妥、无须再唤醒时调用。"
                    "编号取自【已有待接续事项】目录行括号内。标记后该事项不再接续；"
                    "若仍有需要跟进的后续，另行 create_task 登记新安排。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "要标记完成的事项编号，取自目录行括号内的编号。",
                        },
                    },
                    "required": ["task_id"],
                },
            },
        }

    # ===================== 工具表拼装 =====================
    @classmethod
    def get_finish_tool_schema(cls) -> Dict[str, Any]:
        """返回 finish 工具的 schema（会话结束工具，私有于本类）。

        本次工作全部办完（该登记的登记、该改写的改写、该标记的标记）后调用，
        调用即结束本次调用；不调用则视为尚未收尾。
        """
        return {
            "type": "function",
            "function": {
                "name": "finish",
                "description": (
                    "结束本次工作。所有登记 / 改写 / 标记完成都办完后必须调用本工具收尾，"
                    "调用后不再发起其它工具调用。若还有未完成的操作，先做完再调用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": (
                                "可选：本次处理的简短结论，说明登记 / 改写了什么、"
                                "下次唤醒时间，或为何无需变动。"
                            ),
                        },
                    },
                    "required": [],
                },
            },
        }

    def _initialize_tool_functions(self) -> Dict[str, Any]:
        return {
            "create_task": QuestAgent._create_task_impl,
            "update_task": QuestAgent._update_task_impl,
            "complete_task": QuestAgent._complete_task_impl,
            "finish": QuestAgent._finish_impl,
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
        return [
            QuestAgent.get_create_task_tool_schema(),
            QuestAgent.get_update_task_tool_schema(),
            QuestAgent.get_complete_task_tool_schema(),
            QuestAgent.get_finish_tool_schema(),
        ]

    def get_system_prompt(self) -> str:
        return """
【职责】
你负责三件事，可以分多轮推进：
一、判断调用方给出的这条事项是否与已有待接续事项重复。
- 不重复：调用 create_task 登记。
- 同一事项有新时间或新进展：调用 update_task 改写原记录（原有关键线索与会话标识应保留）。
- 同一事项时间和上下文都无变化：直接说明已有安排，不重复登记。
- 调用方明确要求标记完成时（会点明编号）：调用 complete_task；仍有后续则另行 create_task。
二、整理下方注入的【任务相关笔记】。
- 描述某条已有事项的进展 / 线索 / 变化：调用 update_task，把上下文重写为「原文 + 该笔记」的整合结果。
- 与任何事项无关、或只是无用碎片的笔记：不处理，直接丢弃。
三、登记前先查重、先腾位置；容量与字段要求见各工具说明。

不做阶段拆分、不评价是否值得立项。只依据调用方给的事项与下方注入的【已有待接续事项】判断：
标题相近不等于重复，要看上下文是否指向同一件待办；无法确定时简短说明判断依据。
不要把「无变化 / 暂时不需要处理」误当成完成：只有事项确已办妥才标记完成。

【输出】
落库成功后再说明处理了什么及下次唤醒时间；未成功落库不得声称已登记或已完成。
工具报错时按其提示自行纠正后重试。
全部办完后必须调用 finish 收尾，不调用则本次调用不算结束。
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

    # ===================== 五段式骨架（run 自实现多轮循环，其余给默认实现） =====================
    def _preprocess(self, query: str) -> Any:
        return query

    def _should_continue(self, response_message, results: List[Dict[str, Any]]) -> bool:
        return bool(response_message.tool_calls)

    def _postprocess(self, content: str, extra_info: Any) -> Dict[str, Any]:
        return {"content": content, "extra_info": extra_info}

    # ===================== 工具实现 =====================
    # 零动作终止条件：显式零动作词直接命中；含糊词仅在同时提到「等回应」时命中。
    _ZERO_ACTION_MARKERS = ("什么都不做", "啥也不干", "什么也不做", "不做任何事", "不再行动")
    _AMBIGUOUS_STOP_MARKERS = ("到此为止", "就此结束", "就此作罢", "保持沉默", "就算了")
    _NO_REPLY_MARKERS = (
        "没回应", "没有回应", "不回应", "无回应", "无人回应", "没人回应",
        "没回复", "没有回复", "未回复", "不回复", "等不到", "对方不回",
    )

    @classmethod
    def _reject_empty_action_context(cls, context: str) -> None:
        """上下文含「无人回应就零动作」的终止条件时驳回（抛 ValueError 回给 LLM 自纠）。"""
        if not isinstance(context, str):
            return
        hit = next((m for m in cls._ZERO_ACTION_MARKERS if m in context), None)
        if hit is None and any(m in context for m in cls._NO_REPLY_MARKERS):
            hit = next((m for m in cls._AMBIGUOUS_STOP_MARKERS if m in context), None)
        if hit is not None:
            raise ValueError(
                f"上下文含零动作终止条件「{hit}」。到点无人回应不等于无事可做："
                "请改写为可执行的兜底推进（提醒相关的人、在相关群聊露面说句话或发起话题、"
                "换个对象或渠道去问），再登记。"
            )

    @staticmethod
    def _resolve_next_occurrence(next_occurrence: Optional[str]) -> str:
        """解析 next_occurrence 的「尽快」快捷语义。

        「尽快」＝现在就执行：把时间置为当前时间——task_truck 下一轮周期检测
        即视为已到期并触发 PFCA。ISO 8601 时间原样规整返回。
        """
        if not next_occurrence:
            return ""
        from datetime import datetime
        if not isinstance(next_occurrence, str):
            raise ValueError("唤醒时间必须是 ISO 8601 文本或「尽快」。")
        next_occurrence = next_occurrence.strip()
        if next_occurrence == "尽快":
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
        kind: str = "onetime",
    ) -> Dict[str, Any]:
        """登记一条新的待接续事项。

        kind=onetime 必须给出唤醒时间；kind=ongoing 为没有时间点的持续任务。

        Returns:
            {"task_id": str, "title": str, "context": str,
             "kind": str, "next_occurrence": str}
        """
        from brain.hc.database.task import TASK_KIND_ONETIME, TASK_KIND_ONGOING

        kind = (kind or TASK_KIND_ONETIME).strip()
        if kind not in (TASK_KIND_ONETIME, TASK_KIND_ONGOING):
            raise ValueError("kind 只能是 onetime 或 ongoing。")
        next_occurrence = QuestAgent._resolve_next_occurrence(next_occurrence)
        if kind == TASK_KIND_ONETIME and not next_occurrence:
            raise ValueError(
                "登记一次性事项必须提供下次唤醒时间；缺少时间依据时请先向调用方澄清，"
                "或改登记为 kind=ongoing 的持续任务。"
            )
        if kind == TASK_KIND_ONGOING:
            next_occurrence = ""
        QuestAgent._reject_empty_action_context(context)

        task_ops = TaskOperations()
        active_count = await task_ops.count_active_tasks()
        if active_count >= QuestAgent.MAX_ACTIVE_TASKS:
            raise ValueError(
                f"待接续事项已达上限 {QuestAgent.MAX_ACTIVE_TASKS} 条，不能再登记新事项。"
                "请先用 update_task 把这条事项并入指向同一件事的已有条目，"
                "或用 complete_task 提前结束已办妥 / 无须继续等待的旧事项，腾出位置后再登记。"
            )

        logger.info(
            f"创建任务: title={title}, kind={kind}, next_occurrence={next_occurrence}"
        )

        task_id = await task_ops.create_task(
            title=title,
            context=context,
            next_occurrence=next_occurrence,
            kind=kind,
        )

        logger.info(f"任务创建成功: task_id={task_id}")

        return {
            "task_id": task_id,
            "title": title,
            "context": context,
            "kind": kind,
            "next_occurrence": next_occurrence,
        }

    @staticmethod
    async def _update_task_impl(
        task_id: str,
        context: Optional[str] = None,
        next_occurrence: Optional[str] = None,
    ) -> Dict[str, Any]:
        """为 active 事项改期或合并上下文；省略字段保留原值。

        Returns:
            {"task_id": str, "title": str, "context": str, "next_occurrence": str}
        """
        cleaned_context = context.strip() if isinstance(context, str) else None
        if context is not None and not cleaned_context:
            raise ValueError("改写事项必须给出整合后的上下文；无内容可写时不要调用本工具。")
        if cleaned_context is not None:
            QuestAgent._reject_empty_action_context(cleaned_context)
        resolved_when = QuestAgent._resolve_next_occurrence(next_occurrence) if next_occurrence else ""
        if cleaned_context is None and not resolved_when:
            raise ValueError("修改事项至少需要新的唤醒时间或整合后的上下文。")

        task_ops = TaskOperations()
        updated = await task_ops.update_task(
            task_id,
            context=cleaned_context,
            next_occurrence=resolved_when or None,
        )
        if not updated:
            raise ValueError(f"未找到可改写的进行中事项（task_id={task_id}），请核对目录中的编号。")

        record = await task_ops.get_task_by_id(task_id)
        logger.info(f"任务改写成功: task_id={task_id}, title={record['title']}")
        return {
            "task_id": task_id,
            "title": record["title"],
            "context": record["context"],
            "kind": record.get("kind") or "onetime",
            "next_occurrence": record["next_occurrence"],
        }

    @staticmethod
    async def _complete_task_impl(task_id: str) -> Dict[str, Any]:
        """把一条进行中事项标记为已完成（status→expired，不再接续）。

        任务一次性语义下「完成」即置终态，直接复用 expire_task。
        编号不存在 / 已结束时抛 ValueError 回给 LLM 自纠。

        Returns:
            {"task_id": str, "title": str, "status": "expired"}
        """
        task_ops = TaskOperations()
        record = await task_ops.get_task_by_id(task_id)
        if not record:
            raise ValueError(f"未找到编号为「{task_id}」的事项，请核对目录中的编号。")
        if record["status"] != TASK_STATUS_ACTIVE:
            raise ValueError(f"事项「{task_id}」已结束，无须再次标记完成。")

        done = await task_ops.expire_task(task_id)
        if not done:
            raise ValueError(f"标记事项「{task_id}」完成失败（状态可能已变化），请核对后重试。")

        logger.info(f"任务标记完成: task_id={task_id}, title={record['title']}")
        return {
            "task_id": task_id,
            "title": record["title"],
            "status": TASK_STATUS_EXPIRED,
        }

    @staticmethod
    async def _finish_impl(summary: str = "") -> Dict[str, Any]:
        """结束本次工作；返回值仅作为循环终止信号，不写任何记录。

        Returns:
            {"status": "finished", "summary": str}
        """
        return {"status": "finished", "summary": (summary or "").strip()}

    # ===================== 多轮入口 =====================
    async def run(self, title: str = "", context: str = "", next_occurrence: str = "",
                  kind: str = "onetime", task_id: str = "",
                  action: str = "register") -> Dict[str, Any]:
        """独立处理一次请求；多轮推进，由 LLM 调用 finish 收尾。

        action=register（默认）：登记 / 改写一次事项。
        action=complete：把 task_id 指定的事项标记为已完成、不再接续。
        参数错误（缺时间等）回给 LLM 自纠；其余异常上抛崩溃阻断。
        """
        self.reset()
        self._history = new_light_history_manager("quest")
        system_prompt = self.get_system_prompt()
        self._history.append({"role": "system", "content": system_prompt})

        from brain.hc.shortlight.notepad import get_shortlight_notepad, SLOT_TASK
        notepad = get_shortlight_notepad()
        task_notes = await notepad.get_slot_notes(SLOT_TASK)

        # Quest 合并原文需要完整上下文和准确时间；PFCA 的轻量目录仍用默认摘要。
        directory = await TaskOperations().format_active_task_overview(limit=20, full_context=True)
        self._push({
            "role": "user",
            "content": self._build_user_message(
                title, context, next_occurrence, directory,
                self._format_task_notes(task_notes), kind, task_id, action,
            ),
        })

        created_tasks: List[Dict[str, Any]] = []
        updated_tasks: List[Dict[str, Any]] = []
        completed_tasks: List[Dict[str, Any]] = []
        finished = False
        last_assistant_text = ""

        for _ in range(self.MAX_ROUNDS):
            response = await self._call_llm(
                self._messages, level='flash', system_prompt=system_prompt,
            )
            response_message = response.choices[0].message
            if response_message.content:
                last_assistant_text = response_message.content

            if not response_message.tool_calls:
                # 未调工具即未收尾：记下文字继续，要求其按约定调用 finish。
                self._push({"role": "assistant", "content": response_message.content or ""})
                self._push({
                    "role": "user",
                    "content": "请继续：还有操作未完成就直接执行；若已全部办完，调用 finish 收尾。",
                })
                continue

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
                written = isinstance(tool_result, dict) and not tool_result.get("error") \
                    and tool_result.get("task_id")
                if written and function_name == "create_task":
                    created_tasks.append(dict(tool_result))
                elif written and function_name == "update_task":
                    updated_tasks.append(dict(tool_result))
                elif written and function_name == "complete_task":
                    completed_tasks.append(dict(tool_result))
                elif function_name == "finish" and isinstance(tool_result, dict) \
                        and tool_result.get("status") == "finished":
                    finished = True
                    if tool_result.get("summary"):
                        self._final_text = tool_result["summary"]

                self._push({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                })

            if finished:
                break

        if not finished:
            # 多轮用尽仍未收尾：不静默放过，直接崩溃阻断。
            raise RuntimeError(
                f"QuestAgent 连续 {self.MAX_ROUNDS} 轮未调用 finish 收尾，"
                "无法确认本次登记工作已完成。"
            )

        final_text = self._final_text or last_assistant_text
        if created_tasks or updated_tasks or completed_tasks:
            # 工具调用后的回执以实际落库结果为准，改期也必须回报新的准确时间。
            receipts = []
            for action_label, records in (
                ("已完成", completed_tasks),
                ("已登记", created_tasks),
                ("已更新", updated_tasks),
            ):
                for t in records:
                    if action_label == "已完成":
                        receipts.append(f"已标记完成：{t['title']}（{t['task_id']}）；该事项不再接续")
                    elif t.get("kind") == "ongoing":
                        receipts.append(f"{action_label}持续任务：{t['title']}（{t['task_id']}）；长期挂起，无固定唤醒时间")
                    else:
                        receipts.append(f"{action_label}待接续事项：{t['title']}（{t['task_id']}）；下次唤醒：{t['next_occurrence']}")
            final_text = "\n".join(receipts)
        errors = [str(call["result"]["error"]) for call in self._tools_called
                  if isinstance(call.get("result"), dict) and call["result"].get("error")]
        if errors:
            final_text = (final_text + "\n" if created_tasks or updated_tasks or completed_tasks else "") \
                + "未成功的操作：" + "；".join(errors)

        # 处理完毕自动清空任务笔记槽位：未并入任何事项的笔记即视为丢弃。
        # 清空失败（IO / 状态异常）直接上抛崩溃阻断。
        dropped = await notepad.clear_slot(SLOT_TASK)

        return {
            "status": "finished",
            "tools_called": list(self._tools_called),
            "created_tasks": created_tasks,
            "updated_tasks": updated_tasks,
            "completed_tasks": completed_tasks,
            "notes_dropped": dropped,
            "formatted_output": final_text,
        }

    @staticmethod
    def _format_task_notes(notes: List[Dict[str, Any]]) -> str:
        """把任务槽位笔记渲染为注入段文本；无笔记返回空串（调用方据此给「无」）。"""
        lines = []
        for note in notes:
            text = (note.get("text") or "").strip()
            if not text:
                continue
            when = (note.get("record_time") or "")[:16].replace("T", " ")
            session = note.get("session_name") or note.get("session_id") or ""
            suffix = "，".join(part for part in (when, session) if part)
            lines.append(f"- {text}" + (f"（{suffix}）" if suffix else ""))
        return "\n".join(lines)

    def _build_user_message(self, title: str, context: str, next_occurrence: str,
                            directory: str, task_notes: str,
                            kind: str = "onetime", task_id: str = "",
                            action: str = "register") -> str:
        from datetime import datetime
        if action == "complete":
            parts = [
                f"【本次请求】\n动作：标记事项已完成\n任务编号：{task_id}",
                f"【当前时间】{datetime.now().isoformat(timespec='seconds')}",
                f"【已有待接续事项】\n{directory or '（当前没有待接续事项）'}",
                f"【任务相关笔记】\n{task_notes or '（当前没有任务相关笔记）'}",
            ]
            return "\n\n".join(parts)
        parts = [
            f"【本次事项】\n标题：{title}\n类型：{kind}\n上下文：{context}\n下次唤醒时间：{next_occurrence}",
            f"【当前时间】{datetime.now().isoformat(timespec='seconds')}",
            f"【已有待接续事项】\n{directory or '（当前没有待接续事项）'}",
            f"【任务相关笔记】\n{task_notes or '（当前没有任务相关笔记）'}",
        ]
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
