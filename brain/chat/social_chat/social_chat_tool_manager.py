# -*- coding: utf-8 -*-
"""social_chat 工具集：send_to_master / query_history / send_preset_to_target / construct_preset_message / transfer_back_to_social_agent + 继承工具。

设计要点：
- LLM 看不到 L3 / L2 的拦截层；拦截以"工具返回值"的形式对 LLM 暴露。
- L3：send_preset_to_target 每次发审批请求给主人并监听『同意』才放行；construct_preset_message 不拦截。
- L2：send_preset_to_target 仅首次发审批请求给主人并监听『同意』，本次会话后续自动放行；construct_preset_message 不拦截。
- L1：均不拦截。
"""

import json
import asyncio
import time
from datetime import datetime
from typing import Dict, Any, List, Optional

from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from brain.base import ToolManager
from config.settings import chat_config
from brain.amy.constants import ExternalInteractionAct, EvaluationAct, SelfActionAct

# act 表 ↔ 工具 一一对应（对齐 conversation_tool_manager 的语义）：
#   emotional_judgment.act  → ExternalInteractionAct（他人行为直接刺激，评价类）
#                             + EvaluationAct（他人对我能力/表现评价）
#                             + SelfActionAct（我主动选择做什么）
#
# social_chat 特殊性：同时挂两条会话线（master / working）。
# 来源（session_type）参数必须由 LLM 显式标注，描述本条情感判断发生在哪条线上：
#   - 'master'  = 主人会话（与主人沟通引发的情感）
#   - 'working' = 工作会话（与社交对象的互动 / 通过预设条目发出的发言引发的情感）
#
# 对比 conversation：conversation 单会话，session 上下文从
# global_session_state["conversation_session"] 取；social_chat 双会话，
# session 上下文从 social_chat_core.master_session_id / working_session_id 取，
# 按 session_type 路由。

logger = get_logger('SocialChatToolManager', "INFO")

# 系统硬编码的限制等级（统一从 chat_config 读取，详见 config.settings.ChatConfig.SOCIAL_CHAT_ENFORCEMENT_LEVEL）
DEFAULT_ENFORCEMENT_LEVEL = chat_config.SOCIAL_CHAT_ENFORCEMENT_LEVEL


