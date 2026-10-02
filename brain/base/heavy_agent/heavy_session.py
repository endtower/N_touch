# -*- coding: utf-8 -*-
"""重型 Session 基类：有状态、多轮循环、持久化上下文的会话执行单元。

设计记录：docs/已完成.md（HeavySession 框架基类）

本基类抽象出 4 个重型 Session（PFCASession / ConversationSession /
SocialChatSession / ProactiveSession）的公共骨架：
- 固定骨架：start() / process_cycle() / stop() 由基类实现，子类不重写。
- 可变点：消息注入（首条 / 增量）、循环终止、自我保护上限、LLM mode
  由子类通过抽象方法实现。

设计原则：
- 唯一注入点：每轮最多一条 user 消息（首条 > 增量 优先级）。
- 自我保护：no_tool_call_count / tool_error_count 上限，超限抛 AbyssCoreError 崩溃。
- 统一返回值：start() 返回 self._build_result()（默认 _exit_content，str）。
"""

from abc import ABC, abstractmethod
import json
from collections import deque
from typing import Dict, List, Any, Optional

from utils.logger import get_logger
from utils.error_handling import AbyssCoreError
from utils.llm_client_manager import (
    TokenBudget, TokenBudgetExceededError, token_budget_scope, agent_scope, extract_reasoning,
)

logger = get_logger('HeavySession', "DEBUG")


