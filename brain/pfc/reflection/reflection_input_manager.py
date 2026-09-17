# -*- coding: utf-8 -*-
"""
Reflection 输入管理模块。

基本结构与 pfca_input_manager 对齐：
- prepare_first_message：外部（core / tools）缓存本次会话的首条请求上下文。
- get_user_input：统一入口。首条（存在缓存请求）构造首条消息；否则构造增量消息。
- reset：会话开始时重置状态。

Reflection user 消息的两条构造路径：
1. 首条 (_build_first_input)：由外部 prepare_first_message 触发，
   把 message_base / amy_effect / recent_memory / ongoing_tasks /
   notebook_manual / decision_context 等槽位拼成一条 user 消息
   （完整拼接格式见 _build_first_input docstring）。会话消息不在此拉取
   （消息由 Reflection 侧 query_history 等工具自行读取）。
2. 增量 (_build_incremental_input)：每轮经情绪消费者游标
   （get_unread_events("reflection")）检测 amy 情绪事件，仅当未读事件中
   含最终评价类事件（act 命中 FinalEvaluationAct 词表）才输出
   一条 "amy_effect" 形式的 user 消息；其余事件（表现侧 NE/DA 噪声）
   仅推进游标消费掉，不进 Reflection 视野。

amy_effect 段的语义（与增量共用）：
- 核心是决策侧情绪描述，来自无参 get_decision_description()（决策侧出口，见 docs/api/amy.md），
  为单行 SC 行动对应 + "原因："（仅最终评价类想法摘要，成因由出口内嵌）。
  情绪体系隔离（与 PFCA 一致）：Reflection 只见 SC（社交参与度轴），不见 NE/DA 表现侧激素行。
- 前缀由本模块添加：首条【当前情绪】，增量"你的心情变为："。
- 游标推进在成功取到描述之后执行：取描述失败时不推进，事件留给下一轮，不丢。
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


class ReflectionInputManager:
    """Reflection 输入管理器（单例）。"""

    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self._queued_request: Optional[Dict[str, Any]] = None
        self._first_activation: bool = True
        self._blocks: Dict[str, str] = {}
        self._initialized = True
        logger.debug("ReflectionInputManager 初始化")

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
        """缓存首条请求并激活首条标记；reflection_session 在下一轮通过 get_user_input() 消费。"""
        self._queued_request = {
            "message_type": message_type,
            "current_session": current_session,
            "message": message,
            "kwargs": kwargs,
        }
        self._first_activation = True
        logger.debug(f"已缓存首条请求: {message_type}")

    async def get_user_input(self) -> Optional[str]:
        """获取用户输入：首条（_first_activation 或存在缓存请求）或增量（amy 情绪事件）。

        存在 _queued_request 时优先消费它：支持会话中途的 CONTEXT_REBUILD
        （方向修正路径 prepare 后，由下一轮注入）。
        """
        if self._first_activation or self._queued_request is not None:
            self._first_activation = False
            return await self._build_first_input()
        return await self._build_incremental_input()

    # ===== 首条消息构建 =====

    async def _build_first_input(self) -> Optional[str]:
        """消费缓存的首条请求，构造并返回 content。无请求时返回 None。

        预期拼接格式（_reset_blocks 槽位定义序即拼接序，_compile 过滤空槽，
        条件段可能缺席）：

            NORMAL_STARTUP 路径：
            {message_base}                        <- 外部注入的反思请求
            【当前环境状态】{年月日 时刻 时段}     <- 纯同步取系统时间，恒非空
            【当前情绪】{amy 统一出口描述}        <- 三行激素 + 原因；取到非空才拼
            {近期经历}                            <- temporary_memory 取到非空才拼（≤3 条，
                                                     label "近期经历" 内置标题）
            【当前未完成的任务】                 <- 任务命中才拼（≤3 条，自带标题）
            【记事本手册】                       <- 记事本手册非空才拼（render_manual 生成，自带段头）：
            记事本存在以下章节:                      策略/决策场景子集 + 通用条目，
            {章名}                                   ≤3 章、正文 ≤150 字符、每章 ≤5 条新挤旧
            └ {条目}
            可以使用工具查询记事本某一章节的具体信息以查看细节。

            CONTEXT_REBUILD 路径（无 message_base，尾部换为）：
            【方向性建议】{decision_thought}      <- 传入非空才拼
            【现状总结】{current_summary}         <- 传入非空才拼

        记事本手册（章节式裸文本）位于未完成任务后、决策上下文前——长期底色先于
        决策思考；手册拉取失败直接上抛崩溃阻断（按 user_rules），细节经
        query_note 工具溯源。
        """
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

        # _collect_current_environment 为纯同步方法（仅取系统时间），不可 await，
        # 否则抛 TypeError: object NoneType can't be used in 'await' expression，
        # 让首条消息构建在 LLM 调用前静默失败（致命静默）。
        self._collect_current_environment()
        await self._collect_amy_effect()
        self._collect_recent_memory(limit=3)
        await self._collect_ongoing_tasks(limit=3)
        await self._collect_notebook_manual()

        content = self._compile()
        if content:
            logger.info(f"首条 content 构造完成，长度: {len(content)}")
        return content or None

    # ===== 增量消息构建 =====

    async def _build_incremental_input(self) -> Optional[str]:
        """检测 amy 情绪事件，构造增量 content（决策侧：仅最终评价类事件触发）。

        情绪消费者游标（新契约，写法参照 pfca_input_manager._build_incremental_input）：
        - get_unread_events("reflection") 判断是否有未消费的情绪事件，无则返回 None。
        - 情绪体系隔离过滤：仅当未读事件含最终评价类事件（act 命中
          FinalEvaluationAct 词表）才构造内容；表现侧 NE/DA 事件对本模块是
          噪声，仅推进游标消费掉。
        - 有最终评价类事件：取当前决策侧描述（无参 get_decision_description，
          内部已含 SC 行动行 + 最终评价类想法原因），取到后再 advance_cursor
          推进游标（取描述失败/为空时不推进，事件留给下一轮，不丢）。
        - 输出"你的心情变为：{description}"。

        Returns:
            Optional[str]: 无未消费事件 / 无最终评价类事件时返回 None。
        """
        from brain.amy.emotional_interface import get_emotional_interface
        from brain.amy.constants import FinalEvaluationAct

        ei = await get_emotional_interface()
        events = await ei.get_unread_events("reflection")
        if not events:
            return None

        max_seq = max(e["seq"] for e in events)
        # 决策侧只关心最终评价类事件；NE/DA 噪声事件游标照常推进消费掉
        # （不推进会无限积压重查），但不触发增量内容。
        final_events = [
            e for e in events
            if FinalEvaluationAct.kind_of(e.get("act", "") or "") is not None
        ]
        if not final_events:
            await ei.advance_cursor("reflection", max_seq)
            return None

        current_amy = await ei.get_decision_description()
        if not current_amy:
            return None
        await ei.advance_cursor("reflection", max_seq)

        # 输出情绪（description 已含成因，见 _format_amy_segment）
        logger.info(f"增量 - 情绪变化: '{current_amy[:60]}'")
        return await self._format_amy_segment(current_amy, prefix="你的心情变为：")

    # ===== 内部辅助 =====

    def _reset_blocks(self) -> None:
        self._blocks = {
            "message_base": "",
            "current_environment": "",
            "amy_effect": "",
            "recent_memory": "",
            "ongoing_tasks": "",
            "notebook_manual": "",
            "decision_thought": "",
            "current_summary": "",
        }

    async def _format_amy_segment(self, description: str, prefix: str) -> str:
        """构造"情绪描述"段。

        description 来自决策侧出口 get_decision_description()，为单行 SC
        行动对应 + "原因："（仅最终评价类想法摘要，成因出口内嵌），
        因此这里只加前缀。

        prefix:
          - 首条：'【当前情绪】'
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
        """首条专用的情绪槽收集（情绪消费者游标，决策侧）。

        策略：
        - 无条件取当前决策侧描述（统一出口 get_decision_description：单行 SC
          行动对应 + 最终评价类想法原因）；只要 amy 有描述就写槽。
          情绪体系隔离：Reflection 只见 SC，不见 NE/DA 表现侧激素行。
        - 首条消费掉当前全部未读事件并推进游标，避免这些事件在后续增量轮
          重复注入（决策侧过滤下，NE/DA 噪声事件也一并消费掉）。
        - 描述取不到时早退且不推进游标（事件留给增量轮恢复）。
        """
        from brain.amy.emotional_interface import get_emotional_interface

        ei = await get_emotional_interface()
        current = await ei.get_decision_description()
        if not current:
            return

        events = await ei.get_unread_events("reflection")
        if events:
            await ei.advance_cursor("reflection", max(e["seq"] for e in events))

        self._blocks["amy_effect"] = await self._format_amy_segment(
            current, prefix="【当前情绪】"
        )

    def _collect_recent_memory(self, limit: int = 3) -> None:
        """首条专用槽位：拉取近期经历。

        按 user_rules，关键记忆拉取失败直接崩溃阻断，不使用 try-except 静默跳过。
        """
        from brain.hc.temporary_memory import get_recent_experience
        text = get_recent_experience(limit=limit, label="近期经历", with_current_time=False)
        if text:
            self._blocks["recent_memory"] = text

    async def _collect_ongoing_tasks(self, limit: int = 3) -> None:
        """首条专用槽位：从任务数据库拉取未完成（active）任务的格式化列表。

        仅在首条注入一次。任务量很少变化，不进增量。
        按 user_rules，关键任务拉取失败直接崩溃阻断，不使用 try-except 静默跳过。
        """
        from brain.hc.database.task import TaskOperations
        task_ops = TaskOperations()
        task_ids = await task_ops.get_active_tasks(limit=limit)
        if not task_ids:
            return
        text = await task_ops.format_tasks_by_ids(task_ids)
        if text and not text.startswith("未找到"):
            self._blocks["ongoing_tasks"] = f"【当前未完成的任务】\n{text}"

    async def _collect_notebook_manual(self) -> None:
        """首条专用槽位：记事本手册（全部已落库条目，usage 不参与路由）。

        聚合渲染见 notepad.render_manual（章节式裸文本，细节经 query_note 工具溯源）；
        拉取失败直接上抛崩溃阻断（按 user_rules，不 try-ex 静默跳过）。
        """
        from brain.hc.shortlight import get_shortlight_notepad
        manual = await get_shortlight_notepad().render_manual()
        if manual:
            self._blocks["notebook_manual"] = manual

    def _compile(self) -> str:
        """过滤空槽位后用 \\n 拼接，避免出现连续空行。"""
        return "\n".join(v for v in self._blocks.values() if v)

    # ===== 外部 =====

    def clear_bequest_cache(self) -> None:
        from brain.pfc.reflection.reflection_bequest import get_reflection_bequest
        get_reflection_bequest().clear()
        logger.debug("Reflection经验缓存已清空")


_reflection_input_manager_instance: Optional[ReflectionInputManager] = None


def get_reflection_input_manager() -> ReflectionInputManager:
    """获取 ReflectionInputManager 模块级单例。

    prepare_first_message（ReflectionCore 启动 / 方向修正的 CONTEXT_REBUILD）
    与 get_user_input（ReflectionSession 消费）必须落在**同一个实例**上，否则首条
    消息会缓存进一个被丢弃的实例而静默丢失。
    新一轮 Reflection 由 ReflectionCore.start 先调 reset() 复位状态再 prepare。
    __init__ 的 _initialized 守卫即为此模式设计：单例被重复构造时不重置状态。
    """
    global _reflection_input_manager_instance
    if _reflection_input_manager_instance is None:
        _reflection_input_manager_instance = ReflectionInputManager()
    return _reflection_input_manager_instance
