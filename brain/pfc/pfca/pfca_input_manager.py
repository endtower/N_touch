# -*- coding: utf-8 -*-
"""
PFCA 输入管理模块。

基本结构与 conversation_input_manager / social_chat_input_manager 对齐：
- prepare_first_message：外部（core / tools）缓存本次会话的首条请求上下文。
- get_user_input：统一入口。首条（存在缓存请求）构造首条消息；否则构造增量消息。
- reset：会话开始时重置状态。

PFCA user 消息的两条构造路径：
1. 首条 (_build_first_input)：由外部 prepare_first_message 触发，
   把 message_base / persona_roster / amy_effect / recent_memory /
   ongoing_tasks / notebook_manual 等槽位拼成一条 user 消息
   （完整拼接格式见 _build_first_input docstring）。会话消息不再在此拉取
   （消息由 PFCA 侧 query_history 等工具自行读取），故不再处理/推进 pfca 消息游标。
   注：PFCA 不注入单一"当前人格"（2026-09-29 起，人格改为逐会话由工具入参指定），
   但首条消息会拼入人格总览（render_persona_roster，列全部可选人格及其梗概）。
2. 增量 (_build_incremental_input)：经情绪消费者游标
   （get_unread_events("pfca")）检测 amy 情绪事件，
   仅当未读事件中含最终评价类事件（act 命中 FinalEvaluationAct 词表）
   才输出一条 "amy_effect" 形式的 user 消息；其余事件（表现侧 NE/DA 噪声）
   仅推进游标消费掉，不进 PFCA 视野。

amy_effect 段的语义（与增量共用）：
- 核心是决策侧情绪描述，来自无参 get_decision_description()（决策侧出口，见 docs/api/amy.md），
  为按轴分组的两轴现状与各自最近五条变动（每轴以【当前情绪】开头）。
  情绪体系隔离（2026-09-13；2026-09-27 SC 轴剔除）：PFCA 不见 NE/DA 表现侧激素行。
- 首条与增量同构，前缀【当前情绪】由出口自带，本模块不再拼接前缀。
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


class PFCAInputManager:
    """PFCA 输入管理器（单例）。"""

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
        """获取用户输入：首条（_first_activation 或存在缓存请求）或增量（amy 情绪事件）。"""
        if self._first_activation or self._queued_request is not None:
            self._first_activation = False
            return await self._build_first_input()
        return await self._build_incremental_input()

    # ===== 首条消息构建 =====

    async def _build_first_input(self) -> Optional[str]:
        """消费缓存的首条请求，构造并返回 content。无请求时返回 None。

        预期拼接格式（_reset_blocks 槽位定义序即拼接序，_compile 过滤空槽，
        条件段可能缺席）：

            {message_base}                        <- truck 注入的第一轮消息（输入）
            {人格总览}                            <- render_persona_roster 生成的常备描述：
            你具有以下人格。                          人格名：适配场景，逐行列出；不含基本人格条目
            {人格名}：{适配场景}
            ...
            【当前环境状态】{年月日 时刻 时段}     <- 纯同步取系统时间，恒非空
            【当前情绪】{轴名}：{值}/100。{感受}    <- amy 统一出口描述，取到非空才拼；
            {事件标题} {评价标签} {分数变化}          <- 每轴现状行下紧跟该轴最近五条变动
            ...
            {近期经历}                            <- temporary_memory 取到非空才拼（≤3 条，
                                                     label "近期经历" 内置标题）
            【当前已经承接的任务】                <- 任务命中才拼（≤10 行轻量简表：
            {title} 下次处理时间{相对时间}           标题+下次处理时间，概况全文不进首条，
            ...                                      细节由 query_task_status 按需查询）
            【记事本手册】                       <- 记事本手册非空才拼（render_manual 生成，自带段头）：
            记事本存在以下章节:                      策略/决策场景子集 + 通用条目，
            {章名}                                   ≤3 章、正文 ≤300 字符、每章 ≤5 条新挤旧
            └ {条目}
            可以使用工具查询记事本某一章节的具体信息以查看细节。

        记事本手册（章节式裸文本）位于未完成任务后——长期底色先于决策思考；
        手册拉取失败直接上抛崩溃阻断（按 user_rules），细节经 query_note 工具溯源。
        """
        if self._queued_request is None:
            return None

        request = self._queued_request
        self._queued_request = None

        self._reset_blocks()

        self._collect_message_base(request["message"] or "")

        self._collect_persona_roster()

        # _collect_current_environment 为纯同步方法（仅取系统时间），不可 await，
        # 否则抛 TypeError: object NoneType can't be used in 'await' expression，
        # 让首条消息构建在 LLM 调用前静默失败（致命静默，见本轮修复）。
        self._collect_current_environment()
        await self._collect_amy_effect()
        self._collect_recent_memory(limit=3)
        await self._collect_ongoing_tasks()
        await self._collect_notebook_manual()

        content = self._compile()
        if content:
            logger.info(f"首条 content 构造完成，长度: {len(content)}")
        return content or None

    # ===== 增量消息构建 =====

    async def _build_incremental_input(self) -> Optional[str]:
        """构造增量 content（amy 情绪事件）。

        情绪消费者游标（新契约，写法参照 proactive_input_manager._collect_amy_effect）：
        - get_unread_events("pfca") 判断是否有未消费的情绪事件，无则返回 None。
        - 情绪体系隔离过滤：仅当未读事件含最终评价类事件（act 命中
          FinalEvaluationAct 词表）才构造内容；表现侧 NE/DA 事件（conversation/
          social_chat 的情感遭遇）对本模块是噪声，仅推进游标消费掉。
        - 有最终评价类事件：取当前决策侧描述（无参 get_decision_description，
          内部已含最终评价类想法原因），取到后再 advance_cursor
          推进游标（取描述失败/为空时不推进，事件留给下一轮，不丢）。
        - 输出"你的心情变为：{description}"。

        Returns:
            Optional[str]: 无待注入内容时返回 None。
        """
        from brain.amy.emotional_interface import get_emotional_interface
        from brain.amy.constants import FinalEvaluationAct

        ei = await get_emotional_interface()
        events = await ei.get_unread_events("pfca")
        if not events:
            return None

        max_seq = max(e["seq"] for e in events)
        # 决策侧只关心最终评价类事件；NE/DA 噪声事件游标照常推进消费掉
        # （不推进会无限积压重查），但不触发增量内容。
        final_events = [
            e for e in events
            if FinalEvaluationAct.contains(e.get("act", "") or "")
        ]
        if not final_events:
            await ei.advance_cursor("pfca", max_seq)
            return None

        current_amy = await ei.get_decision_description()
        if not current_amy:
            return None
        await ei.advance_cursor("pfca", max_seq)

        # 输出情绪（每轴自带【当前情绪】，见 build_decision_feedback_prompt）
        logger.info(f"增量 - 情绪变化: '{current_amy[:60]}'")
        return current_amy

    # ===== 内部辅助 =====

    def _reset_blocks(self) -> None:
        self._blocks = {
            "message_base": "",
            "persona_roster": "",
            "current_environment": "",
            "amy_effect": "",
            "recent_memory": "",
            "ongoing_tasks": "",
            "notebook_manual": "",
        }

    # ===== 槽位收集 =====

    def _collect_persona_roster(self) -> None:
        """首条专用槽位：人格总览（可见人格清单及其适配场景）。

        常备描述，每次会话首条固定注入（位于输入 message_base 之后），让 PFCA
        知道可调用 / 可指派哪些人格；不含基本人格条目（render_persona_roster 已排除）。
        人格注册表在模块导入时加载，读取失败直接上抛崩溃阻断（user_rules）。
        """
        from brain.base.persona.persona_registry import render_persona_roster

        self._blocks["persona_roster"] = render_persona_roster()

    def _collect_message_base(self, content: str) -> None:
        self._blocks["message_base"] = content or ""

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
        - 无条件取当前决策侧描述（统一出口 get_decision_description：两轴现状与各自最近五条变动）；
          只要 amy 有描述就写槽。
          情绪体系隔离：PFCA 不见 NE/DA 表现侧激素行。
        - 首条消费掉当前全部未读事件并推进游标，避免这些事件在后续增量轮
          重复注入（决策侧过滤下，NE/DA 噪声事件也一并消费掉）。
        - 描述取不到时早退且不推进游标（事件留给增量轮恢复）。
        """
        from brain.amy.emotional_interface import get_emotional_interface

        ei = await get_emotional_interface()
        current = await ei.get_decision_description()
        if not current:
            return

        events = await ei.get_unread_events("pfca")
        if events:
            await ei.advance_cursor("pfca", max(e["seq"] for e in events))

        self._blocks["amy_effect"] = current

    def _collect_recent_memory(self, limit: int = 3) -> None:
        """首条专用槽位：拉取近期经历。

        按 user_rules，关键记忆拉取失败直接崩溃阻断，不使用 try-except 静默跳过。
        """
        from brain.hc.temporary_memory import get_recent_experience
        text = get_recent_experience(limit=limit, label="近期经历", with_current_time=False)
        if text:
            self._blocks["recent_memory"] = text

    async def _collect_ongoing_tasks(self, limit: int = 5) -> None:
        """首条只注入总数与最近的少量接续安排，细节按标题查询。

        目录不代表当前要执行所有事项。数据库失败直接上抛。
        """
        from brain.hc.database.task import TaskOperations
        task_ops = TaskOperations()
        text = await task_ops.format_active_task_overview(limit=limit)
        if text:
            self._blocks["ongoing_tasks"] = f"【待接续事项目录】\n{text}"

    async def _collect_notebook_manual(self) -> None:
        """首条专用槽位：记事本手册（全部已落库条目，条目的 category 不参与路由）。

        聚合渲染见 notepad.render_manual（章节式裸文本，细节经 query_note 工具溯源）；
        字符预算 max_chars=1000：逐步拉取，累计超过 1000 字符即停止继续拉取
        （PFCA 可接受的笔记信息上限，2026-09-21 用户设定）；
        拉取失败直接上抛崩溃阻断（按 user_rules，不 try-ex 静默跳过）。
        """
        from brain.hc.shortlight import get_shortlight_notepad
        manual = await get_shortlight_notepad().render_manual(max_chars=1000)
        if manual:
            self._blocks["notebook_manual"] = manual

    def _compile(self) -> str:
        """过滤空槽位后用 \\n 拼接，避免出现连续空行。"""
        return "\n".join(v for v in self._blocks.values() if v)

    # ===== 外部 =====

    def clear_bequest_cache(self) -> None:
        from brain.pfc.pfca.pfca_bequest import get_pfca_bequest
        get_pfca_bequest().clear()
        logger.debug("PFCA经验缓存已清空")


_pfca_input_manager_instance: Optional[PFCAInputManager] = None


def get_pfca_input_manager() -> PFCAInputManager:
    """获取 PFCAInputManager 模块级单例。

    prepare_first_message（pfca_core 启动）与 get_user_input（PFCASession 消费）
    必须落在**同一个实例**上，否则首条消息会缓存进一个被丢弃的实例而静默丢失
    （历史 bug，见 docs/已完成.md #3）。
    新一轮 PFCA 由 PFCACore.start 先调 reset() 复位状态再 prepare。
    __init__ 的 _initialized 守卫即为此模式设计：单例被重复构造时不重置状态。
    """
    global _pfca_input_manager_instance
    if _pfca_input_manager_instance is None:
        _pfca_input_manager_instance = PFCAInputManager()
    return _pfca_input_manager_instance
