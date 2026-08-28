#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""全局会话状态 - 前端广播子类（FrontendBroadcastMixin）

专门处理事件链状态的前端广播。设计原则：**后端维护单一活动状态，前端只渲染**。

- 后端维护一个 `_current_activity` 单一值（source / tool_name / args_summary / status），
  事件链开始/结束、工具调用、proactive 启动/退出都由后端更新该值并广播。
- 前端只提供最基本的显示方法：收到 `activity` 就渲染，收到 `None`（idle）就清空。
- fire-and-forget：通过 asyncio.create_task 调度，不阻塞主流程。
- 没有 web 客户端 / 通道未注册 / 出错都静默跳过。

取代原先散落在各模块（pfca_tools / proactive_tools / proactive_session）里的
多类广播实现（llm_status / proactive_status），统一收敛为单一 activity 状态。
"""

import asyncio
import time
from typing import Dict, Any, Optional

from utils.logger import setup_logging

logger = setup_logging(__name__, log_level="INFO")


class FrontendBroadcastMixin:
    """前端广播子类 - 后端维护单一活动状态的前端推送"""

    # ============================================================
    # 单一活动状态（后端维护，前端只渲染）
    # ============================================================

    def set_activity(
        self,
        source: str,
        tool_name: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        parent_tool_name: Optional[str] = None,
    ) -> None:
        """更新并广播当前活动状态（后端维护单一值）。

        Args:
            source: 活动来源（"pfca" | "proactive" | "social_agent"）；
                前端根据它显示不同前缀。
            tool_name: 当前调用的工具名；为 None 时表示模块处于活跃但未调工具
                （如 proactive 启动后正在处理），status 记为 "active"。
            tool_args: 工具参数原始 dict（留给前端做扩展展示）。
            parent_tool_name: 父级工具名（层级链场景，如 social_agent 广播时带上
                "social_agent"，前端据此显示 "pfca → social_agent" 简化层级）。
        """
        self._current_activity = {
            "source": source,
            "status": "tool_call" if tool_name else "active",
            "tool_name": tool_name,
            "args_summary": self._build_args_summary(tool_name, tool_args),
            "parent_tool_name": parent_tool_name,
            "timestamp": int(time.time()),
        }
        self._broadcast_activity()

    def clear_activity(self) -> None:
        """清除活动状态并广播 idle（事件链结束时调用，前端据此清空显示）。"""
        self._current_activity = None
        self._broadcast_activity()

    def get_current_activity(self) -> Optional[Dict[str, Any]]:
        """获取当前活动状态（供快照 / 调试）。"""
        return getattr(self, "_current_activity", None)

    # ============================================================
    # 事件链生命周期钩子（由 EventChainMixin 统一调用）
    # ============================================================

    def _on_event_chain_start(self) -> None:
        """事件链开始：广播初始活动状态（重置为"系统处理中"，覆盖可能残留的旧状态）。"""
        self.set_activity(source="pfca", tool_name=None)

    def _on_event_chain_end(self) -> None:
        """事件链结束：清除活动状态（前端据此清空 "LLM 正在调用 xxx" 显示）。"""
        self.clear_activity()

    # ============================================================
    # 内部辅助
    # ============================================================

    def _build_args_summary(
        self,
        tool_name: Optional[str],
        tool_args: Optional[Dict[str, Any]],
    ) -> str:
        """构造 args_summary: "tool_name key=value key=value ..."（截断到 80 字符）。"""
        parts = [tool_name] if tool_name else []
        for k, v in (tool_args or {}).items():
            s = str(v) if v is not None else ""
            if len(s) > 24:
                s = s[:24] + "…"
            parts.append(f"{k}={s}")
        args_summary = " ".join(parts)
        if len(args_summary) > 80:
            args_summary = args_summary[:80] + "…"
        return args_summary

    def _broadcast_activity(self) -> None:
        """fire-and-forget 地把当前活动状态广播给 web 通道。"""
        activity = getattr(self, "_current_activity", None)

        async def _do():
            try:
                from adapt.manager import get_channel_manager
                mgr = await get_channel_manager()
                web_ch = mgr.get("web")
                if web_ch is None:
                    return
                await web_ch.broadcast_activity(activity)
            except Exception as e:
                logger.debug(f"broadcast_activity 跳过: {e}")

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_do())
        except RuntimeError:
            pass