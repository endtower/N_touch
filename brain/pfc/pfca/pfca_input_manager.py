# -*- coding: utf-8 -*-
"""
PFCA 输入管理模块。

基本结构与 conversation_input_manager / social_chat_input_manager 对齐：
- prepare_first_message：外部（core / tools）缓存本次会话的首条请求上下文。
- get_user_input：统一入口。首条（存在缓存请求）构造首条消息；否则构造增量消息。
- reset：会话开始时重置状态。

PFCA user 消息的两条构造路径：
1. 首条 (_build_first_input)：由外部 prepare_first_message 触发，
   把 message_base / amy_effect / recent_memory / ongoing_events / decision_context
   等槽位拼成一条 user 消息。会话消息不再在此拉取（消息由 PFCA 侧
   query_history 等工具自行读取），故不再处理/推进 pfca 游标。
2. 增量 (_build_incremental_input)：每轮检测 amy description 变化，
   变化时输出一条 "amy_effect" 形式的 user 消息。

amy_effect 段的语义（与增量共用）：
- 核心是 amy description，来自 get_amy_description()。
- description 内部已含【当前情绪】+ thoughts 摘要（build_mood_prompt(with_reason=True)），
  因此只直接引用，不再额外追加 thought 列表，避免同一想法被渲染两次。
"""

from typing import Dict, Any, Optional
from utils.logger import get_logger

logger = get_logger(__name__, "INFO")


def _describe_time_period(hour: int) -> str:
    """按小时返回时段描述。"""
    if hour < 6:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 11:
        return "上午"
    if hour < 13:
        return "中午"
    if hour < 18:
        return "下午"
    return "晚上"


class FirstMessageType:
    NORMAL_STARTUP = "normal_startup"
    CONTEXT_REBUILD = "context_rebuild"


