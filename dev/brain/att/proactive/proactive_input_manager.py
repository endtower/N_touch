# -*- coding: utf-8 -*-
"""
proactive 输入管理模块

类比 pfca_input_manager：
- 缓存并更新槽位数据（由外部调用 update 方法）
- 提供槽位数据获取接口（编译后返回 content）

首条 content 的拼接口径与 pfca_input_manager._build_first_input 对齐
（槽位定义序即拼接序，_compile 过滤空槽，条件段可能缺席）：

    {message_base}                        <- 本次主动任务的核心描述
                                             （intent + reason + 目标会话 + 附加信息）
    【当前环境状态】{年月日 时刻 时段}        <- 纯同步取系统时间，恒非空
    【当前情绪】{轴名}：{值}/100。{感受}      <- amy 决策侧描述（全部轴），首条无条件给出；
    {事件标题} {评价标签} {分数变化}            每轴现状行下紧跟该轴最近五条变动
    {近期经历}                            <- temporary_memory 取到非空才拼（≤3 条）
    【待接续事项目录】                      <- 任务命中才拼（轻量简表：标题 + 下次处理时间，
    {title} 下次处理时间{相对时间}            概况全文不进首条，细节由 query_task_status 溯源）
    【当前已到达计划再处理时间的未完成任务】   <- 本次焦点的细节（仅这一条保留 context）
    【记事本手册】                         <- render_manual 生成（自带段头），
    记事本存在以下章节:                      章节式裸文本 ≤1000 字符，细节由 query_note 溯源
    └ {条目}
    【本次主动唤醒原因】                    <- truck 注入的唤醒原因（判断层专属，PFCA 无此段）

与 PFCA 的差异：不注入人格总览（判断层不指派人格，人格由 PFCA 逐会话指定）。
"""

from typing import Dict, Any, Optional

from utils.logger import get_logger

logger = get_logger("ProactiveInputManager", "INFO")


def _describe_time_period(hour: int) -> str:
    """按小时返回时段描述。

    划分与 pfca_input_manager._describe_time_period 一致
    （凌晨/早上/上午/中午/下午/晚上），内部实现，不引入跨模块依赖。
    """
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


