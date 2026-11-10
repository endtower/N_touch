# -*- coding: utf-8 -*-
"""social_chat 工具集：send_to_master / query_history / send_to_target / send_image_to_target / set_send_mode / transfer_back + 通用工具（common_ways）。

设计要点：
- 发送工具收口：文字与图片拆分——send_to_master / send_to_target 只发纯文字
  （message 换行自动拆成多条独立消息，委托 ToolConversation 统一执行）；
  图片走 send_image_to_target（图先发，配文随后单独一条）。
- 逐条审批和自由发送共用权限服务。自由发送可以按链或限时授权，并单独选择发送后知会。
- 审批轮询基于消息库 seq 游标（consumer="social_chat_approval"），纯单调序号判定，
  不涉及时钟，根治"检测到主人上一条消息"的时序错位 bug。
- LLM 看不到拦截层内部；拦截以"工具返回值"的形式对 LLM 暴露。
- 情感遭遇拆分登记（对齐 conversation 机制）：self_action 类随发送工具的 nature
  参数自动登记（send_to_master → master 线 / send_to_target、send_image_to_target → working 线）；
  evaluation 类随 transfer_back 的 encounter_type 登记（working 线）；
  emotional_judgment 显式工具仅收 external（他人刺激）类 act。
"""

import json
import asyncio
import time
from datetime import datetime
from typing import Dict, Any, List, Optional

from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from brain.base import ToolManager, ToolHandlers, ToolConversation, ToolHC, ToolStickers, ToolVision
from adapt.message_segments import text_segment
from adapt.bridge.contract import SendReceipt
from adapt.session_identity import parse_session_id
from brain.hc.event_chain_permissions import MAX_DURATION_MINUTES, SocialSendPolicy
from .social_chat_approval import FreeSendOptions, parse_free_send_reply
from config.settings import chat_config
from brain.amy.constants import ExternalInteractionAct, EvaluationAct, SelfActionAct

logger = get_logger('SocialChatToolManager', "INFO")

# 审批轮询专用消费者游标（消息库 cursors 表，per session_id 隔离）
APPROVAL_CONSUMER_ID = "social_chat_approval"
VALID_SEND_MODES = ("approval", "unrestricted")

# 审批参数
APPROVAL_TIMEOUT_S = 60.0
PERMISSION_APPROVAL_TIMEOUT_S = 60.0
APPROVAL_POLL_INTERVAL_S = 1.5


