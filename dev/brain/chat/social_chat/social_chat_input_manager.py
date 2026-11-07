# -*- coding: utf-8 -*-
"""social_chat 输入管理器：构造首条 user 消息（人格 + 任务书 + 发送模式说明 + 情绪 + 记事本手册 + 经验）与增量输入。

数据源统一走 memory.session_history 的游标拉取（consumer_id=CONVERSATION_CONSUMER_ID），
与 conversation 共用同一游标：两种模式读同一批会话数据，任一模式消费过的消息
另一模式不再重复拉取（审批轮询的 social_chat_approval 专用游标独立，不受影响）。

条目对齐 conversation_input_manager：
- 人格段（首条，最前）
- 任务说明（首条）
- 发送模式 block（首条，approval / unrestricted 两态）
- amy_effect（首条）
- 记事本手册（首条，对话场景子集 + 通用，与 conversation 同一套 shortlight 手册）
- 经验激活（首条，心情/任务双通道向量匹配 + 缓存）
- 主人 / 工作会话新消息（增量输入；工作会话注入受 SOCIAL_CHAT_DUAL_PUSH 开关控制）
- 未使用工具警告（增量输入注入）
"""

import asyncio
from typing import Dict, Any, Optional, List
from utils.logger import get_logger
from config.settings import chat_config
from utils.error_handling import async_handle_errors

from brain.base.common_ways.input_helpers import (
    CONVERSATION_CONSUMER_ID,
    build_social_chat_environment,
)

logger = get_logger('SocialChatInputManager', "INFO")

# 用户消息延长观察窗口（秒）：检测到用户新消息后延时等待用户完整输入，
# 窗口期内继续到达的消息随本次 fetch_unread 一并消费，避免截断用户输入
USER_OBSERVATION_WINDOW_S = 3.0

# ---------- 发送模式 block 文案 ----------
_MODE_APPROVAL = (
    "\n【发送模式：审批】\n"
    "- 你每次调用 send_to_target 前，系统会把消息全文发给主人审批："
    "主人回复『同意』才会真正发出；回复其他任何内容或超时都会拒绝本次发送。\n"
    "- 被拒 / 超时不是崩溃：与主人沟通调整后可再次尝试。\n"
    "- 单条消息的批准不会开启自由模式。可用 set_send_mode 申请自由发送（须主人回复『同意』）。\n"
    "- 自由授权只限当前事件链的当前工作会话；再次进入该会话仍有效，链结束即失效，其他会话需另行申请。"
)
_MODE_UNRESTRICTED = (
    "\n【发送模式：自由】\n"
    "- 你可以直接调用 send_to_target 向工作会话发送消息，无需逐条审批。\n"
    "- 当前模式只限本事件链的当前工作会话；再次进入该会话仍有效，事件链结束后重新按配置初始化。\n"
    "- 如需恢复逐条审批，可调用 set_send_mode(mode=\"approval\")，立即生效。"
)


def _build_mode_block(mode: str) -> str:
    if mode == "unrestricted":
        notice = "\n- 每条发出后系统会自动知会主人。" if chat_config.SOCIAL_CHAT_UNRESTRICTED_NOTIFY else ""
        return _MODE_UNRESTRICTED + notice
    if mode == "approval":
        return _MODE_APPROVAL
    raise ValueError(f"非法发送模式: {mode!r}")


