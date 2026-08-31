# -*- coding: utf-8 -*-
"""
Conversation 输入管理模块

负责管理 Conversation 的 user 消息内容构造：
1. 首条消息构建：聊天记录 + 策略 + 经验 + amy_effect
2. 增量消息构建：仅新消息，无情绪
3. 其他辅助方法：限额提示、警告、经验匹配、审批/监听等

设计原则：
- 情绪仅在首条消息时添加
- 经验缓存用于避免重复激活
"""

from typing import Any, Dict, Optional
from utils.logger import setup_logging
from utils.error_handling import async_handle_errors
from config.role_settings import RoleConfig
from brain.base import build_session_environment

logger = setup_logging(__name__, log_level="INFO")


class ConversationInputManager:
    """Conversation 输入管理器"""

    def __init__(self):
        if not getattr(self, '_initialized', False):
            self._first_activation = True
            self._initialized = True
            logger.debug("ConversationInputManager 实例级状态初始化")

    def reset(self):
        """重置状态（会话开始时调用）"""
        self._first_activation = True

    @async_handle_errors()
    async def get_user_input(self) -> Optional[str]:
        """获取用户输入，构建包含聊天记录、策略和经验的完整输入"""
        is_first = self._first_activation
        self._first_activation = False

        if is_first:
            return await self._build_first_input()
        else:
            return await self._build_incremental_input()

    # ============================================================
    # 首条消息构建
    # ============================================================

    async def _build_first_input(self) -> str:
        """构建首条消息：聊天记录 + 策略 + 补充信息 + amy_effect + 经验"""
        from brain.chat.conversation.conversation_core import get_conversation_core
        from memory.session_history import get_global_session_history, FetchOptions, MessageFormatOptions

        core = get_conversation_core()
        conv_id = core.conversation_id
        sess_name = core.session_name or ""
        if not conv_id:
            return "过去没有你们没有发生过任何对话"

        # 游标拉取：获取本消费者（conversation）未处理的消息，并推进游标。
        # 首次（游标为 -1）会拉取该会话全部消息；之后只拉取游标之后的新消息。
        history = get_global_session_history()
        result = await history.fetch_unread(
            conv_id,
            sess_name,
            consumer_id="conversation",
            options=FetchOptions(
                message_format=MessageFormatOptions(show_new_marker=True, show_timestamp=True),
                header_model="chat_message",
            ),
        )

        message_str = "\n".join(result.formatted_lines) if result.formatted_lines else "没有新的聊天记录"

        conversation_purpose = self._get_conversation_purpose()

        from brain.amy.emotional_interface import get_emotional_interface
        ei = await get_emotional_interface()
        # 新门面：无参 get_amy_description（描述内部已含覆盖态与 thoughts 处理，见 docs/api/amy.md）
        amy_effect = await ei.get_amy_description()

        # 按实际消息拼接顺序组装：聊天记录 -> 会话环境 -> 任务 -> 情绪
        _parts = [f"{message_str}"]
        # 当前会话环境段：从当前会话取 session_id 与 session_name，按角色/群聊/是否主人动态拼接
        env_text = build_session_environment(
            session_id=conv_id,
            session_name=sess_name,
            master_id=RoleConfig.MASTER_ID,
        )
        if env_text:
            _parts.append(f"【当前聊天环境】{env_text}")
        _parts.append(
            f"【本次聊天任务】\n{conversation_purpose}"
        )
        if amy_effect:
            _parts.append(f"【你的情绪状态】{amy_effect}")
        user_input = "\n".join(["【已经发生的聊天记录】"] + _parts)

        user_input = await self._append_matched_bequest("【响应的经验】", user_input)

        # 注：附件清单的展示已迁移到 PFCA 把附件列表写进 purpose / extra_info，
        # 由 input_manager 在首条 user 消息的"任务目标"段中体现，无需再单独拼接。

        limit_hint = self._build_message_limit_hint()
        if limit_hint:
            user_input = f"{user_input}\n{limit_hint}"

        logger.info(f"生成第一段输入，长度: {len(user_input)}")
        return user_input

    # ============================================================
    # 增量消息构建
    # ============================================================

    async def _build_incremental_input(self) -> Optional[str]:
        """构建增量消息：仅新消息 + 消息数量限制提示"""
        from brain.chat.conversation.conversation_core import get_conversation_core
        from memory.session_history import get_global_session_history, FetchOptions, MessageFormatOptions

        core = get_conversation_core()
        session_id = core.conversation_id
        if not session_id:
            return None

        history = get_global_session_history()
        result = await history.fetch_unread(
            session_id,
            "",
            consumer_id="conversation",
            options=FetchOptions(
                message_format=MessageFormatOptions(
                    exclude_system=True, show_timestamp=True, show_new_marker=False
                )
            ),
            min_messages=0,
        )

        user_input = "\n".join(result.formatted_lines) if result.formatted_lines else None

        limit_hint = self._build_message_limit_hint()
        if limit_hint:
            user_input = f"{user_input}\n{limit_hint}" if user_input else limit_hint

        return user_input

    # ============================================================
    # 其他辅助方法
    # ============================================================

    def _build_message_limit_hint(self) -> str:
        """检查消息数量限制，若达到限制则返回提示文本"""
        from brain.chat.conversation.conversation_core import get_conversation_core
        session = get_conversation_core()._session
        if session is None:
            return ""
        max_messages = session.get_max_messages()
        sent_count = session.get_sent_message_count()
        if max_messages > 0 and sent_count >= max_messages:
            return f"【系统提示】当前已达到本轮策略中计划发送的消息条目数量（{sent_count}/{max_messages}），非必要时，请使用 transfer_task_to_PFCA_agent 回到策略阶段重新拟定策略。"
        return ""

    def _get_conversation_purpose(self) -> str:
        """获取本次会话目的（由 PFCA 走 tool 链路传入，conversation 自身持有）"""
        from brain.chat.conversation.conversation_core import get_conversation_core
        core = get_conversation_core()
        return getattr(core, "purpose", "") or ""

    async def _append_matched_bequest(self, header: str, user_input: str) -> str:
        """向量匹配经验并追加到输入（排除固定经验）。

        Args:
            header: 匹配到经验时的段落标题（如 "【响应的经验】"）。
            user_input: 待追加的输入文本。
        """
        if not user_input:
            return user_input

        try:
            from brain.hc.database.bequest import BequestOperations
            from brain.chat.conversation.conversation_bequest import get_conversation_bequest
            from config.settings import chat_config

            bequest_ops = BequestOperations()
            bequest_cache = get_conversation_bequest()

            fixed_title = getattr(chat_config, 'FIXED_BEQUEST_TITLE', None)
            fixed_id = await bequest_ops.get_bequest_id_by_title(fixed_title) if fixed_title else None

            matched = await bequest_ops.search_bequest_by_vector(user_input, top_k=3)
            if not matched:
                return user_input

            activated_titles = []
            matched_lines = []
            count = 0
            for bequest_id in matched:
                if fixed_id and bequest_id == fixed_id:
                    continue

                if bequest_cache.is_activated(bequest_id):
                    continue

                data = await bequest_ops.get_bequest_by_id(bequest_id)
                if not data:
                    continue

                if data.get("apply_scenario") == "decision":
                    continue

                title = data.get("title", "未知")
                summary = data.get("summary", "")
                if summary:
                    bequest_cache.add(bequest_id)
                    matched_lines.append(f"你回想起了经验：{title} - {summary}")
                    activated_titles.append(title)
                    count += 1
                    if count >= 3:
                        break

            if matched_lines:
                user_input = f"{user_input}\n{header}\n" + "\n".join(matched_lines)

            if activated_titles:
                logger.info(f"已激活经验: {', '.join(activated_titles)}")

            return user_input

        except Exception as e:
            logger.warning(f"经验匹配失败: {e}")
            return user_input


def get_conversation_input_manager() -> ConversationInputManager:
    """获取 ConversationInputManager 单例"""
    return ConversationInputManager()
