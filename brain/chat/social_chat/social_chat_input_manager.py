# -*- coding: utf-8 -*-
"""social_chat 输入管理器：构造首条 user 消息（任务书 + 预设条目池 + 等级限制说明 + 情绪 + 经验）。

对齐 conversation_input_manager 的核心条目：
- 任务说明（首条）
- amy_effect（首条）
- thoughts 列表（首条）
- 经验激活（首条，向量匹配 + 缓存）
- 未使用工具警告（增量输入注入）
- 主人会话新消息增量（增量输入，与 L3 双会话推送并存）
"""

from datetime import datetime
from typing import Dict, Any, Optional, List
from utils.logger import get_logger
from config.settings import chat_config
from utils.error_handling import async_handle_errors
from config.role_settings import RoleConfig

from brain.chat.conversation.conversation_input_manager import build_session_environment

logger = get_logger('SocialChatInputManager', "INFO")

# 系统硬编码的限制等级（统一从 chat_config 读取，详见 config.settings.ChatConfig.SOCIAL_CHAT_ENFORCEMENT_LEVEL）
DEFAULT_ENFORCEMENT_LEVEL = chat_config.SOCIAL_CHAT_ENFORCEMENT_LEVEL


# ---------- 等级 block 文案 ----------
_LEVEL_L3 = (
    "\n【限制等级 L3 - 硬约束】\n"
    "- 你的发言对象只看主人会话与工作会话两条线。\n"
    "- 行动前必须先看工作会话的最新情况（主动 query_history）。\n"
)
_LEVEL_L2 = (
    "\n【限制等级 L2 - 软约束】\n"
    "- 你应优先遵循社会计划原定话术；在发送消息时应得到主人的批准。\n"
)
_LEVEL_L1 = (
    "\n【限制等级 L1 - 自由】\n"
    "- 你可以自由发挥，按需使用工具。"
)


def _build_level_block(level: str) -> str:
    level = (level or "L1").upper()
    if level == "L3":
        return _LEVEL_L3
    if level == "L2":
        return _LEVEL_L2
    return _LEVEL_L1