class SocialChatToolManager(ToolManager):
    """social_chat 工具管理器。

    使用流程：
        mgr = SocialChatToolManager(social_chat_core=core)
        mgr.get_tools() -> List[Dict]
        result = await mgr.handle_tool_call(tool_call)
    """

    def __init__(
        self,
        social_chat_core,
    ):
        """构造工具集。

        注：master_session_id / working_session_id 均不作为入参——
        通过 self.social_chat_core.master_session_id / working_session_id 读取
        （master 派生自 RoleConfig.MASTER_ID；working 由 social_chat_core.start() 注入）。

        发送模式由当前事件链统一持有，重新进入同一目标会话继承授权。
        """
        self.social_chat_core = social_chat_core
        from brain.hc.global_session_state import get_global_session_state
        self._permissions = get_global_session_state().get_event_chain_permissions()
        self._permission_scope = (
            social_chat_core.master_session_id, social_chat_core.working_session_id
        )

        # 本次 run 实际发出的消息记录（core._build_result 汇总用）
        self._messages_sent: List[Dict[str, Any]] = []

        # 通用工具（common_ways）：social_chat 与 conversation 平级，
        # 通用能力（vision / stickers / 经验 / wait）直接走 brain/base/common_ways，
        # 不再实例化 ConversationToolManager 做代理。
        # 工具函数表
        self.tool_functions = {
            "send_to_master": self.send_to_master,
            "query_history": self.query_history,
            "send_to_target": self.send_to_target,
            "send_image_to_target": self.send_image_to_target,
            "set_send_mode": self.set_send_mode,
            "transfer_back": self.transfer_back,
            "wait_and_observe": self.wait_and_observe,  # 包装：固定传 master_session_id
            "search_stickers": ToolStickers.search_stickers,
            "manage_sticker": ToolStickers.manage_sticker,
            "inspect_image": ToolVision.inspect_image,
            "experience_agent": self.experience_agent,
            "emotional_judgment": self.emotional_judgment,
            "record_to_notepad": self.record_to_notepad,
            "query_note": self.query_note,
        }
        logger.info(
            f"SocialChatToolManager 已初始化: send_mode={self.send_mode}, "
            f"master={self.social_chat_core.master_session_id}, "
            f"working={self.social_chat_core.working_session_id}"
        )

    def _require_permission_scope(self) -> None:
        self._permissions.require_active()
        if self._permission_scope != (
            self.social_chat_core.master_session_id, self.social_chat_core.working_session_id
        ):
            raise RuntimeError("Social Chat 会话已切换，旧工具实例不能继续发送")

    @property
    def send_mode(self) -> str:
        return self._send_policy().mode

    def _send_policy(self) -> SocialSendPolicy:
        self._require_permission_scope()
        return self._permissions.get_send_policy(*self._permission_scope)

    @staticmethod
    def _expired_approval() -> Dict[str, Any]:
        return {"result": "expired", "content": "", "reason": "事件链已结束，授权及待审批请求均已失效"}

    async def wait_and_observe(self, seconds: float) -> str:
        """social_chat 等待并监听会话新消息（先观察，后等待）。

        实现对齐 conversation_tool_manager.wait_and_observe，差异点：
            - 先观察：调用即检测一次两条会话线——用户消息可能在本工具调用前
              就已到达（如上一轮 LLM 生成回复期间），命中则立即返回，不空等；
            - 会话 ID 从 social_chat_core.working_session_id 取（single source of truth）；
            - 监听检测走 social_chat_input_manager.has_new_messages（消费式 unread 队列语义）；
            - 在 social_chat_agent_with_master 路径下 working_session_id == master_session_id，
              此时 wait_and_observe 监听的就是主人会话——文案如实显示"主人会话"。

        Args:
            seconds: 最长等待秒数（默认工具 schema 限制 10-30）。
        """
        working_id = self.social_chat_core.working_session_id
        if not working_id:
            raise RuntimeError(
                "wait_and_observe 找不到 working_session_id。"
                "请确认 social_chat_core.start() 已正确注入 working_session_id。"
            )
        master_id = self.social_chat_core.master_session_id

        from brain.chat.social_chat.social_chat_input_manager import get_social_chat_input_manager
        input_manager = get_social_chat_input_manager()

        async def _probe_hit_line() -> Optional[str]:
            """同时检测 master 与 working 两条会话线，返回命中线的文案；未命中返回 None。

            with_master 路径下 working_id == master_id，只检测一次（避免重复探测）。
            """
            master_check = await input_manager.has_new_messages(session_id=master_id)
            if master_check.get("has_new"):
                return "主人会话"
            if working_id != master_id:
                working_check = await input_manager.has_new_messages(session_id=working_id)
                if working_check.get("has_new"):
                    return "工作会话"
            return None

        # 先观察：进入即检测一次，命中说明用户消息在调用前已到达，立即反馈不空等
        hit_line = await _probe_hit_line()
        if hit_line:
            current_time = datetime.now().strftime("%H:%M:%S")
            result_str = f"检测到{hit_line}新消息，无需等待，当前时间：{current_time}"
            logger.info(f"social_chat wait_and_observe 先观察命中({hit_line}): {result_str}")
            return result_str

        # 后等待：逐秒轮询，任一线命中即中断
        start_time = time.time()
        elapsed = 0

        while elapsed < seconds:
            await asyncio.sleep(min(1.0, seconds - elapsed))
            elapsed = time.time() - start_time
            hit_line = await _probe_hit_line()
            if hit_line:
                current_time = datetime.now().strftime("%H:%M:%S")
                result_str = (
                    f"检测到{hit_line}新消息，中断等待，"
                    f"总共等待 {int(elapsed)} 秒，当前时间：{current_time}"
                )
                logger.info(f"social_chat wait_and_observe 等待命中({hit_line}): {result_str}")
                return result_str

        current_time = datetime.now().strftime("%H:%M:%S")
        result_str = f"等待观察完成，总共等待 {int(elapsed)} 秒，当前时间：{current_time}"
        logger.info(result_str)
        return result_str

    # ============================================================
    # 通用工具（common_ways）：与 conversation 平级，直接委托共享实现
    # ============================================================

    async def experience_agent(self, scenario: str, category: str = "话术") -> str:
        """经验 agent（social_chat 自有包装，与 conversation 平级）。

        检索核心走 common_ways.search_speech_bequests；category 可选「话术」/「风格」。
        本模块差异点：
        - 激活缓存与 social_chat_input_manager 的首条经验激活共用同一份
          （get_social_chat_input_manager()._activated_bequests），保证 run 内不重复注入；
        - 不附带情感遭遇副作用（检索类操作不做遭遇登记，避免脏数据污染情绪评估）。
        """
        from brain.base.common_ways.bequest_helpers import search_speech_bequests
        from brain.base.common_ways.tool_bequest import ToolBequest
        from brain.chat.social_chat.social_chat_input_manager import get_social_chat_input_manager

        db_category = ToolBequest._resolve_category(category)
        activated = get_social_chat_input_manager()._activated_bequests
        matched = await search_speech_bequests(
            scenario, skip=lambda bequest_id: bequest_id in activated,
            top_k=5, category=db_category,
        )
        for entry in matched:
            activated.add(entry["id"])

        if not matched:
            return "你什么计策都没想起来。"

        return "\n".join(f"{entry['title']} - {entry['summary']}" for entry in matched)

    async def record_to_notepad(self, text: str, category: str) -> str:
        """记事本（灵光一现）写入：与 conversation 共用 ToolHC.record_note。

        仅收纯文本与类别标签；元数据由系统侧自动附加（来源 social_chat，
        会话取工作会话——素材产生地），记录进当前事件链的缓存文档，
        PFCA 真正结束时统一以事件标题命名落库。
        """
        note_id = await ToolHC.record_note(
            text=text,
            category=category,
            source="social_chat",
            session_id=self.social_chat_core.working_session_id,
            session_name="",
        )
        return f"已记录至记事本（{note_id}）。"

    async def query_note(self, title: str) -> str:
        """按章节标题查询一篇普通笔记的完整内容，委托通用 ToolHC.query_note。

        章节标题取自 list_notes 的普通笔记返回；见闻录 / 任务相关笔记等
        特殊槽位由 list_notes 直接给全，不经此工具。
        """
        return await ToolHC.query_note(title)

    # ============================================================
    # 工具 schema
    # ============================================================
    def get_tools(self) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []

        # 1. send_to_master - 永远存在，路由固定到主人
        tools.append({
            "type": "function",
            "function": {
                "name": "send_to_master",
                "description": (
                    "向主人私聊会话发送消息，用于与主人沟通、提议、反馈、询问等。"
                    "每个LLM执行轮次只能调用一次文字发送工具。换行分段，一次最多3条，超出部分不发送。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "要发送的文字内容。用换行分隔多条短句。禁止为空。",
                        },
                        "nature": {
                            "type": "string",
                            "description": (
                                "本条发言的性质（自行为类情感标签，可选）。从下列列表选择：\n"
                                + '、'.join(SelfActionAct.names())
                            ),
                            "enum": SelfActionAct.names(),
                        }
                    },
                    "required": ["message"],
                },
            },
        })

        # 2. query_history - 永远存在，路由固定到工作会话
        tools.append({
            "type": "function",
            "function": {
                "name": "query_history",
                "description": (
                    "查询工作会话的历史消息。返回按时间正序、带序号(#seq)与"
                    "时间戳的消息列表。两种拉取模式二选一：看最近反应用 "
                    "search_range（数量模式）；看某段时间的记录用 time_range"
                    "（时间范围模式，传入后忽略 search_range）；"
                    "可选 speaker 只看目标人物的发言。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "search_range": {
                            "type": "string",
                            "description": (
                                "数量模式拉取条数：'last_n' 表示最新 n 条（如 'last_20'，默认 last_20），"
                                "'all' 表示尽量多拉（上限 100 条）。传了 time_range 时忽略本参数。"
                            ),
                        },
                        "time_range": {
                            "type": "string",
                            "description": (
                                "时间范围模式：相对时长如 '30m' / '2h' / '3d'（最近该时段），"
                                "或绝对区间 'YYYY-MM-DD HH:MM~YYYY-MM-DD HH:MM'（一端可省略）。"
                            ),
                        },
                        "speaker": {
                            "type": "string",
                            "description": (
                                "可选，筛选目标人物的发言：只返回该人物发出的消息"
                                "（名字模糊匹配，可与数量 / 时间范围组合）。"
                            ),
                        }
                    },
                },
            },
        })

        # 3. send_to_target - 对外发送核心工具（纯文字）；approval 模式下工具层硬审批
        tools.append({
            "type": "function",
            "function": {
                "name": "send_to_target",
                "description": (
                    "向工作会话发送文字消息（你要对其他人说的话）。"
                    "每个LLM执行轮次只能调用一次文字发送工具。换行分段，一次最多3条，超出部分不发送。"
                    "审批模式下，系统会先把消息全文发给主人审批，主人回复『同意』后才真正发出；"
                    "主人回复其他任何内容或超时都会拒绝本次发送。自由模式下直接发出。"
                    "发图片用 send_image_to_target。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "要发送的文字内容。用换行分隔多条短句。禁止为空。",
                        },
                        "nature": {
                            "type": "string",
                            "description": (
                                "本条发言的性质（自行为类情感标签，可选）。从下列列表选择：\n"
                                + '、'.join(SelfActionAct.names())
                            ),
                            "enum": SelfActionAct.names(),
                        }
                    },
                    "required": ["message"],
                },
            },
        })

        # 3.1 send_image_to_target - 对外发送图片，与文字发送分离
        tools.append({
            "type": "function",
            "function": {
                "name": "send_image_to_target",
                "description": "向工作会话发送图片。审批模式下同样先经主人审批。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image": {
                            "type": "string",
                            "description": (
                                "图片文件名（不含路径），必须与消息文本中 [图片:xxx] 的 xxx 一致。"
                            ),
                        },
                        "nature": {
                            "type": "string",
                            "description": (
                                "本条发送的性质（自行为类情感标签，可选）。从下列列表选择：\n"
                                + '、'.join(SelfActionAct.names())
                            ),
                            "enum": SelfActionAct.names(),
                        }
                    },
                    "required": ["image"],
                },
            },
        })

        # 4. set_send_mode - 发送模式切换（升级需主人『同意』）
        tools.append({
            "type": "function",
            "function": {
                "name": "set_send_mode",
                "description": (
                    "为当前工作会话申请自由发送，或恢复逐条审批。"
                    "申请时可以建议时长和发送后的知会方式，最终以主人私聊回复为准。"
                    "限时授权可跨事件链，重启后失效。"
                    "恢复逐条审批会撤销当前会话的自由发送授权。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": list(VALID_SEND_MODES),
                            "description": "目标模式：approval=逐条审批；unrestricted=自由发送。",
                        },
                        "duration_minutes": {
                            "type": "integer", "minimum": 1, "maximum": MAX_DURATION_MINUTES,
                            "description": "建议的自由发送时长（分钟）。省略则建议仅本事件链。",
                        },
                        "notify": {
                            "type": "boolean",
                            "description": "建议每次成功自由发送后是否自动知会主人。省略则采用配置默认值。",
                        },
                    },
                    "required": ["mode"],
                },
            },
        })

        # 5. transfer_back - 永远存在
        tools.append({
            "type": "function",
            "function": {
                "name": "transfer_back",
                "description": "结束本次委托，把控制权交还给 PFCA。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "汇报给pfca agent的信息。不要带markdown格式。",
                        },
                        "encounter_type": {
                            "type": "string",
                            "description": (
                                "本次任务中他人对我能力/表现的评价类型"
                                "（评价类情感标签，可选）。从下列列表选择：\n"
                                + '、'.join(EvaluationAct.names())
                            ),
                            "enum": EvaluationAct.names(),
                        },
                        "failure_reason": {"type": "string", "description": "选择 AMY 失败评价分区时填写表现差原因；本地缓存至下次维护申请，发布后归入该申请并清空缓存。"},
                    },
                    "required": ["summary"],
                },
            },
        })

        # 6. 通用工具（common_ways 共享 schema，与 conversation 平级）
        tools.append(ToolHandlers.get_wait_and_observe_tool_schema())

        tools.extend([
            ToolStickers.get_search_stickers_tool_schema(),
            ToolStickers.get_manage_sticker_tool_schema(),
            ToolVision.get_inspect_image_tool_schema(),
        ])

        tools.append({
            "type": "function",
            "function": {
                "name": "experience_agent",
                "description": (
                    "呼叫经验 agent。可传入当前应用场景返回匹配的经验内容；"
                    "「话术」返回可直接说出的固定说法，「风格」返回可模仿的他人原话样本。"
                    "也可向其反馈某些经验不足的情况。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scenario": {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": ["话术", "风格"],
                            "description": "经验类别，默认「话术」。",
                        },
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
                                "遭遇/行为类型，仅收他人行为直接刺激类"
                                "（自行为随发送工具的 nature、他人评价随 transfer_back 的"
                                "encounter_type 自动登记，无需在此调用）。从下列列表选择：\n"
                                + '、'.join(ExternalInteractionAct.names())
                            ),
                            "enum": ExternalInteractionAct.names(),
                        },
                    },
                    "required": ["session_type", "act"],
                },
            },
        })

        # 7. 记事本（灵光一现，shortlight）：统一 schema（ToolHC 统一管理）；
        #    登记元数据由 record_to_notepad 委托注入（source=social_chat，会话取工作会话）
        tools.append(ToolHC.get_record_note_tool_schema())
        tools.append(ToolHC.get_query_note_tool_schema())

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
    async def send_to_master(self, message: str, nature: str = None) -> str:
        """固定路由到主人会话（委托 ToolConversation.send_message，message 换行自动分条）。

        nature: 本条发言的性质（SelfActionAct 自行为类，可选）——发送成功后自动
            登记一次自行为情感判断（master 线）。内部调用（审批请求 / 知会）不传。
        """
        nature = (nature or "").strip() or None
        if nature and nature not in SelfActionAct.names():
            raise ValueError(
                f"send_to_master nature 未注册: {nature!r}（必须从 SelfActionAct 中选择）"
            )

        # master_session_id 由 RoleConfig.MASTER_ID 派生，永远是 private_
        from brain.chat.social_chat.social_chat_session import SocialChatSession
        msid = self.social_chat_core.master_session_id or ""
        result = await ToolConversation.send_message(
            session_id=msid,
            message=message,
            typing_delay_per_char=SocialChatSession.TYPING_DELAY_PER_CHAR,
        )

        # 发言性质登记（整批作为一个发言动作登记一次；发送已成功，登记失败仅告警）
        if nature:
            try:
                await self.emotional_judgment(session_type="master", act=nature)
            except Exception as e:
                logger.warning(f"send_to_master 性质登记情感判断失败（不影响主流程）: {e}")

        return result

    async def query_history(
        self,
        search_range: str = "last_20",
        time_range: Optional[str] = None,
        speaker: Optional[str] = None,
    ) -> str:
        """查工作会话的历史消息（委托通用 ToolSocial.query_history，路由固定到工作会话）。

        consumer_id 传 CONVERSATION_CONSUMER_ID：social_chat 与 conversation 共用游标，
        LLM 已读到的消息 conversation 不会重复拉取。游标仅在"无 speaker、无 time_range
        的最新拉取"时推进——人物筛选/时间范围查询 LLM 未看全最新消息，不代为标已读。
        """
        from brain.base import ToolSocial, CONVERSATION_CONSUMER_ID
        return await ToolSocial.query_history(
            session_id=self.social_chat_core.working_session_id,
            search_range=search_range,
            time_range=time_range,
            speaker=speaker,
            consumer_id=CONVERSATION_CONSUMER_ID,
        )

    async def send_to_target(self, message: str, nature: str = None) -> Dict[str, Any]:
        """向工作会话发送文字消息（委托 ToolConversation.send_message，换行自动分条）。

        approval 模式：先发审批请求给主人，主人首条非空回复 ==『同意』才放行；
        其他回复立即拒绝；超过审批时限则超时拒绝。拒绝 / 超时不真正发送，返回结果给 LLM。
        unrestricted 模式：直接发送；按当前授权决定是否知会主人。

        nature: 本条发言的性质（SelfActionAct 自行为类，可选）——实际发出后自动
            登记一次自行为情感判断（working 线）；被拒 / 超时不登记。
        """
        if not (message or "").strip():
            raise ValueError("message 为空，禁止发送空消息")

        nature = (nature or "").strip() or None
        if nature and nature not in SelfActionAct.names():
            raise ValueError(
                f"send_to_target nature 未注册: {nature!r}（必须从 SelfActionAct 中选择）"
            )

        preview = message
        policy, rejected = await self._approval_gate(preview)
        if rejected is not None:
            return rejected
        assert policy is not None

        tsid = self.social_chat_core.working_session_id or ""
        from brain.chat.social_chat.social_chat_session import SocialChatSession
        sent_text = []
        result = await ToolConversation.send_message(
            session_id=tsid,
            message=message,
            typing_delay_per_char=SocialChatSession.TYPING_DELAY_PER_CHAR,
            on_sent=sent_text.append,
            before_send=lambda: self._check_send_authority(policy),
        )
        if not sent_text:
            return {"status": "pending", "result": result}
        sent_at = await self._finish_target_send(tsid, sent_text[0], nature, notify=policy.notify)

        return {"status": "ok", "sent_at": sent_at, "result": result}

    async def send_image_to_target(self, image: str, nature: str = None) -> Dict[str, Any]:
        """向工作会话发送图片，与文字发送分离的专用工具。

        图片解析 + preflight 前置于审批闸门：文件名有问题先失败，不打扰主人。
        approval 模式下审批预览为 "[图片:文件名]"。

        nature: 同 send_to_target（working 线）。
        """
        filename = (image or "").strip()
        if not filename:
            raise ValueError("image 文件名为空")

        nature = (nature or "").strip() or None
        if nature and nature not in SelfActionAct.names():
            raise ValueError(
                f"send_image_to_target nature 未注册: {nature!r}（必须从 SelfActionAct 中选择）"
            )

        # 图片解析前置于审批闸门（resolve + preflight，失败 raise ValueError 由 LLM 自纠）
        image_seg = ToolConversation._build_image_segment(filename)
        preview = f"[图片:{filename}]"

        policy, rejected = await self._approval_gate(preview)
        if rejected is not None:
            return rejected
        assert policy is not None

        tsid = self.social_chat_core.working_session_id or ""
        scope, target_id = ToolConversation._resolve_target(tsid)
        from brain.chat.social_chat.social_chat_session import SocialChatSession
        receipts = await ToolConversation.send_segments_with_typing(
            scope, target_id, [image_seg],
            typing_delay_per_char=SocialChatSession.TYPING_DELAY_PER_CHAR,
            session_id=tsid,
            before_send=lambda: self._check_send_authority(policy),
        )
        bridge_receipts = [receipt for receipt in (receipts or []) if isinstance(receipt, SendReceipt)]
        if any(receipt.status != "observed" for receipt in bridge_receipts):
            return {"status": "pending", "result": ToolConversation._render_bridge_result(bridge_receipts)}
        sent_at = await self._finish_target_send(tsid, preview, nature, notify=policy.notify)

        return {"status": "ok", "sent_at": sent_at}

    # ============================================================
    # 内部发送 / 审批辅助
    # ============================================================
    async def _approval_gate(self, preview: str) -> tuple[SocialSendPolicy | None, dict | None]:
        """冻结本次发送使用的审批结果和知会选项。"""
        policy = None
        if not self._permissions.active:
            verdict = self._expired_approval()
        else:
            policy = self._send_policy()
            if policy.mode == "unrestricted":
                return policy, None
            verdict = await self._request_master_approval(preview)
        if verdict["result"] == "approved":
            return policy, None
        logger.warning("social_chat 审批未通过（%s），取消发送", verdict["result"])
        return None, self._approval_rejection(verdict)

    @staticmethod
    def _approval_rejection(verdict: dict) -> dict:
        return {
            "status": "rejected",
            "approval": verdict["result"],
            "master_message": verdict.get("content", ""),
            "reason": verdict["reason"],
        }

    def _check_send_authority(self, policy: SocialSendPolicy) -> None:
        """在开始向平台发送前检查有效期、撤销和会话切换。"""
        current = self._send_policy()
        if policy.mode == "unrestricted" and current is not policy:
            raise ValueError("自由发送授权已到期或变化。请重新提交发送，由系统按当前权限处理。")

    async def _finish_target_send(self, tsid: str, preview: str, nature: str = None,
                                  *, notify: bool) -> float:
        """发送成功后的统一收尾：发送记录、发言性质和知会。

        Returns:
            sent_at（发送完成时间戳）。
        """
        sent_at = time.time()
        self._messages_sent.append({"session": tsid, "content": preview, "sent_at": sent_at})
        logger.info(f"social_chat 已发送到工作会话 {tsid}: {preview[:40]!r}")

        # 发言性质登记（整批作为一个发言动作登记一次；发送已成功，登记失败仅告警）
        if nature:
            try:
                await self.emotional_judgment(session_type="working", act=nature)
            except Exception as e:
                logger.warning(f"发送后性质登记情感判断失败（不影响主流程）: {e}")

        if notify:
            working_name = self._resolve_working_session_name()
            try:
                await self._send_raw_to_master(f"【知会】已在 {working_name} 发送：\n{preview}")
            except Exception:
                logger.exception("目标消息已发送，但向主人发送知会失败")

        return sent_at

    async def _send_raw_to_master(self, text: str) -> None:
        """内部单条直发主人会话（审批请求 / 知会专用）。

        与 LLM 面向的 send_to_master 的差异：不做换行拆条（多行审批提示必须保持
        单条完整呈现，避免拆条拉长审批时序）、不登记 nature、不进 _messages_sent。
        发送失败直接抛错阻断（fail-fast，不吞错）。
        """
        from brain.chat.social_chat.social_chat_session import SocialChatSession

        msid = self.social_chat_core.master_session_id or ""
        scope, target_id = ToolConversation._resolve_target(msid)
        await ToolConversation.send_segments_with_typing(
            scope, target_id, [text_segment(text)],
            typing_delay_per_char=SocialChatSession.TYPING_DELAY_PER_CHAR,
            session_id=msid,
        )

    async def set_send_mode(
        self,
        mode: str,
        duration_minutes: int | None = None,
        notify: bool | None = None,
    ) -> Dict[str, Any]:
        """由主人选择自由发送的期限和知会，或即时恢复逐条审批。"""
        if not isinstance(mode, str):
            raise TypeError("mode 必须是字符串")
        mode = mode.strip().lower()
        if mode not in VALID_SEND_MODES:
            raise ValueError(f"mode 非法: {mode!r}（仅支持 {VALID_SEND_MODES}）")
        if duration_minutes is not None and (
            type(duration_minutes) is not int or not 1 <= duration_minutes <= MAX_DURATION_MINUTES
        ):
            raise ValueError(f"duration_minutes 须为 1 到 {MAX_DURATION_MINUTES} 的整数")
        if notify is not None and type(notify) is not bool:
            raise ValueError("notify 必须是布尔值")
        if not self._permissions.active:
            return self._approval_rejection(self._expired_approval())

        old = self._send_policy()
        options = None
        if mode == "approval":
            self._permissions.revoke_free_send(*self._permission_scope)
            policy = self._send_policy()
        else:
            defaults = FreeSendOptions(
                duration_minutes,
                chat_config.SOCIAL_CHAT_UNRESTRICTED_NOTIFY if notify is None else notify,
            )
            verdict = await self._request_master_approval("", request_kind="mode_switch", options=defaults)
            if not self._permissions.active:
                return self._approval_rejection(self._expired_approval())
            if verdict["result"] != "approved":
                return {**self._approval_rejection(verdict), "send_mode": self.send_mode}
            if self._send_policy() is not old:
                return {"status": "rejected", "reason": "等待期间发送权限已变化，请重新申请。"}
            options = verdict["options"]
            policy = self._permissions.grant_free_send(
                *self._permission_scope,
                approval_seq=verdict["reply_seq"],
                duration_minutes=options.duration_minutes,
                notify=options.notify,
            )

        logger.info("social_chat 发送模式切换: %s -> %s", old.mode, policy.mode)
        return {
            "status": "ok", "send_mode": policy.mode, "previous": old.mode,
            "chain_id": self._permissions.chain_id, "target_session_id": self._permission_scope[1],
            "scope": "timed_target" if policy.deadline is not None else "current_event_chain_and_target",
            "duration_minutes": options.duration_minutes if options else None,
            "notify": policy.notify,
        }

    async def transfer_back(self, summary: str, encounter_type: str = None, failure_reason: str = None) -> Dict[str, Any]:
        """结束本次委托，触发 social_chat_core 的退出流程。

        encounter_type: 本次任务中他人对我能力/表现的评价（EvaluationAct 类，可选）——
            退出前登记一次评价类情感判断（working 线，评价产生于社交互动中）。
        """
        encounter_type = (encounter_type or "").strip() or None
        if encounter_type and encounter_type not in EvaluationAct.names():
            raise ValueError(
                f"transfer_back encounter_type 未注册: {encounter_type!r}"
                "（必须从 EvaluationAct 中选择）"
            )
        from brain.amy.failure_reasons import validate_failure_reason, cache_failure_reason
        validate_failure_reason(encounter_type, failure_reason)
        # 评价登记（conversation 同款：流转退出时顺带登记，失败直接抛错阻断，
        # 避免带着未登记的情绪状态结束任务）
        if encounter_type:
            await self.emotional_judgment(session_type="working", act=encounter_type)

        await cache_failure_reason(encounter_type, failure_reason, source="social_chat",
                                   title=summary[:80], session_id=self.social_chat_core.working_session_id or "")

        # 把 summary 写入 core，供 stop_session / 返回结果使用
        if self.social_chat_core is not None:
            self.social_chat_core.set_transfer_summary(summary or "")
            await self.social_chat_core.request_stop()

    async def emotional_judgment(self, session_type: str, act: str) -> str:
        """按 session_type 路由的情感判断。

        平级自有实现，刻意不与 conversation 版合并——encounters 写入目标、
        session_type 语义、schema 参数均不同，监管隔离要求状态分离
        （见 docs/api/social_chat.md「工具介绍」）。

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
                'working' = 工作会话（与社交对象的互动 / 通过工作会话发出的发言引发的情感）。
            act: 遭遇/行为标签。显式工具仅收 ExternalInteractionAct（他人行为直接
                刺激）类——self_action 类随发送工具 nature、evaluation 类随
                transfer_back encounter_type 拆分登记，不从这里进（schema 已用
                enum 强制；实现层白名单仍为三类合集，供自动登记路径复用）。
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
        from adapt.session_identity import session_scope
        sub_session_type = session_scope(core_session_id)

        success, msg, _prompt = await ei.build_conversation_thought(
            act=act,
            session_type=sub_session_type,
            session_name=session_name,
        )

        # 写入 social_chat_core 的对应缓存（不污染 conversation 状态）
        self.social_chat_core.add_emotion_encounter(session_type=session_type, act=act)

        if success:
            logger.info(
                f"social_chat emotional_judgment 完成: session_type={session_type}, "
                f"act={act}, session={sub_session_type}/{session_name}"
            )
            # 表现侧出口：仅 NE/DA 两行激素描述 + 原因（仅非最终评价类想法），
            # 不含 SC 行——pfca 决策侧情绪体系与本模块隔离（见 docs/api/amy.md）
            return await ei.get_language_description()
        # build_conversation_thought 失败：act 已在白名单内，理论上不应到这里；
        # 真发生则按 user_rules 原则直接抛错阻断，不静默吞。
        raise RuntimeError(
            f"emotional_judgment 失败: session_type={session_type}, act={act}, msg={msg}"
        )

    # ============================================================
    # 审批机制（seq 游标轮询，零时钟依赖）
    # ============================================================
    async def _request_master_approval(
        self, preview: str, request_kind: str = "send", options: FreeSendOptions | None = None,
    ) -> Dict[str, Any]:
        async with self._permissions.approval_lock:
            if not self._permissions.active:
                return self._expired_approval()
            self._require_permission_scope()
            return await self._poll_master_approval(preview, request_kind, options)

    async def _poll_master_approval(
        self, preview: str, request_kind: str, options: FreeSendOptions | None,
    ) -> Dict[str, Any]:
        """只读取请求发出前的序号锚点之后、来自主人的回复。"""
        from memory.message_repository import get_message_store

        master_id = self._permission_scope[0]
        master_user_id = parse_session_id(master_id).identity
        store = get_message_store()
        latest = await store.get_latest_messages(master_id, 1)
        anchor_seq = int(latest["messages"][-1]["_seq"]) if latest.get("messages") else 0
        if not self._permissions.active:
            return self._expired_approval()
        self._require_permission_scope()
        await store.advance_cursor(master_id, APPROVAL_CONSUMER_ID, anchor_seq)
        if not self._permissions.active:
            return self._expired_approval()
        self._require_permission_scope()

        working_id = self._permission_scope[1]
        working_name = self._resolve_working_session_name()
        target_label = working_name if working_name == working_id else f"{working_name}（{working_id}）"
        timeout = APPROVAL_TIMEOUT_S
        if options is not None:
            timeout = PERMISSION_APPROVAL_TIMEOUT_S
            duration = (
                "本事件链（事件链结束即失效）"
                if options.duration_minutes is None else f"{options.duration_minutes} 分钟"
            )
            notice = "发送后自动知会" if options.notify else "发送后不知会"
            approval_prompt = (
                f"【自由发送申请】\n目标会话：{target_label}\n"
                f"回复「同意」采用本次申请：{duration}，{notice}。\n"
                "也可回复「同意 本链 知会」或「同意 30分钟 不知会」。"
                "时长支持 1 分钟至 24 小时，限时授权可跨事件链，重启后失效。\n"
                f"请在 {timeout:g} 秒内回复，其他回复视为拒绝。"
            )
        else:
            # 每行都加上引用标记，避免拟发送内容与系统的审批说明混在一起。
            quoted_preview = "\n".join(f"│ {line}" for line in preview.replace("\\n", "\n").split("\n"))
            approval_prompt = (
                f"【单次发送审批】\n收件会话：{target_label}\n"
                f"请在 {timeout:g} 秒内回复「同意」，批准发送下面的正文。其他回复或超时视为拒绝。\n\n"
                f"【拟发送正文】\n{quoted_preview}"
            )
        await self._send_raw_to_master(approval_prompt)

        deadline = time.monotonic() + timeout
        while True:
            messages = await store.get_unread_for(master_id, APPROVAL_CONSUMER_ID)
            if not self._permissions.active:
                return self._expired_approval()
            self._require_permission_scope()
            if time.monotonic() >= deadline:
                return {"result": "timeout", "content": "", "reason": f"主人 {timeout:g} 秒内未回复，审批超时"}
            for message in messages:
                if str(message.get("user_id")) != master_user_id:
                    continue
                content = (message.get("content_text") or "").strip()
                if not content:
                    continue
                approved_options = parse_free_send_reply(content, options) if options is not None else None
                approved = approved_options is not None if options is not None else content == "同意"
                seq = int(message["_seq"])
                logger.info("social_chat 审批裁决（%s，anchor_seq=%s，reply_seq=%s）：%s",
                            request_kind, anchor_seq, seq, "同意" if approved else "拒绝")
                if approved:
                    return {"result": "approved", "content": content, "reason": "主人批准",
                            "reply_seq": seq, "options": approved_options}
                return {"result": "rejected", "content": content, "reason": "主人拒绝或回复格式不符合审批说明"}
            if messages:
                await store.advance_cursor(master_id, APPROVAL_CONSUMER_ID, int(messages[-1]["_seq"]))
            await asyncio.sleep(min(APPROVAL_POLL_INTERVAL_S, max(0, deadline - time.monotonic())))

    # ============================================================
    # 内部辅助
    # ============================================================
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