class ProactiveInputManager:
    """Proactive 输入管理器"""

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        # ========== 外部请求缓存 ==========
        self._queued_request: Dict[str, Any] = None

        # ========== 槽位数据 ==========
        self._blocks: Dict[str, str] = {}

        self._initialized = True
        logger.debug("ProactiveInputManager 初始化")

    # ========== 队列操作方法 ==========

    def prepare_first_message(
        self,
        message_type: str,
        task_draft: Dict[str, Any],
        triggered_task: Optional[Dict[str, Any]] = None,
        wakeup_reason: Optional[str] = None,
    ) -> None:
        """缓存首次消息请求，由 proactive_session 在合适时机调用 build_first_content() 获取 content

        Args:
            message_type: 消息类型（当前仅 NORMAL_STARTUP）
            task_draft: 主动任务草稿（intent / reason / target_session_id / payload）
            triggered_task: 当前已到达预定时间的任务（本次焦点的细节）
            wakeup_reason: 本次主动唤醒原因（注入到首条 user 消息最底部）

        Notes:
            待接续任务目录不再由调用方传入：此处自取
            format_active_task_overview 轻量表（口径同 pfca_input_manager）。
        """
        self._queued_request = {
            "message_type": message_type,
            "task_draft": task_draft or {},
            "triggered_task": triggered_task,
            "wakeup_reason": wakeup_reason,
        }
        logger.debug(f"已缓存 proactive 首次消息请求: {message_type}")

    async def build_first_content(self) -> Optional[str]:
        """根据缓存的请求构造首次消息的 content
        仅返回 str 类型的 content，JSON 结构由调用方构造

        Returns:
            Optional[str]: 构造的 content，无请求时返回 None
        """
        if self._queued_request is None:
            return None

        request = self._queued_request
        self._queued_request = None

        # 1. 重置槽位（槽位定义序即拼接序）
        self._reset_blocks()

        # 2. 本次主动任务的核心描述（intent / reason / 目标会话 / 附加信息）
        self._collect_message_base(request["task_draft"])

        # 3. 当前环境状态（纯同步方法，不可 await）
        self._collect_current_environment()

        # 4. 情绪状态（首条无条件给出全部轴）
        await self._collect_amy_effect()

        # 5. 近期经历（最近 3 条临时记忆）
        self._collect_recent_memory(limit=3)

        # 6. 待接续事项目录（轻量简表，细节由 query_task_status 溯源）
        await self._collect_ongoing_tasks(limit=5)

        # 7. 当前已触发的任务（本次焦点的细节）
        self._collect_triggered_task(request["triggered_task"])

        # 8. 记事本手册（长期底色先于决策思考）
        await self._collect_notebook_manual()

        # 9. 本次主动唤醒原因（位于最末）
        self._collect_wakeup_reason(request.get("wakeup_reason"))

        # 10. 编译并返回
        content = self._compile()
        if content:
            logger.info(f"Proactive 首次消息 content 构造完成，长度: {len(content)}")
            return content
        return None

    # ========== 槽位重置 / 编译 ==========

    def _reset_blocks(self) -> None:
        self._blocks = {
            "message_base": "",
            "current_environment": "",
            "amy_effect": "",
            "recent_memory": "",
            "ongoing_tasks": "",
            "triggered_task": "",
            "notebook_manual": "",
            "wakeup_reason": "",
        }

    def _compile(self) -> str:
        parts = []
        for value in self._blocks.values():
            if value:
                parts.append(value)
        return "\n".join(parts)

    # ========== 槽位收集方法 ==========

    def _collect_message_base(self, task_draft: Dict[str, Any]) -> None:
        """收集主动任务核心描述（intent / reason / 目标会话 / 附加信息）"""
        lines = ["【主动任务】"]
        intent = task_draft.get("intent", "")
        reason = task_draft.get("reason", "")
        target = task_draft.get("target_session_id", "")
        if intent:
            lines.append(f"意图: {intent}")
        if reason:
            lines.append(f"原因: {reason}")
        if target:
            lines.append(f"目标会话: {target}")
        payload = task_draft.get("payload") or {}
        if payload:
            lines.append(f"附加信息: {payload}")
        self._blocks["message_base"] = "\n".join(lines)

    def _collect_current_environment(self) -> None:
        """收集当前环境状态（当前时间 + 时段）。

        格式与 pfca_input_manager._collect_current_environment 一致：
        年/月/日 + 具体时刻 + 时段（凌晨/早上/上午/中午/下午/晚上）。
        纯同步方法，不可 await。
        """
        from datetime import datetime

        now = datetime.now()
        period = _describe_time_period(now.hour)
        self._blocks["current_environment"] = (
            f"【当前环境状态】{now.year}年{now.month}月{now.day}日 "
            f"{now.strftime('%H:%M')} {period}"
        )

    async def _collect_amy_effect(self) -> None:
        """收集情绪状态（情绪消费者游标，决策侧）。

        首条口径与 pfca 一致：无条件取当前决策侧描述（统一出口
        get_decision_description，含全部轴的现状与各自最近五条变动），
        取到描述后再推进游标，把当前未读事件全部消费掉，避免其在后续轮次重复注入。
        描述取不到时不写槽且不推进游标，事件留给下一轮，不丢。
        取描述异常直接上抛崩溃阻断（硬约定），不吞异常。
        """
        from brain.amy.emotional_interface import get_emotional_interface

        ei = await get_emotional_interface()
        current = await ei.get_decision_description()
        if not current:
            return

        events = await ei.get_unread_events("proactive")
        if events:
            await ei.advance_cursor("proactive", max(e["seq"] for e in events))

        self._blocks["amy_effect"] = current

    def _collect_recent_memory(self, limit: int = 3) -> None:
        """收集近期经历（最近 N 条临时记忆）。

        口径与 pfca 一致：label 为"近期经历"，不重复注入当前时间
        （当前时间已在【当前环境状态】槽内）。
        记忆获取失败直接上抛崩溃阻断（硬约定 #4）。
        """
        from brain.hc.temporary_memory import get_recent_experience
        text = get_recent_experience(
            limit=limit, label="近期经历", with_current_time=False
        )
        if text:
            self._blocks["recent_memory"] = text

    async def _collect_ongoing_tasks(self, limit: int = 5) -> None:
        """收集待接续事项目录（只注入总数与最近的少量接续安排，细节按标题查询）。

        口径与 pfca_input_manager._collect_ongoing_tasks 一致：轻量简表，
        目录不代表当前要执行所有事项。数据库失败直接上抛。
        """
        from brain.hc.database.task import TaskOperations
        task_ops = TaskOperations()
        text = await task_ops.format_active_task_overview(limit=limit)
        if text:
            self._blocks["ongoing_tasks"] = f"【待接续事项目录】\n{text}"

    def _collect_triggered_task(self, triggered_task: Optional[Dict[str, Any]]) -> None:
        """收集"当前已到达预定时间的任务"（本次焦点的细节，保留 context 全文）"""
        if not triggered_task:
            return
        title = triggered_task.get("title", "未知")
        context = triggered_task.get("context", "")
        next_occ = triggered_task.get("next_occurrence", "")
        lines = [
            "【当前已到达计划再处理时间的未完成任务】",
            f"标题: {title}",
        ]
        if context:
            lines.append(f"上下文: {context}")
        if next_occ:
            lines.append(f"下次处理时间: {next_occ}")
        self._blocks["triggered_task"] = "\n".join(lines)

    async def _collect_notebook_manual(self) -> None:
        """收集记事本手册（章节式裸文本，细节经 query_note 工具溯源）。

        聚合渲染见 notepad.render_manual；字符预算 max_chars=1000
        （与 pfca_input_manager._collect_notebook_manual 一致）；
        拉取失败直接上抛崩溃阻断（按 user_rules，不静默跳过）。
        """
        from brain.hc.shortlight import get_shortlight_notepad
        manual = await get_shortlight_notepad().render_manual(max_chars=1000)
        if manual:
            self._blocks["notebook_manual"] = manual

    def _collect_wakeup_reason(self, wakeup_reason: Optional[str]) -> None:
        """收集本次主动唤醒原因（注入到首条 user 消息最底部）。

        Args:
            wakeup_reason: 唤醒原因字符串。无 / None / 空字符串时不注入。
        """
        if not wakeup_reason:
            return
        self._blocks["wakeup_reason"] = (
            f"【本次主动唤醒原因】\n{wakeup_reason}"
        )

    # ========== 外部调用方法 ==========

    def clear(self) -> None:
        """清空所有状态（一般用于会话/进程重置）"""
        self._queued_request = None
        self._reset_blocks()
        logger.debug("ProactiveInputManager 已清空")


def get_proactive_input_manager() -> ProactiveInputManager:
    """获取 ProactiveInputManager 单例"""
    return ProactiveInputManager()
