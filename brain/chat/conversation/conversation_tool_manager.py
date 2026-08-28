# -*- coding: utf-8 -*-
import json
from typing import Dict, Any
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from brain.base import ToolManager, ToolHandlers, ToolVision
from brain.amy.constants import ExternalInteractionAct, EvaluationAct, SelfActionAct

# act 表 ↔ 工具 一一对应：
#   emotional_judgment.act         → ExternalInteractionAct（他人行为直接刺激，评价类）
#   transfer_task.encounter_type   → EvaluationAct（他人对我能力/表现评价）
#   send_message.nature            → SelfActionAct（我主动选择做什么）

# 发送消息可标注的性质（自行为类 act，conversation 专有，扩展进 send_message schema）
_SEND_MESSAGE_NATURES = SelfActionAct.names()

logger = get_logger('ConversationToolManager', "INFO")


class ConversationToolManager(ToolManager):

    @classmethod
    def get_tools(cls):

        # 通用 send_message schema（仅 segments）+ conversation 专有 nature 扩展
        send_message_schema = ToolHandlers.get_send_message_tool_schema()
        send_message_schema["function"]["parameters"]["properties"]["nature"] = {
            "type": "string",
            "description": (
                "本条发言的性质（可选）。用于在自行为类情感判断时登记本次发言的属性。"
                f"必须从列表中选择之一：{'、'.join(_SEND_MESSAGE_NATURES)}。"
            ),
            "enum": _SEND_MESSAGE_NATURES
        }

        return [
            send_message_schema,
            ToolHandlers.get_wait_and_observe_tool_schema(),
            {
                "type": "function",
                "function": {
                    "name": "transfer_task_to_PFCA_agent",
                    "description": "将当前任务流转给 PFCA agent 处理。暂时挂起会话，返回思索阶段，仔细思索当前情况。在遇到以下情景情况，且当前策略不适应时调用该工具。使用该工具挂起会话后可以执行其他高级操作。包含查询信息、退出会话、切换环境等一系列功能。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "exit_reason": {
                                "type": "string",
                                "description": "离开原因+情绪感受补充。"
                            },
                            "encounter_type": {
                                "type": "string",
                                "description": f"遭遇类型（可选）。流转前进行情感判断时使用，必须从 EvaluationAct 列表中选择之一：{'、'.join(EvaluationAct.names())}。",
                                "enum": EvaluationAct.names()
                            }
                        },
                        "required": ["exit_reason"]
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
                        "呼叫经验agent。可以传入当前应用场景，让 agent 搜索并返回匹配的经验内容、用词技巧和话术结构；"
                        "也可以向其反馈某些经验不足的情况（例如某类场景下找不到合适的经验、现有经验效果不佳等），"
                        "让 agent 记录这些反馈以供后续积累与改进。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "scenario": {
                                "type": "string",
                                "description": "当前应用场景描述，例如：'表达嘲讽'、'表达喜爱'、'回应调侃'、'表达关心'、'处理冲突'、'结束对话'等。"
                            }
                        },
                        "required": ["scenario"]
                    }
                }
            },
            ToolVision.get_call_vision_agent_tool_schema(),
            ToolVision.get_list_stickers_tool_schema(),
        ]

    def __init__(self, conversation_core):
        self.conversation_core = conversation_core
        self.tool_functions = {
            "send_message": self.send_message_to_user,
            "wait_and_observe": self.wait_and_observe,
            "transfer_task_to_PFCA_agent": self.transfer_task_to_PFCA_agent,
            "emotional_judgment": self.emotional_judgment,
            "experience_agent": self.experience_agent,
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

    async def send_message_to_user(self, segments: list, nature: str = None) -> Dict[str, Any]:
        """
        发送消息到当前会话（委托通用 send_message，自动注入目标 session_id）。

        Args:
            segments: LLM 传入的段列表。每项是单 key 字典（text 或 img）。
            nature:   本条发言的性质（可选），用于登记自行为类情感判断；
                      仅当整段集合发送成功（即非空且至少一段已发出）时登记一次。
        """
        from brain.chat.conversation.conversation_session import ConversationSession

        result = await ToolHandlers.send_message(
            session_id=self.conversation_core.conversation_id or "",
            segments=segments,
            typing_delay_per_char=ConversationSession.TYPING_DELAY_PER_CHAR,
            on_segment_sent=self.conversation_core._session.increment_sent_message,
        )

        # 发言性质登记（整批作为一个发言动作登记一次）
        if nature:
            try:
                await self.emotional_judgment(act=nature)
            except Exception as e:
                logger.warning(f"send_message 性质登记情感判断失败（不影响主流程）: {e}")

        return result

    async def wait_and_observe(self, seconds: float) -> Dict[str, Any]:
        """等待并监听当前 conversation 会话的新消息（委托通用 wait_and_observe）。

        Args:
            seconds: 最长等待秒数（默认工具 schema 限制 10-30）。
        """
        from brain.chat.conversation.conversation_core import get_conversation_core

        session_id = get_conversation_core().conversation_id

        return await ToolHandlers.wait_and_observe(
            session_id=session_id,
            seconds=seconds,
            consumer_id="conversation",
        )

    async def transfer_task_to_PFCA_agent(self, exit_reason: str, encounter_type: str = None, progress_summary: str = "") -> Dict[str, Any]:
        logger.debug(f"收到仔细思索请求，离开原因: {exit_reason}，进度总结: {progress_summary}")

        if encounter_type:
            logger.debug(f"检测到遭遇类型参数: {encounter_type}，执行情感判断")
            await self.emotional_judgment(act=encounter_type)
        logger.debug("执行停止聊天操作")
        result = await self.conversation_core.stop_session(exit_reason, progress_summary)
        return str(result)

    async def emotional_judgment(self, act: str) -> Dict[str, Any]:
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
            snapshot = await ei.thought_cache.calculate_mood_snapshot()
            from brain.amy.prompt_engine import get_dopamine_desc, get_norepinephrine_desc
            reasons = [t.desc for t in snapshot.top_thoughts[:2] if t.desc]
            reasons_text = "、".join(reasons) if reasons else act
            new_feeling = f"你的心情变化为：{get_norepinephrine_desc(snapshot.norepinephrine)}。{get_dopamine_desc(snapshot.dopamine)}"
            return f"因为{reasons_text}，{new_feeling}"
        logger.debug(f"情感判断完成，动作为: {act}, session={session_type}/{session_name}, 成功: {success}")
        return f"情感判断失败: {msg}"

    async def experience_agent(self, scenario: str, feedback: str = None) -> Dict[str, Any]:
        from brain.hc.database.bequest import BequestOperations
        from brain.chat.conversation.conversation_bequest import get_conversation_bequest

        feedback_text = (feedback or "").strip()
        if feedback_text:
            logger.info(f"经验agent收到经验不足反馈: scenario={scenario}, feedback={feedback_text}")

        bequest_ops = BequestOperations()
        bequest_cache = get_conversation_bequest()

        bequest_ids = await bequest_ops.search_bequest_by_vector(scenario, top_k=5)

        matched_bequests = []
        newly_activated = []
        for bequest_id in bequest_ids:
            if bequest_cache.is_activated(bequest_id):
                continue

            data = await bequest_ops.get_bequest_by_id(bequest_id)
            if not data:
                continue
            if data.get("apply_scenario") != "execution":
                continue

            title = data.get("title", "")
            summary = data.get("summary", "")
            if title and summary:
                matched_bequests.append((title, summary))
                bequest_cache.add(bequest_id)
                newly_activated.append(bequest_id)

        self.conversation_core.set_emotion_evaluation(encounters=[f"获取话术经验_{scenario}"])

        if not matched_bequests:
            return "你什么计策都没想起来。"

        lines = [f"{title} - {summary}" for title, summary in matched_bequests]
        return "\n".join(lines)

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