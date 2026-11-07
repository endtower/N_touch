# -*- coding: utf-8 -*-
import re
from typing import List, Dict
from utils.logger import get_logger
from brain.base import HeavySession
from brain.chat.chat_history_manager import get_history_manager
from brain.chat.conversation.conversation_input_manager import get_conversation_input_manager
from brain.chat.conversation.conversation_prompt import get_full_system_prompt
from brain.chat.conversation.conversation_tool_manager import ConversationToolManager
from config.llm_settings import llm_config, VisionConfig
from utils.llm_client_manager import call_llm

logger = get_logger('ConversationSession', "DEBUG")

# 消息文本中的图片占位符（[图片:xxx]）：
# 发送给 LLM 前把最新一条 user 消息里的占位符展开成真正的图片内容块
_IMAGE_PLACEHOLDER_PATTERN = re.compile(r'\[图片:([^\]]+)\]')


class ConversationSession(HeavySession):

    PREFIX = 'conversation'
    TYPING_SPEED_CHARS_PER_MINUTE = 150
    TYPING_DELAY_PER_CHAR = 60.0 / TYPING_SPEED_CHARS_PER_MINUTE

    # 单会话累计 token 预算（熔断值，参照 token_stats.json 校准）
    TOKEN_BUDGET = 500_000
    # wait_and_observe 的语义就是"参数相同地反复等待"，豁免重复调用检测
    repeat_exempt_tools = frozenset({"wait_and_observe"})

    def __init__(self, conversation_core, max_messages: int = 0):
        
        self.conversation_core = conversation_core
        self.prefix = self.PREFIX
        self.history_manager = get_history_manager(prefix=self.prefix)
        self.running = True
        self._exit_reason = None
        self._exit_content = None
        self.tool_manager = ConversationToolManager(conversation_core)
        self._input_manager = get_conversation_input_manager()
        # 注：旧的"基于上一次发送时间戳"的打字延迟已迁移到
        # conversation_tool_manager.send_message 内部（从方法入口起算），
        # 这里不再保留 _last_send_time / _last_send_char_count 状态。
        self.no_tool_call_count = 0
        self.max_no_tool_call = llm_config.MAX_NO_TOOL_CALLS
        # Conversation 不使用工具参数错误上限（max_tool_error=None 表示不启用）
        self.tool_error_count = 0
        self.max_tool_error = None
        # 循环上限（父类 should_continue 默认基于 max_rounds 判断）
        self.max_rounds = llm_config.MAX_SELF_LOOPS
        self._rounds = 0
        # 发送轮次计数（软限制：一次发送工具调用计 1，与拆条后的消息条数无关；
        # 由 conversation_tool_manager 在发送成功后递增，input_manager 读取）
        self._max_messages = max_messages if max_messages > 0 else 0
        self._sent_message_count = 0
        self._initialized = True

    def get_tools(self) -> List[Dict]:
        return self.tool_manager.get_tools()

    async def _handle_tool_call(self, tool_call):
        return await self.tool_manager.handle_tool_call(tool_call)

    def increment_sent_message(self) -> None:
        """增加已发送轮次计数（一次 send_message / send_image 工具调用计 1）"""
        self._sent_message_count += 1
        logger.debug(f"已发送轮次计数: {self._sent_message_count}/{self._max_messages}")
        if self._max_messages > 0 and self._sent_message_count >= self._max_messages:
            logger.info(f"已达到本轮策略中计划发送的轮次数量限制 ({self._max_messages})")

    def get_max_messages(self) -> int:
        """获取当前设置的最大发送轮次数量"""
        return self._max_messages

    def get_sent_message_count(self) -> int:
        """获取已发送轮次数量"""
        return self._sent_message_count

    async def _user_input(self) -> str:
        return await self._input_manager.get_user_input()
    
    async def _inject_first_user_message(self) -> bool:
        """注入首条 user 消息（唯一注入点）。"""
        user_input = await self._user_input()
        if user_input:
            self.history_manager.append({"role": "user", "content": user_input})
            return True
        return False
    
    async def _inject_incremental_user_message(self) -> bool:
        """注入后续增量 user 消息（唯一注入点）。"""
        user_input = await self._user_input()
        if user_input:
            self.history_manager.append({"role": "user", "content": user_input})
            return True
        return False

    async def _call_llm(self):
        tools = self.get_tools()
        messages = self._attach_images_to_latest_user_message(
            self.history_manager.get_history()
        )

        llm_messages = [
            {"role": "system", "content": get_full_system_prompt()}
        ] + messages
        response = await call_llm(
            level='conversation',
            messages=llm_messages,
            tools=tools,
        )
        return response.choices[0].message

    def _attach_images_to_latest_user_message(self, messages: List[Dict]) -> List[Dict]:
        """把最新一条 user 消息里的 [图片:x] 展开成图片内容块。

        只处理最新一条 user 消息，历史消息保持纯文本——避免每轮把历史图片
        重复发给模型（token 成本随轮数线性增长）。

        文件已被用户删除（FileNotFoundError）属正常内容状态、不是模块故障：
        该占位符就地降级为「[图片:x（已删除）]」文本并跳过附件，不让整轮输入
        装配失败。文件名非法 / 读取权限 / 解码失败等其他异常仍直接抛出崩溃阻断。
        """
        last_user_idx = None
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "user":
                last_user_idx = idx
                break
        if last_user_idx is None:
            return messages

        content = messages[last_user_idx].get("content")
        if not isinstance(content, str):
            return messages

        filenames = list(dict.fromkeys(_IMAGE_PLACEHOLDER_PATTERN.findall(content)))
        if not filenames:
            return messages

        from brain.attachments import resolve_media_file
        from brain.base.light_agent.vision_agent import VisionAgent

        text = content
        parts: List[Dict] = []
        attached_count = 0
        for filename in filenames:
            try:
                real_path = resolve_media_file(filename)
            except FileNotFoundError:
                # 用户已删除该图片（如前端删表情包），历史消息里的占位符仍会留在文本里。
                # 这是正常的内容状态而非模块故障：就地降级为文本提示，不阻断本轮对话。
                text = text.replace(f"[图片:{filename}]", f"[图片:{filename}（已删除）]")
                logger.warning(f"图片已被删除，本次跳过附件: {filename}")
                continue
            data_url = VisionAgent._image_to_data_url(
                real_path,
                max_side=VisionConfig.IMAGE_MAX_SIDE,
                quality=VisionConfig.IMAGE_JPEG_QUALITY,
            )
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
            attached_count += 1

        if not parts:
            # 全部图片都已删除：保留纯文本（含降级标记），无需构造多段 content
            attached = list(messages)
            attached[last_user_idx] = {**messages[last_user_idx], "content": text}
            return attached

        logger.debug(f"已为最新 user 消息附加 {attached_count} 张图片: {filenames}")

        attached = list(messages)
        attached[last_user_idx] = {
            **messages[last_user_idx],
            "content": [{"type": "text", "text": text}] + parts,
        }
        return attached
