# -*- coding: utf-8 -*-
"""重型 Core 基类：大模块的统一生命周期接口（前处理 → Session → 后处理）。

设计文档：docs/archive/heavycore_base_design.md

本基类抽象出重型大模块（Conversation / SocialChat / PFCA）的公共生命周期骨架：
- 固定骨架：start() / stop() 由基类实现，子类不重写。
- 可变点：前处理 _preprocess() / 创建 Session _create_session() / 后处理 _postprocess()
  由子类通过抽象方法实现。

设计原则：
- 大模块整体可切换：调用方只依赖统一接口（start / stop），不感知具体模块。
- 前/后处理结构：每个大模块内部统一为"前处理 → Session → 后处理"骨架。
- 统一参数传递：start(**kwargs) 透传，子类按需取参。
"""

from abc import ABC, abstractmethod
from typing import Any, Optional

from utils.logger import get_logger

logger = get_logger('HeavyCore', "DEBUG")


class HeavyCore(ABC):
    """重型 Core 基类：大模块的统一生命周期接口（前处理 → Session → 后处理）"""

    def __init__(self):
        self._session = None

    # ============ 子类必须实现的抽象方法 ============

    @abstractmethod
    async def _preprocess(self, **kwargs) -> None:
        """前处理：会话启动前的准备。
        统一承载：状态重置 / 会话注册 / 上下文初始化 / 锚点快照。"""

    @abstractmethod
    async def _create_session(self, **kwargs):
        """创建并返回本次会话的 Session 实例（子类差异点）。"""

    @abstractmethod
    async def _postprocess(self, session, result, **kwargs) -> Any:
        """后处理：会话结束后的收尾，返回最终结果。
        统一承载：结果拉取 / 结果拼装 / 持久化 / 清理。"""

    # ============ 固定骨架（基类实现，子类不重写） ============

    async def start(self, **kwargs) -> Any:
        """统一入口（固定骨架）：前处理 → 创建并运行 Session → 后处理。

        统一输入约定（所有重型大模块遵循）：
        - 模块的输入（purpose / session_id / current_session / task_draft 等）
          由调用方通过 **kwargs 显式传入，不依赖外部隐式准备。
        - 子类在 _preprocess(**kwargs) 中消费输入并准备首条消息，
          Session 在 _inject_first_user_message 中消费。
        - 测试环境通过外部 API 提供输入（真实数据），模块真实逻辑照常运行。
        """
        await self._preprocess(**kwargs)
        session = await self._create_session(**kwargs)
        self._session = session
        try:
            result = await session.start()
        finally:
            self._session = None
        return await self._postprocess(session, result=result, **kwargs)

    async def stop(self, exit_reason: Optional[str] = None) -> None:
        """统一停止接口（固定骨架）：停止当前 Session。"""
        if self._session is not None:
            await self._session.stop(exit_reason)