# -*- coding: utf-8 -*-
import json
from typing import Dict, Any
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from brain.base import ToolManager, ToolHandlers, ToolConversation, ToolVision, ToolHC, ToolBequest, CONVERSATION_CONSUMER_ID
from brain.amy.constants import ExternalInteractionAct, EvaluationAct, SelfActionAct

# act 表 ↔ 工具 一一对应：
#   emotional_judgment.act         → ExternalInteractionAct（他人行为直接刺激，评价类）
#   transfer_task.encounter_type   → EvaluationAct（他人对我能力/表现评价）
#   send_message / send_image.nature → SelfActionAct（我主动选择做什么）

# 发送消息可标注的性质（自行为类 act，conversation 专有，扩展进 send_message / send_image schema）
_SEND_MESSAGE_NATURES = SelfActionAct.names()

logger = get_logger('ConversationToolManager', "INFO")


class ConversationToolManager(ToolManager):

    @classmethod
    def get_tools(cls):

        # 通用 send_message / send_image schema（ToolConversation 收口）+ conversation 专有 nature 扩展
        send_message_schema = ToolConversation.get_send_message_tool_schema()
        send_image_schema = ToolConversation.get_send_image_tool_schema()
        for schema in (send_message_schema, send_image_schema):
            schema["function"]["parameters"]["properties"]["nature"] = {
                "type": "string",
                "description": (
                    "本次发送的性质（可选）。用于在自行为类情感判断时登记本次发言的属性。"
                    f"必须从列表中选择之一：{'、'.join(_SEND_MESSAGE_NATURES)}。"
                ),
                "enum": _SEND_MESSAGE_NATURES
            }

        # 统一 record_to_notepad schema（ToolHC 收口）+ conversation 专有经验匮乏上报扩展
        record_note_schema = ToolHC.get_record_note_tool_schema()
        record_note_schema["function"]["description"] += (
            "\n也可记录经验匮乏情况：某类场景检索不到可用经验、现有经验效果不佳、"
            "缺少某类话术或表情包素材等，写明场景与缺口，供反思系统裁决是否登记、修订或删除经验。"
        )

        return [
            send_message_schema,
            send_image_schema,
            ToolHandlers.get_wait_and_observe_tool_schema(),
            {
                "type": "function",
                "function": {
                    "name": "transfer_task_to_PFCA_agent",
                    "description": "将当前任务流转给 PFCA agent 处理。暂时挂起会话，返回思索阶段，仔细思索当前情况。在遇到以下情景情况，且当前策略不适应时调用该工具。使用该工具挂起会话后可以执行其他高级操作。包含查询信息、退出会话、切换环境等一系列功能。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {
                                "type": "string",
                                "description": "本段对话的简要总结，由于上游可以看到对话记录，所以主要是对情绪感受补充。"
                            },
                            "encounter_type": {
                                "type": "string",
                                "description": f"遭遇类型（可选）。流转前进行情感判断时使用，必须从 EvaluationAct 列表中选择之一：{'、'.join(EvaluationAct.names())}。",
                                "enum": EvaluationAct.names()
                            },
                            "failure_reason": {
                                "type": "string",
                                "description": (
                                    "表现差的具体原因，例如经验不适用、自身发言问题或上游判断错误。"
                                    "选中负面结束评价（坏评价、失败、结果不满等）时必填，不填会被拒绝并退回重填。"
                                    "正面评价不收录。"
                                )
                            }
                        },
                        "required": ["summary"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "emotional_judgment",
                    "description": "根据遭遇类型进行情感判断，构建情感思考。用于调整情绪。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "act": {
                                "type": "string",
                                "description": f"遭遇类型，必须从 ExternalInteractionAct 列表中选择之一：{'、'.join(ExternalInteractionAct.names())}。",
                                "enum": ExternalInteractionAct.names()
                            }
                        },
                        "required": ["act"]
                    }
                }
            },
            record_note_schema,
            ToolHC.get_query_note_tool_schema(),
            ToolBequest.get_query_bequest_tool_schema(),
            ToolVision.get_vision_agent_tool_schema(),
            ToolVision.get_vision_agent_adjust_tool_schema(),
        ]

    def __init__(self, conversation_core):
        self.conversation_core = conversation_core
        self.tool_functions = {
            "send_message": self.send_message_to_user,
            "send_image": self.send_image_to_user,
            "wait_and_observe": self.wait_and_observe,
            "transfer_task_to_PFCA_agent": self.transfer_task_to_PFCA_agent,
            "emotional_judgment": self.emotional_judgment,
            "record_to_notepad": self.record_to_notepad,
            "query_note": self.query_note,
            "query_bequest": self.query_bequest,
            "vision_agent": self.vision_agent,
            "vision_agent_adjust": self.vision_agent_adjust,
        }
        logger.debug(f"ConversationToolManager 已初始化，包含 {len(self.tool_functions)} 个工具")

    async def handle_tool_call(self, tool_call) -> Dict[str, Any]:
        # —— 解析 tool_call 形态（OpenAI 对象结构 / dict 透传）——
        parsed = self._parse_tool_call(tool_call)
        if parsed is None:
            return self._tool_error_response(
                tool_call_id=getattr(tool_call, 'id', None) if hasattr(tool_call, 'id') else tool_call.get('id') if isinstance(tool_call, dict) else None,
                tool_name="<unknown>",
                err=ValueError(f"无法解析工具调用格式: {tool_call}"),
            )
        tool_name = parsed["name"]
        tool_args_json = parsed["arguments"]
        tool_call_id = parsed["id"]

        # —— 参数解析容错（LLM 偶尔吐 Python dict 字面量而非合法 JSON）——
        try:
            tool_args = self._parse_arguments(tool_args_json)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"工具 {tool_name} 参数解析失败: {e}; raw={tool_args_json!r}")
            return self._tool_error_response(tool_call_id, tool_name, e)

        # —— 工具名查表 —— 不认识的工具也走"错误消息返回 LLM"通道，不崩溃 ——
        if tool_name not in self.tool_functions:
            err = ValueError(f"未知工具: {tool_name}")
            logger.error(str(err))
            return self._tool_error_response(tool_call_id, tool_name, err)

        tool_function = self.tool_functions[tool_name]

        # —— 正常路径：统一错误处理（参数错误回 LLM，其余异常上抛阻断）——
        result = await self._safe_execute(
            tool_function, tool_call_id, tool_name, **tool_args
        )
        if isinstance(result, dict) and result.get("role") == "tool":
            # 出错时 _safe_execute 已返回 role=tool 的错误消息，直接透传
            return result

        return {
            "role": "tool",
            "name": tool_name,
            "content": json.dumps(result, ensure_ascii=False),
            "tool_call_id": tool_call_id
        }

    async def send_message_to_user(self, message: str, nature: str = None) -> str:
        """
        发送纯文字消息到当前会话（委托通用 ToolConversation.send_message，自动注入目标 session_id）。

        message 内换行会被拆成多条独立消息，按真人节奏逐条发出。

        发送轮次计数：一次工具调用计 1（换行拆成多条不重复计数）。

        Args:
            message: 要发送的文字内容，换行分隔多条短句。
            nature:  本次发送的性质（可选），用于登记自行为类情感判断；
                     仅当消息全部发送成功（fail-fast 保证）后登记一次。
        """
        from brain.chat.conversation.conversation_session import ConversationSession

        result = await ToolConversation.send_message(
            session_id=self.conversation_core.conversation_id or "",
            message=message,
            typing_delay_per_char=ConversationSession.TYPING_DELAY_PER_CHAR,
        )
        # 发送轮次计数（一次调用计 1，与拆条条数无关）
        self.conversation_core._session.increment_sent_message()

        # 发言性质登记（一次发送动作登记一次；登记失败不影响主流程，仅告警）
        if nature:
            try:
                await self.emotional_judgment(act=nature)
            except Exception as e:
                logger.warning(f"send_message 性质登记情感判断失败（不影响主流程）: {e}")

        return result

    async def send_image_to_user(self, image: str, nature: str = None) -> str:
        """
        发送图片到当前会话（委托通用 ToolConversation.send_image，自动注入目标 session_id）。

        Args:
            image:  图片文件名（不含路径），与消息文本 [图片:xxx] 的 xxx 一致。
            nature: 本次发送的性质（可选），登记语义同 send_message_to_user。
        """
        from brain.chat.conversation.conversation_session import ConversationSession

        result = await ToolConversation.send_image(
            session_id=self.conversation_core.conversation_id or "",
            image=image,
            typing_delay_per_char=ConversationSession.TYPING_DELAY_PER_CHAR,
        )
        # 发送轮次计数（一次调用计 1）
        self.conversation_core._session.increment_sent_message()

        if nature:
            try:
                await self.emotional_judgment(act=nature)
            except Exception as e:
                logger.warning(f"send_image 性质登记情感判断失败（不影响主流程）: {e}")

        return result

    async def wait_and_observe(self, seconds: float) -> str:
        """等待并监听当前 conversation 会话的新消息（委托通用 wait_and_observe）。

        Args:
            seconds: 最长等待秒数（默认工具 schema 限制 10-30）。
        """
        from brain.chat.conversation.conversation_core import get_conversation_core

        session_id = get_conversation_core().conversation_id

        return await ToolHandlers.wait_and_observe(
            session_id=session_id,
            seconds=seconds,
            consumer_id=CONVERSATION_CONSUMER_ID,
        )

    async def transfer_task_to_PFCA_agent(
        self,
        summary: str,
        encounter_type: str = None,
        failure_reason: str = None,
    ) -> Dict[str, Any]:
        """流转回 PFCA。

        failure_reason: 失败评价分区对应的原因，缓存到下次维护申请；正面评价不收录。
        """
        logger.debug(f"收到仔细思索请求，总结: {summary}")

        from brain.amy.failure_reasons import validate_failure_reason, cache_failure_reason
        if encounter_type and encounter_type not in EvaluationAct.names():
            raise ValueError("encounter_type 必须来自 AMY 结束评价词表")
        validate_failure_reason(encounter_type, failure_reason)
        if encounter_type:
            await self.emotional_judgment(act=encounter_type)
        await cache_failure_reason(encounter_type, failure_reason, source="conversation",
                                   title=summary[:80], session_id=self.conversation_core.conversation_id or "")

        logger.debug("执行停止聊天操作")
        result = await self.conversation_core.stop_session(summary)
        return str(result)

    async def emotional_judgment(self, act: str) -> Dict[str, Any]:
        """情感判断（conversation 自有实现）。

        平级自有实现，刻意不与 social_chat 版合并——encounters 写入目标、
        schema 参数均不同，监管隔离要求状态分离
        （见 docs/api/conversation.md「工具介绍」）。
        """
        from brain.amy.emotional_interface import get_emotional_interface

        ei = await get_emotional_interface()
        from brain.chat.conversation.conversation_core import get_conversation_core
        core = get_conversation_core()
        session_id = core.conversation_id or ""
        session_name = core.session_name or "未知会话"
        # session_id 已是 type_id 格式，前缀即为 session_type
        from adapt.session_identity import session_scope
        session_type = session_scope(session_id)

        success, msg, prompt = await ei.build_conversation_thought(act=act, session_type=session_type, session_name=session_name)

        self.conversation_core.set_emotion_evaluation(encounters=[act])

        if success:
            # 表现侧出口：仅 NE/DA 两行激素描述 + 原因（仅非最终评价类想法），
            # 不含 SC 行——pfca 决策侧情绪体系与本模块隔离（见 docs/api/amy.md）
            return await ei.get_language_description()
        logger.debug(f"情感判断完成，动作为: {act}, session={session_type}/{session_name}, 成功: {success}")
        return f"情感判断失败: {msg}"

    async def record_to_notepad(self, text: str, category: str) -> str:
        """记事本（灵光一现）写入——对话现场观察采集（对方素材），不做经验提炼。
        仅收纯文本与类别标签；元数据由系统侧自动附加，记录进当前事件链的缓存文档，
        PFCA 真正结束时统一以事件标题命名落库。"""
        from brain.base.common_ways import ToolHC

        note_id = await ToolHC.record_note(
            text=text,
            category=category,
            source="conversation",
            session_id=self.conversation_core.conversation_id or "",
            session_name=self.conversation_core.session_name or "",
        )
        return f"已记录至记事本（{note_id}）。"

    async def query_note(self, title: str) -> str:
        """按章节标题查询一篇普通笔记的完整细节，委托通用 ToolHC.query_note。

        章节标题取自 list_notes 的普通笔记返回；见闻录 / 任务相关笔记等
        特殊槽位由 list_notes 直接给全，不经此工具。"""
        return await ToolHC.query_note(title)

    async def query_bequest(self, query: str, top_k: int = 3, category: str = "话术") -> Dict[str, Any]:
        """查询经验（按语义相似度检索 title），委托通用 ToolBequest.query_bequest。

        category 可选「话术」/「风格」；与 reflection 的 query_bequest 同源、
        原始结构化输出（title / summary / similarity），不做激活去重，由 LLM 自行判断取用。
        """
        return await ToolBequest.query_bequest(query=query, top_k=top_k, category=category)

    async def vision_agent(self, type: str, context: str = "", requirement: str = "") -> str:
        """视觉查询工具，委托通用 ToolVision.vision_agent。

        type 取「向量比对」（context=期望用途的表情包描述）/「识别图片」（context=图片标题/名称，
        requirement=可选识别要求）。
        """
        from brain.base import ToolVision
        return await ToolVision.vision_agent(type=type, context=context, requirement=requirement)

    async def vision_agent_adjust(
        self,
        type: str,
        target: str,
        new_title: str = "",
        summary: str = "",
    ) -> str:
        """表情包管理工具，委托通用 ToolVision.vision_agent_adjust。

        type 取「保存」/「删除」/「修改」；target 传图片原标题或名称；
        summary（简介）保存与修改必填、删除可留空。
        """
        from brain.base import ToolVision
        return await ToolVision.vision_agent_adjust(
            type=type,
            target=target,
            new_title=new_title,
            summary=summary,
        )