class SocialChatInputManager:
    """social_chat 输入管理器（单例）。

    对齐 conversation_input_manager 的关键条目：
    1. 首条：任务书 + 预设条目池 + 等级 + amy_effect + thoughts + 经验激活 + L3 双会话推送
    2. 增量：主人会话新消息 +（L3 时）双会话最新未读 + 未调工具警告
    """

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        # ========== 运行状态 ==========
        self.running = False
        self._first_activation = True

        # ========== 任务上下文 ==========
        # master / working 会话 ID 不再在本类缓存，改为每次调用从 social_chat_core 读。
        # social_chat_core 是 single source of truth，本类只持有引用。
        self._social_chat_core = None
        self._task_purpose: str = ""
        self._preset_messages: List[dict] = []
        self._enforcement_level: str = DEFAULT_ENFORCEMENT_LEVEL

        # ========== 已激活经验缓存（避免重复注入） ==========
        self._activated_bequests: set = set()
        # 固定经验（FIXED_BEQUEST_TITLE）的 ID，构造提示词时与对话经验区分
        self._fixed_bequest_id: Optional[str] = None

        # ========== 警告消息 ==========
        self._no_tool_call_warning: str = ""

        # 阶段汇报窗口起点改由 social_chat_core 各自快照并写入。

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
        self._fixed_bequest_id = None
        self._no_tool_call_warning = ""

    # ----------------- 配置接口 -----------------
    def prepare_first_message(
        self,
        purpose: str,
        preset_messages: List[dict],
        enforcement_level: str,
        social_chat_core,
    ) -> None:
        """准备首条 user 消息所需的全部上下文。

        master / working 会话 ID 一律从 social_chat_core 读，调用方不传。
        social_chat_core 必须非 None——core 没初始化就直接崩溃（符合 user_rules）。
        """
        if not purpose:
            raise ValueError("prepare_first_message 缺少必要参数 purpose")
        if not preset_messages:
            raise ValueError("预设条目池不能为空")
        if social_chat_core is None:
            raise ValueError("prepare_first_message 缺少 social_chat_core 引用")

        # 顺手从 core 拉一次 ID 校验 core 已 start()；如未启动直接抛错阻断
        _ = social_chat_core.master_session_id
        _ = social_chat_core.working_session_id

        self._social_chat_core = social_chat_core
        self._task_purpose = purpose
        self._preset_messages = list(preset_messages)
        self._enforcement_level = (enforcement_level or DEFAULT_ENFORCEMENT_LEVEL).upper()
        self._first_activation = True
        logger.info(
            f"social_chat 准备首条 user 消息: level={self._enforcement_level}, "
            f"preset_count={len(self._preset_messages)}, "
            f"master={social_chat_core.master_session_id}, "
            f"working={social_chat_core.working_session_id}"
        )

    # ----------------- 对外接口 -----------------
    @async_handle_errors()
    async def get_user_input(self) -> Optional[str]:
        if self._first_activation:
            self._first_activation = False
            return await self._build_first_input()
        return await self._build_incremental_input()

    # ----------------- 首条 user 消息 -----------------
    async def _build_first_input(self) -> str:
        """构造首条 user 消息，条目顺序与 conversation_input_manager 对齐：

            1. 已经发生的聊天记录
            2. chat_environment
            3. 【本次聊天任务】
            4. amy_effect
            5. thoughts_text（"你当前的想法：…"）

        social_chat 专属条目（预设条目池 / 等级 block / 经验激活 / L3 推送 / 警告）
        暂未叠加，后续扩展阶段再插入。
        """
        from utils.dynamic_prompt_manager import get_dynamic_prompt

        # 1) 已经发生的聊天记录
        message_str = await self._get_first_social_history()

        # 2) chat_environment
        chat_env = get_dynamic_prompt("chat_environment")

        # 2.5) 当前会话环境段：分别从主人会话与工作会话拼出两段【当前会话环境】，
        #      让 LLM 明确"两条线"分别是谁、ID 是什么、是不是主人私聊。
        env_texts: List[str] = []
        master_env = self._build_env_for_session(self._social_chat_core.master_session_id)
        if master_env:
            env_texts.append(master_env)
        working_env = self._build_env_for_session(self._social_chat_core.working_session_id)
        if working_env:
            env_texts.append(working_env)
        env_text = "\n".join(env_texts)

        # 3) 【本次聊天任务】
        task_block = f"【本次聊天任务】\n{self._task_purpose}"

        # 4) amy_effect
        amy_effect = get_dynamic_prompt("amy_effect")

        # 5) thoughts_text（"你当前的想法：…"）
        try:
            from brain.amy.emotional_interface import get_emotional_interface
            from brain.amy.prompt_engine import build_thought_summary

            ei = await get_emotional_interface()
            active_thoughts = await ei.thought_cache.get_active_thoughts()
            thoughts_text = build_thought_summary(active_thoughts, prefix="你当前的想法：")
        except Exception as e:
            logger.debug(f"social_chat 收集 thoughts 列表失败: {e}")
            thoughts_text = ""

        # 拼接：用 parts 拼接避免连续空行
        parts: List[str] = [f"已经发生的聊天记录：\n{message_str}"]
        if env_text:
            parts.append(env_text)
        if chat_env:
            parts.append(chat_env)
        parts.append(task_block)
        if amy_effect:
            parts.append(amy_effect)
        if thoughts_text:
            parts.append(thoughts_text)

        user_input = "\n".join(parts)
        logger.info(f"social_chat 生成第一段输入，长度: {len(user_input)}")
        return user_input

    def _build_env_for_session(self, session_id: str) -> str:
        """从 pfca_sessions 反查 session_name，按你定义的格式拼一段【当前会话环境】。

        严格遵循项目规则：session_id 为空返回空串；反查不到 name 时按"未知"显式标注，
        不静默兜底骗 LLM。
        """
        if not session_id:
            return ""

        from brain.hc.global_session_state import get_global_session_state

        sess_name = ""
        for s in (
            get_global_session_state()
            .get_current_state()
            .get("data", {})
            .get("pfca_sessions", [])
        ):
            if s.get("session_id") == session_id:
                sess_name = s.get("session_name", "") or ""
                break

        return build_session_environment(
            session_id=session_id,
            session_name=sess_name,
            master_id=RoleConfig.MASTER_ID,
        )

    async def _get_first_social_history(self) -> str:
        """拉取主人会话与工作会话的聊天记录。

        渲染顺序固定：
            主人会话：<lines>
            工作会话：<lines>
        找不到或空：对应子段标 "(无)"。

        阶段汇报窗口起点由 social_chat_core 在 start() 时按各自会话的
        last_processed_time 快照，本方法不再写入任何锚点。
        """
        from brain.hc.global_session_state import get_global_session_state
        from brain.hc.global_session_history import (
            get_global_session_history,
            FetchOptions,
            MessageFormatOptions,
            FetchBehavior,
        )

        state = get_global_session_state()
        pfca_sessions = (
            state.get_current_state().get("data", {}).get("pfca_sessions", [])
        )
        history = get_global_session_history()
        end_time = datetime.now().timestamp()

        async def _get_a_first_social_history(session_id: str) -> tuple[str, float]:
            """拉取单个会话首条历史，返回 (渲染文本, 最早未读时间戳)。

            起点回退策略对齐 conversation_input_manager._build_first_input：
                base_start_time = max(0, last_processed_time - 30s)
                + 探测 [base_start_time, now] 内末尾 5 条，把最早一条的 timestamp 也纳入候选
                → 取 base_start_time 与 fifth_msg_ts 中更早者作 fetch 起点
            原因：add_pfca_session 新注册时把 last_processed_time 直接设成"当前时刻"，
                若不主动回退，会拉出 [now, now] 空窗口，"两个会话都空"。

            翻已读 / 推游标行为不变。
            最早未读时间戳 = 本次 fetch 命中消息里 timestamp 最小的那条（>0 且非 system）。
            关键路径（fetch / state 查询）失败一律直接 raise，
            符合 user_rules"关键方法失败应崩溃阻断"的约束。
            """
            sess_name = ""
            last_processed_time: Optional[float] = None
            for s in pfca_sessions:
                if s.get("session_id") == session_id:
                    sess_name = s.get("session_name", "")
                    last_processed_time = s.get("last_processed_time")
                    break
            if last_processed_time is None:
                raise RuntimeError(
                    f"social_chat 会话 {session_id} 的 last_processed_time 缺失或未注册。"
                    "请确认 social_chat_core.start() 注册时已显式写入起点。"
                )

            # base 起点：last_processed_time 回退 30s，保证 [base, now] 不为空窗口
            base_start_time = max(0.0, float(last_processed_time) - 30.0)

            # 探测：用宽松窗口（message_limit=20，update_processed_time=False，不翻已读）
            # 仅用于拿倒数第 5 条消息的 timestamp；fetch 的内部限流够覆盖近期 20 条。
            probe_result = await history.fetch(
                session_id, sess_name,
                start_time=base_start_time,
                end_time=end_time,
                options=FetchOptions(
                    behavior=FetchBehavior(
                        message_limit=20,
                        update_processed_time=False,
                        mark_as_read=False,
                    ),
                    message_format=MessageFormatOptions(
                        show_read_status=False,
                        show_timestamp=True,
                    ),
                ),
            )
            probe_messages = getattr(probe_result, "messages", None) or []
            fifth_msg_ts = 0.0
            if len(probe_messages) >= 5:
                fifth_msg_ts = probe_messages[-5].get("timestamp", 0) or 0.0
            start_time = min(base_start_time, fifth_msg_ts) if fifth_msg_ts > 0 else base_start_time

            # 单次正式 fetch：起点 = start_time；翻已读；推游标
            result = await history.fetch(
                session_id, sess_name,
                start_time=start_time,
                end_time=end_time,
                options=FetchOptions(
                    behavior=FetchBehavior(
                        update_processed_time=True,
                        mark_as_read=True,
                    ),
                    message_format=MessageFormatOptions(
                        show_read_status=True,
                        show_timestamp=True,
                    ),
                    header_model="chat_message",
                ),
            )

            messages = getattr(result, "messages", None) or []
            earliest_unread_ts = 0.0
            for m in messages:
                if m.get("read") is True or m.get("user_id") == "system":
                    continue
                ts = m.get("timestamp", 0) or 0.0
                if ts <= 0:
                    continue
                if earliest_unread_ts == 0.0 or ts < earliest_unread_ts:
                    earliest_unread_ts = ts

            lines = getattr(result, "formatted_lines", None) or []
            return ("\n".join(lines) if lines else ""), earliest_unread_ts

        master_block, _ = await _get_a_first_social_history(
            self._social_chat_core.master_session_id
        )
        working_block, _ = await _get_a_first_social_history(
            self._social_chat_core.working_session_id
        )

        return (
            f"主人会话：\n{master_block or '（无）'}\n"
            f"工作会话：\n{working_block or '（无）'}"
        )

        # ----------------- 增量输入 -----------------
    async def _build_incremental_input(self) -> Optional[str]:
        """构造增量输入，对齐 conversation_input_manager._build_incremental_input：

            1. 主人会话 + 工作会话的新消息（基于 pfca_sessions[*].last_processed_time → now）
            2. 工作会话未读条数提示（仅当工作会话存在未读时追加）
            3. 消息数量上限提示（social_chat 无 PFCA 限额机制，本段恒为空）
            4. 未使用工具警告
        """
        # 1) 主人/工作会话新消息（复用首条 _get_a_first_social_history 的查找路径，
        #    但这里走"基于 last_processed_time 增量"模式而非首条的 start_time → now）
        master_block, _ = await self._fetch_incremental_one(self._social_chat_core.master_session_id)
        working_block, working_unread = await self._fetch_incremental_one(self._social_chat_core.working_session_id)

        msg_parts: List[str] = []
        if master_block:
            msg_parts.append(f"主人会话：\n{master_block}")
        if working_block:
            msg_parts.append(f"工作会话：\n{working_block}")
        elif working_unread > 0:
            # 工作会话本轮没新消息（已被首条 / 上一轮增量消费）但仍存在未读时，
            # 显式提示未读条数，提示 LLM 主动 query_history。
            msg_parts.append(
                f"工作会话：\n当前工作会话中有 {working_unread} 条未读消息。"
            )

        user_input = "\n\n".join(msg_parts) if msg_parts else None

        # 2) 消息数量上限提示（social_chat 无 PFCA 限额机制，恒为空）
        limit_hint = self._build_message_limit_hint()
        if limit_hint:
            user_input = f"{user_input}\n{limit_hint}" if user_input else limit_hint

        # 3) 未使用工具警告
        if self._no_tool_call_warning:
            user_input = f"{user_input}\n{self._no_tool_call_warning}" if user_input else self._no_tool_call_warning

        return user_input or ""

    async def _fetch_incremental_one(self, session_id: str) -> tuple[str, int]:
        """拉取单个会话 last_processed_time → now 的新消息，对齐 conversation_input_manager 的增量拉取。

        Returns:
            (formatted_lines_str, unread_count):
                - formatted_lines_str: 格式化消息行（已读+未读混合，已 exclude_system）；
                - unread_count: 本次 fetch 命中的未读消息条数（read=False 的非 system 消息）。
        """
        if not session_id:
            return "", 0

        from brain.hc.global_session_state import get_global_session_state
        from brain.hc.global_session_history import (
            get_global_session_history,
            FetchOptions,
            MessageFormatOptions,
            FetchBehavior,
        )

        state = get_global_session_state()
        current_state = state.get_current_state()

        last_time: Optional[float] = None
        for s in current_state.get("data", {}).get("pfca_sessions", []):
            if s.get("session_id") == session_id:
                last_time = s.get("last_processed_time")
                break
        if last_time is None:
            raise RuntimeError(
                f"social_chat 增量拉取 {session_id} 失败：last_processed_time 缺失或会话未注册。"
                "请确认 social_chat_core.start() 注册时已显式写入起点。"
            )
        last_time = float(last_time)

        history = get_global_session_history()
        # fetch / mark_as_read / update_processed_time 任一失败必须 raise。
        # history.fetch(behavior=update_processed_time=True) 内部已推进游标，
        # 外层不再重复调 update_session_processed_time。
        result = await history.fetch(
            session_id, "",
            start_time=last_time,
            end_time=datetime.now().timestamp(),
            options=FetchOptions(
                behavior=FetchBehavior(update_processed_time=True, mark_as_read=True),
                message_format=MessageFormatOptions(
                    exclude_system=True,
                    show_timestamp=True,
                ),
            ),
        )

        messages = getattr(result, "messages", None) or []
        unread_count = sum(
            1 for m in messages
            if not m.get("read", False) and m.get("user_id") != "system"
        )

        lines = getattr(result, "formatted_lines", None) or []
        return ("\n".join(lines) if lines else ""), unread_count

    def _build_message_limit_hint(self) -> str:
        """social_chat 无 PFCA 策略限额机制；保留与 conversation 同款接口，恒返回空串。"""
        return ""

    # ----------------- 新消息监听（消费式 unread 队列语义） -----------------
    async def has_new_messages(self, session_id: str) -> Dict[str, Any]:
        """检查指定会话是否有新未读消息，并返回本次拉到的新内容。

        与 ConversationInputManager.has_new_messages 对齐接口：
        - 仅检测，不翻已读（unread_only=True），不推进 last_processed_time；
        - 调用方负责解释"has_new"的语义（wait_and_observe 用于中断等待）。
        - 关键路径（fetch）失败一律 raise，符合 user_rules 关键方法失败应崩溃阻断。

        Args:
            session_id: 必填。要监听的会话 ID（master / working）。

        Returns:
            dict: {
                "has_new": bool,
                "formatted_lines": list[str],
                "message_count": int,
                "raw_result": Any,
            }
        """
        if not session_id:
            raise ValueError("session_id 必填")

        from brain.hc.global_session_history import (
            get_global_session_history,
            FetchOptions,
            FetchBehavior,
        )

        # 窗口起点取 social_chat_core 阶段汇报窗口两个起点的更早者；
        # 两个都没写（start() 未跑 / 未注入）时按 user_rules 直接崩溃阻断，禁止回退到 0。
        starts = [
            getattr(self._social_chat_core, "_master_window_start", None),
            getattr(self._social_chat_core, "_working_window_start", None),
        ]
        valid_starts = [s for s in starts if isinstance(s, (int, float))]
        if not valid_starts:
            raise RuntimeError(
                "_master_window_start / _working_window_start 均为 None，"
            )
        start_time = min(valid_starts)

        history = get_global_session_history()
        result = await history.fetch(
            session_id,
            "",
            start_time=start_time,
            end_time=datetime.now().timestamp(),
            options=FetchOptions(behavior=FetchBehavior(unread_only=True)),
        )
        formatted_lines = getattr(result, "formatted_lines", None) or []
        return {
            "has_new": bool(getattr(result, "has_new", False)),
            "formatted_lines": formatted_lines,
            "message_count": len(formatted_lines),
            "raw_result": result,
        }

    # ----------------- L3 推送 -----------------
    async def _fetch_l3_push(self) -> str:
        """L3 等级：仅拉取主人会话最新 5 条未读。

        工作会话的监听/推送功能已临时禁用（参见设计文档 §9.7 调整）。
        LLM 需要工作会话最新消息时，应主动调 query_history 工具。
        """
        from brain.hc.global_session_history import (
            get_global_session_history,
            FetchOptions,
            MessageFormatOptions,
            FetchBehavior,
        )

        history = get_global_session_history()
        end_time = datetime.now().timestamp()

        parts: List[str] = []

        # 主人会话
        master_msgs = None
        try:
            master_msgs = await history.fetch(
                self._social_chat_core.master_session_id, "",
                start_time=0,
                end_time=end_time,
                options=FetchOptions(
                    behavior=FetchBehavior(
                        update_processed_time=True,
                        filter_read_unread="unread_first",
                        message_limit=5,
                    ),
                    message_format=MessageFormatOptions(
                        show_read_status=False,
                        show_timestamp=True,
                    ),
                ),
            )
        except Exception as e:
            logger.warning(f"L3 拉取主人会话失败: {e}")

        if master_msgs and getattr(master_msgs, "formatted_lines", None):
            parts.append("【主人最新消息（最多 5 条未读）】\n" + "\n".join(master_msgs.formatted_lines[:5]))

        # 工作会话推送已临时禁用；LLM 需自调 query_history。
        return "\n\n".join(parts)

    # ----------------- 警告消息 -----------------
    def set_no_tool_call_warning(self, message: str) -> None:
        self._no_tool_call_warning = message

    def clear_no_tool_call_warning(self) -> None:
        self._no_tool_call_warning = ""

    # ----------------- amy / thoughts -----------------
    async def _build_amy_segment(self) -> str:
        """首条专用：当前情绪描述 + 当前 thoughts 列表。"""
        try:
            from brain.amy.emotional_interface import get_emotional_interface
            from brain.amy.prompt_engine import build_thought_summary

            ei = await get_emotional_interface()
            description = await ei.get_amy_description()
            if not description:
                return ""

            active_thoughts = await ei.thought_cache.get_active_thoughts()
            thoughts_text = build_thought_summary(active_thoughts, prefix="你当前的想法：")

            parts = [f"你当前的情绪：{description}"]
            if thoughts_text:
                parts.append(thoughts_text)
            return "\n".join(parts)
        except Exception as e:
            logger.debug(f"social_chat 构造 amy segment 失败: {e}")
            return ""

    async def _build_thoughts_segment(self) -> str:
        """单独的 thoughts 列表（amy_description 缺失时仍输出）。"""
        try:
            from brain.amy.emotional_interface import get_emotional_interface
            from brain.amy.prompt_engine import build_thought_summary

            ei = await get_emotional_interface()
            active_thoughts = await ei.thought_cache.get_active_thoughts()
            return build_thought_summary(active_thoughts, prefix="你当前的想法：")
        except Exception as e:
            logger.debug(f"social_chat 构造 thoughts 失败: {e}")
            return ""

    # ----------------- 经验激活 -----------------
    async def _append_matched_bequest(self, user_input: str) -> str:
        """向量匹配经验并追加到输入（与 conversation_input_manager 一致）。"""
        if not user_input:
            return user_input

        try:
            from brain.hc.database.bequest import BequestOperations
            from config.settings import chat_config

            bequest_ops = BequestOperations()

            fixed_title = getattr(chat_config, "FIXED_BEQUEST_TITLE", None)
            if fixed_title and self._fixed_bequest_id is None:
                self._fixed_bequest_id = await bequest_ops.get_bequest_id_by_title(fixed_title)

            matched = await bequest_ops.search_bequest_by_vector(user_input, top_k=3)
            if not matched:
                return user_input

            activated_titles = []
            count = 0
            for bequest_id in matched:
                # 排除固定经验（与 conversation 一致）
                if self._fixed_bequest_id and bequest_id == self._fixed_bequest_id:
                    continue
                if bequest_id in self._activated_bequests:
                    continue

                data = await bequest_ops.get_bequest_by_id(bequest_id)
                if not data:
                    continue

                if data.get("apply_scenario") == "decision":
                    continue

                title = data.get("title", "未知")
                summary = data.get("summary", "")
                if summary:
                    self._activated_bequests.add(bequest_id)
                    user_input = f"{user_input}\n你回想起了经验：{title} - {summary}"
                    activated_titles.append(title)
                    count += 1
                    if count >= 3:
                        break

            if activated_titles:
                logger.info(f"social_chat 已激活经验: {', '.join(activated_titles)}")

            return user_input

        except Exception as e:
            logger.warning(f"social_chat 经验匹配失败: {e}")
            return user_input


def get_social_chat_input_manager() -> SocialChatInputManager:
    return SocialChatInputManager()