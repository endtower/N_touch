# -*- coding: utf-8 -*-
"""
proactive 输入管理模块

类比 pfca_input_manager：
- 缓存并更新槽位数据（由外部调用 update 方法）
- 提供槽位数据获取接口（编译后返回 content）
- 增量变化检测

设计原则：
- 解耦：更新和获取分离
- 仅构造 content：只返回 str 类型
- 变化检测：实例级状态用于检测增量变化

Slot 设计（参考 pfca_input_manager）：
    message_base:    本次主动任务的核心描述（intent + reason + target）
    planned_tasks:   当前数据库里所有未完成任务
    triggered_task:  当前已到达预定时间的那个任务
    amy_effect:      情绪状态
    temporary_memory: 短期记忆
"""

from typing import Dict, Any, Optional, List

from utils.logger import get_logger

logger = get_logger("ProactiveInputManager", "INFO")


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

        # ========== 实例级状态（用于变化检测） ==========
        self._last_amy: str = ""

        self._initialized = True
        logger.debug("ProactiveInputManager 初始化")

    # ========== 队列操作方法 ==========

    def prepare_first_message(
        self,
        message_type: str,
        task_draft: Dict[str, Any],
        planned_tasks: List[Dict[str, Any]] = None,
        triggered_task: Optional[Dict[str, Any]] = None,
        wakeup_reason: Optional[str] = None,
    ) -> None:
        """缓存首次消息请求，由 proactive_session 在合适时机调用 build_first_content() 获取 content

        Args:
            message_type: 消息类型（当前仅 NORMAL_STARTUP）
            task_draft: 主动任务草稿
            planned_tasks: 数据库里所有未完成任务（用于"当前计划中要做的任务"）
            triggered_task: 当前已到达预定时间的任务
            wakeup_reason: 本次主动唤醒原因（注入到首条 user 消息最底部）
        """
        self._queued_request = {
            "message_type": message_type,
            "task_draft": task_draft or {},
            "planned_tasks": planned_tasks or [],
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

        # 1. 重置槽位
        self._reset_blocks()

        # 2. 收集计划中要做的任务
        self._collect_planned_tasks(request["planned_tasks"])

        # 3. 收集当前已触发的任务
        self._collect_triggered_task(request["triggered_task"])

        # 4. 收集近期事件（最近 5 条临时记忆）
        self._collect_recent_memory()

        # 5. 收集动态块
        await self._collect_amy_effect()

        # 6. 收集本次主动唤醒原因（位于心情下方，最末）
        self._collect_wakeup_reason(request.get("wakeup_reason"))

        # 7. 编译并返回
        content = self._compile()
        if content:
            logger.info(f"Proactive 首次消息 content 构造完成，长度: {len(content)}")
            return content
        return None

    # ========== 槽位重置 / 编译 ==========

    def _reset_blocks(self) -> None:
        from datetime import datetime
        self._blocks = {
            "current_time": f"【当前时间】{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "planned_tasks": "",
            "triggered_task": "",
            "recent_memory": "",
            "amy_effect": "",
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
        """收集主动任务核心描述（intent / reason / target）"""
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

    def _collect_planned_tasks(self, planned_tasks: List[Dict[str, Any]]) -> None:
        """收集"当前计划中要做的任务"，即所有未完成任务"""
        if not planned_tasks:
            self._blocks["planned_tasks"] = "（当前没有进行中的任务）"
            return
        lines = ["【当前计划中要做的任务】"]
        for i, task in enumerate(planned_tasks, 1):
            title = task.get("title", "未知")
            summary = task.get("summary", "")
            next_occ = task.get("next_occurrence") or "未排期"
            lines.append(f"{i}. {title}")
            if summary:
                lines.append(f"   概况: {summary}")
            lines.append(f"   下次触发时间: {next_occ}")
        self._blocks["planned_tasks"] = "\n".join(lines)

    def _collect_triggered_task(self, triggered_task: Optional[Dict[str, Any]]) -> None:
        """收集"当前已到达预定时间的任务"（即本次触发的任务）"""
        if not triggered_task:
            return
        title = triggered_task.get("title", "未知")
        summary = triggered_task.get("summary", "")
        next_occ = triggered_task.get("next_occurrence", "")
        lines = [
            "【当前已到达计划再处理时间的未完成任务】",
            f"标题: {title}",
        ]
        if summary:
            lines.append(f"概况: {summary}")
        if next_occ:
            lines.append(f"触发时间: {next_occ}")
        self._blocks["triggered_task"] = "\n".join(lines)

    def _collect_recent_memory(self, limit: int = 5) -> None:
        """收集近期事件（最近 N 条临时记忆），拼接在【当前已到达预定时间的事件】之后

        通过临时记忆的统一接口 get_recent_experience 获取，
        返回结构: 【近期事件】(当前时间: ...)\\n1. [...] xxx\\n...

        Args:
            limit: 拉取最近 N 条，默认 5
        """
        # 记忆获取失败直接上抛崩溃阻断（硬约定 #4），与 PFCA 读取侧语义一致
        from brain.hc.temporary_memory import get_recent_experience
        text = get_recent_experience(limit=limit, label="近期事件", with_current_time=True)
        if text:
            self._blocks["recent_memory"] = text

    async def _collect_amy_effect(self) -> None:
        """收集情绪状态（情绪消费者游标，决策侧）。

        契约与 pfca 一致（get_unread_events → 取描述 → 成功后再 advance_cursor）：
        - 无未消费事件直接返回；
        - 情绪体系隔离过滤：仅当未读事件含最终评价类事件（act 命中
          FinalEvaluationAct 词表）才更新情绪槽；表现侧 NE/DA 事件
          （conversation/social_chat 的情感遭遇）是噪声，仅推进游标消费掉。
        - 取描述失败/为空时不推进游标，事件留给下一轮，不丢；
        - 取描述异常直接上抛崩溃阻断（硬约定），不吞异常。
        """
        from brain.amy.emotional_interface import get_emotional_interface
        from brain.amy.constants import FinalEvaluationAct

        ei = await get_emotional_interface()
        events = await ei.get_unread_events("proactive")
        if not events:
            return

        max_seq = max(e["seq"] for e in events)
        # 决策侧只关心最终评价类事件；NE/DA 噪声事件游标照常推进消费掉
        # （不推进会无限积压重查），但不触发情绪槽更新。
        final_events = [
            e for e in events
            if FinalEvaluationAct.kind_of(e.get("act", "") or "") is not None
        ]
        if not final_events:
            await ei.advance_cursor("proactive", max_seq)
            return

        # 决策侧出口描述：单行 SC 行动对应 + 原因：最终评价类想法摘要（成因出口内嵌）
        current = await ei.get_decision_description()
        if not current:
            return

        # 描述已成功取到，推进游标到已消费的最大 seq
        await ei.advance_cursor("proactive", max_seq)

        # 描述与上次相同则不重复注入（游标已推进，仅跳过槽位更新）
        if current == self._last_amy:
            return
        self._last_amy = current
        self._blocks["amy_effect"] = current

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
        self._last_amy = ""
        logger.debug("ProactiveInputManager 已清空")


def get_proactive_input_manager() -> ProactiveInputManager:
    """获取 ProactiveInputManager 单例"""
    return ProactiveInputManager()
