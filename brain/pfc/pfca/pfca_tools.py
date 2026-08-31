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
import asyncio
from typing import Dict, Any, List
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from brain.base import ToolManager
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
                    "description": "启动conversation_agent，直接开启一段对话（约 5-15 句）。可指定目标会话 id；不指定则默认为主人的私聊会话。",
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
                                "description": "本段话题的消息条数软上限，达到后会回到 PFCA 重新拟定策略。首次建议 0-5。"
                            }
                        },
                        "required": ["session_id", "purpose", "max_messages"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "social_agent",
                    "description": "启动social_agent，对其他人（群聊或非主人私聊）发起社交任务。委托 social_agent 制定计划，并执行该计划。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "session_id": {
                                "type": "string",
                                "description": "工作会话 id，格式 'type_id'，例如 'group_xxx' / 'private_yyy'（非主人会话）。"
                            },
                            "task_info": {
                                "type": "string",
                                "description": "社交任务内容：任务目标，与现状简报，前因后果等。"
                            }
                        },
                        "required": ["session_id", "task_info"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "query_history",
                    "description": "查询某个会话的历史聊天记录。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "session_id": {
                                "type": "string",
                                "description": "目标会话 id，格式 'type_id'。"
                            },
                            "search_range": {
                                "type": "string",
                                "description": "格式 'last_n'，例如 'last_10' 表示最后 10 条。"
                            }
                        },
                        "required": ["session_id", "search_range"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "memory_agent",
                    "description": "管理角色第一印象与关系信息（写入通讯录的角色说明 / 短期记忆中的角色群聊信息）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "session_id": {
                                "type": "string",
                                "description": "会话 id，格式 'type_id'。"
                            },
                            "modification_type": {
                                "type": "string",
                                "enum": ["session", "mentioned_person"],
                                "description": "'session' 修改目标会话印象；'mentioned_person' 创建会话中被提及的重要人物。"
                            },
                            "content": {
                                "type": "string",
                                "description": "自然语言描述要修改的方向。"
                            },
                            "extra_name": {
                                "type": "string",
                                "description": "仅 modification_type='mentioned_person' 时必填，被提及人物的名字。"
                            }
                        },
                        "required": ["session_id", "modification_type", "content"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "query_address_book",
                    "description": "查看通讯录（可联系的会话 / 群聊），或直接查询指定角色的印象。查找范围可选「私聊」「群聊」「全部」；也可以输入具体角色名称，此时将直接查询该角色在记忆中的印象。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "search_range": {
                                "type": "string",
                                "description": "查找范围：「私聊」「群聊」「全部」之一，或具体角色名称（如「主人」）。输入具体名称时，将直接查询该角色的印象。"
                            }
                        },
                        "required": ["search_range"]
                    }
                }
            },
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
                    "name": "event_register_agent",
                    "description": "管理长期事件（即下次系统自动唤醒时该处理什么）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "事件前因后果、相关人物、下次激活时间等。ongoing 必须注明具体未完成事件。"
                            },
                            "operation_type": {
                                "type": "string",
                                "enum": ["create_ongoing", "update_ongoing", "create_completed"],
                                "description": "create_ongoing=新建持续关注事件；update_ongoing=推进或结束 ongoing；create_completed=归档有长期学习意义的完成事件。"
                            }
                        },
                        "required": ["content", "operation_type"]
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
                            "reason": {
                                "type": "string",
                                "description": "本次会话总结：前因后果、自我感受，需包含具体人物名称。"
                            }
                        },
                        "required": ["reason"]
                    }
                }
            }
        ]

    def __init__(self):
        """初始化工具管理器"""
        logger.debug("初始化PFCA_tool_manager工具管理器")
        # 初始化工具映射表，用于动态路由
        self.tool_functions = {
            "conversation_agent": self.conversation_agent,
            "social_agent": self.social_agent,
            "query_history": self.query_history,
            "terminate": self.terminate,
            "memory_agent": self.memory_adjustment,
            "query_address_book": self._query_address_book,
            "advanced_decision_agent": self.deep_thinking,
            "recall_memory": self.recall_memory,
            "event_register_agent": self.event_judgement
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
            max_messages: 软限制消息条数
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

    async def social_agent(self, session_id: str, task_info: str) -> Any:
        """
        启动social_agent，对其他人（群聊 / 非主人私聊）发起社交任务。       
        委托 social_agent 制定计划：与主人沟通审批后输出方案。

        Args:
            session_id: 工作会话 ID，格式为 "type_id"（如 "group_xxx" / "private_yyy"）。
                不能是主人的私聊会话（主人私聊请用 conversation_agent）。
            task_info: 社交任务计划书（目标 / 现状 / 期望结局 / 关键约束 / 涉及人物等）。

        Returns:
            Dict[str, Any]: social_agent 返回的结果（含 plan / rounds 等）
        """
        if not session_id or not session_id.strip():
            raise ValueError("session_id 必填")
        if not task_info or not task_info.strip():
            raise ValueError("task_info 必填")

        from config.role_settings import RoleConfig
        master_private = f"private_{RoleConfig.MASTER_ID}"
        if session_id.strip() == master_private:
            raise ValueError(
                f"social_agent 不能用于主人的私聊会话 ({master_private})；"
                f"请改用 conversation_agent。"
            )

        # "消费一次"语义约束：
        # social_agent 下游会通过 social_chat_core.start() 进入 social_chat，
        # social_chat 的 input_manager 从 pfca_sessions[*].last_processed_time 读起点
        # （不存在则直接 raise，阻断全量拉取）。这里在 social_agent 入口处显式
        # 注册两条会话，让下游一定能在 pfca_sessions 里查到起点。
        # 顺序：主人会话先、工作会话后。
        from brain.hc.global_session_state import get_global_session_state
        focus_state = get_global_session_state()
        try:
            await focus_state.add_pfca_session(master_private, "主人")
        except Exception as e:
            logger.error(f"social_agent 注册主人会话 {master_private} 到 pfca_sessions 失败: {e}", exc_info=True)
            raise
        try:
            await focus_state.add_pfca_session(session_id.strip(), "")
        except Exception as e:
            logger.error(f"social_agent 注册工作会话 {session_id.strip()} 到 pfca_sessions 失败: {e}", exc_info=True)
            raise

        logger.debug(f"social_agent: session_id={session_id}, task_info长度={len(task_info)}")

        # 直接委托 ConversationCore / SocialChatCore（不再经过 social_agent 模块）：
        # 1) 先用 ConversationCore 与主人商讨社交计划并获取审批；
        # 2) 再用 SocialChatCore 把主人确认后的方案发送到工作会话。
        from brain.chat.conversation.conversation_core import get_conversation_core
        from brain.chat.social_chat.social_chat_core import SocialChatCore
        from config.settings import chat_config

        # —— 1. 与主人商讨计划并获取审批 ——
        combined_purpose = (
            "本次任务由 pfca 发布\n"
            "本次任务为对其他人（群聊 / 非主人私聊）发起社交任务\n"
            f"任务目标：{task_info.strip()}\n"
            "请与主人商讨社交计划并获取审批。"
        )
        conversation_core = get_conversation_core()
        plan = await conversation_core.start_session(
            session_id=master_private,
            purpose=combined_purpose,
            max_messages=5,
        )
        plan_text = plan or ""

        # —— 2. 把主人确认后的方案发送到工作会话 ——
        enforcement_level = getattr(chat_config, "SOCIAL_CHAT_ENFORCEMENT_LEVEL", "L1")
        social_chat_core = SocialChatCore()
        result = await social_chat_core.start(
            purpose=plan_text,
            working_session_id=session_id.strip(),
            preset_messages=[{"text": plan_text}] if plan_text else [{"text": task_info.strip()}],
            enforcement_level=enforcement_level,
        )
        logger.info(f"social_agent 返回结果: {result}")
        return result

    async def query_history(self, session_id: str, search_range: str) -> str:
        """
        查询指定会话的最新历史聊天记录，并把 pfca 游标推进到本次拉取的最大 seq。

        Args:
            session_id: 会话标识，格式为 "type_id"，如 "private_1234567890" / "group_198431299"
            search_range: 历史记录查询范围，格式为'last_n'，例如'last_10'

        Returns:
            str: 查询到的历史消息内容
        """
        logger.debug(f"执行历史记录查询操作，时间范围: {search_range}")

        message_count = 10
        if search_range:
            import re
            last_n_match = re.match(r'last_(\d+)', search_range)
            if last_n_match:
                message_count = int(last_n_match.group(1))
            else:
                logger.error(f"消息数量格式错误: {search_range}")
                return "消息数量格式错误，请使用正确的格式: 'last_n'，其中n为要返回的消息条数，例如'last_10'表示返回最后10条消息。"

        from memory.message_repository import get_message_store
        from memory.session_history import (
            get_global_session_history,
            FetchOptions,
            MessageFormatOptions,
        )

        # 用单例（测试隔离库/真实库），勿新建默认路径实例
        store = get_message_store()
        session_data = await store.get_latest_session_messages(session_id, message_count)
        messages = session_data.get("message_list", [])

        # 拉取到目标的最新消息后，推进 pfca 游标到本次拉取的最大 seq ——
        # LLM 已读到这些消息，truck 未读检测（seq > pfca 游标）不再重复触发本会话。
        if messages:
            target_seq = max(m.get("_seq") or -1 for m in messages)
            current = await store.get_cursor(session_id, "pfca")
            if target_seq > current:
                await store.advance_cursor(session_id, "pfca", target_seq)
                logger.info(f"query_history 已推进 pfca 游标 {current} -> {target_seq} ({session_id})")

        history = get_global_session_history()
        formatted_lines, _ = history.format_messages(
            messages,
            FetchOptions(
                message_format=MessageFormatOptions(
                    show_new_marker=False,
                    show_timestamp=True
                )
            )
        )
        messages_str = "\n".join(formatted_lines) if formatted_lines else ""
        chat_result = messages_str if messages_str else "未找到匹配的消息"
        logger.debug(f"历史记录查询模块返回结果: {chat_result}")
        return chat_result

    async def terminate(self, reason: str, check: bool = False) -> str:
        """
        结束当前全部的任务链。

        第一次调用：返回行动记录和确认提示，等待用户确认。
        第二次调用：真正执行退出逻辑。

        Args:
            reason: 结束当前任务链的原因或说明
            check: 已废弃，不再使用

        Returns:
            str: 操作结果，包含行动记录和确认提示
        """
        logger.debug(f"执行terminate操作，原因: {reason}")

        from brain.hc.global_session_state import get_global_session_state
        focus_state = get_global_session_state()

        terminate_confirmed = focus_state._data.get("terminate_confirmed", False)

        pfca_actions = focus_state._data.get("pfca_actions", [])
        actions_text = "\n".join([f"{i+1}. {action}" for i, action in enumerate(pfca_actions)]) if pfca_actions else "无行动记录"

        if not terminate_confirmed:
            focus_state._data["terminate_confirmed"] = True
            logger.info("terminate 第一次调用，显示确认提示")

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

        # 第二次调用时，将 LLM 提供的「reason」作为唯一内容写入短期记忆。
        # 不再混入本地拼接的 actions_text 等字段，保证短期记忆内容仅来源于工具属性。
        # 不传 identifier：让每一轮的"事件概要"都作为独立条目保留下来，
        # 仅受 MAX_MESSAGES 自然滚动约束；不要让「pfca_terminate_summary」这种常量
        # 标识符把历史概要逐轮覆盖掉，否则「近期经历」里永远只剩最新一条概要。
        try:
            from brain.hc.temporary_memory import get_temporary_memory
            get_temporary_memory().add(reason)
            logger.info("已将 terminate 总结写入短期记忆")
        except Exception as e:
            logger.warning(f"写入 terminate 短期记忆失败: {e}")

        from brain.pfc.pfca.pfca_core import get_pfca_instance
        pfca_core = get_pfca_instance()
        await pfca_core.stop()
        logger.info("PFCA Core 退出标志已设置，执行退出流程")

        return f"已成功结束任务链，原因: {reason}"

    async def memory_adjustment(self, session_id: str, modification_type: str, content: str, extra_name: str = "") -> str:
        """
        调整或创建记忆

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
    async def _query_address_book(self, search_range: str) -> str:
        """
        查询通讯录 / 指定角色的印象

        Args:
            search_range: 查找范围。可选'私聊'、'群聊'、'全部'；
                          或直接输入具体角色名称，此时将直接查询该角色的印象。

        Returns:
            str: 通讯录信息；输入具体名称时返回该角色的印象信息。
        """
        logger.debug(f"执行query_address_book操作，查找范围: {search_range}")

        from memory.address_book import get_address_book
        address_book = get_address_book()

        if search_range == '私聊':
            return await address_book.get_private_contacts()
        if search_range == '群聊':
            return await address_book.get_group_contacts()
        if search_range == '全部':
            return await address_book.get_all_contacts()

        # 其他输入视为具体角色名称 → 直接查询该角色的印象
        from brain.hc.database.character_compose import CharacterCompose
        character_compose = CharacterCompose()
        impression_result = await character_compose.first_session_impression_search(name=search_range)
        formatted_info = impression_result.get(
            "formatted_info", f"你对{search_range}没有任何印象。"
        )
        return f"你查询了「{search_range}」的印象：\n{formatted_info}"

    async def deep_thinking(self, thinking_mode: str, current_summary: str = "") -> str:
        """
        深入思考工具，处理重新整理思绪、总结观点或启动社交计划三种模式。

        Args:
            thinking_mode:
                - reorganize: 重新整理思绪，使用 ReorganizeAgent 重新思索根本性行动方针
                - opinion: 观点，根据现状总结形成观点
                - social: 社交计划，调用 social_agent(session_id, task_info) 工具处理社交计划

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
                logger.info("检测到根本性方向错误，归档 PFCA 上下文并传入方向性指令")
                pfca_history_manager.archive_context()

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

        elif thinking_mode == "social":
            # thinking_mode "social" 已从 schema 中移除（社交计划请直接调 social_agent 工具）。
            # 此分支保留作为保险：若旧代码 / 单元测试传入该模式，按 ValueError 上抛，强制让调用方改用 social_agent。
            raise ValueError(
                "advanced_decision_agent 不再支持 thinking_mode='social'；"
                "请改用 social_agent(session_id, task_info) 工具。"
            )

    async def recall_memory(self, operation: str, title: str) -> str:
        """
        回想记忆，查找记忆中一个事件或经验/嘱托的完整信息

        Args:
            operation: 操作类型，event=回想事件，bequest=回想经验/嘱托
            title: 要回想的事件或经验/嘱托的标题（完整标题）

        Returns:
            str: 检索结果
        """
        logger.debug(f"执行recall_memory操作，操作类型: {operation}，标题: {title}")

        if operation == "event":
            from brain.hc.database.event import EventOperations
            event_ops = EventOperations()
            event_id = await event_ops.get_event_id_by_title(title)
            if not event_id:
                result = f"未找到标题为「{title}」的事件"
            else:
                result = await event_ops.format_event_by_id(event_id)
        elif operation == "bequest":
            from brain.hc.database.bequest import BequestOperations
            bequest_ops = BequestOperations()
            result = await bequest_ops.format_bequest_by_title(title)
        else:
            return f"未知的操作类型: {operation}"

        return result

    async def event_judgement(self, content: str, operation_type: str) -> str:
        """
        事件调整，根据 PFCA 的请求对事件进行记录、更新或归档。

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
        from brain.pfc.pfca.pfca_core import get_pfca_instance
        from brain.base.light_agent.event_register_agent import EventRegisterAgent

        focus_state = get_global_session_state()
        action_desc = {
            "create_ongoing": f"新增待办任务：{content}",
            "update_ongoing": f"更新待办任务：{content}",
            "create_completed": f"归档完成事件：{content}",
        }.get(operation_type, f"记录待办任务：{content}")
        focus_state.manager_pfca_actions('add', action_desc)

        # 复用同一事件链内的 EventRegisterAgent 实例，确保 PFCA 在本条事件链中
        # 多次调用 event_judgement 时，agent 的 messages / backlog 上下文是连续的。
        pfca_core = get_pfca_instance()
        if pfca_core.event_register_agent is None:
            pfca_core.event_register_agent = EventRegisterAgent()
            logger.debug("event_judgement: 为本次 PFCA 会话新建 EventRegisterAgent")
        agent = pfca_core.event_register_agent
        result = await agent.run(additional_content=content, operation_type=operation_type)

        if isinstance(result, dict) and result.get('formatted_output'):
            return result['formatted_output']
        return str(result) if result else "任务已记录"


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