class SocialChatToolManager(ToolManager):
    """social_chat 工具管理器。

    使用流程：
        mgr = SocialChatToolManager(core=social_chat_core, preset_messages=[...], enforcement_level='L3')
        mgr.get_tools() -> List[Dict]
        result = await mgr.handle_tool_call(tool_call)
    """

    def __init__(
        self,
        social_chat_core,
        preset_messages: List[dict],
        enforcement_level: str = DEFAULT_ENFORCEMENT_LEVEL,
    ):
        """构造工具集。

        注：master_session_id / working_session_id 均不再作为入参——
        通过 self.social_chat_core.master_session_id / working_session_id 读取
        （master 派生自 RoleConfig.MASTER_ID；working 由 social_chat_core.start() 注入）。
        """
        self.social_chat_core = social_chat_core
        self.enforcement_level = (enforcement_level or DEFAULT_ENFORCEMENT_LEVEL).upper()

        # 预设条目池（可能被 construct_preset_message 扩充）
        self._preset_messages: List[dict] = list(preset_messages)
        self._sent_indices: set = set()
        # L2 会话级"已批准"标志：首次 send_preset_to_target 走审批后置 True，
        # 后续所有 send_preset_to_target 自动放行。
        self._l2_session_approved: bool = False

        # 复用 conversation 的工具集（继承工具直接代理，不重写）
        # 注：ConversationToolManager.__init__ 只接受 conversation_core 参数；
        # 这里使用 conversation_core 单例构造，但仅在继承工具被调用时才会触达其内部。
        from brain.chat.conversation.conversation_core import get_conversation_core
        from brain.chat.conversation.conversation_tool_manager import ConversationToolManager
        self._conv_tools = ConversationToolManager(get_conversation_core())

        # 工具函数表
        self.tool_functions = {
            "send_to_master": self.send_to_master,
            "query_history": self.query_history,
            "send_preset_to_target": self.send_preset_to_target,
            "construct_preset_message": self.construct_preset_message,
            "transfer_back_to_social_agent": self.transfer_back_to_social_agent,
            "wait_and_observe": self.wait_and_observe,  # 包装：固定传 master_session_id
            "call_vision_agent": self._conv_tools.call_vision_agent,
            "list_stickers": self._conv_tools.list_stickers,
            "experience_agent": self._conv_tools.experience_agent,
            "emotional_judgment": self.emotional_judgment,
        }
        logger.info(
            f"SocialChatToolManager 已初始化: level={self.enforcement_level}, "
            f"master={self.social_chat_core.master_session_id}, working={self.social_chat_core.working_session_id}, "
            f"preset_count={len(self._preset_messages)}"
        )

    async def wait_and_observe(self, seconds: float) -> Dict[str, Any]:
        """social_chat 等待并监听 working_session_id 对应会话的新消息。

        实现对齐 conversation_tool_manager.wait_and_observe：
            - 会话 ID 从 social_chat_core.working_session_id 取（single source of truth）；
            - 监听检测走 social_chat_input_manager.has_new_messages（消费式 unread 队列语义）；
            - 在 social_chat_agent_with_master 路径下 working_session_id == master_session_id，
              此时 wait_and_observe 监听的就是主人会话——文案如实显示"主人会话"。

        Args:
            seconds: 最长等待秒数（默认工具 schema 限制 10-30）。
        """
        import asyncio
        import time
        from datetime import datetime as _dt

        working_id = self.social_chat_core.working_session_id
        if not working_id:
            raise RuntimeError(
                "wait_and_observe 找不到 working_session_id。"
                "请确认 social_chat_core.start() 已正确注入 working_session_id。"
            )
        master_id = self.social_chat_core.master_session_id
        # 会话文案：with_master 路径下 working_id == master_id，渲染成"主人会话"；
        # 其他路径渲染成"工作会话"。
        session_label = "主人会话" if working_id == master_id else "工作会话"

        from brain.chat.social_chat.social_chat_input_manager import get_social_chat_input_manager
        input_manager = get_social_chat_input_manager()

        start_time = time.time()
        elapsed = 0

        while elapsed < seconds:
            await asyncio.sleep(min(1.0, seconds - elapsed))
            elapsed = time.time() - start_time
            # 同时检测 working_id 与 master_id；任一命中即中断，并显示命中的会话线。
            # with_master 路径下 working_id == master_id，会命中两次，但只在 first hit 触发。
            master_check = await input_manager.has_new_messages(session_id=master_id)
            if master_check.get("has_new"):
                current_time = _dt.now().strftime("%H:%M:%S")
                result_str = (
                    f"检测到主人会话新消息，中断等待，"
                    f"总共等待 {int(elapsed)} 秒，当前时间：{current_time}"
                )
                logger.info(f"social_chat wait_and_observe 命中(master): {result_str}")
                return result_str
            if working_id != master_id:
                working_check = await input_manager.has_new_messages(session_id=working_id)
                if working_check.get("has_new"):
                    current_time = _dt.now().strftime("%H:%M:%S")
                    result_str = (
                        f"检测到工作会话新消息，中断等待，"
                        f"总共等待 {int(elapsed)} 秒，当前时间：{current_time}"
                    )
                    logger.info(f"social_chat wait_and_observe 命中(working): {result_str}")
                    return result_str

        current_time = _dt.now().strftime("%H:%M:%S")
        result_str = f"等待观察完成，总共等待 {int(elapsed)} 秒，当前时间：{current_time}"
        logger.info(result_str)
        return result_str

    # ============================================================
    # 工具 schema
    # ============================================================
    def get_tools(self) -> List[Dict[str, Any]]:
        level = self.enforcement_level
        target_segment_desc = (
            "单 key 段（text 或 img），key 即段类型。"
            "text: 文字内容；img: 图片文件名（不含路径），必须与消息中 [图片:xxx] 的 xxx 一致。"
        )

        tools: List[Dict[str, Any]] = []

        # 1. send_to_master - 永远存在，路由固定到主人
        tools.append({
            "type": "function",
            "function": {
                "name": "send_to_master",
                "description": (
                    "向主人私聊会话发送消息，用于与主人沟通、提议、反馈、询问等。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "segments": {
                            "type": "array",
                            "maxItems": 9,
                            "description": "消息段数组，按顺序拼接显示。最多 9 段。",
                            "items": {
                                "type": "object",
                                "description": target_segment_desc,
                                "properties": {
                                    "text": {"type": "string"},
                                    "img":  {"type": "string"},
                                },
                                "additionalProperties": False,
                                "minProperties": 1,
                                "maxProperties": 1,
                            },
                        }
                    },
                    "required": ["segments"],
                },
            },
        })

        # 2. query_history - 永远存在，路由固定到工作会话
        tools.append({
            "type": "function",
            "function": {
                "name": "query_history",
                "description": (
                    "主动查询工作会话最近的消息。"
                    "默认 last_n 形式。返回的消息按时间正序排列。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "search_range": {
                            "type": "string",
                            "description": "格式 'last_n'，n 为消息条数，例如 'last_10'。",
                        }
                    },
                    "required": ["search_range"],
                },
            },
        })

        # 3. send_preset_to_target - L3 拦截；L2 / L1 不拦截
        tools.append({
            "type": "function",
            "function": {
                "name": "send_preset_to_target",
                "description": (
                    "从预设条目池中按 item_index 选取一条发送到工作会话。"
                    "item_index 是 1-based 下标（首条 user 消息中的【预设条目池】列出）。"
                    "已发送过的下标或超出范围的下标都会抛错。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_index": {
                            "type": "integer",
                            "description": "1-based 下标，对应首条 user 消息中的【预设条目池】。",
                        }
                    },
                    "required": ["item_index"],
                },
            },
        })

        # 4. construct_preset_message - 永远存在；L3 / L2 拦截；L1 自由
        tools.append({
            "type": "function",
            "function": {
                "name": "construct_preset_message",
                "description": (
                    "在对话过程中临时构造一条新的预设消息，以加入待发送序列。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "segments": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 1,
                            "description": "单段（text 或 img）的新条目。",
                            "items": {
                                "type": "object",
                                "description": target_segment_desc,
                                "properties": {
                                    "text": {"type": "string"},
                                    "img":  {"type": "string"},
                                },
                                "additionalProperties": False,
                                "minProperties": 1,
                                "maxProperties": 1,
                            },
                        },
                        "rationale": {
                            "type": "string",
                            "description": "构造本条新条目的理由（用于给主人审阅）。",
                        },
                    },
                    "required": ["segments", "rationale"],
                },
            },
        })

        # 5. transfer_back_to_social_agent - 永远存在
        tools.append({
            "type": "function",
            "function": {
                "name": "transfer_back_to_social_agent",
                "description": "结束本次委托，把控制权交还给 social_agent。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "汇报给pfca agent的信息。不要带markdown格式。",
                        }
                    },
                    "required": ["summary"],
                },
            },
        })

        # 6. 继承 conversation 的工具（与 conversation_tool_manager schema 对齐）
        tools.append({
            "type": "function",
            "function": {
                "name": "wait_and_observe",
                "description": (
                    "等待一段时间，等待新消息。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "seconds": {
                            "type": "number",
                            "minimum": 10.0,
                            "maximum": 30.0,
                        }
                    },
                    "required": ["seconds"],
                },
            },
        })

        tools.append({
            "type": "function",
            "function": {
                "name": "call_vision_agent",
                "description": "图像识别 / 表情包管理 agent（复用 conversation 的 call_vision_agent）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_filename": {"type": "string"},
                        "context_hint": {"type": "string"},
                    },
                    "required": ["image_filename"],
                },
            },
        })

        tools.append({
            "type": "function",
            "function": {
                "name": "list_stickers",
                "description": "查询已保存的表情包库（复用 conversation 的 list_stickers）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string"},
                    },
                    "required": [],
                },
            },
        })

        tools.append({
            "type": "function",
            "function": {
                "name": "experience_agent",
                "description": (
                    "呼叫经验 agent。可传入当前应用场景返回匹配的经验内容；"
                    "也可向其反馈某些经验不足的情况。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scenario": {"type": "string"},
                    },
                    "required": ["scenario"],
                },
            },
        })

        tools.append({
            "type": "function",
            "function": {
                "name": "emotional_judgment",
                "description": (
                    "根据遭遇类型进行情感判断，构建情感思考。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_type": {
                            "type": "string",
                            "enum": ["master", "working"],
                            "description": (
                                "本次情感判断绑定的会话线（来源）。"
                                "'master' = 主人会话（与主人沟通引发的情感）；"
                                "'working' = 工作会话（与社交对象的互动 / "
                            ),
                        },
                        "act": {
                            "type": "string",
                            "description": (
                                "遭遇/行为类型，必须从下列三类合并列表中选择之一：\n"
                                "【external / 他人行为直接刺激】"
                                + '、'.join(ExternalInteractionAct.names()) + "\n"
                                "【evaluation / 他人对我能力/表现评价】"
                                + '、'.join(EvaluationAct.names()) + "\n"
                                "【self_action / 我主动选择的行为】"
                                + '、'.join(SelfActionAct.names())
                            ),
                            "enum": (
                                ExternalInteractionAct.names()
                                + EvaluationAct.names()
                                + SelfActionAct.names()
                            ),
                        },
                    },
                    "required": ["session_type", "act"],
                },
            },
        })

        return tools

    # ============================================================
    # 工具分发
    # ============================================================
    @async_handle_errors()
    async def handle_tool_call(self, tool_call) -> Dict[str, Any]:
        # 解析 tool_call 形态
        parsed = self._parse_tool_call(tool_call)
        if parsed is None:
            return self._tool_error_response(
                getattr(tool_call, "id", None) if hasattr(tool_call, "id") else None,
                "<unknown>",
                ValueError(f"无法解析工具调用格式: {tool_call}"),
            )
        tool_name = parsed["name"]
        tool_args_json = parsed["arguments"]
        tool_call_id = parsed["id"]

        try:
            tool_args = self._parse_arguments(tool_args_json)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"social_chat 工具 {tool_name} 参数解析失败: {e}")
            return self._tool_error_response(tool_call_id, tool_name, e)

        if tool_name not in self.tool_functions:
            err = ValueError(f"未知工具: {tool_name}")
            return self._tool_error_response(tool_call_id, tool_name, err)

        tool_function = self.tool_functions[tool_name]

        # 统一错误处理（参数错误回 LLM，其余异常上抛阻断）
        result = await self._safe_execute(
            tool_function, tool_call_id, tool_name, **tool_args
        )
        if isinstance(result, dict) and result.get("role") == "tool":
            # 出错时 _safe_execute 已返回 role=tool 的错误消息，直接透传
            return result

        return {
            "role": "tool",
            "name": tool_name,
            "content": json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result),
            "tool_call_id": tool_call_id,
        }

    # ============================================================
    # 工具实现
    # ============================================================
    async def send_to_master(self, segments: list) -> str:
        """复用 conversation 的 send_message 底层，固定路由到主人会话。"""
        from adapt import OutboundTarget, OutboundPayload, SessionScope
        from adapt.router import get_outbound_router
        from adapt.message_segments import text_segment, image_segment
        from brain.attachments import resolve_media_file, preflight
        from brain.chat.social_chat.social_chat_session import SocialChatSession

        if not segments:
            raise ValueError("segments 不能为空")

        # 解析为 segments
        out_segments = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            if "text" in seg:
                txt = str(seg.get("text") or "")
                if txt:
                    out_segments.append(text_segment(txt))
            elif "img" in seg:
                filename = str(seg.get("img") or "").strip()
                if not filename:
                    continue
                try:
                    real_path = resolve_media_file(filename)
                except (ValueError, FileNotFoundError, PermissionError) as e:
                    return f"发送失败：图片 {filename} 错误：{e}"
                check = preflight(real_path)
                if not check["ok"]:
                    return f"发送失败：{check['reason']}"
                out_segments.append(image_segment(file=check["path"]))

        if not out_segments:
            raise ValueError("没有任何有效段")

        # 解析路由（master_session_id 由 RoleConfig.MASTER_ID 派生，永远是 private_）
        msid = self.social_chat_core.master_session_id or ""
        if msid.startswith("group_"):
            scope, target_id = SessionScope.GROUP, msid.split("_", 1)[1]
        elif msid.startswith("private_"):
            scope, target_id = SessionScope.PRIVATE, msid.split("_", 1)[1]
        else:
            scope, target_id = SessionScope.PRIVATE, msid

        router = await get_outbound_router()

        sent_count = 0
        INTER_SEGMENT_GAP = 1.5

        for idx, seg in enumerate(out_segments):
            if seg.type == "text":
                per_seg_typing = len(seg.to_text()) * SocialChatSession.TYPING_DELAY_PER_CHAR
            elif seg.type == "image":
                per_seg_typing = 1.0
            else:
                per_seg_typing = 0.0
            per_seg_typing = min(per_seg_typing, 7.0)
            if per_seg_typing > 0:
                await asyncio.sleep(per_seg_typing)

            try:
                await router.send(
                    platform="qq",
                    target=OutboundTarget(scope=scope, target_id=target_id),
                    payload=OutboundPayload(segments=[seg]),
                )
            except Exception as e:
                logger.error(f"send_to_master 段 {idx+1} 路由失败: {e}")
                continue

            sent_count += 1
            if idx < len(out_segments) - 1:
                await asyncio.sleep(INTER_SEGMENT_GAP)

        if sent_count == 0:
            return "发送失败：所有段均未发出"
        return f"已发送给主人，当前时间 {datetime.now().strftime('%H:%M:%S')}"

    async def query_history(self, search_range: str) -> str:
        """复用 conversation 的 query_history 行为，路由固定到工作会话。"""
        from brain.pfc.pfca.pfca_tools import get_pfca_tool_manager
        return await get_pfca_tool_manager().query_history(
            session_id=self.social_chat_core.working_session_id,
            search_range=search_range,
        )

    async def send_preset_to_target(self, item_index: int) -> Dict[str, Any]:
        """从预设池发条目到工作会话。

        L3：先发审批请求给主人，等主人回复『同意』才真正发送（每次都需审批）。
        L2：本次会话内首次发送需主人回复『同意』，之后所有发送自动放行。
        L1：不拦截，直接发送。
        """
        if not isinstance(item_index, int):
            raise ValueError("item_index 必须是整数")
        if item_index < 1 or item_index > len(self._preset_messages):
            raise ValueError(f"item_index 越界：1..{len(self._preset_messages)}")
        if item_index in self._sent_indices:
            raise ValueError(f"条目 {item_index} 已被发送过")

        seg = self._preset_messages[item_index - 1]
        seg_text = (seg or {}).get("text") or f"[图片:{(seg or {}).get('img', '')}]"

        if self.enforcement_level == "L3":
            # L3：每次发送都需主人首条回复含『同意』才放行。
            approval = await self._request_master_approval(
                item_index=item_index,
                seg_text=seg_text,
                already_approved_message=None,
            )
            if approval != "approved":
                # 被拒 / 超时 → 不发送，返回结果给 LLM 决定下一步
                logger.warning(
                    f"social_chat L3 审批未通过（条目 {item_index}，"
                    f"approval={approval}），取消发送"
                )
                return {
                    "status": "rejected",
                    "item_index": item_index,
                    "approval": approval,
                    "reason": (
                        "主人首条回复不含『同意』，视为拒绝"
                        if approval == "rejected"
                        else "主人 15 秒内未回复，审批超时"
                    ),
                }
        elif self.enforcement_level == "L2":
            # L2：本次会话内仅首次需要主人回复『同意』；之后所有发送自动放行。
            if not getattr(self, "_l2_session_approved", False):
                approval = await self._request_master_approval(
                    item_index=item_index,
                    seg_text=seg_text,
                    already_approved_message=None,
                )
                if approval != "approved":
                    logger.warning(
                        f"social_chat L2 审批未通过（条目 {item_index}，"
                        f"approval={approval}），取消发送"
                    )
                    return {
                        "status": "rejected",
                        "item_index": item_index,
                        "approval": approval,
                        "reason": (
                            "主人首条回复不含『同意』，视为拒绝"
                            if approval == "rejected"
                            else "主人 15 秒内未回复，审批超时"
                        ),
                    }
                self._l2_session_approved = True
                logger.info("social_chat L2 等级：主人已批准，本次会话后续 send_preset_to_target 自动放行")

        # 真正发送
        message_id = await self._route_preset_segment(seg)
        self._sent_indices.add(item_index)
        logger.info(f"social_chat 已发送预设条目 {item_index}, message_id={message_id}")
        return {
            "status": "ok",
            "message_id": message_id,
            "item_index": item_index,
            "sent_at": time.time(),
        }

    def _resolve_working_session_name(self) -> str:
        """解析工作会话的展示名。

        优先取 global_session_state.pfca_sessions[*] 中匹配 working_session_id 的
        session_name；找不到则回退到 working_session_id 本身。
        """
        from brain.hc.global_session_state import get_global_session_state

        working_id = self.social_chat_core.working_session_id
        try:
            state = get_global_session_state()
            for s in state.get_sessions():
                if s.get("session_id") == working_id:
                    name = (s.get("session_name") or "").strip()
                    if name:
                        return name
                    break
        except Exception as e:
            logger.debug(f"social_chat 解析工作会话名失败: {e}")

        return working_id or "工作会话"

    async def _request_master_approval(
        self,
        item_index: int,
        seg_text: str,
        already_approved_message: Optional[str] = None,
    ) -> str:
        """向主人发审批请求后**轮询检查**主人会话里最新到达的那一条非空消息。

        判定规则：
            - 命中第一条非空 == "同意" → 返回 "approved"；
            - 命中第一条非空但 != "同意" → 立即返回 "rejected"；
            - 15 秒内**完全没有任何非空消息** → 返回 "timeout"。

        轮询策略：
            - 每一轮 `since_ts = time.time()`，窗口 = `[now, +∞)`；
            - 用 `fetch_message_first` 拉 since 之后的第一条非空消息；
              主人刚发的最新一条就是这一条（若主人尚未回复，窗口内为空 → 进入下一轮）；
            - 每 1.5 秒查一次，最长 15 秒；
            - 一旦命中立刻判定，不继续等。

        Args:
            item_index: 当前要发送的条目下标（仅用于日志/提示）。
            seg_text:  条目渲染文本（用于审批请求内容）。
            already_approved_message: 若调用方已确认主人事前批准，可传入人类可读的
                上下文信息写入日志；当前调用点未启用该参数，保留以备后续扩展。

        Returns:
            "approved" / "rejected" / "timeout"。调用方按 L3 拦截语义决定后续动作。
        """
        working_session_name = self._resolve_working_session_name()
        approval_prompt = (
            f"AI 想在{working_session_name}发送消息[{item_index}]：\n"
            f"「{seg_text}」\n"
            f"请回复『同意』批准。"
        )

        # 1) 先取 since_ts 锚点（固化），再发审批请求，然后轮询拉取 since 之后的第一条非空消息。
        #    锚点固化在发请求"前一刻"，每轮 since_ts 不变；窗口 = [anchor, +∞)，
        #    第一条非空消息就是主人刚发的最新一条（如果主人尚未回复，则窗口内为空，循环继续）。
        #    注意：AI 自己 outbound 的审批请求会被 _persist_outbound 以 user_id="system" 写入同一个 session，
        #    fetch_message_first 必须用 exclude_system 过滤掉，否则会被误判为主人的回复（!= "同意" → 立即 rejected）。
        from brain.chat.conversation.conversation_input_manager import get_conversation_input_manager
        input_manager = get_conversation_input_manager()

        # 关键：approval_anchor_ts 必须用 int(time.time()) 而不是 time.time()，
        # 因为消息入库时（_persist_outbound, message_handler）写的是 int(time.time())，
        # 而 time.time() 含小数位；当主人恰好在锚点之后的同秒回"同意"时，
        # 主人消息 timestamp（小数位被截断为整秒）会 < approval_anchor_ts（小数位 > 0），
        # 触发 fetch 闭区间下界把它踢掉，导致 timeout。
        # 再减 1s 留出缓冲区，覆盖 NapCat 时钟与本地时钟的微小抖动，
        # 主人刚发的"同意"即使同秒也能稳定命中 [anchor, +∞) 窗口。
        approval_anchor_ts = int(time.time()) - 1
        await self.send_to_master(segments=[{"text": approval_prompt}])
        await asyncio.sleep(3)  # 给主人反应时间，避免主人还没看到提示就触发轮询

        # 2) 轮询：每 1.5 秒查一次，最多 15 秒。
        #    - 命中第一条非空 == "同意" → 通过；
        #    - 命中第一条非空且 != "同意" → 立即拒绝；
        #    - 全程没有任何非空消息 → 超时。
        APPROVAL_TIMEOUT_S = 30.0
        POLL_INTERVAL_S = 1.5
        deadline = time.time() + APPROVAL_TIMEOUT_S

        first_msg: dict = {}
        # 收集每一轮命中过的消息：用于超时分支把"窗口内到底看到了谁"全部吐到日志，
        # 方便定位"以为没收到，其实是 content_text 不等于'同意'被 rejected"之类的诊断。
        every_hit_msgs: List[Dict[str, Any]] = []
        polls = 0
        while True:
            polls += 1
            check = await input_manager.fetch_message_first(
                session_id=self.social_chat_core.master_session_id,
                since_ts=approval_anchor_ts,
            )
            first_msg = check.get("first_message") or {}
            first_content = (
                (first_msg.get("content_text") or "").strip()
                if isinstance(first_msg, dict) else ""
            )
            if first_content:
                # 把命中消息全文细节打出来（含 user_id / timestamp / content / content_text），
                # 这样无论判定结果如何都能看到第一手数据，便于定位 bug。
                logger.info(
                    f"social_chat 审批轮询命中（条目 {item_index}，polls={polls}，"
                    f"anchor={approval_anchor_ts:.3f}）: "
                    f"raw_msg={json.dumps(first_msg, ensure_ascii=False)}"
                )
                # 命中后也要累积，这样即使下一句 break，超时分支也拿得到（虽然这一帧不会走到超时）
                every_hit_msgs.append(first_msg)
                break
            else:
                # 空命中：nothing landed in [anchor, +∞) yet
                logger.debug(
                    f"social_chat 审批轮询第 {polls} 轮空命中（条目 {item_index}，"
                    f"anchor={approval_anchor_ts:.3f}，now={time.time():.3f}）"
                )
            if time.time() >= deadline:
                # 超时分支：把窗口内全部 raw 消息（每轮 first_message 累积）打到日志，
                # 即便窗口始终空，也能从 anchor/nothing 之间的状态推断 bug 根因。
                every_hit_summary = "[]"
                try:
                    every_hit_summary = json.dumps(
                        every_hit_msgs, ensure_ascii=False, default=str
                    )
                except Exception:
                    pass
                logger.warning(
                    f"social_chat 审批超时（条目 {item_index}，anchor={approval_anchor_ts:.3f}，"
                    f"polls={polls}），窗口内累计命中消息: {every_hit_summary}"
                )
                return "timeout"
            await asyncio.sleep(POLL_INTERVAL_S)

        # 仅按 content_text 判定（不带 user_name 前缀），避免 _format_messages 拼的
        # "主人:同意" 形式被判为非"同意"而误判 rejected。
        # 主人也可能在回复时前后携带空格 / 换行 / 全角空格，已经在 fetch_message_first 里 strip 过。
        if first_content == "同意":
            sender = first_msg.get("user_name", "?")
            sender_id = first_msg.get("user_id", "?")
            ts = first_msg.get("timestamp", 0)
            logger.info(
                f"social_chat 审批通过（条目 {item_index}）: sender={sender!r}, "
                f"sender_id={sender_id}, timestamp={ts}, reply='{first_content[:40]}'"
            )
            return "approved"
        sender = first_msg.get("user_name", "?")
        sender_id = first_msg.get("user_id", "?")
        ts = first_msg.get("timestamp", 0)
        logger.warning(
            f"social_chat 审批被拒（条目 {item_index}，首条 ≠ 同意）: "
            f"sender={sender!r}, sender_id={sender_id}, timestamp={ts}, "
            f"reply='{first_content[:40]}'"
        )
        return "rejected"

    async def construct_preset_message(self, segments: list, rationale: str) -> Dict[str, Any]:
        """构造一条新条目并追加到预设池。

        本工具不发起审批：直接追加，保留 rationale 供 social_agent / LLM 后续回溯。
        send_preset_to_target 仍受 enforcement_level 影响（L3 强制审批）。
        """
        if not segments:
            raise ValueError("segments 不能为空")
        if not rationale or not rationale.strip():
            raise ValueError("rationale 必填")

        new_seg = segments[0]
        new_index = len(self._preset_messages) + 1

        self._preset_messages.append(new_seg)
        logger.info(
            f"social_chat 已新增预设条目 new_index={new_index}, rationale={rationale[:40]}"
        )
        return {
            "status": "ok",
            "new_index": new_index,
            "rationale": rationale,
        }

    async def transfer_back_to_social_agent(self, summary: str) -> Dict[str, Any]:
        """结束本次委托，触发 social_chat_core 的退出流程。"""
        # 把 summary 写入 core，供 stop_session / 返回结果使用
        if self.social_chat_core is not None:
            self.social_chat_core.set_transfer_summary(summary or "")
            await self.social_chat_core.request_stop()

    async def emotional_judgment(self, session_type: str, act: str) -> str:
        """按 session_type 路由的情感判断。

        与 conversation 版本的差异：
            - 会话上下文从 social_chat_core 取（master_session_id / working_session_id），
              而不是从 global_session_state["conversation_session"] 取；
            - encounters 写到 social_chat_core 内部的 master_encounters /
              working_encounters 两个独立列表，按 session_type 分别累计，
              不再走 conversation_core.set_emotion_evaluation 污染 conversation 状态；
            - 返回文案格式与 conversation 一致：
              "因为{原因}，你的心情变化为：{去甲肾上腺素描述}。{多巴胺描述}"。

        Args:
            session_type: 'master' / 'working'。来源（情感判断发生在哪条会话线）：
                'master'  = 主人私聊会话（与主人沟通引发的情感）；
                'working' = 工作会话（与社交对象的互动 / 通过预设条目发出的发言引发的情感）。
            act: 遭遇/行为标签，必须从 ExternalInteractionAct + EvaluationAct +
                SelfActionAct 三类合并列表中选择之一（schema 已用 enum 强制）。
        """
        if session_type not in ("master", "working"):
            raise ValueError(
                f"emotional_judgment session_type 非法: {session_type!r}"
                "（仅支持 'master' / 'working'）"
            )
        if not act or not act.strip():
            raise ValueError("act 必填")

        # 工具层白名单兜底：即使 schema 已用 enum 限制，这里再校验一次，
        # 防止 schema 被绕过（手动调用 / 旧 prompt 残留）导致脏数据进入 emotional_interface。
        # 不在白名单直接抛错阻断（符合 user_rules：脏数据不污染情绪状态）。
        from brain.amy.constants import ACT_EFFECTS
        if act not in ACT_EFFECTS:
            raise ValueError(
                f"emotional_judgment act 未注册: {act!r}"
                "（必须从 ExternalInteractionAct / EvaluationAct / SelfActionAct 中选择）"
            )

        from brain.amy.emotional_interface import get_emotional_interface

        ei = await get_emotional_interface()

        # 解析来源 session_type / session_name：
        #   master → private_<MASTER_ID>，名字用「主人」
        #   working → working_session_id，名字走 global_session_state.pfca_sessions 反查
        if session_type == "master":
            core_session_id = self.social_chat_core.master_session_id
            session_name = "主人"
        else:
            core_session_id = self.social_chat_core.working_session_id or ""
            session_name = self._resolve_working_session_name()

        # session_id 已是 type_id 格式，前缀即为 session_type（喂给 build_conversation_thought）
        sub_session_type = core_session_id.split("_", 1)[0] if "_" in core_session_id else ""

        success, msg, _prompt = await ei.build_conversation_thought(
            act=act,
            session_type=sub_session_type,
            session_name=session_name,
        )

        # 写入 social_chat_core 的对应缓存（不污染 conversation 状态）
        self.social_chat_core.add_emotion_encounter(session_type=session_type, act=act)

        if success:
            snapshot = await ei.thought_cache.calculate_mood_snapshot()
            from brain.amy.prompt_engine import get_dopamine_desc, get_norepinephrine_desc
            reasons = [t.desc for t in snapshot.top_thoughts[:2] if t.desc]
            reasons_text = "、".join(reasons) if reasons else act
            new_feeling = (
                f"你的心情变化为：{get_norepinephrine_desc(snapshot.norepinephrine)}。"
                f"{get_dopamine_desc(snapshot.dopamine)}"
            )
            logger.info(
                f"social_chat emotional_judgment 完成: session_type={session_type}, "
                f"act={act}, session={sub_session_type}/{session_name}"
            )
            return f"因为{reasons_text}，{new_feeling}"
        # build_conversation_thought 失败：act 已在白名单内，理论上不应到这里；
        # 真发生则按 user_rules 原则直接抛错阻断，不静默吞。
        raise RuntimeError(
            f"emotional_judgment 失败: session_type={session_type}, act={act}, msg={msg}"
        )

    # ---------- 继承工具：复用 conversation_tool_manager 的同名方法，不重写 ----------

    # ============================================================
    # 内部辅助
    # ============================================================
    async def _route_preset_segment(self, seg: dict) -> str:
        """通过 OutboundRouter 发送一条预设段（text / img）到工作会话。"""
        from adapt import OutboundTarget, OutboundPayload, SessionScope
        from adapt.router import get_outbound_router
        from adapt.message_segments import text_segment, image_segment
        from brain.attachments import resolve_media_file, preflight

        tsid = self.social_chat_core.working_session_id or ""
        if tsid.startswith("group_"):
            scope, target_id = SessionScope.GROUP, tsid.split("_", 1)[1]
        elif tsid.startswith("private_"):
            scope, target_id = SessionScope.PRIVATE, tsid.split("_", 1)[1]
        else:
            scope, target_id = SessionScope.PRIVATE, tsid

        out_seg = None
        if "text" in seg:
            out_seg = text_segment(str(seg.get("text") or ""))
        elif "img" in seg:
            filename = str(seg.get("img") or "").strip()
            real_path = resolve_media_file(filename)
            check = preflight(real_path)
            if not check["ok"]:
                raise ValueError(f"图片 {filename} preflight 失败: {check['reason']}")
            out_seg = image_segment(file=check["path"])

        if out_seg is None:
            raise ValueError("预设条目段无效")

        router = await get_outbound_router()
        return await router.send(
            platform="qq",
            target=OutboundTarget(scope=scope, target_id=target_id),
            payload=OutboundPayload(segments=[out_seg]),
        )