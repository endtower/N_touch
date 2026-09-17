# -*- coding: utf-8 -*-
"""
proactive 模块单次会话

类比 PFCASession：
- 接收一个 task_draft，构造并注入 user 消息到 history（唯一注入点）
- 管理 LLM 调用循环
- 处理工具调用
- 当 launch_pfca 工具被调用后，session 退出（task_launched）

设计：
- 类比 PFCASession 的 process_cycle / _inject_single_user_message / _call_llm / _handle_tool_call
- 类比 PFCASession 的 max_no_tool_call / max_tool_error 自我保护
- launch_pfca 视为"终结工具"：调用后设 _exit_reason = "task_launched" 并退出

继承重型 Session 基类（HeavySession）。因 proactive 的 start() 含广播 / 历史清理 /
首条消息准备 / 异常兜底，process_cycle() 含终结工具逻辑，故这两个方法保留子类实现。
"""

import asyncio
from typing import Any, Dict, Literal, Optional

from utils.error_handling import async_handle_errors
from utils.llm_client_manager import TokenBudget, TokenBudgetExceededError, token_budget_scope
from utils.logger import get_logger

from brain.base import HeavySession
from .proactive_history_manager import (
    get_proactive_history_manager,
    reset_proactive_history_manager,
)
from .proactive_input_manager import (
    FirstMessageType,
    get_proactive_input_manager,
)
from .proactive_prompt import get_proactive_system_prompt
from .proactive_tools import get_proactive_tool_registry

logger = get_logger("ProactiveSession", "INFO")


# ============================================================
# 终结工具：调用后 session 退出
# ============================================================
EXIT_TOOLS = {"launch_pfca", "skip_proactive"}