class PFCAInputManager:
    """PFCA 输入管理器（单例）。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self._queued_request: Optional[Dict[str, Any]] = None
        self._first_activation: bool = True
        self._blocks: Dict[str, str] = {}
        self._initialized = True
        logger.debug("PFCAInputManager 初始化")

    def reset(self):
        """重置状态（会话开始时调用）。"""
        self._queued_request = None
        self._first_activation = True
        self._blocks = {}

    # ===== 对外接口 =====

    def prepare_first_message(
        self,
        message_type: str,
        current_session: Dict[str, Any],
        message: Optional[str] = None,
        **kwargs
    ) -> None:
        """缓存首条请求并激活首条标记；pfca_session 在下一轮通过 get_user_input() 消费。"""
        self._queued_request = {
            "message_type": message_type,
            "current_session": current_session,
            "message": message,
            "kwargs": kwargs,
        }
        self._first_activation = True
        logger.debug(f"已缓存首条请求: {message_type}")

    async def get_user_input(self) -> Optional[str]:
        """获取用户输入：首条（_first_activation）或增量（amy 情绪变化）。"""
        if self._first_activation:
            self._first_activation = False
            return await self._build_first_input()
        return await self._build_incremental_input()

    # ===== 首条消息构建 =====

    async def _build_first_input(self) -> Optional[str]:
        """消费缓存的首条请求，构造并返回 content。无请求时返回 None。"""
        if self._queued_request is None:
            return None

        request = self._queued_request
        self._queued_request = None
        message_type = request["message_type"]

        self._reset_blocks()

        if message_type == FirstMessageType.CONTEXT_REBUILD:
            self._collect_decision_context(
                request["kwargs"].get("current_summary", ""),
                request["kwargs"].get("decision_thought", ""),
            )
        else:
            self._collect_message_base(request["message"] or "")

        await self._collect_current_environment()
        await self._collect_amy_effect()
        self._collect_recent_memory(limit=3)
        await self._collect_ongoing_events(limit=3)

        content = self._compile()
        if content:
            logger.info(f"首条 content 构造完成，长度: {len(content)}")
        return content or None

    # ===== 增量消息构建 =====

    async def _build_incremental_input(self) -> Optional[str]:
        """检测 amy 情绪变化，构造增量 content。

        新规则（情绪消费者游标）：
        - 每轮调用 has_unread_amy("pfca") 判断是否有未取的新一轮描述。
        - 无未取描述 → 返回 None。
        - 有崩溃/未崩事件 → 整段输出覆盖描述（不切首句、不挑 thought）。
        - 有 thought 事件 → 输出"你的心情变为：{description}" + 当前全部 thoughts 列表。
        - 取描述后 get_amy_description("pfca") 标记已取。

        Returns:
            Optional[str]: 无未取描述时返回 None。
        """
        from brain.amy.emotional_interface import get_emotional_interface

        ei = await get_emotional_interface()
        if not ei.has_unread_amy("pfca"):
            return None

        current_amy = await ei.get_amy_description("pfca")
        if not current_amy:
            return None

        # 覆盖态（崩溃/未崩）：整段作为增量输出，不切首句、不挑 thought
        if ei.crash_system.is_covered():
            logger.info(f"增量 - 覆盖态变化: '{current_amy[:60]}'")
            return current_amy

        # 正常态：输出情绪（description 已含 thoughts，见 _format_amy_segment）
        logger.info(f"增量 - 情绪变化 (无原因): '{current_amy[:60]}'")
        return await self._format_amy_segment(current_amy, prefix="你的心情变为：")

    # ===== 内部辅助 =====

    def _reset_blocks(self) -> None:
        self._blocks = {
            "message_base": "",
            "current_environment": "",
            "amy_effect": "",
            "recent_memory": "",
            "ongoing_events": "",
            "decision_thought": "",
            "current_summary": "",
        }

    async def _format_amy_segment(self, description: str, prefix: str) -> str:
        """构造"情绪描述"段。

        description 来自 get_amy_description()，内部已含【当前情绪】+ thoughts 摘要
        （build_mood_prompt(with_reason=True)），因此这里只加前缀，不再重复追加
        thought 列表，避免同一想法被渲染两次。

        prefix:
          - 首条：'你当前的情绪：'
          - 增量：'你的心情变为：'
        """
        return f"{prefix}{description}"

    # ===== 槽位收集 =====

    def _collect_message_base(self, content: str) -> None:
        self._blocks["message_base"] = content or ""

    def _collect_decision_context(self, current_summary: str, decision_thought: str) -> None:
        self._blocks["current_summary"] = f"【现状总结】{current_summary}" if current_summary else ""
        self._blocks["decision_thought"] = f"【方向性建议】{decision_thought}" if decision_thought else ""

    def _collect_current_environment(self) -> None:
        """首条专用槽位：构造当前环境状态（当前时间 + 时段）。

        时间格式：年/月/日 + 具体时刻 + 时段（凌晨/早上/上午/中午/下午/晚上）。
        """
        from datetime import datetime

        now = datetime.now()
        period = _describe_time_period(now.hour)
        self._blocks["current_environment"] = (
            f"【当前环境状态】{now.year}年{now.month}月{now.day}日 "
            f"{now.strftime('%H:%M')} {period}"
        )

    async def _collect_amy_effect(self) -> None:
        """首条专用的情绪槽收集。

        策略：
        - 只要 amy 有描述就写槽；AMY 不存在才早退。
        - 取描述（get_amy_description 已含 thoughts），并标记 pfca 已取当前轮。
        - 收集后无需再推进游标（轮次游标由 get_amy_description 标记）。
        """
        from brain.amy.emotional_interface import get_emotional_interface

        ei = await get_emotional_interface()
        current = await ei.get_amy_description("pfca")
        if not current:
            return

        self._blocks["amy_effect"] = current

    def _collect_recent_memory(self, limit: int = 3) -> None:
        """首条专用槽位：拉取近期经历。

        按 user_rules，关键记忆拉取失败直接崩溃阻断，不使用 try-except 静默跳过。
        """
        from brain.hc.temporary_memory import get_recent_experience
        text = get_recent_experience(limit=limit, label="近期经历", with_current_time=False)
        if text:
            self._blocks["recent_memory"] = text

    async def _collect_ongoing_events(self, limit: int = 3) -> None:
        """首条专用槽位：从记忆数据库拉取进行中事件（ongoing）的格式化列表。

        仅在首条注入一次。事件量很少变化，不进增量。
        按 user_rules，关键事件拉取失败直接崩溃阻断，不使用 try-except 静默跳过。
        """
        from brain.hc.database.event import EventOperations
        event_ops = EventOperations()
        event_ids = await event_ops.get_ongoing_events(limit=limit)
        if not event_ids:
            return
        text = await event_ops.format_events_by_ids(event_ids)
        if text and not text.startswith("未找到"):
            self._blocks["ongoing_events"] = f"【当前正在进行的长期事件】\n{text}"

    def _compile(self) -> str:
        """过滤空槽位后用 \\n 拼接，避免出现连续空行。"""
        return "\n".join(v for v in self._blocks.values() if v)

    # ===== 外部 =====

    def clear_bequest_cache(self) -> None:
        from brain.pfc.pfca.pfca_bequest import get_pfca_bequest
        get_pfca_bequest().clear()
        logger.debug("PFCA经验缓存已清空")


def get_pfca_input_manager() -> PFCAInputManager:
    return PFCAInputManager()