class HeavySession(ABC):
    """重型 Session 基类：有状态、多轮循环、持久化上下文的会话执行单元"""

    # token 统计归集用的 agent 身份；None 时回退为类名
    agent_name: Optional[str] = None

    # ============ 熔断/行为阀配置（子类可覆盖） ============

    # 单会话累计 token 预算（含工具内部经 light_agent 等发起的次级调用）。
    # None = 不限制。校准参照根目录 token_stats.json 的单次均值。
    TOKEN_BUDGET: Optional[int] = None
    # 连续相同 (工具, 参数) 调用的判定阈值（防"成功但无进展"的轮询空转）。
    # None = 不启用该检测。
    repeat_threshold: Optional[int] = 3
    # 豁免重复检测的工具名（参数相同是合法语义的，如等待观察类）
    repeat_exempt_tools: frozenset = frozenset()

    # ============ 子类必须实现的抽象方法 ============

    @abstractmethod
    def get_tools(self) -> List[Dict]:
        """获取工具 schema 列表（委托给子类的 *ToolManager）"""

    @abstractmethod
    async def _handle_tool_call(self, tool_call) -> Dict[str, Any]:
        """处理单个工具调用（委托给子类的 *ToolManager）"""

    @abstractmethod
    async def _call_llm(self):
        """调用 LLM（组装 system prompt + history）"""

    @abstractmethod
    async def _inject_first_user_message(self) -> bool:
        """注入首条 user 消息（唯一注入点）。返回是否成功注入。"""

    @abstractmethod
    async def _inject_incremental_user_message(self) -> bool:
        """注入后续增量 user 消息（唯一注入点）。返回是否成功注入。"""

    # ============ 可重写的钩子（默认实现） ============

    async def should_continue(self) -> bool:
        """判断是否继续循环。默认：running 且未达 max_rounds（None = 不限轮数）。"""
        return self.running and (self.max_rounds is None or self._rounds < self.max_rounds)

    def _inject_no_tool_call_warning(self) -> None:
        """无工具调用时注入警告 user 消息（持久化上下文，与正常上下文注入逻辑一致）。
        子类可重写以自定义警告文案。"""
        self.history_manager.append({
            "role": "user",
            "content": "【系统警告】未使用工具，请使用工具回复。",
        })

    def _should_append_tool_response(self, tool_response: Any) -> bool:
        """判断工具响应是否写入 history。默认 True。"""
        return True

    def _build_result(self) -> str:
        """构造返回值。默认返回 self._exit_content（str）。"""
        return self._exit_content

    # ============ 固定骨架（基类实现，子类不重写） ============

    async def start(self) -> str:
        """启动会话循环（固定骨架），返回 self._build_result()。"""
        self.running = True
        self._exit_reason = None
        self._exit_content = None
        self.no_tool_call_count = 0
        self.tool_error_count = 0
        self._rounds = 0
        self._recent_tool_sigs = deque(maxlen=self.repeat_threshold)
        self._tool_repeat_strikes = 0
        self._last_repeat_tool = ""

        self.history_manager.archive_context()   # 归档上一轮

        budget = TokenBudget(self.TOKEN_BUDGET) if self.TOKEN_BUDGET else None
        self.token_budget = budget
        # agent_scope 必须在进入循环前设好：工具经 asyncio.create_task 派生的子任务
        # 会拷贝当前 context，从而把次级 LLM 调用一并归入本会话（轻量 agent 自行覆盖）
        with token_budget_scope(budget), agent_scope(self.agent_name or type(self).__name__):
            while self.running:
                # 累计 token 预算熔断（预算在 LLM 网关记账，这里负责优雅退出）
                if budget is not None and budget.exceeded:
                    self._exit_reason = f"token 预算耗尽 ({budget.used}/{budget.max_tokens})"
                    logger.warning(f"{self.__class__.__name__} {self._exit_reason}")
                    break
                if not await self.should_continue():
                    logger.info("会话结束条件满足，退出循环")
                    break
                try:
                    await self.process_cycle()
                except TokenBudgetExceededError as e:
                    # 预算在 process_cycle 内部（如无工具重试循环）被网关拒绝
                    self._exit_reason = str(e)
                    logger.warning(f"{self.__class__.__name__} 会话因 token 预算退出: {e}")
                    break

        return self._build_result()

    async def process_cycle(self) -> None:
        """处理单次 LLM 调用和工具执行（固定骨架）。

        流程：
        1. 自我保护检查（连续 max_no_tool_call 次无工具 / max_tool_error 次工具错误 → 崩溃）。
        2. 注入 user 消息：第一轮注入首条，后续轮次注入增量。
        3. 调用 LLM，并把 LLM 输出的消息注入上下文（持久化）。
        4. 若使用工具：执行工具并回灌，正式进入下一轮循环。
        5. 若未使用工具：注入警告 user 消息，继续调用 LLM，直到使用工具或达到上限。
        """
        # 自我保护检查（max_* 为 None 表示不启用该上限）
        if self.max_no_tool_call is not None and self.no_tool_call_count >= self.max_no_tool_call:
            raise AbyssCoreError(
                f"LLM 累计 {self.no_tool_call_count} 次未使用工具，强制停止"
            )
        if self.max_tool_error is not None and self.tool_error_count >= self.max_tool_error:
            raise AbyssCoreError(
                f"工具调用累计 {self.tool_error_count} 次参数错误，强制停止"
            )

        # 1. 注入 user 消息（第一轮首条，后续增量）
        if self._rounds == 0:
            await self._inject_first_user_message()
        else:
            await self._inject_incremental_user_message()

        # 2. 调用 LLM（内部循环：无工具则注入警告并继续，直到使用工具或达上限）
        while True:
            assistant_message = await self._call_llm()
            if assistant_message is None:
                logger.warning("无效的 LLM 响应格式")
                return

            # 3. 将 LLM 输出的消息注入上下文（持久化）
            self._write_assistant_message(assistant_message)

            # 4. 处理响应
            if assistant_message.tool_calls:
                self.no_tool_call_count = 0
                repeat_warned = False
                for tool_call in assistant_message.tool_calls:
                    # 连续重复调用检测：同参数空转的熔断（如反复 query_history 等新消息）
                    verdict = self._register_tool_call(tool_call)
                    if verdict != "ok":
                        # 不执行该调用，但必须回灌合成 tool 消息保持消息配对合法
                        # （assistant(tool_calls) 之后的每个 tool_call 都要有对应 tool 响应）
                        self.history_manager.append(self._skipped_tool_response(tool_call))
                        if verdict == "stop":
                            self.running = False
                            self._exit_reason = "连续重复调用同一工具且无进展（再次命中）"
                            logger.error(f"{self.__class__.__name__} {self._exit_reason}")
                            continue  # 剩余 tool_calls 仍需回灌，外层 while 随后退出
                        repeat_warned = True
                        continue
                    tool_response = await self._handle_tool_call(tool_call)
                    if isinstance(tool_response, str) and tool_response.startswith("[错误]"):
                        self.tool_error_count += 1
                    if self._should_append_tool_response(tool_response):
                        self.history_manager.append(tool_response)
                if repeat_warned and self.running:
                    self._inject_repeat_warning()
                break  # 使用工具，正式进入下一轮循环
            else:
                # 5. 无工具调用：注入警告 user 消息，继续下一轮 LLM 输出
                self.no_tool_call_count += 1
                logger.warning(
                    f"LLM 未使用工具 ({self.no_tool_call_count}/{self.max_no_tool_call}): "
                    f"{(assistant_message.content or '')[:50]}..."
                )
                if self.max_no_tool_call is not None and self.no_tool_call_count >= self.max_no_tool_call:
                    raise AbyssCoreError(
                        f"LLM 累计 {self.no_tool_call_count} 次未使用工具，强制停止"
                    )
                self._inject_no_tool_call_warning()

        self._rounds += 1

    def _write_assistant_message(self, assistant_message) -> None:
        """把 LLM 输出的消息注入上下文（持久化）。无论是否带 tool_calls。"""
        assistant_dict = {
            "role": "assistant",
            "content": assistant_message.content,
            "reasoning_content": extract_reasoning(assistant_message),
        }
        if assistant_message.tool_calls:
            assistant_dict["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in assistant_message.tool_calls
            ]
        self.history_manager.append(assistant_dict)

    def _register_tool_call(self, tool_call) -> str:
        """记录工具调用签名并判定"成功但无进展"的空转。

        签名 = 工具名 + 规范化参数（dict 排序键后序列化，防字段顺序抖动）。

        Returns:
            "ok"   — 正常执行
            "warn" — 连续 repeat_threshold 次相同调用，第一次命中（注入自纠提示）
            "stop" — 命中后仍重复（第二次满额），应强制结束会话
        """
        function = getattr(tool_call, "function", None)
        name = getattr(function, "name", "") or ""
        raw_args = getattr(function, "arguments", "") or ""

        if self.repeat_threshold is None:
            return "ok"   # 检测未启用

        try:
            normalized = json.dumps(json.loads(raw_args), sort_keys=True, ensure_ascii=False)
        except Exception:
            normalized = str(raw_args)
        sig = f"{name}:{normalized}"

        if name in self.repeat_exempt_tools:
            self._recent_tool_sigs.clear()
            return "ok"

        sigs = self._recent_tool_sigs
        sigs.append(sig)
        if len(sigs) < self.repeat_threshold or any(s != sig for s in sigs):
            return "ok"

        # 满额命中：清空窗口重新计数，strike 记一次
        self._recent_tool_sigs.clear()
        self._last_repeat_tool = name
        self._tool_repeat_strikes += 1
        return "warn" if self._tool_repeat_strikes == 1 else "stop"

    def _skipped_tool_response(self, tool_call) -> Dict[str, Any]:
        """构造"已跳过"的合成 tool 响应（保持 assistant(tool_calls) 的消息配对合法）。"""
        return {
            "role": "tool",
            "tool_call_id": getattr(tool_call, "id", None),
            "name": getattr(getattr(tool_call, "function", None), "name", ""),
            "content": (
                "[已跳过-重复调用] 连续多次以相同参数调用同一工具且结果无变化，"
                "本次调用未执行。若在等待外部消息，请调用 terminate 结束本轮会话，"
                "新消息到达会自动唤醒新一轮。"
            ),
        }

    def _inject_repeat_warning(self) -> None:
        """注入点名式自纠提示（user 消息，进持久化上下文）。"""
        self.history_manager.append({
            "role": "user",
            "content": (
                f"【系统提示】检测到你连续 {self.repeat_threshold} 次以相同参数调用 "
                f"{self._last_repeat_tool} 且结果无变化。等待外部回复不是本会话的职责："
                f"请基于当前信息行动，或调用 terminate 结束本轮——新消息到达会自动唤醒新一轮。"
            ),
        })
        logger.warning(
            f"{self.__class__.__name__} 检测到重复调用空转 (tool={self._last_repeat_tool})，"
            f"已注入自纠提示 (strike {self._tool_repeat_strikes})"
        )

    async def stop(self, exit_reason: Optional[str] = None) -> None:
        """停止会话（固定骨架）"""
        self.running = False
        if exit_reason is not None:
            self._exit_reason = exit_reason
        logger.debug(f"{self.__class__.__name__} 已停止")
