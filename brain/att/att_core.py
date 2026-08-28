# -*- coding: utf-8 -*-

"""
注意力核心模块

极简版：只负责启动station.py
跑在主 event loop（与 FastAPI / adapt / MessageBus 共享），
不再开独立线程/loop。
"""

# 导入模块依赖
import asyncio
from brain.att.station import AttentionStation


class AttentionCore:
    """极简版注意力核心"""

    def __init__(self):
        self.station: AttentionStation | None = None
        self.running = False
        self._task: asyncio.Task | None = None

    async def start(self):
        """启动 station 作为主 loop 的 task。"""
        if self.running:
            return
        self.station = AttentionStation()
        self._task = asyncio.create_task(self.station.start(), name="attention-station")
        self.running = True

    async def stop(self):
        """通知 station 停止，并等待 task 自然结束（最多 5s）。"""
        if not self.running or self.station is None:
            return
        self.station.stop()
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = None
        self.station = None
        self.running = False


# 全局注意力核心实例
_attention_core_instance = None


def get_attention_core() -> AttentionCore:
    """获取注意力核心的单例实例
    
    Returns:
        AttentionCore: 注意力核心实例
    """
    global _attention_core_instance
    if _attention_core_instance is None:
        _attention_core_instance = AttentionCore()
    return _attention_core_instance