class SocialChatInputManager:
    """social_chat 输入管理器（单例）。

    条目：
    1. 首条：人格段 + 主人/工作会话近期记录 + 会话环境 + 任务书 + 发送模式 block + amy_effect + 记事本手册 + 经验激活
    2. 增量：主人会话新消息 +（SOCIAL_CHAT_DUAL_PUSH 开启时）工作会话新消息 + 未调工具警告
    """

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        # ========== 运行状态 ==========
        self.running = False
        self._first_activation = True

        # ========== 任务上下文 ==========
        # master / working 会话 ID 不在本类缓存，每次调用从 social_chat_core 读。
        # social_chat_core 是 single source of truth，本类只持有引用。
        self._social_chat_core = None
        self._task_purpose: str = ""
        self._permissions = None
        self._permission_scope = None

        # ========== 已激活经验缓存（避免重复注入） ==========
        self._activated_bequests: set = set()

        # ========== 警告消息 ==========
        self._no_tool_call_warning: str = ""

        self._initialized = True
        logger.debug("SocialChatInputManager 实例级状态初始化")

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def reset(self):
        """重置状态（会话开始时调用）"""
        self._first_activation = True
        self._activated_bequests = set()
        self._no_tool_call_warning = ""
        self._permissions = None
        self._permission_scope = None

    # ----------------- 配置接口 -----------------
    def prepare_first_message(
        self,
        purpose: str,
        social_chat_core,
    ) -> None:
        """准备首条 user 消息所需的全部上下文。

        master / working 会话 ID 一律从 social_chat_core 读，调用方不传。
        social_chat_core 必须非 None——core 没初始化就直接崩溃（符合 user_rules）。
        发送模式从当前事件链权限服务实时读取，不接受调用方指定。
        """
        if not purpose:
            raise ValueError("prepare_first_message 缺少必要参数 purpose")
        if social_chat_core is None:
            raise ValueError("prepare_first_message 缺少 social_chat_core 引用")

        # 顺手从 core 拉一次 ID 校验 core 已 start()；如未启动直接抛错阻断
        _ = social_chat_core.master_session_id
        _ = social_chat_core.working_session_id

        self._social_chat_core = social_chat_core
        self._task_purpose = purpose
        from brain.hc.global_session_state import get_global_session_state
        self._permissions = get_global_session_state().get_event_chain_permissions()
        self._permission_scope = (social_chat_core.master_session_id, social_chat_core.working_session_id)
        self._first_activation = True
        logger.info(
            f"social_chat 准备首条 user 消息: send_mode={self._current_send_mode()}, "
            f"master={social_chat_core.master_session_id}, "
            f"working={social_chat_core.working_session_id}"
        )

    def _current_send_mode(self) -> str:
        if self._permissions is None or self._permission_scope != (
            self._social_chat_core.master_session_id, self._social_chat_core.working_session_id
        ):
            raise RuntimeError("Social Chat 输入未绑定当前事件链及目标会话")
        return self._permissions.get_send_mode(*self._permission_scope)

    # ----------------- 对外接口 -----------------
    @async_handle_errors()
    async def get_user_input(self) -> Optional[str]:
        if self._first_activation:
            self._first_activation = False
            # 首条两线均拉取，故两线均检测观察窗口
            await self._wait_user_observation_window(check_working=True)
            return await self._build_first_input()
        # 增量阶段仅检测本轮真正会消费的线：working 线受 SOCIAL_CHAT_DUAL_PUSH 控制，
        # 未消费线的未读数恒 > 0，检测会导致每轮循环误延时
        await self._wait_user_observation_window(
            check_working=bool(chat_config.SOCIAL_CHAT_DUAL_PUSH)
        )
        return await self._build_incremental_input()

    async def _wait_user_observation_window(self, check_working: bool) -> None:
        """用户消息延长观察窗口：检测到用户新消息则延时 USER_OBSERVATION_WINDOW_S 秒再构造输入。

        遍历（master [, working]）会话线只读检测未读用户消息数
        （user_id != 'system'，AI 自身 outbound 不算），任一线 > 0 说明用户
        正在（或刚结束）输入——延时等待窗口期，让继续到达的消息随本次
        fetch_unread 一并消费，避免截断用户完整输入。
        count_unread_user_for 失败直接抛错崩溃阻断（符合 user_rules）。

        Args:
            check_working: 是否检测 working 线（须与本轮实际消费的线一致）。
        """
        from memory.message_repository import get_message_store

        session_ids = [self._social_chat_core.master_session_id]
        if check_working:
            session_ids.append(self._social_chat_core.working_session_id)

        store = get_message_store()
        for sid in session_ids:
            unread_user = await store.count_unread_user_for(sid, CONVERSATION_CONSUMER_ID)
            if unread_user > 0:
                logger.info(
                    f"会话 {sid} 检测到 {unread_user} 条未读用户消息，"
                    f"观察窗口延时 {USER_OBSERVATION_WINDOW_S} 秒等待用户完整输入"
                )
                await asyncio.sleep(USER_OBSERVATION_WINDOW_S)
                return

    # ----------------- 首条 user 消息 -----------------
    async def _build_first_input(self) -> str:
        """构造首条 user 消息：

            0. 人格段（最前，本段社交显式指定的人格内核）
            1. 已经发生的聊天记录（主人会话 + 工作会话，游标拉取）
            2. 当前会话环境（合并单段：主人线 + 工作线）
            3. 【本次聊天任务】
            4. 发送模式 block
            5. amy_effect
            6. 记事本手册（对话场景子集 + 通用，拼在情绪段后、经验段前）
        """
        # 0) 一次性解析两条会话线的名字（聊天记录与环境段共用）
        sess_name_map = self._resolve_session_names(
            [self._social_chat_core.master_session_id, self._social_chat_core.working_session_id]
        )
        master_name = sess_name_map.get(self._social_chat_core.master_session_id, "")
        working_name = sess_name_map.get(self._social_chat_core.working_session_id, "")

        # 1) 已经发生的聊天记录（游标拉取，游标推进）
        message_str = await self._get_first_social_history(master_name, working_name)

        # 2) 会话环境（合并单段，避免两条线各一段【当前会话环境】段头）
        env_text = build_social_chat_environment(
            master_session_id=self._social_chat_core.master_session_id,
            master_name=master_name,
            working_session_id=self._social_chat_core.working_session_id,
            working_session_name=working_name,
        )

        # 3) 任务
        task_block = f"【本次聊天任务】\n{self._task_purpose}"

        # 4) 发送模式
        mode_block = _build_mode_block(self._current_send_mode())

        # 5) amy_effect（表现侧出口：仅 NE/DA 两行激素描述 + 原因（仅非最终评价类想法），
        #    不含 SC 行——pfca 决策侧情绪体系与本模块隔离，见 docs/api/amy.md）
        #    展示块暂时隐藏原因（amy_effect_display，with_reason=False）；
        #    amy_effect（带原因）仍作为经验检索心情通道的 query，检索行为不变
        try:
            from brain.amy.emotional_interface import get_emotional_interface
            ei = await get_emotional_interface()
            amy_effect = await ei.get_language_description()
            amy_effect_display = await ei.get_language_description(with_reason=False)
        except Exception as e:
            logger.debug(f"social_chat 获取 amy_effect 失败: {e}")
            amy_effect = ""
            amy_effect_display = ""

        # 拼接：用 parts 拼接避免连续空行（段头样式与 conversation 对齐）
        parts: List[str] = [f"【已经发生的聊天记录】\n{message_str}"]
        if env_text:
            parts.append(env_text)
        parts.append(task_block)
        parts.append(mode_block)
        if amy_effect_display:
            parts.append(f"【你的情绪状态】{amy_effect_display}")

        user_input = "\n".join(parts)

        # 人格段（最前）：本段社交由 social_chat_agent 显式指定的人格内核，
        # 与 conversation 同款注入位置（整条消息之首）。
        persona_header = self._build_persona_header()
        if persona_header:
            user_input = f"{persona_header}\n{user_input}"

        # 6) 记事本手册：长期经验底色（全部已落库条目，条目的 category 不参与路由），
        #    章节式裸文本；拉取失败直接上抛崩溃阻断（按 user_rules），细节经 query_note 工具溯源
        from brain.hc.shortlight import get_shortlight_notepad
        manual = await get_shortlight_notepad().render_manual()
        if manual:
            user_input = f"{user_input}\n{manual}"

        user_input = await self._append_matched_bequest(amy_effect, self._task_purpose, user_input)

        logger.info(f"social_chat 生成第一段输入，长度: {len(user_input)}")
        return user_input

    def _build_persona_header(self) -> str:
        """构建首条 user 消息开头的【当前人格】段（本段社交显式指定的人格）。

        人格由 social_chat_agent 经 social_chat_core.persona_name 传入（缺省正常人格），
        本段注入该人格提示词（公共聊天准则 + kernel_prompt，公共段在前），
        作为本次社交的语言与行为基准。
        人格不存在属重大关键路径失败，get_persona 直接抛 KeyError 崩溃阻断（user_rules）。
        """
        from brain.base.persona.persona_registry import compose_persona_prompt, get_persona

        persona = get_persona(self._social_chat_core.persona_name)
        return f"【当前人格】{persona.persona_name}\n{compose_persona_prompt(persona)}"

    async def _fetch_one_history(
        self,
        session_id: str,
        sess_name: str,
        max_unread: int,
        min_messages: int,
    ) -> str:
        """游标拉取单个会话的消息并渲染为格式化行（推进游标）。

        首次（游标 -1）会拉取该会话最近的 max_unread 条；之后只拉游标之后的新消息。
        关键路径（fetch）失败一律直接 raise，符合 user_rules 关键方法失败应崩溃阻断。
        """
        from memory.session_history import (
            get_global_session_history,
            FetchOptions,
            MessageFormatOptions,
        )

        history = get_global_session_history()
        result = await history.fetch_unread(
            session_id,
            sess_name,
            consumer_id=CONVERSATION_CONSUMER_ID,
            options=FetchOptions(
                message_format=MessageFormatOptions(
                    show_new_marker=True,
                    show_timestamp=True,
                    exclude_system=False,
                ),
            ),
            advance_cursor=True,
            max_unread=max_unread,
            min_messages=min_messages,
        )

        lines = result.formatted_lines or []
        return "\n".join(lines) if lines else ""

    async def _get_first_social_history(self, master_name: str, working_name: str) -> str:
        """拉取主人会话与工作会话的近期记录（渲染顺序固定：主人会话在前）。

        名字由 _build_first_input 一次性解析后传入（环境段共用同一份解析结果）。
        """
        master_block = await self._fetch_one_history(
            self._social_chat_core.master_session_id,
            master_name,
            max_unread=30,
            min_messages=10,
        )
        working_block = await self._fetch_one_history(
            self._social_chat_core.working_session_id,
            working_name,
            max_unread=30,
            min_messages=10,
        )

        return (
            f"主人会话：\n{master_block or '（无）'}\n"
            f"工作会话：\n{working_block or '（无）'}"
        )

    def _resolve_session_names(self, session_ids: List[str]) -> Dict[str, str]:
        """从 pfca_sessions 反查 session_name（缺失返回空串）。"""
        from brain.hc.global_session_state import get_global_session_state

        result: Dict[str, str] = {}
        pfca_sessions = get_global_session_state().get_sessions()
        for sid in session_ids:
            name = ""
            for s in pfca_sessions:
                if s.get("session_id") == sid:
                    name = s.get("session_name", "") or ""
                    break
            result[sid] = name
        return result

    # ----------------- 增量输入 -----------------
    async def _build_incremental_input(self) -> Optional[str]:
        """构造增量输入：

            1. 主人会话新消息（恒定注入——主人可见性是监管与沟通的基础）
            2. 工作会话新消息（受 SOCIAL_CHAT_DUAL_PUSH 开关控制）
            3. 未使用工具警告
        """
        sess_name_map = self._resolve_session_names(
            [self._social_chat_core.master_session_id, self._social_chat_core.working_session_id]
        )

        msg_parts: List[str] = []

        master_block = await self._fetch_one_history(
            self._social_chat_core.master_session_id,
            sess_name_map.get(self._social_chat_core.master_session_id, ""),
            max_unread=20,
            min_messages=0,
        )
        if master_block:
            msg_parts.append(f"主人会话：\n{master_block}")

        if chat_config.SOCIAL_CHAT_DUAL_PUSH:
            working_block = await self._fetch_one_history(
                self._social_chat_core.working_session_id,
                sess_name_map.get(self._social_chat_core.working_session_id, ""),
                max_unread=20,
                min_messages=0,
            )
            if working_block:
                msg_parts.append(f"工作会话：\n{working_block}")

        user_input = "\n\n".join(msg_parts) if msg_parts else None

        # 未使用工具警告
        if self._no_tool_call_warning:
            user_input = f"{user_input}\n{self._no_tool_call_warning}" if user_input else self._no_tool_call_warning

        return user_input or None

    def _build_message_limit_hint(self) -> str:
        """social_chat 无 PFCA 策略限额机制；保留与 conversation 同款接口，恒返回空串。"""
        return ""

    # ----------------- 新消息监听（消费式 unread 队列语义） -----------------
    async def has_new_messages(self, session_id: str) -> Dict[str, Any]:
        """检查指定会话是否有新未读消息（只读不消费，供 wait_and_observe 中断等待）。

        关键路径（fetch）失败一律 raise，符合 user_rules 关键方法失败应崩溃阻断。

        Returns:
            dict: {"has_new": bool, "formatted_lines": list[str], "message_count": int}
        """
        if not session_id:
            raise ValueError("session_id 必填")

        from brain.base import ToolHandlers
        from memory.session_history import MessageFormatOptions

        result = await ToolHandlers.peek_unread(
            session_id,
            CONVERSATION_CONSUMER_ID,
            format_options=MessageFormatOptions(
                show_new_marker=True,
                show_timestamp=False,
                exclude_system=True,
            ),
            max_unread=5,
            min_messages=0,
        )  # 只读不消费，消息仍保持未读供增量输入正常拉取

        # 仅统计用户消息（排除 system / AI 自身 outbound），对齐 wait_and_observe 语义
        user_messages = [
            m for m in (result.messages or [])
            if m.get("user_id") != "system"
        ]
        lines = [
            line for m, line in zip(
                [m for m in (result.messages or []) if m.get("user_id") != "system"],
                [line for m, line in zip(result.messages or [], result.formatted_lines or [])
                 if m.get("user_id") != "system"],
            )
        ]
        return {
            "has_new": bool(user_messages),
            "formatted_lines": lines,
            "message_count": len(user_messages),
        }

    # ----------------- 警告消息 -----------------
    def set_no_tool_call_warning(self, message: str) -> None:
        self._no_tool_call_warning = message

    def clear_no_tool_call_warning(self) -> None:
        self._no_tool_call_warning = ""

    # ----------------- amy / thoughts -----------------
    # 注：情绪表现侧出口 get_language_description() 恒带成因（仅 NE/DA 两行激素描述
    # + 原因：仅非最终评价类想法摘要），首条直调门面获取（见 _build_first_input），
    # 本地不再二次拼接 thoughts；PFCA 决策侧（SC）情绪体系与本模块隔离。

    # ----------------- 经验激活 -----------------
    async def _append_matched_bequest(
        self, mood_query: str, purpose_query: str, user_input: str
    ) -> str:
        """分通道经验检索并追加到输入（检索核心走 common_ways，与 conversation 同源）。

        注入分三段：
        - 固定搭配·心情通道（最多 4 条）：amy 表现侧情绪描述作 query，召回可直接说出的原话短语；
        - 固定搭配·任务通道（最多 6 条）：PFCA 下达的本次聊天任务作 query，召回任务相关原话短语；
        - 可模仿的说话样本（最多 2 条）：心情 + 任务合并作 query，召回他人原话样本。

        固定搭配允许直接照用原话，也可按当下语序拼接改造；风格样本只学语气、
        用词与节奏，不复述内容。

        本模块差异点：
        - 仅在首条 user 消息构造时激活一次；
        - 激活缓存与 social_chat_tool_manager.experience_agent 共用同一份
          （self._activated_bequests），保证 run 内不重复注入。

        Args:
            mood_query: 心情通道检索 query（amy 情绪描述，可为空）。
            purpose_query: 任务通道检索 query（本次聊天任务，可为空）。
            user_input: 待追加的输入文本。
        """
        if not user_input:
            return user_input

        try:
            from brain.base.common_ways.bequest_helpers import activate_bequests_for_input

            channel_seen: set = set()

            def _skip(bequest_id: str) -> bool:
                return bequest_id in channel_seen or bequest_id in self._activated_bequests

            channels = [
                ("【可直接用的固定搭配·契合当前心情】", mood_query, 4),
                ("【可直接用的固定搭配·契合本次任务】", purpose_query, 6),
            ]

            activated_titles = []
            sections = []
            for header, query, limit in channels:
                query = (query or "").strip()
                if not query:
                    continue
                matched = await activate_bequests_for_input(
                    query, skip=_skip, top_k=limit, max_activate=limit, category="speech",
                )
                if not matched:
                    continue
                lines = []
                for entry in matched:
                    channel_seen.add(entry["id"])
                    self._activated_bequests.add(entry["id"])
                    activated_titles.append(entry["title"])
                    lines.append(f"· {entry['summary']}：{entry['title']}")
                sections.append(
                    f"{header}\n"
                    "这些是可直接说出的固定说法。语境合适就照用原话；也可按当下的语序拼接、改造。"
                    "若某条只是解释、没有可直接说的话，就忽略它。\n"
                    + "\n".join(lines)
                )

            # 风格样本：他人原话，只学怎么说，不复述内容
            style_query = "\n".join(
                q for q in ((mood_query or "").strip(), (purpose_query or "").strip()) if q
            )
            if style_query:
                matched = await activate_bequests_for_input(
                    style_query, skip=_skip, top_k=3, max_activate=2, category="style",
                )
                if matched:
                    lines = []
                    for entry in matched:
                        channel_seen.add(entry["id"])
                        self._activated_bequests.add(entry["id"])
                        activated_titles.append(entry["title"])
                        lines.append(f"· {entry['title']}：{entry['summary']}")
                    sections.append(
                        "【可模仿的说话样本】\n"
                        "这些是他人原话样本。模仿其节奏、用词、标点和语气；只学怎么说，不要复述样本内容。\n"
                        + "\n".join(lines)
                    )

            if activated_titles:
                logger.info(f"social_chat 已激活经验: {', '.join(activated_titles)}")

            if sections:
                user_input = f"{user_input}\n" + "\n".join(sections)

            return user_input

        except Exception as e:
            # 按 user_rules：关键注入失败直接崩溃阻断，不吞错返回残缺输入。
            logger.exception(f"social_chat 经验匹配失败: {e}")
            raise


_input_manager_instance: Optional[SocialChatInputManager] = None


def get_social_chat_input_manager() -> SocialChatInputManager:
    """获取 SocialChatInputManager 单例"""
    global _input_manager_instance
    if _input_manager_instance is None:
        _input_manager_instance = SocialChatInputManager()
    return _input_manager_instance
