# -*- coding: utf-8 -*-
import json
from typing import Dict, Any
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from brain.base import ToolManager, ToolHandlers, ToolConversation, ToolVision, ToolHC, CONVERSATION_CONSUMER_ID
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

        # 通用 wait_and_observe schema（ToolHandlers 收口）+ conversation 专有 experience_scenario 扩展
        wait_and_observe_schema = ToolHandlers.get_wait_and_observe_tool_schema()
        wait_and_observe_schema["function"]["description"] = (
            "等待一段时间，等待对方消息的回复。"
            "话题转变或需要话术灵感时，可附带 experience_scenario 检索匹配的经验内容、用词技巧和话术结构。"
        )
        wait_and_observe_schema["function"]["parameters"]["properties"]["experience_scenario"] = {
            "type": "string",
            "description": (
                "经验检索场景描述（可选），例如：'表达嘲讽'、'表达喜爱'、'回应调侃'、'表达关心'。"
                "传入后等待结束时附带返回匹配经验（已激活过的经验不再返回）。"
            ),
        }

        return [
            send_message_schema,
            send_image_schema,
            wait_and_observe_schema,
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
                                "description": "本段对话的总结，含情绪感受补充。"
                            },
                            "encounter_type": {
                                "type": "string",
                                "description": f"遭遇类型（可选）。流转前进行情感判断时使用，必须从 EvaluationAct 列表中选择之一：{'、'.join(EvaluationAct.names())}。",
                                "enum": EvaluationAct.names()
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
            {
                "type": "function",
                "function": {
                    "name": "experience_agent",
                    "description": (
                        "向经验系统上报经验不足情况（例如某类场景下找不到合适的经验、现有经验效果不佳等）。"
                        "反馈会记录下来供反思系统裁决是否登记、修订或删除经验。"
                        "经验检索已并入 wait_and_observe（experience_scenario 参数），本工具不再返回经验内容。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "scenario": {
                                "type": "string",
                                "description": "场景描述，例如：'表达嘲讽'、'表达喜爱'、'回应调侃'、'处理冲突'。"
                            },
                            "feedback": {
                                "type": "string",
                                "description": "经验不足反馈的具体内容（缺什么经验、现有经验哪里不合适）。"
                            }
                        },
                        "required": ["scenario", "feedback"]
                    }
                }
            },
            ToolHC.get_conversation_record_note_tool_schema(),
            ToolHC.get_query_note_tool_schema(),
            ToolVision.get_call_vision_agent_tool_schema(),
            ToolVision.get_list_stickers_tool_schema(),
        ]

    def __init__(self, conversation_core):
        self.conversation_core = conversation_core
        self.tool_functions = {
            "send_message": self.send_message_to_user,
            "send_image": self.send_image_to_user,
            "wait_and_observe": self.wait_and_observe,
            "transfer_task_to_PFCA_agent": self.transfer_task_to_PFCA_agent,
            "emotional_judgment": self.emotional_judgment,
            "experience_agent": self.experience_agent,
            "record_to_notepad": self.record_to_notepad,
            "query_note": self.query_note,
            "call_vision_agent": self.call_vision_agent,
            "list_stickers": self.list_stickers,
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

    async def wait_and_observe(self, seconds: float, experience_scenario: str = None) -> str:
        """等待并监听当前 conversation 会话的新消息（委托通用 wait_and_observe）。

        Args:
            seconds: 最长等待秒数（默认工具 schema 限制 10-30）。
            experience_scenario: 经验检索场景（可选）。传入时按该场景向量检索话术经验，
                结果附在观察结果之后；激活缓存去重（已激活不再返回）。
                检索失败直接上抛崩溃阻断（按 user_rules）。
        """
        from brain.chat.conversation.conversation_core import get_conversation_core

        session_id = get_conversation_core().conversation_id

        result = await ToolHandlers.wait_and_observe(
            session_id=session_id,
            seconds=seconds,
            consumer_id=CONVERSATION_CONSUMER_ID,
        )

        scenario = (experience_scenario or "").strip()
        if scenario:
            from brain.base.common_ways.bequest_helpers import search_speech_bequests
            from brain.chat.conversation.conversation_bequest import get_conversation_bequest

            bequest_cache = get_conversation_bequest()
            matched = await search_speech_bequests(
                scenario, skip=bequest_cache.is_activated, top_k=5,
            )
            for entry in matched:
                bequest_cache.add(entry["id"])
                logger.info(f"wait_and_observe 激活经验: {entry['title']} score={entry['score']:.3f}")

            lines = [f"{entry['title']} - {entry['summary']}" for entry in matched]
            result = (
                f"{result}\n【经验联想·{scenario}】\n"
                + ("\n".join(lines) if lines else "没有联想到新的经验。")
            )

        return result

    async def transfer_task_to_PFCA_agent(self, summary: str, encounter_type: str = None) -> Dict[str, Any]:
        logger.debug(f"收到仔细思索请求，总结: {summary}")

        if encounter_type:
            logger.debug(f"检测到遭遇类型参数: {encounter_type}，执行情感判断")
            await self.emotional_judgment(act=encounter_type)
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
        session_type = session_id.split("_", 1)[0] if session_id and "_" in session_id else ""

        success, msg, prompt = await ei.build_conversation_thought(act=act, session_type=session_type, session_name=session_name)

        self.conversation_core.set_emotion_evaluation(encounters=[act])

        if success:
            # 表现侧出口：仅 NE/DA 两行激素描述 + 原因（仅非最终评价类想法），
            # 不含 SC 行——pfca 决策侧情绪体系与本模块隔离（见 docs/api/amy.md）
            return await ei.get_language_description()
        logger.debug(f"情感判断完成，动作为: {act}, session={session_type}/{session_name}, 成功: {success}")
        return f"情感判断失败: {msg}"

    async def experience_agent(self, scenario: str, feedback: str = None) -> str:
        """经验 agent（conversation 自有包装）——仅保留经验不足上报。

        检索能力已并入 wait_and_observe（experience_scenario 参数）；
        本工具不再检索，仅记录经验不足反馈，供反思系统裁决是否登记、修订或删除经验。
        """
        feedback_text = (feedback or "").strip()
        if not feedback_text:
            return "未提供反馈内容。请描述经验不足的具体情况（哪类场景缺少经验、现有经验效果如何）。"

        logger.info(f"经验agent收到经验不足反馈: scenario={scenario}, feedback={feedback_text}")
        return "已记录经验不足反馈，将交由反思系统裁决是否补充或修订经验。"

    async def record_to_notepad(self, text: str, usage: str) -> str:
        """记事本（灵光一现）写入——对话现场观察采集（对方素材），不做经验提炼。
        仅收纯文本与语境标注；元数据由系统侧自动附加，记录进当前事件链的缓存文档，
        PFCA 真正结束时统一以事件标题命名落库。"""
        from brain.base.common_ways import ToolHC

        note_id = await ToolHC.record_note(
            text=text,
            source="conversation",
            session_id=self.conversation_core.conversation_id or "",
            session_name=self.conversation_core.session_name or "",
            usage=usage,
        )
        return f"已记录至记事本（{note_id}）。"

    async def query_note(self, title: str) -> str:
        """查询记事本某一章节（按标题精确匹配，重名取最新）的完整细节，
        委托通用 ToolHC.query_note（起因/总结/自我评价/逐条记录及元数据）。"""
        return await ToolHC.query_note(title)

    async def call_vision_agent(self, image_filename: str, context_hint: str = None) -> str:
        """
        识图工具（委托通用 ToolVision.call_vision_agent，返回其 content 文案）。

        Args:
            image_filename: 图片文件名（与消息中 [图片:xxx] 的 xxx 一致）。
            context_hint: 可选附加提示，辅助识图模型理解。

        Returns:
            识图结果文案（成功 / 失败）。
        """
        result = await ToolVision.call_vision_agent(image_filename, context_hint)
        return result.get("content") or "vision agent 未返回任何信息。"

    async def list_stickers(self, keyword: str = "") -> str:
        """查询已保存的表情包库（委托通用 ToolVision.list_stickers）。"""
        return await ToolVision.list_stickers(keyword=keyword)