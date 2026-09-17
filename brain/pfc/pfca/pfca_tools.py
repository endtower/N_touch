# -*- coding: utf-8 -*-

"""
pfca模块的工具定义与处理

专门负责：
1. 工具定义（function call模式所需的工具描述）
2. 工具功能的具体实现
3. 工具调用的路由和分发
配置由上层pre/core.py统一管理，被pfca_core.py调用。

会话标识统一使用 session_id，格式为 "type_id"，例如 "private_1234567890" / "group_198431299"。
LLM 工具入参也统一为 session_id，不再单独传 session_type。
"""

import json
import time
import asyncio
from functools import partial
from typing import Dict, Any, List
from brain.amy.constants import FinalEvaluationAct
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from brain.base import ToolManager, ToolHC, ToolSocial
from brain.hc.global_session_state import get_global_session_state
from brain.pfc.pfca.pfca_input_manager import (
    get_pfca_input_manager,
    FirstMessageType,
)


# 获取日志记录器
logger = get_logger('PFCATools', "INFO")


class PFCA_tool_manager(ToolManager):
    """PFCA工具管理器"""

    @staticmethod
    def get_tools() -> List[Dict[str, Any]]:
        """
        获取可用的工具列表，用于LLM的function call

        Returns:
            List[Dict[str, Any]]: 工具列表，每个工具包含名称、描述和参数信息
        """
        logger.debug("获取可用工具列表")
        return [
            {
                "type": "function",
                "function": {
                    "name": "conversation_agent",
                    "description": "启动conversation_agent，开启一段对话（约 5-15 句）。可指定目标会话 id；不指定则默认为主人的私聊会话。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "session_id": {
                                "type": "string",
                                "description": "目标会话 id，格式 'type_id'，例如 'private_xxx' / 'group_yyy'。"
                            },
                            "purpose": {
                                "type": "string",
                                "description": "本段话题的目标 / 期望结局，给出具体策略而非话术。禁止描述自己当前的情绪。"
                            },
                            "extra_info": {
                                "type": "string",
                                "description": "可选，本任务的额外补充信息（如其他会话消息原文、agent 结论 / 计划书等）。"
                            },
                            "max_messages": {
                                "type": "integer",
                                "description": "本段话题的发送轮次软上限（一次发送工具调用计 1，即使拆成多条消息），达到后回到 PFCA 重新拟定策略。首次建议 0-5。"
                            }
                        },
                        "required": ["session_id", "purpose", "max_messages"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "social_chat_agent",
                    "description": ("启动 social_chat_agent，在主人的监管/协作/支持下，与目标对话进行沟通，适用于谨慎环境的社交任务。"),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "session_id": {
                                "type": "string",
                                "description": "工作会话 id，格式 'type_id'（如 'group_xxx' / 'private_yyy'）。不能是主人的私聊会话（主人私聊用 conversation_agent）。"
                            },
                            "purpose": {
                                "type": "string",
                                "description": "社交任务书：目标 / 现状 / 期望结局 / 关键约束。给出具体策略而非话术；禁止描述自己的情绪。"
                            },
                            "extra_info": {
                                "type": "string",
                                "description": "可选，任务补充材料：相关消息原文、人物印象要点、前因后果。严禁含糊指代（要求告知某事结果必须附结果内容）。"
                            }
                        },
                        "required": ["session_id", "purpose"]
                    }
                }
            },
            ToolSocial.get_query_history_tool_schema(),
            {
                "type": "function",
                "function": {
                    "name": "wait_and_observe",
                    "description": (
                        "在指定目标会话等待对方的回复。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "session_id": {
                                "type": "string",
                                "description": "目标会话 id，格式 'type_id'，例如 'private_xxx' / 'group_yyy'。"
                            },
                            "seconds": {
                                "type": "number",
                                "description": "最长等待秒数",
                                "minimum": 10.0,
                                "maximum": 30.0
                            }
                        },
                        "required": ["session_id", "seconds"]
                    }
                }
            },
            ToolSocial.get_query_address_book_tool_schema(),
            {
                "type": "function",
                "function": {
                    "name": "advanced_decision_agent",
                    "description": "调度高级决策 agent 获取严谨结论。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "thinking_mode": {
                                "type": "string",
                                "enum": ["reorganize", "opinion"],
                                "description": "reorganize=受挫时方向性决议；opinion=理性观点。"
                            },
                            "current_summary": {
                                "type": "string",
                                "description": "当前现状总结：前因后果、状态、问题、已尝试方法。"
                            }
                        },
                        "required": ["thinking_mode"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "terminate",
                    "description": "结束当前 PFCA 会话，等待下一事件再次唤醒。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {
                                "type": "string",
                                "description": (
                                    "本次会话的总结：完整前因后果、自我感受与关键细节，需包含具体人物名称。"
                                    "该总结会写入短期记忆，并随记事本一同落库存档"
                                    "——事件经过无需再用 record_to_notepad 记录。"
                                )
                            },
                            "title": {
                                "type": "string",
                                "maxLength": 15,
                                "description": "本次事件的简易标题（不超过 15 字），最终评价想法将绑定该标题。"
                            },
                            "final_evaluation": {
                                "type": "string",
                                "enum": FinalEvaluationAct.names(),
                                "description": "本次 PFCA 会话的最终评价，仅第二次调用（确认退出时）生效；不填按无评价"
                            }
                        },
                        "required": ["summary", "title"]
                    }
                }
            },
            ToolHC.get_list_notes_tool_schema(),
            ToolHC.get_query_note_tool_schema(),
            ToolHC.get_pfca_record_note_tool_schema(),
        ]

    def __init__(self):
        """初始化工具管理器"""
        logger.debug("初始化PFCA_tool_manager工具管理器")
        # 初始化工具映射表，用于动态路由
        self.tool_functions = {
            "conversation_agent": self.conversation_agent,
            "social_chat_agent": self.social_chat_agent,
            # consumer_id 显式传 "pfca"：LLM 已读到的消息推进 pfca 游标，truck 不再重复触发
            "query_history": partial(ToolSocial.query_history, consumer_id="pfca"),
            "wait_and_observe": self.wait_and_observe,
            "terminate": self.terminate,
            "query_address_book": ToolSocial.query_address_book,
            "advanced_decision_agent": self.deep_thinking,
            "recall_memory": self.recall_memory,
            "query_note": self.query_note,
            "list_notes": self.list_notes,
            "record_to_notepad": self.record_to_notepad
        }

    @async_handle_errors()
    async def handle_tool_call(self, tool_call) -> Dict[str, Any]:
        """
        处理LLM的工具调用请求
        作为工具调用的中央路由，负责将工具调用分发到对应的处理函数

        错误处理策略：
        - AbyssCoreError 视为"不可恢复"（程序/系统级故障）→ 仍走 os._exit 强制退出。
        - 参数错误（参数解析失败 / 工具名拼错 / 工具内部参数错误）按"可恢复错误"处理：
          构造一条 role=tool 的错误消息返回给 LLM，让其自纠。
        - 其余异常（模块缺失 / IO 错等）→ 上抛不吞，直接崩溃阻断，防止错误扩大蔓延。

        Args:
            tool_call: 工具调用请求信息

        Returns:
            Dict[str, Any]: 工具调用的结果；参数错误时返回 role=tool 的错误消息。
        """
        # —— 1. 解析 tool_call 形态 ——
        parsed = self._parse_tool_call(tool_call)
        if parsed is None:
            return self._tool_error_response(
                tool_call_id=getattr(tool_call, 'id', None) if hasattr(tool_call, 'id') else (tool_call.get('id') if isinstance(tool_call, dict) else None),
                tool_name="<unknown>",
                err=ValueError(f"无法解析工具调用格式: {tool_call}"),
            )
        tool_name = parsed["name"]
        tool_args_json = parsed["arguments"]
        tool_call_id = parsed["id"]

        # —— 2. 参数解析容错（LLM 偶尔吐 Python dict 字面量而非合法 JSON）——
        try:
            tool_args = self._parse_arguments(tool_args_json)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"工具 {tool_name} 参数解析失败: {e}; raw={tool_args_json!r}")
            return self._tool_error_response(tool_call_id, tool_name, e)

        # —— 3. 工具名查表 ——
        if tool_name not in self.tool_functions:
            err = ValueError(f"未知工具: {tool_name}")
            logger.error(str(err))
            return self._tool_error_response(tool_call_id, tool_name, err)

        tool_function = self.tool_functions[tool_name]

        # —— 4. 前端同步: 每次工具调用都把动作广播给 web 通道 ——
        # (后端维护单一 activity 状态, 前端只渲染)
        get_global_session_state().set_activity(
            source="pfca", tool_name=tool_name, tool_args=tool_args
        )

        # —— 5. 执行工具函数；统一错误处理（参数错误回 LLM，其余异常上抛阻断）——
        result = await self._safe_execute(
            tool_function, tool_call_id, tool_name, **tool_args
        )
        if isinstance(result, dict) and result.get("role") == "tool":
            # 出错时 _safe_execute 已返回 role=tool 的错误消息，直接透传
            return result

        content = json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)
        archived = isinstance(result, dict) and bool(result.get("archived"))

        response = {
            "role": "tool",
            "name": tool_name,
            "content": content,
            "tool_call_id": tool_call_id,
        }
        if archived:
            response["archived"] = True

        return response

    async def conversation_agent(self, purpose: str, max_messages: int = 0, extra_info: str = "", session_id: str = "") -> Any:
        """
        直接开启一段对话（约 5-15 句的短对话）。
        调用方可通过 session_id 指定目标会话；不指定则默认为主人的私聊会话（private_<MASTER_ID>）。

        涉及附件时，PFCA 把附件清单（[图片:xxx] / [文件:xxx] 等）写进 purpose 或 extra_info 里。

        Args:
            purpose: 本次聊天的目的与期望结局，具体策略与计划。
            extra_info: 额外补充信息（其他会话消息原文、agent 结论 / 计划书等）。
            max_messages: 软限制发送轮次数（一次发送工具调用计 1）
            session_id: 可选，目标会话 id，格式 'type_id'。为空时默认主人的私聊会话。

        Returns:
            Dict[str, Any]: 遵循统一格式的操作结果
        """
        from config.role_settings import RoleConfig
        from brain.hc.global_session_state import get_global_session_state
        from brain.chat.conversation.conversation_core import get_conversation_core

        session_id = (session_id or "").strip() or f"private_{RoleConfig.MASTER_ID}"
        focus_state = get_global_session_state()

        # 注册会话（如未注册）
        pfca_sessions = focus_state.get_sessions()
        if not any(s.get("session_id") == session_id for s in pfca_sessions):
            await focus_state.add_pfca_session(session_id, search_impression=True)

        # 拼装 purpose 模板（仅当 extra_info 非空时套外壳）
        if extra_info:
            combined_purpose = (
                f"本次任务由pfca(核心任务调度器，能力：查询可联系目标和其聊天记录、"
                f"发布对主人的社交任务、发布对其他人的社交任务、修订对角色的印象，构造长期任务)发布\n"
                f"任务目标：{purpose}\n"
                f"任务格外信息：{extra_info}"
            )
        else:
            combined_purpose = purpose

        conversation_core = get_conversation_core()
        logger.debug(f"启动对话: session_id={session_id}, max_messages={max_messages}")
        chat_result = await conversation_core.start_session(
            session_id=session_id,
            purpose=combined_purpose,
            max_messages=max_messages,
        )
        logger.info(f"conversation模块返回结果(session_id={session_id}): {chat_result}")
        return chat_result

    async def social_chat_agent(self, session_id: str, purpose: str, extra_info: str = "") -> Any:
        """
        启动 social_chat_agent，对其他人（群聊 / 非主人私聊）推进一个社交任务。

        purpose / extra_info 拼装为任务书直传 SocialChatCore.start()；
        会话注册与汇报窗口锚点由 core 自治，工具层不做重复注册；
        对外发言的监管（发送模式 / 主人审批）由 social_chat 内部处理。

        Args:
            session_id: 工作会话 id，格式 "type_id"（如 "group_xxx" / "private_yyy"）。
                不能是主人的私聊会话（主人私聊请用 conversation_agent）。
            purpose: 社交任务书：目标 / 现状 / 期望结局 / 关键约束。
            extra_info: 可选补充材料（相关消息原文、人物印象要点、前因后果）。

        Returns:
            Dict[str, Any]: social_chat 返回结果 {summary_text, messages_sent, exit_reason}
        """
        if not session_id or not session_id.strip():
            raise ValueError("session_id 必填")
        if not purpose or not purpose.strip():
            raise ValueError("purpose 必填")

        from config.role_settings import RoleConfig
        master_private = f"private_{RoleConfig.MASTER_ID}"
        if session_id.strip() == master_private:
            raise ValueError(
                f"social_chat_agent 不能用于主人的私聊会话 ({master_private})；"
                f"请改用 conversation_agent。"
            )

        if extra_info and extra_info.strip():
            combined_purpose = (
                f"本次任务由 pfca 发布（对其他人 / 群聊的社交任务）\n"
                f"任务目标：{purpose.strip()}\n"
                f"任务格外信息：{extra_info.strip()}"
            )
        else:
            combined_purpose = purpose.strip()

        from brain.chat.social_chat.social_chat_core import SocialChatCore
        result = await SocialChatCore().start(
            purpose=combined_purpose,
            working_session_id=session_id.strip(),
        )
        logger.info(f"social_chat_agent 返回结果: {result}")
        return result

    async def wait_and_observe(self, session_id: str, seconds: float) -> str:
        """在目标会话等待对方的回复（走 PFCA 游标，"pfca" 消费者）。

        游标语义（与 query_history / truck 未读检测共用同一游标）：
        1. 获取 PFCA 游标进度，作为本轮等待的已读基线；
        2. 每秒轮询目标会话未读用户消息（seq > 游标且 user_id != 'system'，
           即只认对方的回复，自己发出的消息不算回复）；
        3. 检测到回复：fetch_unread 拉取消息内容并推进 PFCA 游标到最新 seq
           （消费）——之后 truck 未读检测不再重复触发本会话；
        4. 超时无回复：游标不动（无消息可消费）。

        Args:
            session_id: 目标会话 id，格式 "type_id"，如 "private_xxx" / "group_xxx"。
            seconds: 最长等待秒数（10-30，越界收敛到边界）。

        Returns:
            str: 收到的回复内容，或超时提示。
        """
        import datetime
        session_id = (session_id or "").strip()
        if not session_id:
            raise ValueError("wait_and_observe 缺少必填参数 session_id")
        seconds = min(max(float(seconds), 10.0), 30.0)

        from memory.message_repository import get_message_store
        from memory.session_history import (
            get_global_session_history,
            FetchOptions,
            MessageFormatOptions,
        )

        store = get_message_store()
        history = get_global_session_history()

        # 1. 获取 PFCA 游标进度，作为等待基线
        baseline = await store.get_cursor(session_id, "pfca")
        logger.info(
            f"wait_and_observe 开始等待: session_id={session_id}, "
            f"seconds={seconds}, pfca游标基线={baseline}"
        )

        start_time = time.time()
        elapsed = 0.0
        while elapsed < seconds:
            wait_time = min(1.0, seconds - elapsed)
            await asyncio.sleep(wait_time)
            elapsed = time.time() - start_time

            # 2. 只读探测未读用户消息（对方回复；system 消息是自己发言，不算回复）
            if await store.count_unread_user_for(session_id, "pfca") <= 0:
                continue

            # 3. 消费：拉取未读（仅未读，不补历史）并推进 PFCA 游标到最新 seq
            result = await history.fetch_unread(
                session_id,
                "",
                consumer_id="pfca",
                options=FetchOptions(
                    message_format=MessageFormatOptions(
                        exclude_system=True,
                        show_timestamp=True,
                    )
                ),
                advance_cursor=True,
                max_unread=20,
                min_messages=0,
            )
            if not result.has_new:
                # 计数与拉取之间游标已被其他路径推进（竞态），继续等待
                continue

            new_cursor = await store.get_cursor(session_id, "pfca")
            current_time = datetime.datetime.now().strftime("%H:%M:%S")
            content = "\n".join(result.formatted_lines) if result.formatted_lines else ""
            logger.info(
                f"wait_and_observe 收到回复，推进 pfca 游标 "
                f"{baseline} -> {new_cursor} ({session_id})"
            )
            return f"等待 {int(elapsed)} 秒后收到回复，当前时间：{current_time}\n{content}"

        current_time = datetime.datetime.now().strftime("%H:%M:%S")
        logger.info(
            f"wait_and_observe 超时: session_id={session_id}, pfca游标保持 {baseline} 不变"
        )
        return f"等待观察完成，未收到对方回复，总共等待 {int(seconds)} 秒，当前时间：{current_time}"

    async def query_note(self, title: str) -> str:
        """查询记事本某一章节（按标题精确匹配，重名取最新）的完整细节，
        委托通用 ToolHC.query_note（起因/总结/自我评价/逐条记录及元数据）。"""
        return await ToolHC.query_note(title)

    async def list_notes(self) -> str:
        """列出记事本全部已落库笔记概览，委托通用 ToolHC.list_notes
        （标题/触发原因摘要/落库时间/记录条数；为 query_note 提供标题来源）。"""
        return await ToolHC.list_notes()

    async def record_to_notepad(self, text: str, usage: str) -> str:
        """记事本（灵光一现）写入——PFCA 策略视角：采集观察到的对方素材，不做经验提炼。
        仅收纯文本与语境标注；来源标注 source="pfca"，溯源走事件链，
        记录进当前事件链的缓存文档，terminate 落库时统一以事件标题命名。"""
        note_id = await ToolHC.record_note(
            text=text,
            source="pfca",
            session_id="",
            session_name="",
            usage=usage,
        )
        return f"已记录至记事本（{note_id}）。"

    async def terminate(self, summary: str, title: str = "", check: bool = False, final_evaluation: str = "") -> str:
        """
        结束当前全部的任务链。

        第一次调用：将 summary/title/final_evaluation 暂存进 GlobalSessionState（不落库），
        返回行动记录和确认提示。
        第二次调用（最后一次）：以本次参数把 summary 写入短期记忆、随记事本落库并登记
        最终评价（SC 轴闭环 + 想法入池）——保证两次调用只有最后一次计入 temporary memory。
        若会话未经第二次调用结束（max_rounds 熔断 / 强停等），由 pfca_core.start 的
        退出兜底把暂存补写入短期记忆（防总结丢失，docs/已完成.md #9）。

        Args:
            summary: 本次会话的总结（完整前因后果、自我感受与关键细节）
            title: 本次事件的简易标题（≤15 字），最终评价想法绑定该标题
            check: 已废弃，不再使用
            final_evaluation: 本次 PFCA 会话的最终评价（词表名，来自 FinalEvaluationAct）；
                仅第二次调用（确认退出）时生效；留空按「未得到他人评价与反馈」登记

        Returns:
            str: 操作结果，包含行动记录和确认提示
        """
        logger.debug(f"执行terminate操作，总结: {summary}")

        from brain.hc.global_session_state import get_global_session_state
        focus_state = get_global_session_state()

        terminate_confirmed = focus_state._data.get("terminate_confirmed", False)

        pfca_actions = focus_state._data.get("pfca_actions", [])
        actions_text = "\n".join([f"{i+1}. {action}" for i, action in enumerate(pfca_actions)]) if pfca_actions else "无行动记录"

        if not terminate_confirmed:
            focus_state._data["terminate_confirmed"] = True
            logger.info("terminate 第一次调用，暂存总结并显示确认提示")

            # 只暂存、不落库（docs/已完成.md #9 修复）：两次调用语义下只有"最后一次"
            # 计入 temporary memory——第二次调用（确认退出）才写入；若会话经
            # max_rounds 熔断/强停等路径结束、永远没有第二次调用，则由
            # pfca_core.start 的退出兜底把暂存补写入临时记忆。
            focus_state._data["pending_terminate"] = {
                "summary": summary,
                "title": title,
                "final_evaluation": final_evaluation,
            }

            return f"""你的行动记录：
{actions_text}
系统提醒，确认是否退出：
- 查看本次对话或事件是否未完待续？包含情绪上的愤怒气不过，记恨，引发的持续关注或伺机待发，或是事件上更新与记录，需要通过工具来更新
- 如果是重要的理性任务，看看有无跟关系好或主要相关的人道别或提醒对方自己的离开
- 如果是其他类型的情况，是否达成了情绪上的闭环？如临走时骂回去等等
- 是否需要调整记忆？记忆将会影响下次你对目标群聊与个人的第一印象，并更好的辅助与理解对方。
如果确认退出，重新调用该工具。"""

        focus_state._data["terminate_confirmed"] = False
        logger.info("terminate 第二次调用，执行真正的退出逻辑")

        # 第二次调用（最后一次）才把「summary」写入短期记忆：保证两次调用只有最后一次
        # 计入，首次调用的暂存在此弹出作废（若 LLM 更新了总结，以本次为准）。
        # 不再混入本地拼接的 actions_text 等字段，保证短期记忆内容仅来源于工具属性。
        # 不传 identifier：让每一轮的"事件概要"都作为独立条目保留下来，
        # 仅受 MAX_MESSAGES 自然滚动约束；不要让「pfca_terminate_summary」这种常量
        # 标识符把历史概要逐轮覆盖掉，否则「近期经历」里永远只剩最新一条概要。
        # 写入失败直接上抛（硬约定 #4），不吞。
        from brain.hc.temporary_memory import get_temporary_memory
        focus_state._data.pop("pending_terminate", None)
        get_temporary_memory().add(summary)
        logger.info("已将 terminate 总结写入短期记忆（最后一次调用）")

        # 登记本次 PFCA 会话的最终评价（SC 轴闭环 + 想法入池）：
        # 好 +10 / 坏 -10 / 无评价向 40 单点抚平（pull_toward_value）。
        # 评价归一：LLM 未明确选择「好/坏」（空值、词表外、含糊文本）一律自动落
        # 「未得到他人评价与反馈」，不让幻觉词表值触发 ValueError 崩溃。
        final_evaluation = (final_evaluation or "").strip()
        if FinalEvaluationAct.kind_of(final_evaluation) not in ("good", "bad"):
            final_evaluation = FinalEvaluationAct.none_name()
        # 关键闭环步骤，禁止 try-except 吞异常；获取失败直接上抛崩溃阻断。
        # title 透传给最终评价，想法 desc 格式：在事件{title} 中，{评价词}。
        from brain.amy.emotional_interface import get_emotional_interface
        ei = await get_emotional_interface()
        await ei.apply_final_evaluation(final_evaluation, title)

        # 灵光一现记事本落库（真正结束）：以本次事件标题命名，将本事件链期间
        # conversation 记录的素材、触发原因、PFCA 总结与最终评价一并持久化到 shortlight/notes/。
        # 无活跃链文档属状态异常，直接上抛崩溃阻断（硬约定 #4）。
        from brain.hc.shortlight import get_shortlight_notepad
        await get_shortlight_notepad().finalize_chain(
            title, final_evaluation=final_evaluation, summary=summary
        )

        from brain.pfc.pfca.pfca_core import get_pfca_instance
        pfca_core = get_pfca_instance()
        await pfca_core.stop()
        logger.info("PFCA Core 退出标志已设置，执行退出流程")

        return f"已成功结束任务链，总结: {summary}"

    async def deep_thinking(self, thinking_mode: str, current_summary: str = "") -> str:
        """
        深入思考工具，处理重新整理思绪或总结观点两种模式。

        Args:
            thinking_mode:
                - reorganize: 重新整理思绪，使用 ReorganizeAgent 重新思索根本性行动方针
                - opinion: 观点，根据现状总结形成观点

        Returns:
            str: 思考结果
        """
        logger.info(f"执行deep_thinking操作，思考模式: {thinking_mode}")

        from brain.pfc.pfca.pfca_history_manager import get_pfca_history_manager

        pfca_history_manager = get_pfca_history_manager()

        if thinking_mode == "reorganize":
            from brain.base.light_agent.reorganize_agent import ReorganizeAgent
            reorganize_agent = ReorganizeAgent(current_summary=current_summary)
            direction_result = await reorganize_agent.run()

            logger.info(f"ReorganizeAgent 返回结果: {direction_result}")

            clear_mind = direction_result.get("clear_mind", False)
            decision_thought = direction_result.get("decision_thought", "")

            if clear_mind:
                logger.info("检测到根本性方向错误，清空 PFCA 上下文并传入方向性指令")
                pfca_history_manager.clear()

                focus_state = get_global_session_state()
                current_session = (focus_state.get_sessions() or [{}])[0]

                first_message_manager = get_pfca_input_manager()
                first_message_manager.prepare_first_message(
                    FirstMessageType.CONTEXT_REBUILD,
                    current_session,
                    current_summary=current_summary,
                    decision_thought=decision_thought,
                )
                # 消息已放入队列，由 session 统一注入 history
                return {
                    "archived": True,
                    "content": f"【方向检查】检测到根本性方向错误。\n\n决策思想：{decision_thought}\n\n已归档上下文并更新新的思考方向。"
                }
            else:
                return f"决策思想：{decision_thought}"

        elif thinking_mode == "opinion":
            from brain.base.light_agent.opinion_agent import OpinionAgent
            opinion_agent = OpinionAgent(current_summary=current_summary)
            opinion_result = await opinion_agent.run()
            opinion = opinion_result.get("opinion", "")
            return f"【观点】{opinion}"

    async def recall_memory(self, operation: str, title: str) -> str:
        """
        回想记忆，查找记忆中一个任务或经验/嘱托的完整信息

        Args:
            operation: 操作类型，task=回想任务，bequest=回想经验/嘱托
            title: 要回想的任务或经验/嘱托的标题（完整标题）

        Returns:
            str: 检索结果
        """
        logger.debug(f"执行recall_memory操作，操作类型: {operation}，标题: {title}")

        if operation == "task":
            from brain.hc.database.task import TaskOperations
            task_ops = TaskOperations()
            task_id = await task_ops.get_task_id_by_title(title)
            if not task_id:
                result = f"未找到标题为「{title}」的任务"
            else:
                result = await task_ops.format_task_by_id(task_id)
        elif operation == "bequest":
            from brain.hc.database.bequest import BequestOperations
            bequest_ops = BequestOperations()
            result = await bequest_ops.format_bequest_by_title(title)
        else:
            return f"未知的操作类型: {operation}"

        return result


# 全局PFCA工具管理器实例，延迟初始化
_global_pfca_tool_manager = None


def get_pfca_tool_manager():
    """
    获取PFCA工具管理器实例的方法，实现单例模式

    Returns:
        PFCA_tool_manager: PFCA工具管理器实例
    """
    global _global_pfca_tool_manager
    if _global_pfca_tool_manager is None:
        _global_pfca_tool_manager = PFCA_tool_manager()
    return _global_pfca_tool_manager


# 导出常用函数，作为模块的公共接口
async def handle_tool_call(tool_call) -> Dict[str, Any]:
    """处理工具调用的便捷函数

    作为模块的主要入口点，负责处理来自pfca_core的工具调用请求
    """
    # 获取工具管理器实例
    tool_manager = get_pfca_tool_manager()
    return await tool_manager.handle_tool_call(tool_call)


def get_available_tools() -> List[Dict[str, Any]]:
    """
    获取可用的工具列表，用于LLM的function call

    Returns:
        List[Dict[str, Any]]: 工具列表，每个工具包含名称、描述和参数信息
    """
    return PFCA_tool_manager.get_tools()
