# -*- coding: utf-8 -*-
"""
通用上下文管理器（AgentContextManager）。

为 advance agent（opinion / reorganize / judgment）统一装配 LLM user 上下文，
采用「类 input manager」的槽位 + provider 模式（参照 pfca_input_manager 的
_blocks dict + _compile 空段过滤）：
- add_block：静态槽位，按添加顺序聚合。
- register_provider：注册异步 provider（async callable，无参，返回 str 或 None），
  按注册顺序聚合。
- build()：先槽位、后 provider，过滤空段后以 "\n" 拼接。

无状态设计：每次任务实例化使用，不做模块级单例（不提供 get_xxx() 访问器）。
"""

from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from utils.logger import get_logger

logger = get_logger('context_manager', "INFO")


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


Provider = Callable[[], Awaitable[Optional[str]]]


class AgentContextManager:
    """通用上下文管理器（无状态，每次任务实例化使用）。

    两类数据源：
    - 槽位（add_block）：静态文本，按添加顺序聚合；同 key 重复添加覆盖。
    - provider（register_provider）：异步无参 callable，返回 str 或 None，
      按注册顺序聚合；返回 None / 空文本（去空白后为空）时跳过。
    """

    def __init__(self) -> None:
        self._blocks: Dict[str, str] = {}
        self._providers: List[Tuple[str, Provider]] = []

    def add_block(self, key: str, text: str) -> None:
        """添加静态槽位；同 key 重复添加时覆盖（None/空视为空槽）。"""
        self._blocks[key] = text or ""

    def register_provider(self, name: str, provider: Provider) -> None:
        """注册异步 provider（async callable，无参，返回 str 或 None）。

        provider 抛出的异常（如 AMY 实例未运行）不做任何吞并，
        由 build() 原样上抛——关键上下文获取失败必须崩溃阻断（user_rules）。
        """
        self._providers.append((name, provider))

    async def build(self) -> str:
        """按注册顺序聚合非空段：先槽位、后按注册顺序的 provider。

        空段（槽位去空白后为空、provider 返回 None / 去空白后为空）过滤后以 "\n"
        拼接，避免出现连续空行；全部为空时返回空字符串。
        """
        parts: List[str] = [v for v in self._blocks.values() if v and v.strip()]
        for name, provider in self._providers:
            text = await provider()
            if text and text.strip():
                parts.append(text)
            else:
                logger.debug(f"provider '{name}' 返回空，跳过该段")
        return "\n".join(parts)


def _collect_current_environment(manager: AgentContextManager) -> None:
    """预置【当前环境状态】槽位：当前日期 + 时刻 + 时段。"""
    from datetime import datetime

    now = datetime.now()
    period = _describe_time_period(now.hour)
    manager.add_block(
        "current_environment",
        f"【当前环境状态】{now.year}年{now.month}月{now.day}日 "
        f"{now.strftime('%H:%M')} {period}",
    )


def register_amy_emotion_provider(manager: AgentContextManager) -> None:
    """注册 AMY 情绪 provider，供自定义装配时单独注入情绪段。

    provider 产出文本：决策侧出口 get_decision_description() 的单行 SC
    行动对应 + "原因："（仅最终评价类想法摘要），不再自行二次拼接
    build_thought_summary（成因已由出口内嵌，见 docs/api/amy.md）。
    情绪体系隔离（2026-09-13）：light_agent 属决策侧，只见 SC 轴，
    不见 NE/DA 表现侧激素行（conversation/social_chat 专属）。
    均空则返回 None，由 build() 跳过。

    重要用户规则：AMY 情绪获取失败（如实例未运行）必须直接崩溃阻断上抛，
    禁止 try-except 吞掉继续——故此处不对 get_emotional_interface /
    get_decision_description 做任何捕获。
    """

    async def _amy_emotion_provider() -> Optional[str]:
        from brain.amy.emotional_interface import get_emotional_interface

        amy = await get_emotional_interface()
        return await amy.get_decision_description()

    manager.register_provider("amy_emotion", _amy_emotion_provider)


def build_default_context() -> AgentContextManager:
    """模块级默认装配：当前环境/时间段槽位 + AMY 情绪 provider。

    使用方每次任务调用本函数拿新实例，再按需 add_block 补充业务槽位，
    最后 await build() 出拼接文本。不做模块级单例（无状态设计）。
    """
    manager = AgentContextManager()
    _collect_current_environment(manager)
    register_amy_emotion_provider(manager)
    return manager