class ProactiveSession(HeavySession):
    """单次主动任务会话 — 类比 PFCASession"""

    # 单会话累计 token 预算（熔断值，参照 token_stats.json 校准）
    TOKEN_BUDGET = 500_000
    # 本类重写了 start()（不走基类骨架），max_rounds 在自有循环里手工判
    max_rounds = 40

    def __init__(self, task_draft: Dict[str, Any]):
        self.task_draft = task_draft
        self.intent: str = task_draft.get("intent", "unknown")
        self.reason: str = task_draft.get("reason", "")
        self.target_session_id: Optional[str] = task_draft.get("target_session_id")
        self.payload: Dict[str, Any] = task_draft.get("payload", {}) or {}

        self.history_manager = get_proactive_history_manager()
        self.input_manager = get_proactive_input_manager()
        self.tool_registry = get_proactive_tool_registry()

        self.running: bool = True
        self.should_stop: bool = False
        self._exit_reason: Optional[str] = None
        self._exit_content: Optional[str] = None
        self.no_tool_call_count: int = 0
        self.max_no_tool_call: int = 3
        self.tool_error_count: int = 0
        self.max_tool_error: int = 3
        self._rounds: int = 0

        # 用于触发时构造 input_manager（外层在 start() 前调用 prepare）
        self._prepared: bool = False

        # 结果
        self.decision_result: Optional[Dict[str, Any]] = None

    def get_tools(self):
        """获取工具 schema 列表（委托给 tool_registry）"""
        return self.tool_registry.get_tools()

    async def _handle_tool_call(self, tool_call):
        """处理单个工具调用（委托给 tool_registry）"""
        return await self.tool_registry.handle_tool_call(tool_call)

    # ============================================================
    # 生命周期
    # ============================================================

    @async_handle_errors()
    async def start(self) -> Dict[str, Any]:
        """启动一次 proactive 会话"""
        if self.task_draft.get("running_flag") is False:
            # 防呆：start 被重复调
            return {}

        logger.info(f"ProactiveSession 启动 intent={self.intent}, reason={self.reason}")

        # 广播: proactive_session 激活 -> 前端 IdentityBar 显示 "proactive 处理中"
        # (后端维护单一 activity 状态, source='proactive')
        from brain.hc.global_session_state import get_global_session_state
        get_global_session_state().set_activity(source="proactive", tool_name="proactive_session")

        # 重置 history（保证每次会话是干净的上下文）
        #
        # 内存/磁盘分离后 LLM 只消费 history_manager 的内存会话历史（重启后
        # 恒为空），旧"磁盘残留触发 OpenAI 400"的问题不复存在。这里直接归档
        # 上一轮：终结链写入资格 / 轮转 unlinked 常驻文件，磁盘展示记录保留。
        if self.history_manager is None:
            self.history_manager = get_proactive_history_manager()
        self.history_manager.archive_context()

        self.input_manager.clear_bequest_cache() if hasattr(self.input_manager, "clear_bequest_cache") else None

        # 1. 准备 input_manager 的首次消息
        await self._prepare_first_message()

        # 2. 推进主循环（含 token 预算作用域与轮数上限；process_cycle 用基类实现）
        budget = TokenBudget(self.TOKEN_BUDGET) if self.TOKEN_BUDGET else None
        self.token_budget = budget
        self._recent_tool_sigs = []
        self._tool_repeat_strikes = 0
        self._last_repeat_tool = ""
        try:
            with token_budget_scope(budget):
                while (self.running and not self.should_stop
                       and self._rounds < self.max_rounds):
                    if budget is not None and budget.exceeded:
                        self._exit_reason = f"token 预算耗尽 ({budget.used}/{budget.max_tokens})"
                        logger.warning(f"ProactiveSession {self._exit_reason}")
                        break
                    await self.process_cycle()
        except TokenBudgetExceededError as e:
            self._exit_reason = str(e)
            logger.warning(f"ProactiveSession 会话因 token 预算退出: {e}")
        except Exception as e:
            logger.warning(f"ProactiveSession 主循环异常: {e}")
            self._exit_reason = f"error: {e}"

        if self._exit_reason is None and self._rounds >= self.max_rounds:
            self._exit_reason = f"达到最大轮数 ({self.max_rounds})"
            logger.warning(f"ProactiveSession {self._exit_reason}")

        logger.info(f"ProactiveSession 退出, reason={self._exit_reason}")

        # 广播: proactive_session 退出 -> 清除活动状态, 前端回退到 "事件链未激活"
        from brain.hc.global_session_state import get_global_session_state
        get_global_session_state().clear_activity()

        return {
            "intent": self.intent,
            "exit_reason": self._exit_reason,
            "decision_result": self.decision_result,
        }

    async def stop(self, exit_reason: Optional[str] = None) -> None:
        """外部请求停止"""
        logger.info(f"ProactiveSession 收到停止信号 intent={self.intent}, reason={exit_reason}")
        self.should_stop = True
        self.running = False
        if exit_reason:
            self._exit_reason = exit_reason

    # ============================================================
    # 准备首次 user 消息
    # ============================================================

    async def _prepare_first_message(self) -> None:
        """通过 input_manager 准备首次消息（plan + triggered + 任务草稿）"""
        if self._prepared:
            return
        self._prepared = True

        planned_tasks = await self._fetch_planned_tasks()
        triggered_task = (
            self.payload.get("task")
            or self.task_draft.get("triggered_task")
        )

        # 当 truck 传入了触发任务时, 在首条 user 消息底部追加一句「本次主动唤醒原因」
        # 唤醒原因文本由 truck 在 task_draft.wakeup_reason 中显式给出
        # (目前由 scheduled_proactive_truck / ongoing_truck 两个推给 proactive 的 truck 强制填入);
        # 未提供则温柔兜底 (有 triggered_task 时用任务型文案, 否则省略避免空话)。
        wakeup_reason = self.task_draft.get("wakeup_reason")
        if not wakeup_reason and triggered_task:
            wakeup_reason = "存在未完成的任务，请处理！"

        self.input_manager.prepare_first_message(
            message_type=FirstMessageType.NORMAL_STARTUP,
            task_draft=self.task_draft,
            planned_tasks=planned_tasks,
            triggered_task=triggered_task,
            wakeup_reason=wakeup_reason,
        )
        logger.debug(
            f"proactive 首次消息已准备好, payload.task={'有' if triggered_task else '无'}, "
            f"planned={len(planned_tasks)}, wakeup_reason={'有' if wakeup_reason else '无'}"
        )

    async def _fetch_planned_tasks(self) -> list:
        """拉取所有未完成任务，喂给 input_manager

        拉取失败直接上抛崩溃阻断（按 user_rules，不 try-ex 静默跳过）。"""
        from brain.hc.database.task import TaskOperations
        task_ops = TaskOperations()
        task_ids = await task_ops.get_active_tasks(limit=50)
        tasks = []
        for tid in task_ids:
            rec = await task_ops.get_task_by_id(tid)
            if rec:
                tasks.append({
                    "task_id": rec.get("task_id"),
                    "title": rec.get("title"),
                    "summary": rec.get("summary"),
                    "next_occurrence": rec.get("next_occurrence"),
                })
        return tasks

    # ============================================================
    # 主循环（复用父类 HeavySession.process_cycle 固定骨架）
    # 终结工具（launch_pfca / skip_proactive）通过 _on_tool_result 钩子处理
    # ============================================================

    def _on_tool_result(self, tool_name: str, result: Any) -> None:
        """终结工具：调用后设置 decision_result / exit_reason 并退出 session。"""
        content = result.get("content", "") if isinstance(result, dict) else str(result)
        if tool_name == "launch_pfca":
            self.decision_result = {
                "intent": self.intent,
                "tool_name": tool_name,
                "tool_result": content,
            }
            self._exit_reason = "task_launched"
            self.running = False
            logger.info(f"proactive 终结工具 {tool_name} 已调用, 退出 session")
        elif tool_name == "skip_proactive":
            self.decision_result = {
                "intent": self.intent,
                "tool_name": tool_name,
                "tool_result": content,
                "skipped": True,
            }
            self._exit_reason = "task_skipped"
            self.running = False
            logger.info(f"proactive 终结工具 {tool_name} 已调用, 退出 session (skipped)")

    async def _inject_single_user_message(self) -> bool:
        """从 input_manager 拿首次消息，注入 history"""
        if self.input_manager._queued_request is None:
            return False
        content = await self.input_manager.build_first_content()
        if content:
            self.history_manager.append({"role": "user", "content": content})
            logger.debug(f"proactive 注入首次 user 消息, 长度={len(content)}")
            return True
        return False

    async def _call_llm(self):
        """调 LLM"""
        from utils.llm_client_manager import call_llm

        messages = self.history_manager.get_history()
        # 防御性兜底：如果历史里最后一条是 "assistant 带 tool_calls 但没有跟 tool message"，
        # 这种错位序列会让 OpenAI API 返回 400 'insufficient tool messages'。
        # （上次会话异常退出但磁盘已写入 assistant 消息的极端情况下会出现）
        messages = self._sanitize_history_for_llm(messages)
        full_messages = [{"role": "system", "content": get_proactive_system_prompt()}] + messages

        response = await call_llm(
            level="flash",
            messages=full_messages,
            tools=self.tool_registry.get_tools(),
        )
        assistant_message = response.choices[0].message
        logger.info(
            f"proactive LLM 响应: content={assistant_message.content[:80] if assistant_message.content else 'None'}, "
            f"tool_calls={len(assistant_message.tool_calls or [])}"
        )
        return assistant_message

    @staticmethod
    def _sanitize_history_for_llm(messages):
        """
        兜底清洗：删除末尾的"未配对 assistant(tool_calls)"消息。
        OpenAI 协议要求 assistant(tool_calls) 必须紧跟 tool message，错位会 400。
        返回清洗后的 messages（不修改原列表）。
        """
        if not messages:
            return messages

        cleaned = list(messages)
        # 只在末尾检查，从尾往前裁
        while cleaned:
            last = cleaned[-1]
            role = last.get("role")
            tool_calls = last.get("tool_calls") or []
            if role == "assistant" and tool_calls:
                # 这条 assistant 后面没有对应的 tool 响应 → 裁掉
                logger.warning(
                    f"proactive 历史末尾发现未配对的 assistant(tool_calls)，裁掉避免 400: "
                    f"tool_calls={[tc.get('function',{}).get('name') for tc in tool_calls]}"
                )
                cleaned.pop()
                continue
            break
        return cleaned
