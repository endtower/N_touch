# -*- coding: utf-8 -*-
"""
Conversation 输入管理模块

负责管理 Conversation 的 user 消息内容构造：
1. 首条消息构建：聊天记录 + 会话环境 + 聊天任务 + 情绪 + 经验（格式预期见 _build_first_input docstring）
2. 增量消息构建：仅新消息，无情绪
3. 其他辅助方法：限额提示、经验匹配等

设计原则：
- 情绪仅在首条消息时添加
- 经验缓存用于避免重复激活
"""

import asyncio
from typing import Any, Dict, Optional
from utils.logger import setup_logging
from utils.error_handling import async_handle_errors
from config.role_settings import RoleConfig
from brain.base import build_session_environment, CONVERSATION_CONSUMER_ID

logger = setup_logging(__name__, log_level="INFO")

# 用户消息延长观察窗口（秒）：检测到用户新消息后延时等待用户完整输入，
# 窗口期内继续到达的消息随本次 fetch_unread 一并消费，避免截断用户输入
USER_OBSERVATION_WINDOW_S = 3.0


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
        # 用户消息观察窗口：检测到用户新消息则延时等待其完整输入，再统一构造
        await self._wait_user_observation_window()

        is_first = self._first_activation
        self._first_activation = False

        if is_first:
            return await self._build_first_input()
        else:
            return await self._build_incremental_input()

    async def _wait_user_observation_window(self) -> None:
        """用户消息延长观察窗口：检测到用户新消息则延时 USER_OBSERVATION_WINDOW_S 秒再构造输入。

        只读检测本消费者的未读用户消息数（user_id != 'system'，AI 自身 outbound 不算），
        > 0 说明用户正在（或刚结束）输入——延时等待窗口期，让继续到达的消息
        随本次 fetch_unread 一并消费，避免截断用户完整输入。
        count_unread_user_for 失败直接抛错崩溃阻断（符合 user_rules）。
        """
        from brain.chat.conversation.conversation_core import get_conversation_core
        from memory.message_repository import get_message_store

        conv_id = get_conversation_core().conversation_id
        if not conv_id:
            return

        unread_user = await get_message_store().count_unread_user_for(
            conv_id, CONVERSATION_CONSUMER_ID
        )
        if unread_user > 0:
            logger.info(
                f"检测到 {unread_user} 条未读用户消息，"
                f"观察窗口延时 {USER_OBSERVATION_WINDOW_S} 秒等待用户完整输入"
            )
            await asyncio.sleep(USER_OBSERVATION_WINDOW_S)

    # ============================================================
    # 首条消息构建
    # ============================================================

    async def _build_first_input(self) -> str:
        """构建首条消息，预期拼接格式（各段按序 "\\n".join，条件段可能缺席）：

            【已经发生的聊天记录】               <- 固定首行标题
            {游标拉取的未读消息渲染}             <- fetch_unread(consumer_id="conversation")
            【当前会话环境】你正在与{name} id {session_id} 交流
            {私聊/群聊可见性 + 非主人警示}       <- build_session_environment 生成，自带标题
            【本次聊天任务】
            {conversation_purpose}
            【你的情绪状态】{amy 统一出口描述}   <- 三行激素 + 原因；取到非空才拼
            【记事本手册】                       <- 记事本手册非空才拼（render_manual 生成，自带段头）：
            记事本存在以下章节:                     对话场景子集 + 通用条目，
            {章名}                                  ≤3 章、正文 ≤300 字符、每章 ≤5 条新挤旧
            └ {条目}
            可以使用工具查询记事本某一章节的具体信息以查看细节。
            【响应的经验·契合当前心情】           <- 心情通道（amy 情绪描述）命中才追加
            你回想起了经验：{title} - {summary}   每通道最多 3 条，共最多 6 条
            【响应的经验·契合本次任务】           <- 任务通道（PFCA 任务书）命中才追加
            你回想起了经验：{title} - {summary}
            【系统提示】…（{sent}/{max}）        <- 达到条数上限才追加

        环境段标题由 build_session_environment 内置，拼接处不得再手动包前缀（避免双标题）。
        记事本手册（章节式裸文本）注入在情绪段后、经验段前——长期底色先于动态经验；
        手册拉取失败直接上抛崩溃阻断（按 user_rules），细节经 query_note 工具溯源。
        """
        from brain.chat.conversation.conversation_core import get_conversation_core
        from memory.session_history import get_global_session_history, FetchOptions, MessageFormatOptions

        core = get_conversation_core()
        conv_id = core.conversation_id
        sess_name = core.session_name or ""
        if not conv_id:
            return "过去没有你们没有发生过任何对话"

        # 游标拉取：获取本消费者（conversation）未处理的消息，并推进游标。
        # 首次（游标为 -1）会拉取该会话全部消息；之后只拉取游标之后的新消息。
        # CONVERSATION_CONSUMER_ID 为 conversation / social_chat 共用游标（见 common_ways/input_helpers）。
        history = get_global_session_history()
        result = await history.fetch_unread(
            conv_id,
            sess_name,
            consumer_id=CONVERSATION_CONSUMER_ID,
            options=FetchOptions(
                message_format=MessageFormatOptions(show_new_marker=True, show_timestamp=True),
                header_model="chat_message",
            ),
        )

        message_str = "\n".join(result.formatted_lines) if result.formatted_lines else "没有新的聊天记录"

        conversation_purpose = self._get_conversation_purpose()

        from brain.amy.emotional_interface import get_emotional_interface
        ei = await get_emotional_interface()
        # 表现侧出口：仅 NE/DA 两行激素描述 + 原因（仅非最终评价类想法），
        # 不含 SC 行——pfca 决策侧情绪体系与本模块隔离（见 docs/api/amy.md）
        amy_effect = await ei.get_language_description()

        # 按实际消息拼接顺序组装：聊天记录 -> 会话环境 -> 任务 -> 情绪
        _parts = [f"{message_str}"]
        # 当前会话环境段：从当前会话取 session_id 与 session_name，按角色/群聊/是否主人动态拼接。
        # build_session_environment 自带【当前会话环境】标题（与 social_chat 同一 helper），不再手动加前缀
        env_text = build_session_environment(
            session_id=conv_id,
            session_name=sess_name,
            master_id=RoleConfig.MASTER_ID,
        )
        if env_text:
            _parts.append(env_text)
        _parts.append(
            f"【本次聊天任务】\n{conversation_purpose}"
        )
        if amy_effect:
            _parts.append(f"【你的情绪状态】{amy_effect}")
        user_input = "\n".join(["【已经发生的聊天记录】"] + _parts)

        # 记事本手册：长期经验底色（全部已落库条目，usage 不参与路由），
        # 章节式裸文本；拉取失败直接上抛崩溃阻断（按 user_rules），细节经 query_note 工具溯源
        from brain.hc.shortlight import get_shortlight_notepad
        manual = await get_shortlight_notepad().render_manual()
        if manual:
            user_input = f"{user_input}\n{manual}"

        user_input = await self._append_matched_bequests(amy_effect, conversation_purpose, user_input)

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
            consumer_id=CONVERSATION_CONSUMER_ID,
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
        """检查发送轮次限制，若达到限制则返回提示文本"""
        from brain.chat.conversation.conversation_core import get_conversation_core
        session = get_conversation_core()._session
        if session is None:
            return ""
        max_messages = session.get_max_messages()
        sent_count = session.get_sent_message_count()
        if max_messages > 0 and sent_count >= max_messages:
            return f"【系统提示】当前已达到本轮策略中计划发送的轮次数量（{sent_count}/{max_messages}），建议更多观察对方反应，获取更多信息后，使用 transfer_task_to_PFCA_agent 回到策略阶段重新拟定策略。"
        return ""

    def _get_conversation_purpose(self) -> str:
        """获取本次会话目的（由 PFCA 走 tool 链路传入，conversation 自身持有）"""
        from brain.chat.conversation.conversation_core import get_conversation_core
        core = get_conversation_core()
        return getattr(core, "purpose", "") or ""

    async def _append_matched_bequests(self, mood_query: str, purpose_query: str, user_input: str) -> str:
        """双通道经验检索并追加到输入（心情 + 任务，检索核心走 common_ways，与 social_chat 同源）。

        通道划分（每通道 top_k=3、最多激活 3 条，合计最多 6 条）：
        - 心情通道：amy 表现侧情绪描述（get_language_description 出口）作 query，
          召回表达方式类话术经验；
        - 任务通道：PFCA 下达的本次聊天任务作 query，召回任务相关话术经验。

        跨通道按 bequest_id 去重 + 激活缓存去重（已激活不再注入）；
        检索失败直接上抛崩溃阻断（按 user_rules，与记事本手册同策略）。

        Args:
            mood_query: 心情通道检索 query（amy 情绪描述，可为空）。
            purpose_query: 任务通道检索 query（本次聊天任务，可为空）。
            user_input: 待追加的输入文本。
        """
        if not user_input:
            return user_input

        from brain.base.common_ways.bequest_helpers import activate_bequests_for_input
        from brain.chat.conversation.conversation_bequest import get_conversation_bequest

        bequest_cache = get_conversation_bequest()
        channel_seen: set = set()

        def _skip(bequest_id: str) -> bool:
            return bequest_id in channel_seen or bequest_cache.is_activated(bequest_id)

        channels = [
            ("【响应的经验·契合当前心情】", mood_query),
            ("【响应的经验·契合本次任务】", purpose_query),
        ]

        sections = []
        for header, query in channels:
            query = (query or "").strip()
            if not query:
                continue
            matched = await activate_bequests_for_input(
                query, skip=_skip, top_k=3, max_activate=3,
            )
            if not matched:
                continue
            lines = []
            for entry in matched:
                channel_seen.add(entry["id"])
                bequest_cache.add(entry["id"])
                lines.append(f"你回想起了经验：{entry['title']} - {entry['summary']}")
                logger.info(f"已激活经验({header}): {entry['title']} score={entry['score']:.3f}")
            sections.append(f"{header}\n" + "\n".join(lines))

        if sections:
            user_input = f"{user_input}\n" + "\n".join(sections)
        return user_input


_input_manager_instance: Optional[ConversationInputManager] = None


def get_conversation_input_manager() -> ConversationInputManager:
    """获取 ConversationInputManager 单例"""
    global _input_manager_instance
    if _input_manager_instance is None:
        _input_manager_instance = ConversationInputManager()
    return _input_manager_instance
