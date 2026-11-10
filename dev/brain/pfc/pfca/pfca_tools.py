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
from brain.amy.constants import FinalEvaluationAct, TaskResultAct, PfcaRegulationAct, clamp, FEEDBACK_MAX
from brain.amy.feedback_state import get_feedback_state
from brain.amy.decision_feelings import AXIS_NAMES
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from brain.base import ToolManager, ToolHC, ToolSocial, ToolTask, ToolPersona, ToolJinnang
from brain.hc.shortlight import SCOPE_NORMAL
from brain.hc.global_session_state import get_global_session_state


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
                    "description": "启动conversation_agent，开启一段对话（约 5-15 句）。可指定目标会话 id；不指定则默认为主人的私聊会话。一次只能调用一次该工具，不能一次性同时调用多次该工具。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "session_id": {
                                "type": "string",
                                "description": "目标会话 id，格式 'type_id'，例如 'private_xxx' / 'group_yyy'。"
                            },
                            "purpose": {
                                "type": "string",
                                "maxLength": 120,
                                "description": "一句话说清这段要达成的结果或要做的事，像标题一样，最多两句。不写语气、句式、具体台词，也不写禁止项。"
                            },
                            "extra_info": {
                                "type": "string",
                                "description": "可选，可以写长：相关消息原文、前因后果、人物印象、你希望参考的要点。这些只是参考与建议，不是要求对话逐条照做。"
                            },
                            "max_messages": {
                                "type": "integer",
                                "description": "本段话题的发送轮次软上限（一次发送工具调用计 1，即使拆成多条消息），达到后回到 PFCA 重新拟定策略。首次建议 0-5。"
                            },
                            "persona_name": {
                                "type": "string",
                                "description": "可选，指定本段对话使用的人格名，只能填人格清单里已有的名字。不填则使用默认人格。"
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
                    "description": ("启动 social_chat_agent，在主人的监管/协作/支持下，与目标对话进行沟通，适用于谨慎环境的社交任务。一次只能调用一次该工具，不能一次性同时调用多次该工具。"),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "session_id": {
                                "type": "string",
                                "description": "工作会话 id，格式 'type_id'（如 'group_xxx' / 'private_yyy'）。不能是主人的私聊会话（主人私聊用 conversation_agent）。"
                            },
                            "purpose": {
                                "type": "string",
                                "maxLength": 120,
                                "description": "一句话说清这次社交要达成的结果，像标题一样，最多两句。不写语气、句式、具体台词，也不写禁止项。"
                            },
                            "extra_info": {
                                "type": "string",
                                "description": "可选，可以写长：相关消息原文、人物印象、前因后果、希望参考的要点。这些只是参考与建议，不是要求对方逐条照做。"
                            },
                            "persona_name": {
                                "type": "string",
                                "description": "可选，指定本段社交使用的人格名，只能填人格清单里已有的名字。不填则使用默认人格。"
                            }
                        },
                        "required": ["session_id", "purpose"]
                    }
                }
            },
            # 情绪调节（PFCA 专属）：主动平复/调整自身情绪，写入 AMY thought。
            # enum 收口于 PfcaRegulationAct（不对 conversation / social_chat 开放）。
            {
                "type": "function",
                "function": {
                    "name": "regulate_emotion",
                    "description": (
                        "主动调节自己的情绪，写入情绪想法池；调用后会返回你此刻的心情描述。"
                        "情绪挡在前面时用它，可下调也可上调："
                        "烦躁、戒备、兴奋上头、抵触某人，用降温档把自己拉回理性；"
                        "提不起劲、不想动、语气发闷，用提气档把自己拽起来。"
                        "每次只调一档，可重复调用叠加；情绪已合适时不必调用。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "act": {
                                "type": "string",
                                "description": (
                                    "调节方式，必须从以下列表中选择之一："
                                    f"{'、'.join(PfcaRegulationAct.names())}。"
                                ),
                                "enum": PfcaRegulationAct.names(),
                            }
                        },
                        "required": ["act"],
                    },
                },
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
            # 内心人格征询（2026-09-29 调整：PFCA 只给现状，全部人格并行自荐评分，
            # 返回各人格评分 + 行动方向供 PFCA 参考，不再替换人格；
            # schema 收口于通用层 ToolPersona）
            ToolPersona.get_inner_persona_tool_schema(),
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
                                    "本次会话的总结：完整前因后果、自我感受与关键细节，需包含具体人物名称。不能超过100字"
                                    "该总结会写入短期记忆，并随记事本一同落库存档"
                                    "——事件经过无需再用 record_to_notepad 记录。"
                                )
                            },
                            "title": {
                                "type": "string",
                                "maxLength": 15,
                                "description": "本次事件的简易标题（不超过 15 字），最终评价想法将绑定该标题。"
                            },
                            "task_result": {
                                "type": "string", "enum": TaskResultAct.names(),
                                "description": (
                                    "本轮行动的实际效益，必须按证据选档，不能按自己投入多少或话题是否顺利来选："
                                    "+10 仅限对方明确说自己有用、或已产生可指认的实际价值、或明确使对方收益；"
                                    "对方没有明确表达就不算 +10，只能选 0。"
                                    "0＝可能产生收益但未确认；-5＝结果让对方不满；-10＝任务被延期或推迟；"
                                    "-20＝产生负面收益；-30＝帮倒忙。"
                                    "把话说得体面、聊得顺利、自己长了见识、任务清单被推进，都不构成 +10 的依据。"
                                )
                            },
                            "failure_reason": {"type": "string", "description": "结束评价属于 AMY 的负面分区时填写本次表现差原因。自动本地缓存；下次申请维护时归入该申请并清空待维护缓存。可能产生收益(0)与无评价本身不属于表现失败。"},
                            "final_evaluation": {
                                "type": "string", "enum": FinalEvaluationAct.names(),
                                "description": "总体社交评价：明确好 +20，含蓄好 +10，含蓄坏 -10，明确坏 -20，无评价 -5。与实际效益评价独立；仅第二次确认退出时结算。"
                            },
                            "enter_reflection": {
                                "type": "boolean",
                                "description": (
                                    "可选。是否在本次会话结束后进入反思状态，反思系统可以对经验进行思考，任务进行复盘，笔记进行整理，登记相关经验，修改角色印象，调整任务等"
                                    "设为 true 时必须同时提供 reflection_purpose；"
                                    "仅第二次调用（确认退出时）生效。"
                                )
                            },
                            "reflection_purpose": {
                                "type": "string",
                                "description": (
                                    "可选。进入反思的目的（仅 enter_reflection=true 且第二次调用时生效）："
                                    "本次要复盘什么、想提炼什么经验或检验什么认知，将作为反思会话的首条输入。"
                                )
                            }
                        },
                        "required": ["summary", "title", "task_result", "final_evaluation"]
                    }
                }
            },
            ToolHC.get_list_notes_tool_schema(),
            ToolHC.get_query_note_tool_schema(),
            ToolHC.get_record_note_tool_schema(),
            ToolJinnang.get_query_jinnang_tool_schema(),
            # 通用级任务工具（2026-09-18 接入：查询任务状态；
            # 裁决级 create_task 仍私有于 QuestAgent，不对 PFCA 开放）
            *ToolTask.get_task_tool_schemas(),
            # quest_agent 联系通道（2026-09-18 接入：直接联系 quest agent 登记待接续事项，
            # 由其查重后登记；PFCA 不持有裁决权）
            ToolTask.get_quest_agent_tool_schema(),
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
            "inner_persona_agent": self.summon_personas,
            # 情绪调节（PFCA 专属 act 表）
            "regulate_emotion": self.regulate_emotion,
            "query_note": self.query_note,
            "list_notes": self.list_notes,
            "record_to_notepad": self.record_to_notepad,
            "query_jinnang": ToolJinnang.query_jinnang,
            # 通用级任务工具（直接引用 ToolTask 类方法，风格同 query_history 引用 ToolSocial）
            "query_task_status": ToolTask.query_task_status,
            # quest_agent 联系通道（直接引用 ToolTask 类方法）
            "quest_agent": ToolTask.quest_agent,
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

    async def conversation_agent(self, purpose: str, max_messages: int = 0, extra_info: str = "", session_id: str = "", persona_name: str = "") -> Any:
        """
        直接开启一段对话（约 5-15 句的短对话）。
        调用方可通过 session_id 指定目标会话；不指定则默认为主人的私聊会话（private_<MASTER_ID>）。

        保护模式（SafetyConfig.CONVERSATION_PROTECTION_MODE）：
        - 开启时仅允许对以下会话发起对话：主人私聊会话（private_<MASTER_ID>）、
          SafetyConfig.CONVERSATION_ALLOWED_SESSIONS 白名单内的会话、以及在记忆里
          勾选了「允许 conversation」（allow_conversation）的角色/群组所关联的会话；
          其他会话一律阻止，返回 blocked 结果并提醒 LLM 改用 social_chat_agent。

        涉及附件时，PFCA 把附件清单（[图片:xxx] / [文件:xxx] 等）写进 purpose 或 extra_info 里。

        人格指定：本段对话使用 persona_name 指定的人格（缺省 normal）作为语言与行为基准，
        由 conversation 在本段会话首条消息注入该人格 kernel_prompt；人格不再随 PFCA 全局切换。

        Args:
            purpose: 这一段要达成的结果或要做的事，一两句话，像标题。
            extra_info: 可选的参考材料（消息原文、前因后果、人物印象等），可以写长，仅作建议。
            max_messages: 软限制发送轮次数（一次发送工具调用计 1）
            session_id: 可选，目标会话 id，格式 'type_id'。为空时默认主人的私聊会话。
            persona_name: 可选，本段对话使用的人格名。为空时用默认人格（normal）。

        Returns:
            Dict[str, Any]: 遵循统一格式的操作结果；保护模式阻止时返回
            {"blocked": True, "reason", "session_id", "message"}。
        """
        from config.role_settings import RoleConfig
        from config.settings import SafetyConfig
        from brain.hc.database.core import get_memory_db
        from brain.hc.global_session_state import get_global_session_state
        from brain.chat.conversation.conversation_core import get_conversation_core

        session_id = (session_id or "").strip() or f"private_{RoleConfig.MASTER_ID}"
        persona_name = self._resolve_persona_name(persona_name)

        # 保护模式：仅允许对主人私聊会话 + 白名单会话 + 记忆中勾选放行的实体会话对话，
        # 非放行会话一律阻止并提醒改用 social_chat_agent（对外社交走受监管的社交通道）。
        # 放行开关在「记忆 → 社交实体」详情卡里勾选，按实体写入记忆库。
        if SafetyConfig.CONVERSATION_PROTECTION_MODE:
            master_private = f"private_{RoleConfig.MASTER_ID}"
            allowed = {master_private} | set(SafetyConfig.CONVERSATION_ALLOWED_SESSIONS)
            if session_id not in allowed and not await get_memory_db().conversation_allowed(session_id):
                logger.warning(
                    f"conversation_agent 保护模式已阻止对未放行会话发起对话: {session_id}"
                )
                return {
                    "blocked": True,
                    "reason": "保护模式已开启：conversation_agent 仅允许与主人私聊会话及已放行的会话对话。",
                    "session_id": session_id,
                    "message": (
                        f"已阻止 conversation_agent 向未放行的会话 {session_id} 发起对话。"
                        "如需放行，请在记忆里给对应角色/群组勾选「允许 conversation」；"
                        "对其他人/群聊的日常沟通请改用 social_chat_agent。"
                    ),
                }

        focus_state = get_global_session_state()

        # 注册会话（如未注册）
        pfca_sessions = focus_state.get_sessions()
        if not any(s.get("session_id") == session_id for s in pfca_sessions):
            await focus_state.add_pfca_session(session_id, search_impression=True)

        # 拼装 purpose：目标在前，extra_info 作为参考材料附后，明确其非强制性质
        if extra_info and extra_info.strip():
            combined_purpose = (
                f"{purpose}\n\n【可参考的信息】\n以下只是参考与建议，不必逐条照做：\n{extra_info.strip()}"
            )
        else:
            combined_purpose = purpose

        conversation_core = get_conversation_core()
        logger.debug(f"启动对话: session_id={session_id}, max_messages={max_messages}")
        chat_result = await conversation_core.start_session(
            session_id=session_id,
            purpose=combined_purpose,
            max_messages=max_messages,
            persona_name=persona_name,
        )
        logger.info(f"conversation模块返回结果(session_id={session_id}): {chat_result}")
        return chat_result

    async def social_chat_agent(self, session_id: str, purpose: str, extra_info: str = "", persona_name: str = "") -> Any:
        """
        启动 social_chat_agent，对其他人（群聊 / 非主人私聊）推进一个社交任务。

        purpose / extra_info 拼装为任务书直传 SocialChatCore.start()；
        会话注册与汇报窗口锚点由 core 自治，工具层不做重复注册；
        对外发言的监管（发送模式 / 主人审批）由 social_chat 内部处理。

        人格指定：本段社交使用 persona_name 指定的人格（缺省 normal）作为语言与行为基准，
        由 social_chat 在本段首条消息注入该人格 kernel_prompt；人格不再随 PFCA 全局切换。

        Args:
            session_id: 工作会话 id，格式 "type_id"（如 "group_xxx" / "private_yyy"）。
                不能是主人的私聊会话（主人私聊请用 conversation_agent）。
            purpose: 这次社交要达成的结果，一两句话，像标题。
            extra_info: 可选参考材料（消息原文、人物印象、前因后果），可以写长，仅作建议。
            persona_name: 可选，本段社交使用的人格名。为空时用默认人格（normal）。

        Returns:
            Dict[str, Any]: social_chat 返回结果 {summary_text, exit_reason}
        """
        if not session_id or not session_id.strip():
            raise ValueError("session_id 必填")
        if not purpose or not purpose.strip():
            raise ValueError("purpose 必填")

        persona_name = self._resolve_persona_name(persona_name)

        from config.role_settings import RoleConfig
        master_private = f"private_{RoleConfig.MASTER_ID}"
        if session_id.strip() == master_private:
            raise ValueError(
                f"social_chat_agent 不能用于主人的私聊会话 ({master_private})；"
                f"请改用 conversation_agent。"
            )

        if extra_info and extra_info.strip():
            combined_purpose = (
                f"{purpose.strip()}\n\n【可参考的信息】\n以下只是参考与建议，不必逐条照做：\n{extra_info.strip()}"
            )
        else:
            combined_purpose = purpose.strip()

        from brain.chat.social_chat.social_chat_core import SocialChatCore
        result = await SocialChatCore().start(
            purpose=combined_purpose,
            working_session_id=session_id.strip(),
            persona_name=persona_name,
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
        """按章节标题查询一篇普通笔记的完整细节，委托通用 ToolHC.query_note。

        章节标题取自 list_notes 的普通笔记返回；见闻录 / 任务相关笔记等
        特殊槽位由 list_notes 直接给全，不经此工具。"""
        return await ToolHC.query_note(title)

    async def list_notes(self, scope: str = SCOPE_NORMAL) -> str:
        """列出笔记概览，委托通用 ToolHC.list_notes。

        scope 可选普通笔记（默认，已落库事件链文档，给章节标题与条目数）/
        见闻录 / 任务相关笔记（后两者为特殊储存槽位台账，逐条给全）。"""
        return await ToolHC.list_notes(scope=scope)

    async def record_to_notepad(self, text: str, category: str) -> str:
        """记事本（灵光一现）写入——PFCA 策略视角：采集观察到的对方素材，不做经验提炼。
        仅收纯文本与类别标签；来源标注 source="pfca"，溯源走事件链，
        记录进当前事件链的缓存文档，terminate 落库时统一以事件标题命名。"""
        note_id = await ToolHC.record_note(
            text=text,
            category=category,
            source="pfca",
            session_id="",
            session_name="",
        )
        return f"已记录至记事本（{note_id}）。"

    async def regulate_emotion(self, act: str) -> str:
        """情绪调节（PFCA 专属）：把一条 PfcaRegulationAct 想法写入 AMY thought 池。

        调完回写当前 DA 数值与表现侧 DA 描述原文，让 PFCA 看得到自己调到了哪一档
        （仅此工具单向暴露 DA；NE 与最终评价类原因仍不进入决策侧）。
        act 非法属工具入参错误：抛 ValueError 交 LLM 自纠。
        """
        if act not in PfcaRegulationAct.names():
            raise ValueError(
                f"未知情绪调节方式：{act}；请选择{'、'.join(PfcaRegulationAct.names())}"
            )

        from brain.amy.emotional_interface import get_emotional_interface
        from brain.amy.prompt_engine import get_dopamine_desc
        ei = await get_emotional_interface()
        success, msg, _prompt = await ei.build_thought(act=act, desc=act)
        if not success:
            raise RuntimeError(f"情绪调节写入失败：{msg}")

        snapshot = await ei.get_mood_snapshot()
        logger.info(f"PFCA 情绪调节：{act}（DA={snapshot.dopamine:.0f}）")
        return (
            f"已调节情绪：{act}。\n"
            f"当前心情 DA {snapshot.dopamine:.0f}/100："
            f"{get_dopamine_desc(snapshot.dopamine)}"
        )

    async def terminate(self, summary: str, title: str = "", check: bool = False, final_evaluation: str = "",
                        enter_reflection: bool = False, reflection_purpose: str = "",
                        task_result: str = TaskResultAct.DEFAULT, failure_reason: str = "") -> str:
        """
        结束当前全部的任务链。

        第一次调用：将 summary/title/final_evaluation 暂存进 GlobalSessionState（不落库），
        返回行动记录和确认提示。
        第二次调用（最后一次）：以本次参数把 summary 写入短期记忆、随记事本落库并登记
        最终评价（自我评价记录 + 想法入池）——保证两次调用只有最后一次计入 temporary memory。
        若会话未经第二次调用结束（max_rounds 熔断 / 强停等），由 pfca_core.start 的
        退出兜底把暂存补写入短期记忆（防总结丢失，docs/已完成.md #9）。

        Args:
            summary: 本次会话的总结（完整前因后果、自我感受与关键细节）
            title: 本次事件的简易标题（≤15 字），最终评价想法绑定该标题
            check: 已废弃，不再使用
            final_evaluation: 本次 PFCA 会话的最终评价（词表名，来自 FinalEvaluationAct）；
                仅第二次调用（确认退出）时生效；留空按「无评价」登记（旧调用兼容）
            task_result: 六档实际效益评价；省略时按「可能产生收益」0 结算
            failure_reason: 负面结束评价时填写，缓存到下次维护申请；数值变动说明采用 title
            enter_reflection: 是否在本会话结束后进入反思状态（接力 Reflection 会话）；
                仅第二次调用（确认退出）时生效
            reflection_purpose: 进入反思的目的（作为反思会话首条输入）；
                仅 enter_reflection=true 时使用

        Returns:
            str: 操作结果，包含行动记录和确认提示
        """
        logger.debug(f"执行terminate操作，总结: {summary}")

        from brain.hc.global_session_state import get_global_session_state
        focus_state = get_global_session_state()

        # 参数校验前置（在任何落库副作用之前）：enter_reflection=true 时
        # reflection_purpose 必填，缺失属参数错误，抛 ValueError 交 LLM 自纠
        # （_safe_execute 可恢复错误通道；先校验再落库，避免部分副作用已提交）。
        if enter_reflection:
            if not (reflection_purpose or "").strip():
                raise ValueError("enter_reflection=true 时必须提供 reflection_purpose")

        TaskResultAct.validate(task_result)
        if not title.strip():
            raise ValueError("收尾必须提供事件标题 title")
        final_evaluation = (final_evaluation or "").strip()
        if not final_evaluation:
            final_evaluation = FinalEvaluationAct.default_name()
        if not FinalEvaluationAct.contains(final_evaluation):
            raise ValueError(f"未知社交评价：{final_evaluation}")
        final_evaluation = FinalEvaluationAct.normalize(final_evaluation)

        from brain.amy.failure_reasons import validate_failure_reason
        failure_act = task_result if TaskResultAct.is_failure(task_result) else final_evaluation
        validate_failure_reason(failure_act, failure_reason)

        terminate_confirmed = focus_state._data.get("terminate_confirmed", False)

        if not terminate_confirmed:
            focus_state._data["terminate_confirmed"] = True
            logger.info("terminate 第一次调用，暂存总结并显示结束预览")

            # 只暂存、不落库（docs/已完成.md #9 修复）：两次调用语义下只有"最后一次"
            # 计入 temporary memory——第二次调用（确认退出）才写入；若会话经
            # max_rounds 熔断/强停等路径结束、永远没有第二次调用，则由
            # pfca_core.start 的退出兜底把暂存补写入临时记忆。
            focus_state._data["pending_terminate"] = {
                "summary": summary,
                "title": title,
                "final_evaluation": final_evaluation,
                "task_result": task_result,
                "failure_reason": failure_reason,
            }

            # ===== 结束预览（确认退出前的预告） =====
            # 1) 未完成任务轻量简表（标题 + 下次处理时间），拉取失败直接上抛阻断（user_rules）。
            from brain.hc.database.task import TaskOperations
            task_ops = TaskOperations()
            task_ids = await task_ops.get_active_tasks(limit=10)
            tasks_text = await task_ops.format_tasks_brief(task_ids) if task_ids else ""

            # 2) 两轴预览。参数已前置校验，这里只计算，不结算。
            fe = final_evaluation
            delta = FinalEvaluationAct.feedback_delta(fe)
            current = get_feedback_state().read_value()
            after = clamp(current + delta, 0.0, FEEDBACK_MAX)
            presence_line = (
                f"{AXIS_NAMES['recognition']}：{int(round(current))}/{int(FEEDBACK_MAX)} "
                f"→ {int(round(after))}/{int(FEEDBACK_MAX)}"
            )
            competence = get_feedback_state().snapshot()["competence_value"]
            competence_after = clamp(competence + TaskResultAct.deltas[task_result], 0.0, FEEDBACK_MAX)
            competence_line = f"{AXIS_NAMES['competence']}：{competence:g}/100 → {competence_after:g}/100（{task_result}）"
            reason_line = ""
            if delta < 0:
                reason_line = f"原因：{title} {fe} {int(delta)}   ←新增"

            preview = ["你需要二次确认才能真正结束。", "", "本次行动结束后："]
            if tasks_text:
                preview += ["有以下任务仍未完成，将择时再次处理", tasks_text]
            preview += ["", competence_line, presence_line, f"事件：{title}"]
            if reason_line:
                preview.append(reason_line)
            preview += [
                "",
                "结束前，你需要确认是否可以提升别人对你的评价，请注意，强行要求他人评价"
                "可能导致评价反而降低，并选择确认是否要通知重要人物是否知道你要离开。",
            ]
            return "\n".join(preview)

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

        # 本次参数已经校验；确认退出后登记实际效益与外部评价。
        # 关键闭环步骤，禁止 try-except 吞异常；获取失败直接上抛崩溃阻断。
        # 事件标题及原因进入可追溯变动历史。
        from brain.amy.emotional_interface import get_emotional_interface
        ei = await get_emotional_interface()
        await ei.apply_final_evaluation(final_evaluation, title, task_result, failure_reason=failure_reason)

        # 灵光一现记事本落库（真正结束）：以本次事件标题命名，将本事件链期间
        # conversation 记录的素材、触发原因、PFCA 总结与最终评价一并持久化到 shortlight/notes/。
        # 无活跃链文档属状态异常，直接上抛崩溃阻断（硬约定 #4）。
        from brain.hc.shortlight import get_shortlight_notepad
        await get_shortlight_notepad().finalize_chain(
            title, final_evaluation=final_evaluation, summary=summary
        )

        # 可选接力：本会话结束后进入反思状态（由 core.py run_pfc_cycle 消费）。
        # 仅第二次调用（确认退出）生效；enter_reflection=true 时 reflection_purpose 必填，
        # 缺失属参数错误，抛 ValueError 交 LLM 自纠（_safe_execute 可恢复错误通道）。
        if enter_reflection:
            purpose = (reflection_purpose or "").strip()
            if not purpose:
                raise ValueError("enter_reflection=true 时必须提供 reflection_purpose")
            focus_state._data["pfc_handoff"] = {
                "state": "reflection",
                "purpose": purpose,
            }
            logger.info("terminate 已登记反思接力，会话结束后进入反思状态")

        from brain.pfc.pfca.pfca_core import get_pfca_instance
        pfca_core = get_pfca_instance()
        await pfca_core.stop()
        logger.info("PFCA Core 退出标志已设置，执行退出流程")

        return f"已成功结束任务链，总结: {summary}"

    @staticmethod
    def _resolve_persona_name(persona_name: str) -> str:
        """规整工具入参 persona_name：为空取默认人格；非法人格抛 ValueError 走 LLM 自纠通道。

        人格为逐会话显式指定（conversation_agent / social_chat_agent 的 persona_name 入参）；
        persona_name 缺失/非法属工具层入参问题，走 ValueError → LLM 自纠，不崩溃阻断。
        """
        from brain.base.persona.persona_registry import (
            get_default_persona_name,
            list_selectable_personas,
        )

        key = (persona_name or "").strip()
        if not key:
            return get_default_persona_name()
        # 用 list_selectable 而非 roster：roster 排除了基本人格，
        # 全新安装只有 common 时会把唯一合法人格也判为不存在。
        selectable = list_selectable_personas()
        if key not in (p.persona_name for p in selectable):
            valid = "、".join(p.persona_name for p in selectable)
            raise ValueError(f"人格不存在: {key!r}。可用人格: {valid}。")
        return key

    async def summon_personas(self, current_summary: str = "") -> Dict[str, Any]:
        """
        唤起内心人格自荐：全部已注册人格并行自荐，各自给出参与度评分与行动方向。

        PFCA 不再点名选人，只给现状；本工具**不替换任何人格**，仅把各人格的
        参与度评分（0-100）与「若上场将采取的行动方向」返回给 PFCA 作决策参考。
        人格由 conversation_agent / social_chat_agent 的 persona_name 入参逐会话指定。

        Args:
            current_summary: 当前现状总结：前因后果、状态、问题、已尝试方法。

        Returns:
            Dict: {
                "personas": [{"persona_name", "score", "reason", "plan"}, ...],
                "suggested": {"persona_name", "score", "reason", "plan"},
            }
            （suggested 为评分最高者，同分则先给出者；仅供 PFCA 参考，不产生人格切换）
        """
        logger.info("执行summon_personas操作，唤起内心人格自荐")

        from brain.base.persona.persona_agent import PersonaAgent
        from brain.base.persona.persona_registry import PersonaDef, list_roster_personas

        # 内荐只由普通人格参与，基本人格条目（persona_name="common"）不参荐。
        personas = list_roster_personas()
        if not personas:
            return {"personas": [], "suggested": None, "message": "当前没有可征询的内心人格，可使用基本说话风格。"}

        # 完成顺序记录：分数相同者以此定胜负（先给出者排在前面）。
        # 事件循环单线程，各协程完成时 append，天然记录给出先后。
        completion_order: List[str] = []

        async def _self_nominate(persona: PersonaDef) -> Dict[str, Any]:
            result = await PersonaAgent(
                persona=persona, current_summary=current_summary
            ).run()
            completion_order.append(result["persona_name"])
            logger.info(
                f"内心人格 [{result['persona_name']}] 给出参与度评分 {result['score']}；"
                f"行动方向: {result['plan'] or '（未给出）'}"
            )
            return result

        # gather 结果序与输入序一致（completion_order 记录真实给出序）；
        # 任一人格失败（评分解析不出等）直接上抛阻断，不吞错继续（user_rules）。
        results = await asyncio.gather(
            *(_self_nominate(persona) for persona in personas)
        )

        best_score = max(r["score"] for r in results)
        by_name = {r["persona_name"]: r for r in results}
        suggested = next(
            by_name[name] for name in completion_order if by_name[name]["score"] == best_score
        )
        logger.info(
            f"内心人格自荐完成，最高分人格: {suggested['persona_name']} "
            f"评分={suggested['score']}（仅供参考，不切换人格）"
        )

        return {
            "personas": [
                {
                    "persona_name": r["persona_name"],
                    "score": r["score"],
                    "reason": r.get("reason", ""),
                    "plan": r.get("plan", ""),
                }
                for r in results
            ],
            "suggested": {
                "persona_name": suggested["persona_name"],
                "score": suggested["score"],
                "reason": suggested.get("reason", ""),
                "plan": suggested.get("plan", ""),
            },
        }


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
